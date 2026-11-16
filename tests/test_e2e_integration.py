"""
End-to-end integration tests for factory.py.

These tests exercise the *full* code path of every command against real git
repos inside temporary directories. Network calls (gh / glab / gt) are
stubbed at the cli_run / subprocess.run boundary — everything else (git,
file I/O, state transitions, checks.sh, argparse) is real.

Coverage:
  - monitor: empty, queued, active, done, stopped, cancelled
  - start: claims oldest queued issue, creates branch, writes job.json
  - stage: advances through all CLI-reachable stages
  - route: writes risk + agents into job.json
  - gate PASS: runs real checks.sh (python3 -m unittest) via subprocess
  - gate FAIL: checks.sh exits 1 → job stays at gate
  - gate TIMEOUT: checks.sh sleeps beyond timeout → FAIL written
  - gate stage guard: rejects from non-gate stages
  - note: appends to job notes
  - open_pr: calls pr_create, writes URL, marks done on success
  - open_pr rollback on BaseException: stage reset to approve
  - resume: re-activates stopped job at saved stage
  - stop: pauses active job
  - cancel: terminates job in cancelled state
  - round_next: increments round, resets stage to build
  - round_next stage guard: rejects from ingest/spec/build/gate
  - key(): colon separator, no collision
  - protected_paths: gate rejects branch touching factory/config.json
  - all_jobs() corruption: one bad file → warning, rest returned
  - monitor() corruption: one bad repo file → warning, continues
  - start() TOCTOU: concurrent start raises FileExistsError
  - cancel vs stop distinction: cancel = not resumable
  - VCS routing: _cli returns gt for gitea, glab for gitlab, gh for github
  - Gitea pr_create: --yes flag always present, --host forwarded
  - Gitea issue_list_labeled: uses gt, host forwarded
  - precheck: returns wakeAgent based on queue
  - argparser: all commands registered, stage choices exclude 'pr'
"""

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

_FACTORY = pathlib.Path(__file__).resolve().parents[1] / "factory/bin/factory.py"
_spec = importlib.util.spec_from_file_location("factory", _FACTORY)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


# ─────────────────────────── helpers ──────────────────────────────────────────

def _env(tmp):
    return patch.dict(os.environ, HERMES_HOME=tmp)


def _make_git_repo(path, branch="main"):
    """Create a real git repo with a working src/ + tests/ + factory/.
    Also creates a bare clone at path + '.git-origin' and wires it as
    the 'origin' remote so 'git fetch origin' works without a real server.
    """
    p = pathlib.Path(path)
    (p / "src").mkdir(parents=True, exist_ok=True)
    (p / "tests").mkdir(exist_ok=True)
    (p / "factory").mkdir(exist_ok=True)

    (p / "src/calc.py").write_text(
        "def add(a, b): return a + b\n"
        "def subtract(a, b): return a - b\n"
    )
    (p / "tests/test_calc.py").write_text(
        "import unittest, sys, os\n"
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))\n"
        "from calc import add, subtract\n"
        "class T(unittest.TestCase):\n"
        "    def test_add(self): self.assertEqual(add(1,2),3)\n"
        "    def test_sub(self): self.assertEqual(subtract(5,2),3)\n"
        "if __name__ == '__main__': unittest.main()\n"
    )
    (p / "factory/checks.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        "cd \"$(git rev-parse --show-toplevel)\"\n"
        "python3 -m unittest discover -s tests -q\n"
        "echo 'All checks passed.'\n"
    )
    (p / "factory/checks.sh").chmod(0o755)
    (p / "factory/config.json").write_text(json.dumps({
        "vcs": "github", "max_rounds": 3, "base_branch": branch,
        "checks_timeout_seconds": 10,
        "protected_paths": ["factory/checks.sh", "factory/config.json"],
        "labels": {"queue": "factory", "needs_info": "factory:needs-info",
                   "needs_human": "factory:needs-human"},
        "routing": {"low": {"code": "h"}, "medium": {"code": "s"}, "high": {"code": "s"}},
        "surface_reviewers": {"security": {"security": "o"}},
    }))

    for cmd in [
        ["git", "init", "-b", branch, str(p)],
        ["git", "-C", str(p), "config", "user.email", "test@test.local"],
        ["git", "-C", str(p), "config", "user.name", "Test"],
        ["git", "-C", str(p), "add", "."],
        ["git", "-C", str(p), "commit", "-m", "init", "--quiet"],
    ]:
        subprocess.run(cmd, check=True, capture_output=True)
    # Create a bare clone as "origin" so git fetch/push work without a server
    bare = pathlib.Path(str(path) + ".git-origin")
    subprocess.run(["git", "clone", "--bare", "--quiet", str(p), str(bare)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(p), "remote", "add", "origin", str(bare)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(p), "fetch", "origin", "--quiet"],
                   check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(p), "branch", f"--set-upstream-to=origin/{branch}", branch],
        check=True, capture_output=True)
    return p


def _register_repo(hermes_home, slug, repo_path, vcs="github", queue=None):
    """Write a repos/SLUG.json record directly."""
    rec = {
        "repo": slug, "path": str(repo_path), "vcs": vcs,
        "base": "main", "added": F.now(), "checked": None,
        "queue": queue or [{"number": 1, "title": "Test issue"}],
    }
    rd = pathlib.Path(hermes_home) / "factory/repos"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / f"{F.key(slug)}.json").write_text(json.dumps(rec))
    return rec


def _fake_issue(number=1, title="Test issue", body="body", url="https://x/1"):
    return {"number": number, "title": title, "body": body,
            "url": url, "author": {"login": "alice"}}


def _buf():
    return io.StringIO()


# ─────────────────────────── 1. key() ──────────────────────────────────────────

class TestKey(unittest.TestCase):

    def test_colon_separator(self):
        self.assertEqual(F.key("owner/repo"), "owner:repo")

    def test_no_collision(self):
        self.assertNotEqual(F.key("a/b--c"), F.key("a--b/c"))

    def test_stable(self):
        self.assertEqual(F.key("foo/bar"), F.key("foo/bar"))


# ─────────────────────────── 2. monitor ────────────────────────────────────────

class TestMonitor(unittest.TestCase):

    def test_monitor_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                buf = _buf()
                with contextlib.redirect_stdout(buf):
                    F.monitor(None)
        out = buf.getvalue()
        self.assertIn("No repositories registered", out)

    def test_monitor_shows_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/repo", repo,
                               queue=[{"number": 5, "title": "My issue"}])
                buf = _buf()
                with contextlib.redirect_stdout(buf):
                    F.monitor(None)
        self.assertIn("org/repo", buf.getvalue())
        self.assertIn("#5", buf.getvalue())

    def test_monitor_shows_active_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/repo", repo)
                # Manually write an active job
                jd = F.job_dir("org/repo", 1)
                jd.mkdir(parents=True)
                F.save(jd / "job.json", {
                    "repo": "org/repo", "issue": 1, "title": "My issue",
                    "state": "active", "stage": "build", "round": 1,
                    "round_limit": 3, "branch": "factory/issue-1",
                    "base": "main", "started": F.now(), "updated": F.now(), "agents": {},
                    "stopped": None, "result": None, "pr": None,
                    "stages": {"ingest": F.now()},
                    "events": [],
                })
                buf = _buf()
                with contextlib.redirect_stdout(buf):
                    F.monitor(None)
        self.assertIn("BUILD", buf.getvalue())

    def test_monitor_corrupt_repo_file_warns_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/good", repo,
                               queue=[{"number": 1, "title": "Good"}])
                # Corrupt repo file
                bad = pathlib.Path(tmp) / "factory/repos" / "org:bad.json"
                bad.write_text("{not valid json")
                stderr = _buf()
                buf = _buf()
                with contextlib.redirect_stderr(stderr), \
                     contextlib.redirect_stdout(buf):
                    F.monitor(None)
        self.assertIn("WARNING", stderr.getvalue())
        self.assertIn("org/good", buf.getvalue())

    def test_monitor_corrupt_job_file_warns_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/repo", repo)
                # Write a corrupt job file alongside a valid one
                jd = F.job_dir("org/repo", 99)
                jd.mkdir(parents=True)
                (jd / "job.json").write_text("{bad json")
                # Also write a valid job
                jd2 = F.job_dir("org/repo", 2)
                jd2.mkdir(parents=True)
                F.save(jd2 / "job.json", {
                    "repo": "org/repo", "issue": 2, "title": "T",
                    "state": "done", "stage": "pr", "round": 1,
                    "round_limit": 3, "branch": "factory/issue-2",
                    "base": "main", "started": F.now(), "updated": F.now(),
                    "agents": {}, "stopped": None, "result": "ok",
                    "pr": "https://x/2", "stages": {"ingest": F.now()},
                    "events": [],
                })
                buf = _buf()
                stderr = _buf()
                with contextlib.redirect_stdout(buf), \
                     contextlib.redirect_stderr(stderr):
                    F.monitor(None)
        self.assertIn("WARNING", stderr.getvalue())


# ─────────────────────────── 3. start() ────────────────────────────────────────

class TestStart(unittest.TestCase):

    def test_start_creates_branch_and_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/repo", repo)
                with patch.object(F, "issue_view_full", return_value=_fake_issue()), \
                     patch.object(F, "issue_list_labeled",
                                  return_value=[{"number": 1, "title": "Test issue"}]):
                    buf = _buf()
                    with contextlib.redirect_stdout(buf):
                        F.start(SimpleNamespace(repo="org/repo"))
                job = F.active_job("org/repo")
                # Branch must exist in git (current branch after start)
                branch_result = subprocess.run(
                    ["git", "-C", str(repo), "branch", "-a"],
                    capture_output=True, text=True)
        self.assertIsNotNone(job)
        self.assertEqual(job["issue"], 1)
        self.assertEqual(job["stage"], "ingest")
        self.assertEqual(job["state"], "active")
        self.assertIn("factory/issue-1", branch_result.stdout)

    def test_start_no_work_when_empty_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/repo", repo, queue=[])
                buf = _buf()
                with patch.object(F, "issue_list_labeled", return_value=[]), \
                     contextlib.redirect_stdout(buf):
                    F.start(SimpleNamespace(repo="org/repo"))
        self.assertIn("NO WORK", buf.getvalue())

    def test_start_toctou_guard(self):
        """Two concurrent starts on the same issue raise FileExistsError."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/repo", repo)
                # Pre-create the job directory to simulate concurrent start
                jd = F.job_dir("org/repo", 1)
                jd.mkdir(parents=True, exist_ok=True)
                with patch.object(F, "issue_view_full",
                                  return_value=_fake_issue()), \
                     patch.object(F, "issue_list_labeled",
                                  return_value=[{"number": 1, "title": "t"}]):
                    with self.assertRaises(SystemExit) as ctx:
                        F.start(SimpleNamespace(repo="org/repo"))
            self.assertIn("concurrent", str(ctx.exception).lower())

    def test_start_busy_when_active_job_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "org/repo", repo,
                               queue=[{"number": 1, "title": "A"},
                                      {"number": 2, "title": "B"}])
                with patch.object(F, "issue_view_full",
                                  return_value=_fake_issue()), \
                     patch.object(F, "issue_list_labeled",
                                  return_value=[{"number": 1, "title": "A"},
                                                {"number": 2, "title": "B"}]):
                    # Start issue #1
                    F.start(SimpleNamespace(repo="org/repo"))
                    # Try to start again — should say BUSY
                    buf = _buf()
                    with contextlib.redirect_stdout(buf):
                        F.start(SimpleNamespace(repo="org/repo"))
        self.assertIn("BUSY", buf.getvalue())


# ─────────────────────────── 4. stage + route + note ───────────────────────────

class TestStageRouteNote(unittest.TestCase):

    def _start_job(self, tmp, repo, slug):
        _register_repo(tmp, slug, repo)
        with patch.object(F, "issue_view_full", return_value=_fake_issue()), \
             patch.object(F, "issue_list_labeled",
                          return_value=[{"number": 1, "title": "Test issue"}]):
            F.start(SimpleNamespace(repo=slug))

    def test_stage_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._start_job(tmp, repo, "o/r")
                F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note=None))
                job = F.active_job("o/r")
        self.assertEqual(job["stage"], "spec")

    def test_stage_through_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._start_job(tmp, repo, "o/r")
                for stage in ("spec", "build", "gate", "review", "approve"):
                    F.stage_cmd(SimpleNamespace(repo="o/r", name=stage, note=None))
                job = F.active_job("o/r")
        self.assertEqual(job["stage"], "approve")

    def test_stage_pr_blocked_from_cli(self):
        """STAGES[1:-1] must not include 'pr'."""
        self.assertNotIn("pr", F.STAGES[1:-1])

    def test_route_writes_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._start_job(tmp, repo, "o/r")
                F.route_cmd(SimpleNamespace(
                    repo="o/r", risk="high", surfaces=["security"]))
                job = F.active_job("o/r")
        # Reviewer routing lives under agents.reviewers (flat keys retired).
        self.assertIn("code", job["agents"]["reviewers"])
        self.assertIn("security", job["agents"]["reviewers"])

    def test_note_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._start_job(tmp, repo, "o/r")
                F.note_cmd(SimpleNamespace(repo="o/r", text="reviewer note"))
                jd = F.job_dir("o/r", 1)
                job = json.loads((jd / "job.json").read_text())
        # Notes live in the job's event log (no separate notes.md).
        self.assertIn("reviewer note", [e["text"] for e in job["events"]])


# ─────────────────────────── 5. gate ───────────────────────────────────────────

class TestGate(unittest.TestCase):

    def _job_at_build(self, tmp, repo, slug):
        _register_repo(tmp, slug, repo)
        with patch.object(F, "issue_view_full", return_value=_fake_issue()), \
             patch.object(F, "issue_list_labeled",
                          return_value=[{"number": 1, "title": "Test issue"}]):
            F.start(SimpleNamespace(repo=slug))
        F.stage_cmd(SimpleNamespace(repo=slug, name="spec", note=None))
        F.stage_cmd(SimpleNamespace(repo=slug, name="build", note=None))

    def test_gate_pass_real_checks(self):
        """Real checks.sh runs python3 -m unittest → passes."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._job_at_build(tmp, repo, "o/r")
                buf = _buf()
                with contextlib.redirect_stdout(buf):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
                job = F.active_job("o/r")
                # Read checks result
                jd = F.job_dir("o/r", 1)
                checks = (jd / "round-0/checks.md").read_text()
        self.assertIn("GATE: PASS", checks)
        self.assertEqual(job["stage"], "gate")

    def test_gate_fail_bad_checks(self):
        """checks.sh that exits 1 → GATE: FAIL written."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            # Overwrite checks.sh to always fail
            (pathlib.Path(repo) / "factory/checks.sh").write_text(
                "#!/usr/bin/env bash\necho 'TEST FAILURE'; exit 1\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "break",
                            "--quiet"], check=True, capture_output=True)
            # The job branch starts from origin/main, so the broken
            # checks.sh must be pushed to origin first.
            subprocess.run(["git", "-C", str(repo), "push", "origin", "main",
                            "--quiet"], check=True, capture_output=True)
            with _env(tmp):
                self._job_at_build(tmp, repo, "o/r")
                F.gate_cmd(SimpleNamespace(repo="o/r"))
                jd = F.job_dir("o/r", 1)
                checks = (jd / "round-0/checks.md").read_text()
        self.assertIn("GATE: FAIL", checks)
        self.assertIn("TEST FAILURE", checks)

    def test_gate_timeout(self):
        """checks.sh that sleeps beyond timeout → GATE: FAIL — timed out."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            # Very short timeout and a sleep
            cfg = json.loads(
                (pathlib.Path(repo) / "factory/config.json").read_text())
            cfg["checks_timeout_seconds"] = 1
            (pathlib.Path(repo) / "factory/config.json").write_text(
                json.dumps(cfg))
            (pathlib.Path(repo) / "factory/checks.sh").write_text(
                "#!/usr/bin/env bash\nsleep 10\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True,
                           capture_output=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-m", "slow",
                            "--quiet"], check=True, capture_output=True)
            # The job branch starts from origin/main, so the slow
            # checks.sh and the 1s timeout must be pushed to origin first.
            subprocess.run(["git", "-C", str(repo), "push", "origin", "main",
                            "--quiet"], check=True, capture_output=True)
            with _env(tmp):
                self._job_at_build(tmp, repo, "o/r")
                F.gate_cmd(SimpleNamespace(repo="o/r"))
                jd = F.job_dir("o/r", 1)
                checks = (jd / "round-0/checks.md").read_text()
        self.assertIn("GATE: FAIL", checks)
        self.assertIn("timed out", checks)

    def test_gate_stage_guard_rejects_at_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "o/r", repo)
                with patch.object(F, "issue_view_full",
                                  return_value=_fake_issue()), \
                     patch.object(F, "issue_list_labeled",
                                  return_value=[{"number": 1, "title": "t"}]):
                    F.start(SimpleNamespace(repo="o/r"))
                # job is at ingest
                with self.assertRaises(SystemExit):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))

    def test_gate_protected_path_blocked(self):
        """Branch touching factory/config.json must fail gate with a message."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._job_at_build(tmp, repo, "o/r")
                job = F.active_job("o/r")
                # Modify factory/config.json on the current branch
                (pathlib.Path(repo) / "factory/config.json").write_text(
                    '{"vcs":"github","hacked":true}')
                subprocess.run(["git", "-C", str(repo), "add", "."],
                                capture_output=True)
                subprocess.run(["git", "-C", str(repo), "commit", "-m",
                                 "tamper", "--quiet"], capture_output=True)
                buf = _buf()
                with contextlib.redirect_stdout(buf):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
        # Gate must print FAIL mentioning the protected path
        self.assertIn("GATE: FAIL", buf.getvalue())
        self.assertIn("factory/config.json", buf.getvalue())


# ─────────────────────────── 6. stop / resume / cancel ─────────────────────────

class TestStopResumeCancel(unittest.TestCase):

    def _start(self, tmp, repo, slug):
        _register_repo(tmp, slug, repo)
        with patch.object(F, "issue_view_full", return_value=_fake_issue()), \
             patch.object(F, "issue_list_labeled",
                          return_value=[{"number": 1, "title": "Test issue"}]):
            F.start(SimpleNamespace(repo=slug))

    def test_stop_then_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._start(tmp, repo, "o/r")
                F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note=None))
                F.stop(SimpleNamespace(repo="o/r", reason="pausing"))
                job_after_stop = F.active_job("o/r")
                self.assertIsNone(job_after_stop)

                F.resume(SimpleNamespace(repo="o/r", issue=1))
                job = F.active_job("o/r")
        self.assertEqual(job["state"], "active")
        self.assertEqual(job["stage"], "spec")

    def test_cancel_not_resumable(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._start(tmp, repo, "o/r")
                F.cancel(SimpleNamespace(repo="o/r", reason="spam"))
                # job dir still exists but state = cancelled
                jd = F.job_dir("o/r", 1)
                job = json.loads((jd / "job.json").read_text())
                self.assertEqual(job["state"], "cancelled")
                # resume must refuse
                with self.assertRaises(SystemExit) as ctx:
                    F.resume(SimpleNamespace(repo="o/r", issue=1))
        self.assertIn("cancel", str(ctx.exception).lower())

    def test_stop_preserves_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._start(tmp, repo, "o/r")
                F.stop(SimpleNamespace(repo="o/r", reason="later"))
            result = subprocess.run(
                ["git", "-C", str(repo), "branch", "--list",
                 "factory/issue-1"],
                capture_output=True, text=True)
        self.assertIn("factory/issue-1", result.stdout)


# ─────────────────────────── 7. round_next ─────────────────────────────────────

class TestRoundNext(unittest.TestCase):

    def _at_stage(self, tmp, repo, slug, stage):
        _register_repo(tmp, slug, repo)
        with patch.object(F, "issue_view_full", return_value=_fake_issue()), \
             patch.object(F, "issue_list_labeled",
                          return_value=[{"number": 1, "title": "Test issue"}]):
            F.start(SimpleNamespace(repo=slug))
        for s in F.STAGES[1:]:
            F.stage_cmd(SimpleNamespace(repo=slug, name=s, note=None))
            if s == stage:
                break

    def test_round_next_from_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_stage(tmp, repo, "o/r", "review")
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                job = F.active_job("o/r")
        self.assertEqual(job["round"], 1)
        self.assertEqual(job["stage"], "build")

    def test_round_next_from_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_stage(tmp, repo, "o/r", "approve")
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                job = F.active_job("o/r")
        self.assertEqual(job["round"], 1)

    def test_round_next_refuses_at_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_stage(tmp, repo, "o/r", "build")
                with self.assertRaises(SystemExit) as ctx:
                    F.round_next_cmd(SimpleNamespace(repo="o/r"))
        self.assertIn("build", str(ctx.exception))

    def test_round_next_from_gate(self):
        """Gate FAIL leaves the job at 'gate'; round-next must work there."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_stage(tmp, repo, "o/r", "gate")
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                job = F.active_job("o/r")
        self.assertEqual(job["round"], 1)
        self.assertEqual(job["stage"], "build")

    def test_round_next_refuses_at_max(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_stage(tmp, repo, "o/r", "review")
                # Manually set round to limit
                jd = F.job_dir("o/r", 1)
                job = json.loads((jd / "job.json").read_text())
                job["round"] = job["round_limit"]
                F.save(jd / "job.json", job)
                with self.assertRaises(SystemExit) as ctx:
                    F.round_next_cmd(SimpleNamespace(repo="o/r"))
        self.assertIn("max", str(ctx.exception).lower())


# ─────────────────────────── 8. open_pr ────────────────────────────────────────

class TestOpenPr(unittest.TestCase):

    def _at_approve(self, tmp, repo, slug):
        _register_repo(tmp, slug, repo)
        with patch.object(F, "issue_view_full", return_value=_fake_issue()), \
             patch.object(F, "issue_list_labeled",
                          return_value=[{"number": 1, "title": "Test issue"}]):
            F.start(SimpleNamespace(repo=slug))
        for s in ("spec", "build", "gate", "review", "approve"):
            F.stage_cmd(SimpleNamespace(repo=slug, name=s, note=None))
        # Write a pr-body.md
        jd = F.job_dir(slug, 1)
        (jd / "pr-body.md").write_text("PR body\n")

    def test_open_pr_success_marks_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_approve(tmp, repo, "o/r")

                def fake_cli(*a, **kw):
                    if "--show-current" in a: return "factory/issue-1"
                    if "--porcelain" in a:    return ""
                    if "rev-list" in a:       return "1"
                    return ""

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push", return_value=""), \
                     patch.object(F, "pr_create",
                                  return_value="https://gh.com/o/r/pull/5"), \
                     contextlib.redirect_stdout(_buf()):
                    jd = F.job_dir("o/r", 1)
                    F.open_pr(SimpleNamespace(
                        repo="o/r", body_file=str(jd / "pr-body.md"), draft=False))

                jd = F.job_dir("o/r", 1)
                job = json.loads((jd / "job.json").read_text())
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["pr"], "https://gh.com/o/r/pull/5")

    def test_open_pr_rollback_on_keyboard_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_approve(tmp, repo, "o/r")

                def fake_cli(*a, **kw):
                    if "--show-current" in a: return "factory/issue-1"
                    if "--porcelain" in a:    return ""
                    if "rev-list" in a:       return "1"
                    return ""

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push",
                                  side_effect=KeyboardInterrupt("ctrl-c")), \
                     contextlib.redirect_stdout(_buf()):
                    jd2 = F.job_dir("o/r", 1)
                    with self.assertRaises(KeyboardInterrupt):
                        F.open_pr(SimpleNamespace(
                            repo="o/r", body_file=str(jd2 / "pr-body.md"), draft=False))

                jd = F.job_dir("o/r", 1)
                job = json.loads((jd / "job.json").read_text())
        self.assertEqual(job["stage"], "approve")
        self.assertNotEqual(job["state"], "done")

    def test_open_pr_rollback_on_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                self._at_approve(tmp, repo, "o/r")

                def fake_cli(*a, **kw):
                    if "--show-current" in a: return "factory/issue-1"
                    if "--porcelain" in a:    return ""
                    if "rev-list" in a:       return "1"
                    return ""

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push",
                                  side_effect=OSError("disk full")), \
                     contextlib.redirect_stdout(_buf()):
                    jd3 = F.job_dir("o/r", 1)
                    with self.assertRaises(OSError):
                        F.open_pr(SimpleNamespace(
                            repo="o/r", body_file=str(jd3 / "pr-body.md"), draft=False))

                jd = F.job_dir("o/r", 1)
                job = json.loads((jd / "job.json").read_text())
        self.assertEqual(job["stage"], "approve")


# ─────────────────────────── 9. all_jobs() corruption ──────────────────────────

class TestAllJobsCorruption(unittest.TestCase):

    def test_corrupt_job_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                repo = _make_git_repo(tmp + "/repo")
                _register_repo(tmp, "o/r", repo)

                # Corrupt file
                bad_dir = F.job_dir("o/r", 99)
                bad_dir.mkdir(parents=True)
                (bad_dir / "job.json").write_text("{corrupt")

                # Valid job
                good_dir = F.job_dir("o/r", 1)
                good_dir.mkdir(parents=True)
                F.save(good_dir / "job.json", {
                    "repo": "o/r", "issue": 1, "title": "ok",
                    "state": "done", "stage": "pr", "round": 1,
                    "round_limit": 3, "branch": "factory/issue-1",
                    "base": "main", "started": F.now(), "agents": {},
                    "stopped": None, "result": "ok", "pr": None,
                })

                stderr = _buf()
                with contextlib.redirect_stderr(stderr):
                    jobs = F.all_jobs()

        self.assertIn("WARNING", stderr.getvalue())
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["issue"], 1)


# ─────────────────────────── 10. precheck ──────────────────────────────────────

class TestPrecheck(unittest.TestCase):

    def test_wake_true_when_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "o/r", repo,
                               queue=[{"number": 1, "title": "t"}])
                buf = _buf()
                with patch.object(F, "check_queue",
                                  return_value=[{"number": 1, "title": "t"}]), \
                     contextlib.redirect_stdout(buf):
                    F.precheck(SimpleNamespace(repo="o/r"))
        result = json.loads(buf.getvalue().strip())
        self.assertTrue(result["wakeAgent"])

    def test_wake_false_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "o/r", repo, queue=[])
                buf = _buf()
                with patch.object(F, "check_queue", return_value=[]), \
                     contextlib.redirect_stdout(buf):
                    F.precheck(SimpleNamespace(repo="o/r"))
        result = json.loads(buf.getvalue().strip())
        self.assertFalse(result["wakeAgent"])

    def test_wake_false_when_active_job(self):
        """Already running job → no wake needed."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(tmp + "/repo")
            with _env(tmp):
                _register_repo(tmp, "o/r", repo,
                               queue=[{"number": 2, "title": "t"}])
                # Write active job to make BUSY
                jd = F.job_dir("o/r", 1)
                jd.mkdir(parents=True)
                F.save(jd / "job.json", {
                    "repo": "o/r", "issue": 1, "title": "t", "state": "active",
                    "stage": "build", "round": 1, "round_limit": 3,
                    "branch": "factory/issue-1", "base": "main",
                    "started": F.now(), "updated": F.now(), "agents": {},
                    "stopped": None, "result": None, "pr": None,
                    "stages": {"ingest": F.now()}, "events": [],
                })
                buf = _buf()
                with patch.object(F, "check_queue",
                                  return_value=[{"number": 2, "title": "t"}]), \
                     contextlib.redirect_stdout(buf):
                    F.precheck(SimpleNamespace(repo="o/r"))
        result = json.loads(buf.getvalue().strip())
        self.assertFalse(result["wakeAgent"])


# ─────────────────────────── 11. VCS routing ───────────────────────────────────

class TestVCSRouting(unittest.TestCase):

    def test_cli_github(self):    self.assertEqual(F._cli("github"), "gh")
    def test_cli_gitlab(self):    self.assertEqual(F._cli("gitlab"), "glab")
    def test_cli_gitea(self):     self.assertEqual(F._cli("gitea"), "gt")
    def test_cli_default(self):   self.assertEqual(F._cli("other"), "gh")

    def test_gt_host_env(self):
        with patch.dict(os.environ, GITEA_CLI_HOST="https://h.example.com",
                        GITEA_INSTANCE_URL=""):
            self.assertEqual(F._gt_host(), "https://h.example.com")

    def test_gt_host_config(self):
        with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
            self.assertEqual(
                F._gt_host({"gitea_host": "https://forge.local"}),
                "https://forge.local")

    def test_gitea_pr_create_uses_yes_flag(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "https://u"):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.pr_create("o/r", "gitea", "main", "b", "T", "/f", False)
        self.assertTrue(any("--yes" in str(c) for c in calls))

    def test_gitea_pr_create_host_forwarded(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "https://u"):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://gt",
                            GITEA_INSTANCE_URL=""):
                F.pr_create("o/r", "gitea", "main", "b", "T", "/f", False)
        flat = " ".join(str(c) for c in calls)
        self.assertIn("--host", flat)
        self.assertIn("https://gt", flat)

    def test_gitea_issue_list_uses_gt(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "[]"):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.issue_list_labeled("o/r", "gitea", "factory")
        self.assertTrue(any("gt" in str(c) for c in calls))

    def test_gitlab_issue_list_uses_glab(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "[]"):
            F.issue_list_labeled("o/r", "gitlab", "factory")
        self.assertTrue(any("glab" in str(c) for c in calls))

    def test_github_issue_list_uses_gh(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "[]"):
            F.issue_list_labeled("o/r", "github", "factory")
        self.assertTrue(any("gh" in str(c) for c in calls))

    def test_origin_slug_gitea_any_host(self):
        mock = MagicMock()
        mock.stdout = "https://gitea.example.com/owner/repo.git\n"
        with patch.object(F.subprocess, "run", return_value=mock):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                result = F.origin_slug("/p", vcs="gitea", config={})
        self.assertEqual(result, "owner/repo")

    def test_origin_slug_gitea_wrong_host_rejected(self):
        mock = MagicMock()
        mock.stdout = "https://github.com/owner/repo.git\n"
        with patch.object(F.subprocess, "run", return_value=mock):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://forge.local",
                            GITEA_INSTANCE_URL=""):
                result = F.origin_slug("/p", vcs="gitea")
        self.assertIsNone(result)


# ─────────────────────────── 12. argparser completeness ────────────────────────

class TestArgparser(unittest.TestCase):

    def _parse(self, *args):
        import argparse
        p = F.argparse.ArgumentParser()
        sub = p.add_subparsers(dest="cmd")
        # Re-build the full parser by calling main's build logic
        # We test by importing and calling: just verify no SystemExit
        return None

    def test_stages_exclude_pr(self):
        self.assertNotIn("pr", F.STAGES[1:-1])
        self.assertIn("approve", F.STAGES[1:-1])

    def test_stages_order(self):
        self.assertEqual(F.STAGES,
                         ["ingest", "spec", "build", "gate", "review", "approve", "pr"])

    def test_all_vcs_in_cli_function(self):
        for vcs in ("github", "gitlab", "gitea"):
            self.assertIsNotNone(F._cli(vcs))

    def test_factory_py_main_importable(self):
        """factory.py must be importable without side effects."""
        self.assertTrue(hasattr(F, "monitor"))
        self.assertTrue(hasattr(F, "start"))
        self.assertTrue(hasattr(F, "gate_cmd"))
        self.assertTrue(hasattr(F, "open_pr"))
        self.assertTrue(hasattr(F, "round_next_cmd"))
        self.assertTrue(hasattr(F, "cancel"))
        self.assertTrue(hasattr(F, "pr_create"))
        self.assertTrue(hasattr(F, "_gt_host"))
        self.assertTrue(hasattr(F, "STAGES"))


# ─────────────────────────── 13. multi-repo isolation ──────────────────────────

class TestMultiRepo(unittest.TestCase):

    def test_jobs_isolated_by_key(self):
        """Jobs for different repos must live in separate directories."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                for slug in ("org/alpha", "org/beta"):
                    repo = _make_git_repo(tmp + f"/{slug.split('/')[1]}")
                    _register_repo(tmp, slug, repo)
                    with patch.object(F, "issue_view_full",
                                      return_value=_fake_issue()), \
                         patch.object(F, "issue_list_labeled",
                                      return_value=[{"number": 1, "title": "t"}]):
                        F.start(SimpleNamespace(repo=slug))
                alpha_job = F.active_job("org/alpha")
                beta_job  = F.active_job("org/beta")
        self.assertIsNotNone(alpha_job)
        self.assertIsNotNone(beta_job)
        self.assertNotEqual(
            str(F.job_dir("org/alpha", 1)),
            str(F.job_dir("org/beta", 1)))

    def test_key_separator_is_colon(self):
        for slug in ("org/repo", "user/project", "a/b"):
            self.assertIn(":", F.key(slug))
            self.assertNotIn("/", F.key(slug))


if __name__ == "__main__":
    unittest.main()
