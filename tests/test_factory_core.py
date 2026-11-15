"""
Comprehensive corner-case tests for factory.py.

Coverage targets (per code review of factory.py):
  helpers          : check_slug, key, save, load_repo, active_jobs, require_active, log
  routing          : routing_for (low/medium/high x surfaces)
  protected_paths  : no patterns, match, no-match, Added files exempt
  origin_slug      : github HTTPS, github SSH, gitlab HTTPS, gitlab SSH, no match, empty
  start            : BUSY, dirty tree, NO WORK, queue mismatch, normal, VCS mocked
  stage_cmd        : forward one step, skip, backward, unknown name, no active job
  note_cmd         : happy path, no active job
  route_cmd        : sets fields, surface merging
  gate_cmd         : missing checks.sh, protected hit, PASS, FAIL
  open_pr          : wrong stage, wrong branch, dirty tree, no commits
  resume           : not found, not stopped (done), not stopped (active), path mismatch,
                     wrong branch, happy path
  stop             : named, unnamed-one, unnamed-many, unnamed-none
  dispatch         : lock re-entry, busy at start, no work, two done, stopped halts
  precheck         : busy (no wake), work waiting (wake), empty (no wake)
  install_precheck : precheck script, dispatch script, content, idempotent overwrite
  monitor          : no repos, idle+no history, idle+last-job, active, finished section,
                     two repos, very long title truncation, PR/MR URL in finished row
  _age             : <60m, >=60m
  _fit             : short, exact, over
  _stage_status    : future stage, current active, current stopped, past stage, last stage
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

# ── Load factory module ───────────────────────────────────────────────────────
_FACTORY_PATH = (
    Path(__file__).resolve().parents[1] / "factory/bin/factory.py"
)
_spec = importlib.util.spec_from_file_location("factory", _FACTORY_PATH)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _repo(tmp, slug, path=None, vcs="github", base="main", queue=None):
    """Write and return a repo record."""
    rec = {"repo": slug, "path": path or tmp,
           "vcs": vcs, "base": base,
           "added": F.now(), "checked": None, "queue": queue or []}
    F.save(F.repo_file(slug), rec)
    return rec


def _job(tmp, slug, number, stage="ingest", state="active",
         vcs="github", path=None, branch=None, base="main",
         round_=0, round_limit=3):
    """Write and return a job record."""
    branch = branch or f"factory/issue-{number}"
    job = {
        "repo": slug, "path": path or tmp, "vcs": vcs,
        "issue": number, "title": f"Issue {number}",
        "url": f"https://example.com/{number}",
        "branch": branch, "base": base,
        "stage": stage, "state": state, "result": None,
        "pr": None, "pr_url": None,
        "risk": None, "surfaces": [], "agents": {},
        "round": round_, "round_limit": round_limit,
        "reviews": {}, "started": F.now(), "updated": F.now(),
        "stages": {stage: F.now()}, "events": [],
    }
    F.save(F.job_dir(slug, number) / "job.json", job)
    return job


def _env(tmp):
    """Context manager: set HERMES_HOME to tmp."""
    return patch.dict(os.environ, HERMES_HOME=tmp)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckSlug(unittest.TestCase):

    def test_valid_simple(self):
        self.assertEqual(F.check_slug("owner/repo"), "owner/repo")

    def test_valid_dots_and_hyphens(self):
        self.assertEqual(F.check_slug("my.org/my-repo.js"), "my.org/my-repo.js")

    def test_invalid_no_slash(self):
        with self.assertRaises(SystemExit) as ctx:
            F.check_slug("justarepo")
        self.assertIn("OWNER/REPO", str(ctx.exception))

    def test_invalid_spaces(self):
        with self.assertRaises(SystemExit):
            F.check_slug("owner/ repo")

    def test_invalid_two_slashes(self):
        with self.assertRaises(SystemExit):
            F.check_slug("org/group/repo")

    def test_invalid_empty(self):
        with self.assertRaises(SystemExit):
            F.check_slug("")


class TestKey(unittest.TestCase):

    def test_replaces_slash(self):
        self.assertEqual(F.key("owner/repo"), "owner:repo")

    def test_no_slash_unchanged(self):
        self.assertEqual(F.key("noslash"), "noslash")

    def test_multiple_slashes(self):
        # key replaces ALL slashes
        self.assertEqual(F.key("a/b/c"), "a:b:c")


class TestSave(unittest.TestCase):

    def test_writes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "sub/data.json"
            F.save(p, {"k": "v"})
            self.assertEqual(json.loads(p.read_text()), {"k": "v"})

    def test_atomic_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.json"
            F.save(p, {})
            # .tmp file must not persist
            self.assertFalse(p.with_suffix(".tmp").exists())

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "x.json"
            F.save(p, {"a": 1})
            F.save(p, {"a": 2})
            self.assertEqual(json.loads(p.read_text())["a"], 2)

    def test_creates_parents(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "a/b/c/d.json"
            F.save(p, {})
            self.assertTrue(p.exists())


class TestLoadRepo(unittest.TestCase):

    def test_missing_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                with self.assertRaises(SystemExit) as ctx:
                    F.load_repo("no/repo")
                self.assertIn("isn't registered", str(ctx.exception))

    def test_present_returns_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "owner/repo")
                rec = F.load_repo("owner/repo")
            self.assertEqual(rec["repo"], "owner/repo")


class TestActiveJobs(unittest.TestCase):

    def test_empty_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                self.assertEqual(F.active_jobs(), [])

    def test_one_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="active")
                self.assertEqual(len(F.active_jobs()), 1)

    def test_stopped_not_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="stopped")
                self.assertEqual(F.active_jobs(), [])

    def test_done_not_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="done")
                self.assertEqual(F.active_jobs(), [])

    def test_slug_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/a"); _repo(tmp, "o/b")
                _job(tmp, "o/a", 1); _job(tmp, "o/b", 2)
                self.assertEqual(len(F.active_jobs("o/a")), 1)
                self.assertEqual(F.active_jobs("o/a")[0]["repo"], "o/a")

    def test_multiple_active_different_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/a"); _repo(tmp, "o/b")
                _job(tmp, "o/a", 1); _job(tmp, "o/b", 2)
                self.assertEqual(len(F.active_jobs()), 2)


class TestRequireActive(unittest.TestCase):

    def test_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 5)
                job = F.require_active("o/r")
            self.assertEqual(job["issue"], 5)

    def test_not_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                with self.assertRaises(SystemExit) as ctx:
                    F.require_active("o/r")
                self.assertIn("No active", str(ctx.exception))


class TestLog(unittest.TestCase):

    def test_appends_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                job = _job(tmp, "o/r", 1)
                F.log(job, "hello world")
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(len(saved["events"]), 1)
            self.assertEqual(saved["events"][0]["text"], "hello world")

    def test_updates_updated_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                job = _job(tmp, "o/r", 1)
                old = job["updated"]
                time.sleep(0.01)
                F.log(job, "tick")
                # updated must be >= old (same second or later)
                self.assertGreaterEqual(job["updated"], old)

    def test_multiple_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                job = _job(tmp, "o/r", 1)
                for i in range(5):
                    F.log(job, f"event {i}")
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(len(saved["events"]), 5)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. ROUTING
# ═══════════════════════════════════════════════════════════════════════════════

class TestRoutingFor(unittest.TestCase):

    CONFIG = {
        "routing": {
            "low":    {"code": "haiku"},
            "medium": {"code": "sonnet"},
            "high":   {"code": "sonnet"},
        },
        "surface_reviewers": {
            "security": {"security": "opus"},
            "ui":       {"ui": "haiku", "ux": "sonnet"},
        },
    }

    def test_low_no_surfaces(self):
        r = F.routing_for(self.CONFIG, "low", [])
        # keys are reviewer names; "code" reviewer should be present
        self.assertIn("code", r.keys())
        self.assertEqual(r["code"], "haiku")

    def test_medium_no_surfaces(self):
        r = F.routing_for(self.CONFIG, "medium", [])
        self.assertNotIn("security", r.values())

    def test_security_surface_adds_reviewer(self):
        r = F.routing_for(self.CONFIG, "low", ["security"])
        self.assertIn("security", r)

    def test_ui_surface_adds_ui_and_ux(self):
        r = F.routing_for(self.CONFIG, "low", ["ui"])
        self.assertIn("ui", r)
        self.assertIn("ux", r)

    def test_both_surfaces(self):
        r = F.routing_for(self.CONFIG, "high", ["security", "ui"])
        self.assertIn("security", r)
        self.assertIn("ui", r)

    def test_empty_config_returns_fallback(self):
        r = F.routing_for({}, "low", [])
        # Must return something (the fallback)
        self.assertIsInstance(r, dict)
        self.assertGreater(len(r), 0)

    def test_unknown_surface_ignored(self):
        # Should not raise
        r = F.routing_for(self.CONFIG, "low", ["nonexistent"])
        self.assertIsInstance(r, dict)

    def test_empty_surfaces_list(self):
        r = F.routing_for(self.CONFIG, "high", [])
        # No surface reviewers
        self.assertNotIn("security", r)


# ═══════════════════════════════════════════════════════════════════════════════
# 3. PROTECTED PATHS
# ═══════════════════════════════════════════════════════════════════════════════

class TestProtectedPathsCheck(unittest.TestCase):

    CONFIG = {"protected_paths": ["factory/checks.sh", "factory/config.json"]}

    def test_no_patterns_returns_empty(self):
        result = F.protected_paths_check("/any", {}, "main")
        self.assertEqual(result, [])

    def test_modified_protected_file(self):
        diff = "M\tfactory/checks.sh\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", self.CONFIG, "main")
        self.assertIn("factory/checks.sh", result)

    def test_added_file_not_protected(self):
        # Added files (A status) are exempt
        diff = "A\tfactory/checks.sh\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", self.CONFIG, "main")
        self.assertEqual(result, [])

    def test_unrelated_file_not_protected(self):
        diff = "M\tsrc/app.py\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", self.CONFIG, "main")
        self.assertEqual(result, [])

    def test_both_protected_files_modified(self):
        diff = "M\tfactory/checks.sh\nM\tfactory/config.json\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", self.CONFIG, "main")
        self.assertEqual(len(result), 2)

    def test_wildcard_pattern(self):
        config = {"protected_paths": ["factory/*"]}
        diff = "M\tfactory/anything.sh\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", config, "main")
        self.assertIn("factory/anything.sh", result)

    def test_deleted_file_caught(self):
        diff = "D\tfactory/checks.sh\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", self.CONFIG, "main")
        self.assertIn("factory/checks.sh", result)

    def test_empty_diff_returns_empty(self):
        with patch.object(F, "cli_run", return_value=""):
            result = F.protected_paths_check("/repo", self.CONFIG, "main")
        self.assertEqual(result, [])


# ═══════════════════════════════════════════════════════════════════════════════
# 4. ORIGIN_SLUG
# ═══════════════════════════════════════════════════════════════════════════════

class TestOriginSlug(unittest.TestCase):

    def _run(self, url, vcs="github"):
        mock = MagicMock()
        mock.stdout = url + "\n"
        with patch.object(F.subprocess, "run", return_value=mock):
            return F.origin_slug("/path", vcs=vcs)

    def test_github_https(self):
        self.assertEqual(
            self._run("https://github.com/owner/repo.git"),
            "owner/repo")

    def test_github_ssh(self):
        self.assertEqual(
            self._run("git@github.com:owner/repo.git"),
            "owner/repo")

    def test_github_https_no_dot_git(self):
        self.assertEqual(
            self._run("https://github.com/owner/repo"),
            "owner/repo")

    def test_gitlab_https(self):
        self.assertEqual(
            self._run("https://gitlab.com/owner/repo.git", vcs="gitlab"),
            "owner/repo")

    def test_gitlab_ssh(self):
        self.assertEqual(
            self._run("git@gitlab.com:owner/repo.git", vcs="gitlab"),
            "owner/repo")

    def test_no_match_returns_none(self):
        self.assertIsNone(self._run("https://bitbucket.org/owner/repo.git"))

    def test_empty_url_returns_none(self):
        self.assertIsNone(self._run(""))

    def test_gitlab_url_not_matched_as_github(self):
        # gitlab URL should not match github regex
        self.assertIsNone(
            self._run("https://gitlab.com/owner/repo.git", vcs="github"))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. START
# ═══════════════════════════════════════════════════════════════════════════════

class TestStart(unittest.TestCase):

    ISSUE = {"number": 7, "title": "Fix bug",
             "body": "broken", "url": "https://x.com/7",
             "author": {"login": "alice"}}

    def _start(self, tmp, slug, queue=None, issue=None, dirty=""):
        _repo(tmp, slug, path=tmp)
        if queue is not None:
            rec = F.load_repo(slug)
            rec["queue"] = queue
            F.save(F.repo_file(slug), rec)

        def fake_cli(*args, **kw):
            if "status" in args:
                return dirty
            return ""

        with patch.object(F, "check_queue",
                          return_value=queue or []),\
             patch.object(F, "issue_view_full",
                          return_value=self.ISSUE),\
             patch.object(F, "cli_run",
                          side_effect=fake_cli),\
             contextlib.redirect_stdout(io.StringIO()) as out:
            F.start(SimpleNamespace(repo=slug, issue=issue))
        return out.getvalue()

    def test_no_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                out = self._start(tmp, "o/r", queue=[])
        self.assertIn("NO WORK", out)

    def test_busy(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 3)
                out = self._start(tmp, "o/r", queue=[{"number": 7, "title": "t"}])
        self.assertIn("BUSY", out)

    def test_dirty_tree_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                with self.assertRaises(SystemExit) as ctx:
                    self._start(tmp, "o/r",
                                queue=[{"number": 7, "title": "t"}],
                                dirty="M modified.py")
        self.assertIn("uncommitted", str(ctx.exception))

    def test_queue_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                # Queue has issue 7, but we request issue 99
                with self.assertRaises(SystemExit) as ctx:
                    self._start(tmp, "o/r",
                                queue=[{"number": 7, "title": "t"}],
                                issue=99)
        self.assertIn("Queue changed", str(ctx.exception))

    def test_happy_path_creates_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                out = self._start(tmp, "o/r",
                                  queue=[{"number": 7, "title": "Fix bug"}])
                job_path = F.job_dir("o/r", 7) / "job.json"
                self.assertTrue(job_path.exists())
                job = json.loads(job_path.read_text())
            self.assertIn("STARTED", out)
            self.assertEqual(job["issue"], 7)
            self.assertEqual(job["stage"], "ingest")
            self.assertEqual(job["state"], "active")

    def test_dequeues_claimed_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                self._start(tmp, "o/r",
                            queue=[{"number": 7, "title": "t"},
                                   {"number": 8, "title": "t2"}])
                repo = F.load_repo("o/r")
            # Issue 7 should be removed from the queue
            self.assertEqual(repo["queue"], [{"number": 8, "title": "t2"}])

    def test_issue_specific_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                out = self._start(tmp, "o/r",
                                  queue=[{"number": 7, "title": "t"}],
                                  issue=7)
            self.assertIn("STARTED", out)


# ═══════════════════════════════════════════════════════════════════════════════
# 6. STAGE ADVANCE
# ═══════════════════════════════════════════════════════════════════════════════

class TestStageCmd(unittest.TestCase):

    def test_forward_one_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="ingest")
                F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note=None))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["stage"], "spec")

    def test_all_stage_transitions(self):
        """Walk every valid transition in order."""
        stages = ["ingest", "spec", "build", "gate", "review", "approve", "pr"]
        for i in range(len(stages) - 1):
            with tempfile.TemporaryDirectory() as tmp:
                with _env(tmp):
                    _repo(tmp, "o/r")
                    _job(tmp, "o/r", 1, stage=stages[i])
                    F.stage_cmd(SimpleNamespace(
                        repo="o/r", name=stages[i + 1], note=None))
                    saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
                self.assertEqual(saved["stage"], stages[i + 1])

    def test_skip_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="ingest")
                with self.assertRaises(SystemExit) as ctx:
                    F.stage_cmd(SimpleNamespace(repo="o/r", name="build", note=None))
        self.assertIn("spec", str(ctx.exception))

    def test_backward_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="build")
                with self.assertRaises(SystemExit):
                    F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note=None))

    def test_no_active_job_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                with self.assertRaises(SystemExit):
                    F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note=None))

    def test_note_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="ingest")
                F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note="custom note"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["events"][-1]["text"], "custom note")

    def test_default_note_when_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="ingest")
                F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note=None))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertIn("spec", saved["events"][-1]["text"].lower())

    def test_stage_timestamps_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="ingest")
                F.stage_cmd(SimpleNamespace(repo="o/r", name="spec", note=None))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertIn("spec", saved["stages"])


# ═══════════════════════════════════════════════════════════════════════════════
# 7. NOTE
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoteCmd(unittest.TestCase):

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1)
                F.note_cmd(SimpleNamespace(repo="o/r", text="progress note"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["events"][-1]["text"], "progress note")

    def test_no_active_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                with self.assertRaises(SystemExit):
                    F.note_cmd(SimpleNamespace(repo="o/r", text="x"))

    def test_empty_text_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1)
                F.note_cmd(SimpleNamespace(repo="o/r", text=""))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["events"][-1]["text"], "")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. ROUTE
# ═══════════════════════════════════════════════════════════════════════════════

class TestRouteCmd(unittest.TestCase):

    CONFIG = {
        "routing": {"low": {"code": "haiku"}, "high": {"code": "sonnet"}},
        "surface_reviewers": {"security": {"security": "opus"}},
    }

    def test_sets_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1)
                with patch.object(F, "load_config", return_value=self.CONFIG):
                    F.route_cmd(SimpleNamespace(repo="o/r", risk="low", surfaces=""))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["risk"], "low")

    def test_sets_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1)
                with patch.object(F, "load_config", return_value=self.CONFIG):
                    F.route_cmd(SimpleNamespace(
                        repo="o/r", risk="low", surfaces="security"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertIn("security", saved["surfaces"])

    def test_sets_reviewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1)
                with patch.object(F, "load_config", return_value=self.CONFIG):
                    F.route_cmd(SimpleNamespace(
                        repo="o/r", risk="low", surfaces="security"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertIn("security", saved["agents"]["reviewers"])

    def test_does_not_advance_stage(self):
        """route must not change the stage field."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="build")
                with patch.object(F, "load_config", return_value=self.CONFIG):
                    F.route_cmd(SimpleNamespace(repo="o/r", risk="low", surfaces=""))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["stage"], "build")

    def test_multiple_surfaces_comma_separated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1)
                with patch.object(F, "load_config", return_value=self.CONFIG):
                    F.route_cmd(SimpleNamespace(
                        repo="o/r", risk="high", surfaces="security,ui"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertIn("security", saved["surfaces"])
            self.assertIn("ui", saved["surfaces"])


# ═══════════════════════════════════════════════════════════════════════════════
# 9. GATE
# ═══════════════════════════════════════════════════════════════════════════════

class TestGateCmd(unittest.TestCase):

    def _make_checks_sh(self, tmp, exit_code=0):
        checks = Path(tmp) / "factory/checks.sh"
        checks.parent.mkdir(parents=True, exist_ok=True)
        checks.write_text(
            f'#!/usr/bin/env bash\necho "checks ran"\nexit {exit_code}\n')
        checks.chmod(0o755)
        return checks

    def test_missing_checks_sh_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp)
                with patch.object(F, "protected_paths_check", return_value=[]):
                    with patch.object(F, "cli_run", return_value="abc1234"):
                        with self.assertRaises(SystemExit) as ctx:
                            F.gate_cmd(SimpleNamespace(repo="o/r"))
        self.assertIn("checks.sh", str(ctx.exception))

    def test_protected_path_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=1)
                self._make_checks_sh(tmp)
                with patch.object(F, "protected_paths_check",
                                  return_value=["factory/checks.sh"]),\
                     patch.object(F, "load_config", return_value={}),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
                # Read job.json — should have FAIL in events
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            output = out.getvalue()
        self.assertIn("FAIL", output)
        self.assertIn("protected", saved["events"][-1]["text"].lower())

    def test_pass_writes_checks_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=1)
                self._make_checks_sh(tmp, exit_code=0)
                with patch.object(F, "protected_paths_check", return_value=[]),\
                     patch.object(F, "load_config", return_value={}),\
                     patch.object(F, "cli_run", return_value="abc1234"),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
                checks_md = F.job_dir("o/r", 1) / "round-1/checks.md"
                output = out.getvalue()
                self.assertIn("GATE: PASS", output)
                self.assertTrue(checks_md.exists())
                self.assertIn("GATE: PASS", checks_md.read_text())

    def test_fail_writes_checks_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=2)
                self._make_checks_sh(tmp, exit_code=1)
                with patch.object(F, "protected_paths_check", return_value=[]),\
                     patch.object(F, "load_config", return_value={}),\
                     patch.object(F, "cli_run", return_value="abc1234"),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
                checks_md = F.job_dir("o/r", 1) / "round-2/checks.md"
                output = out.getvalue()
                self.assertIn("GATE: FAIL", output)
                self.assertIn("GATE: FAIL", checks_md.read_text())

    def test_checks_log_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=1)
                self._make_checks_sh(tmp, exit_code=0)
                with patch.object(F, "protected_paths_check", return_value=[]),\
                     patch.object(F, "load_config", return_value={}),\
                     patch.object(F, "cli_run", return_value="abc1234"),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
                log_file = F.job_dir("o/r", 1) / "round-1/checks.log"
            self.assertTrue(log_file.exists())


# ═══════════════════════════════════════════════════════════════════════════════
# 10. OPEN_PR
# ═══════════════════════════════════════════════════════════════════════════════

class TestOpenPr(unittest.TestCase):

    def _body_file(self, tmp):
        p = Path(tmp) / "pr-body.md"
        p.write_text("## Summary\nThis is the PR body.\n")
        return str(p)

    def test_wrong_stage_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="build")
                with self.assertRaises(SystemExit) as ctx:
                    F.open_pr(SimpleNamespace(
                        repo="o/r",
                        body_file=self._body_file(tmp),
                        draft=False))
        self.assertIn("build", str(ctx.exception))

    def test_wrong_branch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="pr", branch="factory/issue-1")
                def fake_cli(*args, **kw):
                    if "branch" in args and "--show-current" in args:
                        return "main"  # wrong branch
                    return ""
                with patch.object(F, "cli_run", side_effect=fake_cli):
                    with self.assertRaises(SystemExit) as ctx:
                        F.open_pr(SimpleNamespace(
                            repo="o/r",
                            body_file=self._body_file(tmp),
                            draft=False))
        self.assertIn("Check out", str(ctx.exception))

    def test_dirty_tree_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="pr", branch="factory/issue-1")
                def fake_cli(*args, **kw):
                    if "branch" in args and "--show-current" in args:
                        return "factory/issue-1"
                    if "status" in args:
                        return "M dirty.py"
                    return ""
                with patch.object(F, "cli_run", side_effect=fake_cli):
                    with self.assertRaises(SystemExit) as ctx:
                        F.open_pr(SimpleNamespace(
                            repo="o/r",
                            body_file=self._body_file(tmp),
                            draft=False))
        self.assertIn("Commit or discard", str(ctx.exception))

    def test_no_commits_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="pr", branch="factory/issue-1")
                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "0"
                    return ""
                with patch.object(F, "cli_run", side_effect=fake_cli):
                    with self.assertRaises(SystemExit) as ctx:
                        F.open_pr(SimpleNamespace(
                            repo="o/r",
                            body_file=self._body_file(tmp),
                            draft=False))
        self.assertIn("No commits", str(ctx.exception))

    def test_happy_path_marks_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="pr", branch="factory/issue-1")
                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "3"
                    return ""
                with patch.object(F, "cli_run", side_effect=fake_cli),\
                     patch.object(F, "git_push", return_value=""),\
                     patch.object(F, "pr_create",
                                  return_value="https://github.com/o/r/pull/42"),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.open_pr(SimpleNamespace(
                        repo="o/r",
                        body_file=self._body_file(tmp),
                        draft=False))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["state"], "done")
            self.assertEqual(saved["result"], "ready")
            self.assertIn("pull/42", saved["pr_url"])

    def test_draft_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="pr", branch="factory/issue-1")
                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "1"
                    return ""
                pr_calls = []
                def fake_pr(slug, vcs, base, branch, title, body_file, draft):
                    pr_calls.append(draft)
                    return "https://github.com/o/r/pull/99"
                with patch.object(F, "cli_run", side_effect=fake_cli),\
                     patch.object(F, "git_push", return_value=""),\
                     patch.object(F, "pr_create", side_effect=fake_pr),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.open_pr(SimpleNamespace(
                        repo="o/r",
                        body_file=self._body_file(tmp),
                        draft=True))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["result"], "draft")
            self.assertTrue(pr_calls[0])  # draft=True was passed

    def test_footer_appended_to_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="pr", branch="factory/issue-1")
                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "1"
                    return ""
                body_path = Path(tmp) / "pr-body.md"
                body_path.write_text("My body\n")
                captured = {}
                def fake_pr(slug, vcs, base, branch, title, body_file, draft):
                    captured["body"] = Path(body_file).read_text()
                    return "https://github.com/o/r/pull/1"
                with patch.object(F, "cli_run", side_effect=fake_cli),\
                     patch.object(F, "git_push", return_value=""),\
                     patch.object(F, "pr_create", side_effect=fake_pr),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.open_pr(SimpleNamespace(
                        repo="o/r",
                        body_file=str(body_path),
                        draft=False))
            self.assertIn("Closes #1", captured["body"])
            self.assertIn("Hermes Software Factory", captured["body"])


# ═══════════════════════════════════════════════════════════════════════════════
# 11. RESUME
# ═══════════════════════════════════════════════════════════════════════════════

class TestResume(unittest.TestCase):

    def test_missing_job_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                with self.assertRaises(SystemExit) as ctx:
                    F.resume(SimpleNamespace(repo="o/r", issue=99))
        self.assertIn("No recorded", str(ctx.exception))

    def test_done_state_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="done")
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    with self.assertRaises(SystemExit) as ctx:
                        F.resume(SimpleNamespace(repo="o/r", issue=1))
        self.assertIn("done", str(ctx.exception))

    def test_active_state_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="active")
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    with self.assertRaises(SystemExit) as ctx:
                        F.resume(SimpleNamespace(repo="o/r", issue=1))
        self.assertIn("active", str(ctx.exception))

    def test_another_active_job_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="stopped")
                _job(tmp, "o/r", 2, state="active")  # another active job
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    with self.assertRaises(SystemExit) as ctx:
                        F.resume(SimpleNamespace(repo="o/r", issue=1))
        self.assertIn("already active", str(ctx.exception))

    def test_path_mismatch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, state="stopped", path="/different/path")
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    with self.assertRaises(SystemExit) as ctx:
                        F.resume(SimpleNamespace(repo="o/r", issue=1))
        self.assertIn("clone path", str(ctx.exception))

    def test_wrong_branch_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, state="stopped", path=tmp,
                     branch="factory/issue-1")
                with patch.object(F, "cli_run", return_value="main"):  # wrong
                    with self.assertRaises(SystemExit) as ctx:
                        F.resume(SimpleNamespace(repo="o/r", issue=1))
        self.assertIn("Check out", str(ctx.exception))

    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, state="stopped", stage="gate",
                     path=tmp, branch="factory/issue-1")
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    F.resume(SimpleNamespace(repo="o/r", issue=1))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["state"], "active")
            self.assertEqual(saved["stage"], "gate")

    def test_events_preserved(self):
        """Resume must not wipe prior events."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                job = _job(tmp, "o/r", 1, state="stopped",
                           path=tmp, branch="factory/issue-1")
                F.log(job, "before stop")
                # Re-read because log writes to disk
                job2 = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
                job2["state"] = "stopped"
                F.save(F.job_dir("o/r", 1) / "job.json", job2)
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    F.resume(SimpleNamespace(repo="o/r", issue=1))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            # Should have at least the old event plus the resume event
            self.assertGreaterEqual(len(saved["events"]), 2)


# ═══════════════════════════════════════════════════════════════════════════════
# 12. STOP
# ═══════════════════════════════════════════════════════════════════════════════

class TestStop(unittest.TestCase):

    def test_named_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 5, stage="build")
                F.stop(SimpleNamespace(repo="o/r", reason="user stopped"))
                saved = json.loads((F.job_dir("o/r", 5) / "job.json").read_text())
            self.assertEqual(saved["state"], "stopped")
            self.assertIn("user stopped", saved["events"][-1]["text"])

    def test_unnamed_one_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1)
                F.stop(SimpleNamespace(repo=None, reason="test"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["state"], "stopped")

    def test_unnamed_none_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                with self.assertRaises(SystemExit) as ctx:
                    F.stop(SimpleNamespace(repo=None, reason="x"))
        self.assertIn("No active", str(ctx.exception))

    def test_unnamed_many_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/a"); _repo(tmp, "o/b")
                _job(tmp, "o/a", 1); _job(tmp, "o/b", 2)
                with self.assertRaises(SystemExit) as ctx:
                    F.stop(SimpleNamespace(repo=None, reason="x"))
        self.assertIn("Several", str(ctx.exception))

    def test_no_active_named_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                with self.assertRaises(SystemExit):
                    F.stop(SimpleNamespace(repo="o/r", reason="x"))

    def test_stop_at_last_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r"); _job(tmp, "o/r", 1, stage="pr")
                F.stop(SimpleNamespace(repo="o/r", reason="abandoned"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["stage"], "pr")
            self.assertEqual(saved["state"], "stopped")


# ═══════════════════════════════════════════════════════════════════════════════
# 13. DISPATCH
# ═══════════════════════════════════════════════════════════════════════════════

class TestDispatch(unittest.TestCase):

    def _make_worker(self, outcomes, calls_list, slug):
        def fake_worker(command, **kwargs):
            idx = len(calls_list)
            calls_list.append(command)
            number = outcomes[idx][0]
            state  = outcomes[idx][1]
            p = F.job_dir(slug, number) / "job.json"
            job = json.loads(p.read_text())
            job["state"] = state
            F.save(p, job)
            return SimpleNamespace(returncode=0)
        return fake_worker

    def _make_starter(self, slug, tmp):
        def fake_start(args):
            n = args.issue
            F.save(F.job_dir(slug, n) / "job.json", {
                "repo": slug, "path": tmp, "vcs": "github",
                "issue": n, "state": "active", "stage": "ingest",
                "events": [], "branch": f"factory/issue-{n}",
            })
        return fake_start

    def test_two_done(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            waiting = [{"number": 10, "title": "a"}, {"number": 11, "title": "b"}]
            calls = []
            outcomes = [(10, "done"), (11, "done")]
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=waiting),\
                     patch.object(F, "start",
                                  side_effect=self._make_starter(slug, tmp)),\
                     patch.object(F.subprocess, "run",
                                  side_effect=self._make_worker(outcomes, calls, slug)),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.dispatch(SimpleNamespace(repo=slug))
            self.assertEqual(len(calls), 2)

    def test_stopped_worker_halts(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            waiting = [{"number": 10, "title": "a"}, {"number": 11, "title": "b"}]
            calls = []
            outcomes = [(10, "stopped"), (11, "done")]
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=waiting),\
                     patch.object(F, "start",
                                  side_effect=self._make_starter(slug, tmp)),\
                     patch.object(F.subprocess, "run",
                                  side_effect=self._make_worker(outcomes, calls, slug)),\
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        F.dispatch(SimpleNamespace(repo=slug))
            self.assertEqual(len(calls), 1)  # halted after first

    def test_no_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=[]),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.dispatch(SimpleNamespace(repo=slug))
            self.assertIn("NO WORK", out.getvalue())

    def test_busy_existing_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                _job(tmp, slug, 1)  # existing active job
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.dispatch(SimpleNamespace(repo=slug))
            self.assertIn("BUSY", out.getvalue())

    def test_worker_env_unsets_session_vars(self):
        """Every PARENT_SESSION_VAR must appear as -u <VAR> in the worker command."""
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            waiting = [{"number": 5, "title": "t"}]
            calls = []
            outcomes = [(5, "done")]
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=waiting),\
                     patch.object(F, "start",
                                  side_effect=self._make_starter(slug, tmp)),\
                     patch.object(F.subprocess, "run",
                                  side_effect=self._make_worker(outcomes, calls, slug)),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.dispatch(SimpleNamespace(repo=slug))
            cmd = calls[0]
            for var in F.PARENT_SESSION_VARS:
                self.assertIn(var, cmd,
                              f"Expected {var} to appear in worker command for unsetting")

    def test_worker_uses_oneshot_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            waiting = [{"number": 5, "title": "t"}]
            calls = []
            outcomes = [(5, "done")]
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=waiting),\
                     patch.object(F, "start",
                                  side_effect=self._make_starter(slug, tmp)),\
                     patch.object(F.subprocess, "run",
                                  side_effect=self._make_worker(outcomes, calls, slug)),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.dispatch(SimpleNamespace(repo=slug))
            self.assertIn("--oneshot", calls[0])
            self.assertNotIn("--source", calls[0])

    def test_worker_log_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            waiting = [{"number": 7, "title": "t"}]
            calls = []
            outcomes = [(7, "done")]
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=waiting),\
                     patch.object(F, "start",
                                  side_effect=self._make_starter(slug, tmp)),\
                     patch.object(F.subprocess, "run",
                                  side_effect=self._make_worker(outcomes, calls, slug)),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.dispatch(SimpleNamespace(repo=slug))
                log_path = F.job_dir(slug, 7) / "worker-output.log"
            self.assertTrue(log_path.exists())


# ═══════════════════════════════════════════════════════════════════════════════
# 14. PRECHECK
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrecheck(unittest.TestCase):

    def _run(self, tmp, slug, active=False, queue=None):
        with _env(tmp):
            _repo(tmp, slug)
            if active:
                _job(tmp, slug, 1)
            with patch.object(F, "check_queue", return_value=queue or []),\
                 contextlib.redirect_stdout(io.StringIO()) as out:
                F.precheck(SimpleNamespace(repo=slug))
        output = out.getvalue()
        # Parse the last JSON line
        for line in reversed(output.splitlines()):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
        return None

    def test_busy_no_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, "o/r", active=True)
        self.assertFalse(result["wakeAgent"])

    def test_work_waiting_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, "o/r", queue=[{"number": 3, "title": "t"}])
        self.assertTrue(result["wakeAgent"])

    def test_empty_queue_no_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, "o/r", queue=[])
        self.assertFalse(result["wakeAgent"])

    def test_output_is_valid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                with patch.object(F, "check_queue", return_value=[]),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.precheck(SimpleNamespace(repo="o/r"))
            last_json_line = [l for l in out.getvalue().splitlines()
                              if l.strip().startswith("{")]
        self.assertEqual(len(last_json_line), 1)
        parsed = json.loads(last_json_line[0])
        self.assertIn("wakeAgent", parsed)


# ═══════════════════════════════════════════════════════════════════════════════
# 15. INSTALL-PRECHECK
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstallPrecheck(unittest.TestCase):

    def test_precheck_script_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                F.install_precheck(SimpleNamespace(repo="o/r", dispatch=False))
                script = Path(tmp) / "scripts/factory-precheck-o:r.py"
            self.assertTrue(script.exists())
            self.assertIn("precheck", script.read_text())
            self.assertIn("o/r", script.read_text())

    def test_dispatch_script_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                F.install_precheck(SimpleNamespace(repo="o/r", dispatch=True))
                script = Path(tmp) / "scripts/factory-dispatch-o:r.py"
            self.assertTrue(script.exists())
            self.assertIn("dispatch", script.read_text())

    def test_script_is_valid_python(self):
        import py_compile
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                F.install_precheck(SimpleNamespace(repo="o/r", dispatch=False))
                script = Path(tmp) / "scripts/factory-precheck-o:r.py"
            # Should compile without syntax errors
            py_compile.compile(str(script), doraise=True)

    def test_idempotent_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                F.install_precheck(SimpleNamespace(repo="o/r", dispatch=False))
                F.install_precheck(SimpleNamespace(repo="o/r", dispatch=False))
                script = Path(tmp) / "scripts/factory-precheck-o:r.py"
            # Should not raise; file should still exist and be valid
            self.assertTrue(script.exists())

    def test_script_references_factory_py(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                F.install_precheck(SimpleNamespace(repo="o/r", dispatch=False))
                script = Path(tmp) / "scripts/factory-precheck-o:r.py"
            content = script.read_text()
            # Script must reference the factory.py file
            self.assertIn("factory.py", content)
            self.assertIn("runpy", content)


# ═══════════════════════════════════════════════════════════════════════════════
# 16. MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

class TestMonitor(unittest.TestCase):

    def _monitor(self, tmp):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            F.monitor(None)
        return buf.getvalue()

    def test_no_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                out = self._monitor(tmp)
        self.assertIn("No repositories registered", out)
        self.assertIn("HERMES SOFTWARE FACTORY", out)

    def test_idle_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                out = self._monitor(tmp)
        self.assertIn("IDLE", out)
        self.assertIn("o/r", out)

    def test_idle_with_last_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="done", stage="pr")
                out = self._monitor(tmp)
        self.assertIn("LAST JOB", out)

    def test_active_job_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 3, state="active", stage="build")
                out = self._monitor(tmp)
        self.assertIn("NOW BUILDING", out)
        self.assertIn("HERMES", out)

    def test_pipeline_diagram_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="active", stage="gate")
                out = self._monitor(tmp)
        self.assertIn("INGEST", out)
        self.assertIn("GATE", out)
        self.assertIn("APPROVE", out)

    def test_finished_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                j = _job(tmp, "o/r", 1, state="done", stage="pr")
                j["pr_url"] = "https://github.com/o/r/pull/1"
                j["result"] = "ready"
                F.save(F.job_dir("o/r", 1) / "job.json", j)
                out = self._monitor(tmp)
        self.assertIn("Finished", out)
        self.assertIn("pull/1", out)

    def test_multiple_repos(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "org/alpha")
                _repo(tmp, "org/beta")
                out = self._monitor(tmp)
        self.assertIn("org/alpha", out)
        self.assertIn("org/beta", out)

    def test_long_title_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                j = _job(tmp, "o/r", 1, state="active")
                j["title"] = "A" * 200
                F.save(F.job_dir("o/r", 1) / "job.json", j)
                out = self._monitor(tmp)
        self.assertIn("...", out)

    def test_vcs_tag_shown_in_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", vcs="gitlab")
                out = self._monitor(tmp)
        self.assertIn("gitlab", out)

    def test_stopped_job_in_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="stopped", stage="build")
                out = self._monitor(tmp)
        self.assertIn("stopped", out)


# ═══════════════════════════════════════════════════════════════════════════════
# 17. MONITOR HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgeHelper(unittest.TestCase):

    def test_minutes_under_60(self):
        t = F.now()
        result = F._age(t)
        self.assertRegex(result, r"^\d+m$")

    def test_hours_format(self):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(minutes=90)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        result = F._age(past)
        self.assertRegex(result, r"^1h\d{2}m$")

    def test_zero_minutes(self):
        result = F._age(F.now())
        self.assertEqual(result, "0m")


class TestFitHelper(unittest.TestCase):

    def test_short_unchanged(self):
        self.assertEqual(F._fit("hello", 10), "hello")

    def test_exact_unchanged(self):
        self.assertEqual(F._fit("12345", 5), "12345")

    def test_truncated_with_ellipsis(self):
        result = F._fit("1234567890", 7)
        self.assertEqual(result, "1234...")
        self.assertEqual(len(result), 7)

    def test_empty_string(self):
        self.assertEqual(F._fit("", 10), "")


class TestStageStatus(unittest.TestCase):

    def _job(self, stage, state="active", stages=None):
        ts = F.now()
        s = stages or {s: ts for s in F.STAGES[:F.STAGES.index(stage) + 1]}
        return {"stage": stage, "state": state, "stages": s, "result": "ready"}

    def test_future_stage_empty(self):
        job = self._job("spec")
        self.assertEqual(F._stage_status(job, "build"), "")

    def test_current_active(self):
        job = self._job("build")
        result = F._stage_status(job, "build")
        self.assertIn("working", result)

    def test_current_stopped(self):
        job = self._job("build", state="stopped")
        self.assertEqual(F._stage_status(job, "build"), "STOPPED")

    def test_past_stage_done(self):
        job = self._job("build")
        result = F._stage_status(job, "ingest")
        self.assertIn("done", result)

    def test_last_stage_done_shows_result(self):
        job = self._job("pr", state="done")
        result = F._stage_status(job, "pr")
        self.assertEqual(result, "ready")


# ═══════════════════════════════════════════════════════════════════════════════
# 18. MULTI-REPO ISOLATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiRepoIsolation(unittest.TestCase):
    """Jobs from different repos must not interfere with each other."""

    def test_active_job_scoped_to_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "org/a"); _repo(tmp, "org/b")
                _job(tmp, "org/a", 1)  # active in a
                # org/b should show no active job
                self.assertIsNone(F.active_job("org/b"))
                self.assertIsNotNone(F.active_job("org/a"))

    def test_stop_scoped_to_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "org/a"); _repo(tmp, "org/b")
                _job(tmp, "org/a", 1); _job(tmp, "org/b", 2)
                F.stop(SimpleNamespace(repo="org/a", reason="test"))
                # org/b still active
                self.assertIsNotNone(F.active_job("org/b"))
                saved_b = json.loads(
                    (F.job_dir("org/b", 2) / "job.json").read_text())
                self.assertEqual(saved_b["state"], "active")

    def test_stage_scoped_to_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "org/a"); _repo(tmp, "org/b")
                _job(tmp, "org/a", 1, stage="ingest")
                _job(tmp, "org/b", 2, stage="ingest")
                F.stage_cmd(SimpleNamespace(repo="org/a", name="spec", note=None))
                saved_b = json.loads(
                    (F.job_dir("org/b", 2) / "job.json").read_text())
                # org/b must be unchanged
                self.assertEqual(saved_b["stage"], "ingest")


# ═══════════════════════════════════════════════════════════════════════════════
# 19. EDGE CASES / BOUNDARY CONDITIONS
# ═══════════════════════════════════════════════════════════════════════════════

class TestEdgeCases(unittest.TestCase):

    def test_hermes_home_not_set_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                F.hermes_home()
        self.assertIn("HERMES_HOME", str(ctx.exception))

    def test_save_and_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                data = {"key": "value", "nested": {"a": [1, 2, 3]}}
                p = Path(tmp) / "test.json"
                F.save(p, data)
                loaded = json.loads(p.read_text())
            self.assertEqual(loaded, data)

    def test_job_dir_slug_with_hyphens(self):
        """Hyphens in slug must not confuse the key() function."""
        slug = "my-org/my-repo"
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, slug)
                _job(tmp, slug, 42)
                job = F.active_job(slug)
            self.assertIsNotNone(job)
            self.assertEqual(job["issue"], 42)

    def test_all_jobs_with_corrupted_file_skipped(self):
        """all_jobs() should warn and skip corrupt JSON, not crash."""
        import io
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1)
                corrupt = F.state_dir() / "jobs" / "corrupt" / "job.json"
                corrupt.parent.mkdir(parents=True)
                corrupt.write_text("not valid json")
                import sys
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    jobs = F.all_jobs()
            # Valid job still returned; corrupt file skipped
            self.assertEqual(len(jobs), 1)
            self.assertIn("WARNING", err.getvalue())

    def test_check_queue_excludes_already_built(self):
        """Issues with existing jobs (any state) must not be re-queued."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 5, state="done")
                issues = [{"number": 5, "title": "done"},
                          {"number": 6, "title": "new"}]
                with patch.object(F, "issue_list_labeled", return_value=issues):
                    waiting = F.check_queue(F.load_repo("o/r"))
                    waiting_nums = [i["number"] for i in waiting]
            self.assertNotIn(5, waiting_nums)
            self.assertIn(6, waiting_nums)

    def test_route_does_not_advance_stage(self):
        """Explicitly test the documented pitfall: route ≠ stage advance."""
        config = {"routing": {"low": {"code": "haiku"}}, "surface_reviewers": {}}
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="build")
                with patch.object(F, "load_config", return_value=config):
                    F.route_cmd(SimpleNamespace(repo="o/r", risk="low", surfaces=""))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            # stage must still be build, not gate
            self.assertEqual(saved["stage"], "build")

    def test_note_on_stopped_job_raises(self):
        """note_cmd requires an ACTIVE job."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="stopped")
                with self.assertRaises(SystemExit):
                    F.note_cmd(SimpleNamespace(repo="o/r", text="x"))

    def test_stage_on_stopped_job_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="stopped", stage="spec")
                with self.assertRaises(SystemExit):
                    F.stage_cmd(SimpleNamespace(repo="o/r", name="build", note=None))

    def test_resume_then_stop_cycle(self):
        """Full stop→resume→stop cycle must work."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, state="active", stage="build",
                     path=tmp, branch="factory/issue-1")
                # Stop it
                F.stop(SimpleNamespace(repo="o/r", reason="first stop"))
                # Resume it
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    F.resume(SimpleNamespace(repo="o/r", issue=1))
                # Stop it again
                F.stop(SimpleNamespace(repo="o/r", reason="second stop"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["state"], "stopped")
            # Should have at least 3 events
            self.assertGreaterEqual(len(saved["events"]), 3)

    def test_queue_ordering_oldest_first(self):
        """check_queue must return issues sorted by number (oldest first)."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                issues = [{"number": 10, "title": "later"},
                          {"number": 3,  "title": "older"},
                          {"number": 7,  "title": "middle"}]
                with patch.object(F, "issue_list_labeled", return_value=issues):
                    waiting = F.check_queue(F.load_repo("o/r"))
            self.assertEqual([i["number"] for i in waiting], [3, 7, 10])

    def test_monitor_shows_pr_url_in_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                j = _job(tmp, "o/r", 1, state="done", stage="pr")
                j["pr_url"] = "https://github.com/o/r/pull/55"
                j["result"] = "ready"
                F.save(F.job_dir("o/r", 1) / "job.json", j)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    F.monitor(None)
        self.assertIn("pull/55", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
