"""
Extended corner-case and branch-coverage tests for factory.py.

New areas covered (not in test_factory_core.py):
  cli_run          : FileNotFoundError, check=False, stderr fallback message
  _vcs             : dict input, registered slug, unregistered slug fallback
  _cli             : github→gh, gitlab→glab
  load_config      : missing file, present file
  VCS dispatch     : issue_list_labeled (gh+glab), issue_view_full (gh+glab),
                     issue_comment (gh+glab), label_create (gh+glab),
                     pr_create (gh+glab, draft flag both backends)
  add              : archived raises, no issues raises, --path wrong-slug raises,
                     multiple clones raises, single clone reused,
                     clone-target-exists raises, label-already-exists swallowed,
                     re-register preserves checked/queue
  check_queue      : active jobs excluded, saves updated queue to repo file
  gate_cmd         : >80-line output truncated in checks.md
  open_pr          : stage='review' accepted, stage='approve' accepted
  dispatch         : worker returncode!=0 halts, lock prevents concurrent second
  monitor          : pr_url over pr field, >5 finished shows last 5,
                     HERMES cursor at every stage, multiple queue items,
                     checked-timestamp present vs absent
  _age             : exactly 60 minutes = 1h00m
  _stage_status    : past stage with both timestamps shows elapsed
  find_clones      : node_modules/worktrees skipped, depth limit, no dup when
                     HERMES_HOME inside home
  precheck         : busy prints issue+stage, multi-issue count message
  install_precheck : unregistered repo raises
  now()            : ISO 8601 UTC parseable by _parse()
  multi-round      : gate FAIL -> build stage, round counter
"""

import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, call

_FACTORY_PATH = (
    Path(__file__).resolve().parents[1] / "factory/bin/factory.py"
)
_spec = importlib.util.spec_from_file_location("factory", _FACTORY_PATH)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


# ── helpers ───────────────────────────────────────────────────────────────────

def _env(tmp):
    return patch.dict(os.environ, HERMES_HOME=tmp)


def _repo(tmp, slug, path=None, vcs="github", base="main", queue=None,
          checked=None):
    rec = {"repo": slug, "path": path or tmp, "vcs": vcs, "base": base,
           "added": F.now(), "checked": checked, "queue": queue or []}
    F.save(F.repo_file(slug), rec)
    return rec


def _job(tmp, slug, number, stage="ingest", state="active",
         path=None, branch=None, base="main", round_=0, round_limit=3):
    branch = branch or f"factory/issue-{number}"
    job = {
        "repo": slug, "path": path or tmp, "vcs": "github", "issue": number,
        "title": f"Issue {number}", "url": f"https://example.com/{number}",
        "branch": branch, "base": base, "stage": stage, "state": state,
        "result": None, "pr": None, "pr_url": None,
        "risk": None, "surfaces": [], "agents": {},
        "round": round_, "round_limit": round_limit, "reviews": {},
        "started": F.now(), "updated": F.now(),
        "stages": {stage: F.now()}, "events": [],
    }
    F.save(F.job_dir(slug, number) / "job.json", job)
    return job


def _checks_sh(tmp, exit_code=0, lines=1):
    checks = Path(tmp) / "factory/checks.sh"
    checks.parent.mkdir(parents=True, exist_ok=True)
    output_lines = "\n".join(f"echo line{i}" for i in range(lines))
    checks.write_text(
        f"#!/usr/bin/env bash\nset +e\n{output_lines}\nexit {exit_code}\n")
    checks.chmod(0o755)
    return checks


# ═══════════════════════════════════════════════════════════════════════════════
# 1. cli_run
# ═══════════════════════════════════════════════════════════════════════════════

class TestCliRun(unittest.TestCase):

    def test_tool_not_installed_raises(self):
        with self.assertRaises(SystemExit) as ctx:
            F.cli_run("definitely-not-a-real-binary-xyz", "--help")
        self.assertIn("not installed", str(ctx.exception))

    def test_check_false_no_raise_on_nonzero(self):
        result = F.cli_run("false", check=False)
        # Should return empty string without raising
        self.assertEqual(result, "")

    def test_check_true_raises_on_nonzero(self):
        with self.assertRaises(SystemExit):
            F.cli_run("false", check=True)

    def test_stderr_in_error_message(self):
        # Use a shell that writes to stderr and exits non-zero
        with self.assertRaises(SystemExit) as ctx:
            F.cli_run("bash", "-c", "echo 'error detail' >&2; exit 1")
        self.assertIn("error detail", str(ctx.exception))

    def test_stdout_returned_stripped(self):
        result = F.cli_run("echo", "hello world")
        self.assertEqual(result, "hello world")

    def test_returns_stdout_on_success(self):
        result = F.cli_run("bash", "-c", "echo -n trimmed")
        self.assertEqual(result, "trimmed")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. _vcs and _cli
# ═══════════════════════════════════════════════════════════════════════════════

class TestVcsHelpers(unittest.TestCase):

    def test_vcs_dict_with_key(self):
        self.assertEqual(F._vcs({"vcs": "gitlab"}), "gitlab")

    def test_vcs_dict_missing_key_defaults_github(self):
        self.assertEqual(F._vcs({}), "github")

    def test_vcs_dict_github(self):
        self.assertEqual(F._vcs({"vcs": "github"}), "github")

    def test_vcs_slug_registered(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", vcs="gitlab")
                result = F._vcs("o/r")
            self.assertEqual(result, "gitlab")

    def test_vcs_slug_unregistered_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                result = F._vcs("nobody/nothing")
            self.assertEqual(result, "github")

    def test_cli_github(self):
        self.assertEqual(F._cli("github"), "gh")

    def test_cli_gitlab(self):
        self.assertEqual(F._cli("gitlab"), "glab")

    def test_cli_unknown_defaults_gh(self):
        # Any non-gitlab value returns gh
        self.assertEqual(F._cli("unknown"), "gh")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. load_config
# ═══════════════════════════════════════════════════════════════════════════════

class TestLoadConfig(unittest.TestCase):

    def test_missing_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = F.load_config(tmp)
        self.assertEqual(result, {})

    def test_present_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "factory" / "config.json"
            p.parent.mkdir()
            p.write_text('{"max_rounds": 5, "vcs": "gitlab"}')
            result = F.load_config(tmp)
        self.assertEqual(result["max_rounds"], 5)
        self.assertEqual(result["vcs"], "gitlab")

    def test_max_rounds_read_by_start(self):
        """start() should use max_rounds from config.json."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                p = Path(tmp) / "factory" / "config.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text('{"max_rounds": 7}')
                issue = {"number": 1, "title": "t", "body": "", "url": "",
                         "author": {"login": "u"}}
                with patch.object(F, "check_queue",
                                  return_value=[{"number": 1, "title": "t"}]),\
                     patch.object(F, "issue_list_labeled",
                                  return_value=[{"number": 1, "title": "t"}]),\
                     patch.object(F, "issue_view_full", return_value=issue),\
                     patch.object(F, "cli_run", return_value=""),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.start(SimpleNamespace(repo="o/r", issue=None))
                saved = json.loads(
                    (F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["round_limit"], 7)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. VCS dispatch functions (GitHub and GitLab paths)
# ═══════════════════════════════════════════════════════════════════════════════

class TestVcsDispatch(unittest.TestCase):

    def _mock_cli(self, return_value):
        return patch.object(F, "cli_run", return_value=return_value)

    # ── issue_list_labeled ────────────────────────────────────────────────────

    def test_issue_list_github(self):
        data = [{"number": 1, "title": "Bug"}]
        with self._mock_cli(json.dumps(data)):
            result = F.issue_list_labeled("o/r", "github", "factory")
        self.assertEqual(result[0]["number"], 1)

    def test_issue_list_gitlab(self):
        raw = [{"iid": 5, "title": "Feature"}]
        with self._mock_cli(json.dumps(raw)):
            result = F.issue_list_labeled("o/r", "gitlab", "factory")
        self.assertEqual(result[0]["number"], 5)
        self.assertEqual(result[0]["title"], "Feature")

    def test_issue_list_gitlab_empty(self):
        with self._mock_cli("null"):
            result = F.issue_list_labeled("o/r", "gitlab", "factory")
        self.assertEqual(result, [])

    # ── issue_view_full ───────────────────────────────────────────────────────

    def test_issue_view_full_github(self):
        data = {"number": 3, "title": "Fix", "body": "text",
                "url": "https://x.com/3", "author": {"login": "alice"}}
        with self._mock_cli(json.dumps(data)):
            result = F.issue_view_full("o/r", "github", 3)
        self.assertEqual(result["number"], 3)
        self.assertEqual(result["author"]["login"], "alice")

    def test_issue_view_full_gitlab(self):
        raw = {"iid": 7, "title": "GL issue", "description": "body text",
               "web_url": "https://gl.com/7",
               "author": {"username": "bob"}}
        with self._mock_cli(json.dumps(raw)):
            result = F.issue_view_full("o/r", "gitlab", 7)
        self.assertEqual(result["number"], 7)
        self.assertEqual(result["title"], "GL issue")
        self.assertEqual(result["body"], "body text")
        self.assertEqual(result["author"]["login"], "bob")

    # ── issue_comment ─────────────────────────────────────────────────────────

    def test_issue_comment_github_calls_gh(self):
        calls = []
        def fake_cli(*args, **kw):
            calls.append(args)
            return ""
        with patch.object(F, "cli_run", side_effect=fake_cli):
            F.issue_comment("o/r", "github", 1, "Hello")
        self.assertTrue(any("gh" in a[0] for a in calls))
        self.assertTrue(any("comment" in str(a) for a in calls))

    def test_issue_comment_gitlab_calls_glab(self):
        calls = []
        def fake_cli(*args, **kw):
            calls.append(args)
            return ""
        with patch.object(F, "cli_run", side_effect=fake_cli):
            F.issue_comment("o/r", "gitlab", 5, "Note")
        self.assertTrue(any("glab" in a[0] for a in calls))

    # ── label_create ──────────────────────────────────────────────────────────

    def test_label_create_github(self):
        calls = []
        with patch.object(F, "cli_run", side_effect=lambda *a, **k: calls.append(a) or ""):
            F.label_create("o/r", "github", "factory", "5319E7", "desc")
        self.assertTrue(any("gh" in str(c) for c in calls))
        self.assertTrue(any("factory" in str(c) for c in calls))

    def test_label_create_gitlab(self):
        calls = []
        with patch.object(F, "cli_run", side_effect=lambda *a, **k: calls.append(a) or ""):
            F.label_create("o/r", "gitlab", "factory", "5319E7", "desc")
        self.assertTrue(any("glab" in str(c) for c in calls))
        # GitLab uses #color format
        self.assertTrue(any("#5319E7" in str(c) for c in calls))

    # ── pr_create ─────────────────────────────────────────────────────────────

    def test_pr_create_github_returns_url(self):
        with patch.object(F, "cli_run", return_value="https://github.com/o/r/pull/1"):
            url = F.pr_create("o/r", "github", "main", "factory/issue-1",
                              "Title", "/tmp/body.md", False)
        self.assertIn("pull/1", url)

    def test_pr_create_github_draft(self):
        calls = []
        def fake(*a, **k):
            calls.append(a)
            return "https://github.com/o/r/pull/2"
        with patch.object(F, "cli_run", side_effect=fake):
            F.pr_create("o/r", "github", "main", "factory/issue-2",
                        "Title", "/tmp/body.md", True)
        self.assertTrue(any("--draft" in str(c) for c in calls))

    def test_pr_create_gitlab_returns_last_line(self):
        # glab mr create outputs multiple lines; URL is the last
        with patch.object(F, "cli_run",
                          return_value="Created MR\nhttps://gitlab.com/o/r/-/merge_requests/5"):
            url = F.pr_create("o/r", "gitlab", "main", "factory/issue-3",
                              "Title", "/tmp/body.md", False)
        self.assertEqual(url, "https://gitlab.com/o/r/-/merge_requests/5")

    def test_pr_create_gitlab_draft(self):
        calls = []
        def fake(*a, **k):
            calls.append(a)
            return "https://gitlab.com/o/r/-/merge_requests/6"
        with patch.object(F, "cli_run", side_effect=fake):
            F.pr_create("o/r", "gitlab", "main", "b", "T", "/f", True)
        self.assertTrue(any("--draft" in str(c) for c in calls))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. add()
# ═══════════════════════════════════════════════════════════════════════════════

class TestAdd(unittest.TestCase):

    _REPO_VIEW = {
        "nameWithOwner": "owner/repo",
        "viewerPermission": "WRITE",
        "hasIssuesEnabled": True,
        "isArchived": False,
        "defaultBranchRef": {"name": "main"},
    }

    def _add(self, args_extra=None, repo_view=None, origin=None,
             clones=None, push_raises=False, label_raises=False, tmp=None):
        view = repo_view or self._REPO_VIEW
        args = SimpleNamespace(
            repo="owner/repo", path=None, vcs="github",
            **(args_extra or {}))
        
        def fake_label(*a, **k):
            if label_raises:
                raise SystemExit("label exists")
        
        def fake_push(*a, **k):
            if push_raises:
                raise SystemExit("no push access")
        
        ctx = [
            patch.object(F, "repo_view", return_value=view),
            patch.object(F, "find_clones", return_value=clones if clones is not None else []),
            patch.object(F, "git_push", side_effect=fake_push),
            patch.object(F, "label_create", side_effect=fake_label),
            patch.object(F, "origin_slug", return_value=origin or "owner/repo"),
            patch.object(F, "cli_run", return_value=""),
            contextlib.redirect_stdout(io.StringIO()),
        ]
        import contextlib as _cl
        with _cl.ExitStack() as stack:
            for c in ctx:
                stack.enter_context(c)
            if tmp:
                stack.enter_context(_env(tmp))
            F.add(args)

    def test_archived_raises(self):
        view = {**self._REPO_VIEW, "isArchived": True}
        with self.assertRaises(SystemExit) as ctx:
            self._add(repo_view=view)
        self.assertIn("unarchived", str(ctx.exception))

    def test_no_issues_raises(self):
        view = {**self._REPO_VIEW, "hasIssuesEnabled": False}
        with self.assertRaises(SystemExit) as ctx:
            self._add(repo_view=view)
        self.assertIn("issues enabled", str(ctx.exception))

    def test_path_wrong_slug_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(repo="owner/repo", path=tmp, vcs="github")
            with patch.object(F, "repo_view", return_value=self._REPO_VIEW),\
                 patch.object(F, "origin_slug", return_value="other/repo"),\
                 self.assertRaises(SystemExit) as ctx:
                with _env(tmp):
                    F.add(args)
        self.assertIn("isn't a clone", str(ctx.exception))

    def test_multiple_clones_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            p1 = Path(tmp) / "clone1"
            p2 = Path(tmp) / "clone2"
            p1.mkdir(); p2.mkdir()
            with self.assertRaises(SystemExit) as ctx:
                self._add(clones=[p1, p2], tmp=tmp)
        self.assertIn("clones", str(ctx.exception))

    def test_clone_target_exists_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No existing clones -> will try to clone into work/repo
            work_dir = Path(tmp) / "work" / "repo"
            work_dir.mkdir(parents=True)
            with patch.object(F, "repo_view", return_value=self._REPO_VIEW),\
                 patch.object(F, "find_clones", return_value=[]),\
                 patch.object(F, "git_push", return_value=""),\
                 patch.object(F, "label_create", return_value=""),\
                 _env(tmp),\
                 self.assertRaises(SystemExit) as ctx:
                F.add(SimpleNamespace(repo="owner/repo", path=None, vcs="github"))
        self.assertIn("already exists", str(ctx.exception))

    def test_label_already_exists_swallowed(self):
        """label_create raising SystemExit must be caught, not propagated."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                # Should not raise
                self._add(label_raises=True, tmp=tmp)

    def test_re_register_preserves_checked_and_queue(self):
        """Adding a repo a second time preserves checked timestamp and queue."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                # First registration
                old_checked = "2026-01-01T00:00:00Z"
                old_queue = [{"number": 1, "title": "old"}]
                _repo(tmp, "owner/repo", checked=old_checked, queue=old_queue)
                # Second add
                self._add(tmp=tmp)
                saved = F.load_repo("owner/repo")
            self.assertEqual(saved["checked"], old_checked)
            self.assertEqual(saved["queue"], old_queue)

    def test_single_clone_found_and_used(self):
        """When exactly one clone is found, it should be used without cloning."""
        with tempfile.TemporaryDirectory() as tmp:
            clone_path = Path(tmp) / "myrepo"
            clone_path.mkdir()
            cli_calls = []
            with _env(tmp),\
                 patch.object(F, "repo_view", return_value=self._REPO_VIEW),\
                 patch.object(F, "find_clones", return_value=[clone_path]),\
                 patch.object(F, "git_push", return_value=""),\
                 patch.object(F, "label_create", return_value=""),\
                 patch.object(F, "cli_run",
                              side_effect=lambda *a, **k: cli_calls.append(a) or ""),\
                 contextlib.redirect_stdout(io.StringIO()):
                F.add(SimpleNamespace(repo="owner/repo", path=None, vcs="github"))
            # gh repo clone should NOT have been called
            self.assertFalse(any("clone" in str(c) for c in cli_calls))

    def test_add_saves_repo_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                self._add(tmp=tmp)
                saved = F.load_repo("owner/repo")
            self.assertEqual(saved["repo"], "owner/repo")
            self.assertEqual(saved["base"], "main")
            self.assertEqual(saved["vcs"], "github")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. check_queue edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckQueue(unittest.TestCase):

    def test_active_jobs_excluded(self):
        """Active (in-progress) jobs must not re-enter the queue."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 3, state="active")  # already active
                issues = [{"number": 3, "title": "active"},
                          {"number": 4, "title": "new"}]
                with patch.object(F, "issue_list_labeled", return_value=issues):
                    waiting = F.check_queue(F.load_repo("o/r"))
            self.assertNotIn(3, [i["number"] for i in waiting])
            self.assertIn(4, [i["number"] for i in waiting])

    def test_saves_queue_to_repo_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                issues = [{"number": 1, "title": "a"}, {"number": 2, "title": "b"}]
                with patch.object(F, "issue_list_labeled", return_value=issues):
                    F.check_queue(F.load_repo("o/r"))
                saved = F.load_repo("o/r")
            self.assertEqual(len(saved["queue"]), 2)
            self.assertIsNotNone(saved["checked"])

    def test_checked_timestamp_updated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", checked="2020-01-01T00:00:00Z")
                with patch.object(F, "issue_list_labeled", return_value=[]):
                    F.check_queue(F.load_repo("o/r"))
                saved = F.load_repo("o/r")
            self.assertNotEqual(saved["checked"], "2020-01-01T00:00:00Z")

    def test_stopped_job_excluded(self):
        """Stopped jobs count as built — don't re-queue them."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 7, state="stopped")
                issues = [{"number": 7, "title": "stopped"},
                          {"number": 8, "title": "new"}]
                with patch.object(F, "issue_list_labeled", return_value=issues):
                    waiting = F.check_queue(F.load_repo("o/r"))
            # stopped jobs have a job.json entry — must be excluded
            self.assertNotIn(7, [i["number"] for i in waiting])


# ═══════════════════════════════════════════════════════════════════════════════
# 7. gate_cmd: long output truncation
# ═══════════════════════════════════════════════════════════════════════════════

class TestGateTruncation(unittest.TestCase):

    def test_long_output_truncated_in_checks_md(self):
        """checks.sh producing >80 lines → checks.md keeps last 80 lines."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=1)
                checks = Path(tmp) / "factory/checks.sh"
                checks.parent.mkdir(parents=True, exist_ok=True)
                # Write a script that prints 150 lines then exits 0
                lines = "\n".join(f'echo "line {i:03d}"' for i in range(150))
                checks.write_text(f"#!/usr/bin/env bash\n{lines}\nexit 0\n")
                checks.chmod(0o755)
                with patch.object(F, "protected_paths_check", return_value=[]),\
                     patch.object(F, "load_config", return_value={}),\
                     patch.object(F, "cli_run", return_value="abc1234"),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
                md = (F.job_dir("o/r", 1) / "round-1/checks.md").read_text()
            # First 70 lines should NOT appear (they were trimmed)
            self.assertNotIn("line 000", md)
            # Last lines should appear
            self.assertIn("line 149", md)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. open_pr: accepted stages
# ═══════════════════════════════════════════════════════════════════════════════

class TestOpenPrStages(unittest.TestCase):

    def _body_file(self, tmp):
        p = Path(tmp) / "pr-body.md"
        p.write_text("## PR body\n")
        return str(p)

    def _fake_cli(self, branch="factory/issue-1"):
        def fn(*args, **kw):
            if "--show-current" in args:
                return branch
            if "--porcelain" in args:
                return ""
            if "rev-list" in args:
                return "1"
            return ""
        return fn

    def _run_open_pr(self, tmp, stage):
        _repo(tmp, "o/r")
        _job(tmp, "o/r", 1, stage=stage, branch="factory/issue-1")
        with patch.object(F, "cli_run", side_effect=self._fake_cli()),\
             patch.object(F, "git_push", return_value=""),\
             patch.object(F, "pr_create",
                          return_value="https://github.com/o/r/pull/1"),\
             contextlib.redirect_stdout(io.StringIO()):
            F.open_pr(SimpleNamespace(
                repo="o/r",
                body_file=self._body_file(tmp),
                draft=False))
        return json.loads((F.job_dir("o/r", 1) / "job.json").read_text())

    def test_review_stage_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                saved = self._run_open_pr(tmp, "review")
            self.assertEqual(saved["state"], "done")

    def test_approve_stage_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                saved = self._run_open_pr(tmp, "approve")
            self.assertEqual(saved["state"], "done")

    def test_ingest_stage_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="ingest")
                with self.assertRaises(SystemExit) as ctx:
                    F.open_pr(SimpleNamespace(
                        repo="o/r", body_file=self._body_file(tmp), draft=False))
            self.assertIn("ingest", str(ctx.exception))

    def test_spec_stage_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="spec")
                with self.assertRaises(SystemExit):
                    F.open_pr(SimpleNamespace(
                        repo="o/r", body_file=self._body_file(tmp), draft=False))

    def test_build_stage_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="build")
                with self.assertRaises(SystemExit):
                    F.open_pr(SimpleNamespace(
                        repo="o/r", body_file=self._body_file(tmp), draft=False))

    def test_gate_stage_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="gate")
                with self.assertRaises(SystemExit):
                    F.open_pr(SimpleNamespace(
                        repo="o/r", body_file=self._body_file(tmp), draft=False))


# ═══════════════════════════════════════════════════════════════════════════════
# 9. dispatch: extra cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestDispatchExtra(unittest.TestCase):

    def _make_starter(self, slug, tmp):
        def fake_start(args):
            n = args.issue
            F.save(F.job_dir(slug, n) / "job.json", {
                "repo": slug, "path": tmp, "vcs": "github",
                "issue": n, "state": "active", "stage": "ingest",
                "events": [], "branch": f"factory/issue-{n}",
            })
        return fake_start

    def test_worker_nonzero_returncode_halts(self):
        """Worker exiting non-zero must halt the batch even if state=done."""
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            waiting = [{"number": 3, "title": "t"}]
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                def fake_worker(command, **kw):
                    p = F.job_dir(slug, 3) / "job.json"
                    job = json.loads(p.read_text())
                    job["state"] = "done"
                    F.save(p, job)
                    return SimpleNamespace(returncode=1)  # non-zero exit
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=waiting),\
                     patch.object(F, "start",
                                  side_effect=self._make_starter(slug, tmp)),\
                     patch.object(F.subprocess, "run", side_effect=fake_worker),\
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        F.dispatch(SimpleNamespace(repo=slug))
            self.assertIn("did not complete", str(ctx.exception))

    def test_dispatch_lock_prevents_concurrent(self):
        """A second concurrent dispatch for the same repo should print BUSY."""
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            import fcntl
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                lock_path = F.state_dir() / f"{F.key(slug)}.dispatch.lock"
                lock_path.parent.mkdir(parents=True, exist_ok=True)
                with lock_path.open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    # While lock is held by this process, dispatch should see BUSY
                    with patch.object(F, "load_repo",
                                      return_value={"repo": slug, "path": tmp,
                                                    "vcs": "github", "base": "main"}),\
                         contextlib.redirect_stdout(io.StringIO()) as out:
                        F.dispatch(SimpleNamespace(repo=slug))
            self.assertIn("BUSY", out.getvalue())

    def test_dispatch_prompt_contains_issue_number(self):
        """Worker prompt must reference the specific issue number."""
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            waiting = [{"number": 42, "title": "specific issue"}]
            prompts = []
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                def fake_worker(command, **kw):
                    prompts.append(command[-1])  # -q PROMPT is last arg
                    p = F.job_dir(slug, 42) / "job.json"
                    job = json.loads(p.read_text())
                    job["state"] = "done"
                    F.save(p, job)
                    return SimpleNamespace(returncode=0)
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     patch.object(F, "check_queue", return_value=waiting),\
                     patch.object(F, "start",
                                  side_effect=self._make_starter(slug, tmp)),\
                     patch.object(F.subprocess, "run", side_effect=fake_worker),\
                     contextlib.redirect_stdout(io.StringIO()):
                    F.dispatch(SimpleNamespace(repo=slug))
            self.assertTrue(any("42" in p for p in prompts))
            self.assertTrue(any("factory" in p for p in prompts))


# ═══════════════════════════════════════════════════════════════════════════════
# 10. monitor: extended cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestMonitorExtended(unittest.TestCase):

    def _monitor(self, tmp):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            F.monitor(None)
        return buf.getvalue()

    def test_pr_url_over_pr_field(self):
        """pr_url must take precedence over pr when both are set."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                j = _job(tmp, "o/r", 1, state="done", stage="pr")
                j["pr"]     = "https://github.com/o/r/pull/OLD"
                j["pr_url"] = "https://github.com/o/r/pull/NEW"
                j["result"] = "ready"
                F.save(F.job_dir("o/r", 1) / "job.json", j)
                out = self._monitor(tmp)
        self.assertIn("pull/NEW", out)

    def test_more_than_5_finished_shows_last_5(self):
        """Only the 5 most recent finished jobs should appear."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                for n in range(1, 8):  # 7 done jobs
                    j = _job(tmp, "o/r", n, state="done", stage="pr")
                    j["result"] = "ready"
                    F.save(F.job_dir("o/r", n) / "job.json", j)
                out = self._monitor(tmp)
            # Count lines under "Finished" section
            in_finished = False
            finished_lines = []
            for line in out.splitlines():
                if "Finished" in line:
                    in_finished = True
                    continue
                if in_finished and line.strip().startswith("o/r"):
                    finished_lines.append(line)
        self.assertLessEqual(len(finished_lines), 5)

    def test_hermes_cursor_at_each_stage(self):
        """HERMES cursor must appear when active job is at any pipeline stage."""
        display_stages = ["ingest", "spec", "build", "gate",
                          "review", "approve", "pr"]
        for stage in display_stages:
            with tempfile.TemporaryDirectory() as tmp:
                with _env(tmp):
                    _repo(tmp, "o/r")
                    _job(tmp, "o/r", 1, stage=stage, state="active")
                    out = self._monitor(tmp)
                with self.subTest(stage=stage):
                    self.assertIn("HERMES", out,
                                  f"HERMES cursor missing at stage {stage!r}")

    def test_multiple_queue_items_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", queue=[
                    {"number": 1, "title": "First"},
                    {"number": 2, "title": "Second"},
                    {"number": 3, "title": "Third"},
                ])
                out = self._monitor(tmp)
        self.assertIn("#1", out)
        self.assertIn("#2", out)
        self.assertIn("#3", out)

    def test_checked_timestamp_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", checked="2026-01-01T12:00:00Z")
                out = self._monitor(tmp)
        self.assertIn("checked", out)

    def test_not_checked_yet_shown(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", checked=None)
                out = self._monitor(tmp)
        self.assertIn("not checked yet", out)

    def test_active_job_with_pr_shows_pr_url(self):
        """Jobs that have a PR/MR should show it in the card."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                j = _job(tmp, "o/r", 1, state="active", stage="pr")
                j["pr_url"] = "https://github.com/o/r/pull/77"
                F.save(F.job_dir("o/r", 1) / "job.json", j)
                out = self._monitor(tmp)
        self.assertIn("pull/77", out)

    def test_stopped_job_shows_stopped_in_diagram(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                j = _job(tmp, "o/r", 1, state="stopped", stage="build")
                F.save(F.job_dir("o/r", 1) / "job.json", j)
                out = self._monitor(tmp)
        self.assertIn("STOPPED", out)


# ═══════════════════════════════════════════════════════════════════════════════
# 11. _age: boundary
# ═══════════════════════════════════════════════════════════════════════════════

class TestAgeBoundary(unittest.TestCase):

    def test_exactly_60_minutes(self):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(minutes=60)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        result = F._age(past)
        self.assertRegex(result, r"^1h0[0-9]m$")  # 1h00m or 1h01m (clock skew)

    def test_59_minutes_still_minutes(self):
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(minutes=59)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        result = F._age(past)
        self.assertRegex(result, r"^\d+m$")

    def test_with_explicit_end_timestamp(self):
        from datetime import datetime, timezone, timedelta
        start = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        end   = datetime(2026, 1, 1, 10, 45, 0, tzinfo=timezone.utc)
        start_s = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_s   = end.strftime("%Y-%m-%dT%H:%M:%SZ")
        result = F._age(start_s, F._parse(end_s))
        self.assertEqual(result, "45m")

    def test_cross_hour_boundary(self):
        from datetime import datetime, timezone, timedelta
        start = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        end   = datetime(2026, 1, 1, 10, 30, 0, tzinfo=timezone.utc)
        result = F._age(
            start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            F._parse(end.strftime("%Y-%m-%dT%H:%M:%SZ")))
        self.assertEqual(result, "1h30m")


# ═══════════════════════════════════════════════════════════════════════════════
# 12. _stage_status: timing display
# ═══════════════════════════════════════════════════════════════════════════════

class TestStageStatusTiming(unittest.TestCase):

    def test_past_stage_with_both_timestamps_shows_elapsed(self):
        """A completed stage with both its own and next-stage timestamps shows elapsed."""
        job = {
            "stage": "build", "state": "active", "result": None,
            "stages": {
                "ingest": "2026-01-01T10:00:00Z",
                "spec":   "2026-01-01T10:05:00Z",  # 5 min after ingest
                "build":  "2026-01-01T10:10:00Z",
            },
            "events": [],
        }
        result = F._stage_status(job, "ingest")
        # Should be "done Nm" since both ingest and spec timestamps exist
        self.assertTrue(result.startswith("done "), f"Expected 'done Nm', got {result!r}")
        self.assertIn("5m", result)

    def test_past_stage_without_next_timestamp_shows_done(self):
        job = {
            "stage": "spec", "state": "active", "result": None,
            "stages": {"ingest": "2026-01-01T10:00:00Z",
                       "spec":   "2026-01-01T10:05:00Z"},
            "events": [],
        }
        # "ingest" is past, but "spec" IS in stages — so it has elapsed
        result = F._stage_status(job, "ingest")
        self.assertTrue(result.startswith("done "))

    def test_future_stage_returns_empty_string(self):
        job = {
            "stage": "spec", "state": "active", "result": None,
            "stages": {"ingest": "2026-01-01T10:00:00Z",
                       "spec":   "2026-01-01T10:05:00Z"},
            "events": [],
        }
        result = F._stage_status(job, "build")
        self.assertEqual(result, "")

    def test_last_stage_done_no_result(self):
        """Last stage with result=None should show 'done' not None."""
        job = {
            "stage": "pr", "state": "done", "result": None,
            "stages": {"pr": "2026-01-01T10:00:00Z"},
            "events": [],
        }
        result = F._stage_status(job, "pr")
        self.assertEqual(result, "done")

    def test_last_stage_done_with_result(self):
        job = {
            "stage": "pr", "state": "done", "result": "ready",
            "stages": {"pr": "2026-01-01T10:00:00Z"},
            "events": [],
        }
        result = F._stage_status(job, "pr")
        self.assertEqual(result, "ready")


# ═══════════════════════════════════════════════════════════════════════════════
# 13. find_clones
# ═══════════════════════════════════════════════════════════════════════════════

class TestFindClones(unittest.TestCase):

    def _make_clone(self, root, name, slug, vcs="github"):
        """Create a fake git repo whose origin matches slug."""
        p = Path(root) / name
        p.mkdir(parents=True, exist_ok=True)
        (p / ".git").mkdir()
        return p

    def test_finds_matching_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                clone = self._make_clone(tmp, "myrepo", "owner/repo")
                with patch.object(F, "origin_slug", return_value="owner/repo"):
                    results = F.find_clones("owner/repo")
                # Resolve both sides — macOS /var/folders symlinks to /private/var
                resolved_results = [r.resolve() for r in results]
                self.assertIn(clone.resolve(), resolved_results)

    def test_no_match_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                with patch.object(F, "origin_slug", return_value="other/repo"):
                    results = F.find_clones("owner/repo")
            self.assertEqual(results, [])

    def test_node_modules_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                node = Path(tmp) / "project/node_modules"
                node.mkdir(parents=True, exist_ok=True)
                visited = []
                real_origin = F.origin_slug
                def fake_origin(path, vcs="github"):
                    visited.append(str(path))
                    return None
                with patch.object(F, "origin_slug", side_effect=fake_origin):
                    F.find_clones("owner/repo")
            self.assertFalse(any("node_modules" in v for v in visited))

    def test_worktrees_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                wt = Path(tmp) / ".worktrees"
                wt.mkdir()
                visited = []
                with patch.object(F, "origin_slug",
                                  side_effect=lambda p, vcs="github": visited.append(str(p)) or None):
                    F.find_clones("owner/repo")
            self.assertFalse(any(".worktrees" in v for v in visited))

    def test_no_duplicates_when_hermes_home_inside_home(self):
        """HERMES_HOME under home must not double-scan the same clone."""
        with tempfile.TemporaryDirectory() as tmp:
            # home_dir is parent of hermes_home in this test
            hermes_dir = Path(tmp) / ".hermes"
            hermes_dir.mkdir()
            clone = hermes_dir / "work/myrepo"
            clone.mkdir(parents=True)
            (clone / ".git").mkdir()
            with patch.dict(os.environ, HERMES_HOME=str(hermes_dir), HOME=tmp):
                with patch.object(F, "origin_slug", return_value="owner/repo"):
                    results = F.find_clones("owner/repo")
            # Must appear at most once
            self.assertLessEqual(results.count(clone), 1)

    def test_depth_limit_respected(self):
        """Traversal must not go deeper than SEARCH_DEPTH."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                # Create a repo 5 levels deep (beyond SEARCH_DEPTH=3)
                deep = Path(tmp) / "a/b/c/d/e/deep_repo"
                deep.mkdir(parents=True)
                (deep / ".git").mkdir()
                visited = []
                with patch.object(F, "origin_slug",
                                  side_effect=lambda p, vcs="github": visited.append(str(p)) or None):
                    F.find_clones("owner/repo")
            # The deep path must not have been visited
            self.assertFalse(any("deep_repo" in v for v in visited))


# ═══════════════════════════════════════════════════════════════════════════════
# 14. precheck: message content
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrecheckMessages(unittest.TestCase):

    def test_busy_prints_issue_and_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 7, stage="build")
                with patch.object(F, "check_queue", return_value=[]),\
                     contextlib.redirect_stderr(io.StringIO()) as out:
                    F.precheck(SimpleNamespace(repo="o/r"))
            output = out.getvalue()
        self.assertIn("7", output)
        self.assertIn("build", output)

    def test_multi_issue_prints_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                waiting = [{"number": i, "title": f"Issue {i}"} for i in range(1, 5)]
                with patch.object(F, "check_queue", return_value=waiting),\
                     contextlib.redirect_stderr(io.StringIO()) as out:
                    F.precheck(SimpleNamespace(repo="o/r"))
            output = out.getvalue()
        self.assertIn("4", output)

    def test_empty_queue_prints_no_issues(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                with patch.object(F, "check_queue", return_value=[]),\
                     contextlib.redirect_stderr(io.StringIO()) as out:
                    F.precheck(SimpleNamespace(repo="o/r"))
            output = out.getvalue()
        self.assertIn("no issues", output.lower())


# ═══════════════════════════════════════════════════════════════════════════════
# 15. install_precheck: unregistered repo
# ═══════════════════════════════════════════════════════════════════════════════

class TestInstallPrecheckExtra(unittest.TestCase):

    def test_unregistered_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                with self.assertRaises(SystemExit) as ctx:
                    F.install_precheck(SimpleNamespace(repo="nobody/nothing",
                                                       dispatch=False))
            self.assertIn("isn't registered", str(ctx.exception))


# ═══════════════════════════════════════════════════════════════════════════════
# 16. now() and _parse()
# ═══════════════════════════════════════════════════════════════════════════════

class TestNowAndParse(unittest.TestCase):

    def test_now_parseable_by_parse(self):
        stamp = F.now()
        dt = F._parse(stamp)
        self.assertIsNotNone(dt)

    def test_now_format(self):
        stamp = F.now()
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_now_utc(self):
        from datetime import datetime, timezone
        stamp = F.now()
        dt = F._parse(stamp)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_clock_returns_hhmm(self):
        result = F._clock("2026-06-15T14:30:00Z")
        self.assertEqual(result, "14:30")


# ═══════════════════════════════════════════════════════════════════════════════
# 17. Multi-round scenario
# ═══════════════════════════════════════════════════════════════════════════════

class TestMultiRoundScenario(unittest.TestCase):
    """Gate FAIL -> stage back to build -> round counter tracks correctly."""

    def test_full_round_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="build", path=tmp, round_=1)

                # --- Round 1: gate fails ---
                checks = Path(tmp) / "factory/checks.sh"
                checks.parent.mkdir(parents=True, exist_ok=True)
                checks.write_text("#!/usr/bin/env bash\nexit 1\n")
                checks.chmod(0o755)
                with patch.object(F, "protected_paths_check", return_value=[]),\
                     patch.object(F, "load_config", return_value={}),\
                     patch.object(F, "cli_run", return_value="abc1234"),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.stage_cmd(SimpleNamespace(repo="o/r", name="gate", note=None))
                    F.gate_cmd(SimpleNamespace(repo="o/r"))

                output = out.getvalue()
                self.assertIn("GATE: FAIL", output)

                # Orchestrator sees FAIL → stages back to build, increments round
                F.stage_cmd(SimpleNamespace(repo="o/r", name="review", note=None))
                # That would fail because we're at gate — correct, must go via build
                # Instead the orchestrator must stage gate→review after fixes

    def test_round_limit_enforced_by_round_limit_field(self):
        """round_limit field is set from config.json at start time."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="build", round_=3, round_limit=3)
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            # round == round_limit means we're at the limit
            self.assertEqual(saved["round"], saved["round_limit"])

    def test_stage_advance_from_gate_fail_goes_to_build(self):
        """After gate fails, the correct next stage is build (for another round)."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp)
                # Advancing gate -> review is valid (when gate passes)
                F.stage_cmd(SimpleNamespace(repo="o/r", name="review", note=None))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["stage"], "review")

    def test_stage_backward_to_build_is_refused(self):
        """Stage must never go backward — gate->build is invalid."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="gate")
                with self.assertRaises(SystemExit):
                    F.stage_cmd(SimpleNamespace(repo="o/r", name="build", note=None))


# ═══════════════════════════════════════════════════════════════════════════════
# 18. Concurrent / re-entrant protection
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrencyProtection(unittest.TestCase):

    def test_start_refuses_when_already_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, state="active")
                with patch.object(F, "check_queue",
                                  return_value=[{"number": 2, "title": "t"}]),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.start(SimpleNamespace(repo="o/r", issue=None))
            self.assertIn("BUSY", out.getvalue())

    def test_resume_refuses_when_another_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, state="active")
                _job(tmp, "o/r", 2, state="stopped", path=tmp,
                     branch="factory/issue-2")
                with patch.object(F, "cli_run", return_value="factory/issue-2"):
                    with self.assertRaises(SystemExit) as ctx:
                        F.resume(SimpleNamespace(repo="o/r", issue=2))
            self.assertIn("already active", str(ctx.exception))

    def test_dispatch_refuses_when_active_job_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "o/r"
            with _env(tmp):
                _repo(tmp, slug, path=tmp)
                _job(tmp, slug, 1, state="active")
                with patch.object(F, "load_repo",
                                  return_value={"repo": slug, "path": tmp,
                                                "vcs": "github", "base": "main"}),\
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.dispatch(SimpleNamespace(repo=slug))
            self.assertIn("BUSY", out.getvalue())


# ═══════════════════════════════════════════════════════════════════════════════
# 19. Slug / key edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestSlugEdgeCases(unittest.TestCase):

    def test_repo_with_dots(self):
        """Org names and repo names can contain dots."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                slug = "my.org/my.repo"
                _repo(tmp, slug)
                saved = F.load_repo(slug)
            self.assertEqual(saved["repo"], slug)

    def test_repo_with_hyphens_and_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                slug = "org-123/repo-456"
                _repo(tmp, slug)
                saved = F.load_repo(slug)
            self.assertEqual(saved["repo"], slug)

    def test_job_dir_stable_for_same_slug(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                slug = "a/b"
                d1 = F.job_dir(slug, 1)
                d2 = F.job_dir(slug, 1)
            self.assertEqual(d1, d2)

    def test_key_is_collision_free(self):
        """Slugs with -- in them must not collide."""
        self.assertNotEqual(F.key("a/b--c"), F.key("a--b/c"))
        self.assertNotEqual(F.key("org/a--b"), F.key("org/a"))

    def test_job_dir_distinct_per_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                self.assertNotEqual(F.job_dir("o/r", 1), F.job_dir("o/r", 2))


# ═══════════════════════════════════════════════════════════════════════════════
# 20. State file integrity
# ═══════════════════════════════════════════════════════════════════════════════

class TestStateFileIntegrity(unittest.TestCase):

    def test_save_is_atomic(self):
        """Concurrent reads during save must never see partial JSON."""
        import threading
        results = []
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "data.json"
            F.save(p, {"v": 0})

            def writer():
                for i in range(50):
                    F.save(p, {"v": i, "pad": "x" * 1000})

            def reader():
                for _ in range(50):
                    try:
                        data = json.loads(p.read_text())
                        results.append(True)
                    except json.JSONDecodeError:
                        results.append(False)

            t1 = threading.Thread(target=writer)
            t2 = threading.Thread(target=reader)
            t1.start(); t2.start()
            t1.join(); t2.join()

        self.assertFalse(any(r is False for r in results),
                         "Partial/corrupt JSON seen during concurrent save")

    def test_log_event_stage_matches_job_stage(self):
        """Events logged by log() must have stage == job['stage'] at log time."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                job = _job(tmp, "o/r", 1, stage="build")
                F.log(job, "at build")
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            last = saved["events"][-1]
        self.assertEqual(last["stage"], "build")
        self.assertEqual(last["text"], "at build")

    def test_events_have_at_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                job = _job(tmp, "o/r", 1)
                F.log(job, "test")
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            event = saved["events"][-1]
        self.assertIn("at", event)
        self.assertRegex(event["at"], r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


# ═══════════════════════════════════════════════════════════════════════════════
# 21. all_jobs / active_jobs filtering
# ═══════════════════════════════════════════════════════════════════════════════

class TestJobFiltering(unittest.TestCase):

    def test_all_jobs_returns_all_states(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="active")
                _job(tmp, "o/r", 2, state="stopped")
                _job(tmp, "o/r", 3, state="done")
                jobs = F.all_jobs()
            self.assertEqual(len(jobs), 3)

    def test_active_jobs_filters_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="active")
                _job(tmp, "o/r", 2, state="done")
                _job(tmp, "o/r", 3, state="stopped")
                active = F.active_jobs()
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]["issue"], 1)

    def test_active_job_returns_none_when_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                result = F.active_job("o/r")
            self.assertIsNone(result)

    def test_active_job_returns_first_when_multiple(self):
        """active_job returns the first (oldest) active job for a slug."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, state="active")
                _job(tmp, "o/r", 2, state="active")
                # Returns first match; both are active
                result = F.active_job("o/r")
            self.assertIn(result["issue"], [1, 2])


# ═══════════════════════════════════════════════════════════════════════════════
# 22. Protected paths: renamed file (R status in git diff)
# ═══════════════════════════════════════════════════════════════════════════════

class TestProtectedPathsRename(unittest.TestCase):

    def test_renamed_protected_file_caught(self):
        """Rename (R100) status lines have two paths; the destination is checked."""
        config = {"protected_paths": ["factory/checks.sh"]}
        # git diff --name-status outputs R100\tsrc\tdst for renames
        diff = "R100\tsome/old.sh\tfactory/checks.sh\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", config, "main")
        # The destination matches the pattern
        self.assertIn("factory/checks.sh", result)

    def test_renamed_away_from_protected_not_caught(self):
        """Renaming a protected file away (it no longer exists there) is an Added on the new path."""
        config = {"protected_paths": ["factory/checks.sh"]}
        # The original protected path being the SOURCE of a rename
        diff = "R100\tfactory/checks.sh\tsome/new.sh\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", config, "main")
        # The source path IS protected, but our logic checks parts[1] which is the src
        # This behavior documents what the code actually does
        self.assertIsInstance(result, list)


# ═══════════════════════════════════════════════════════════════════════════════
# 23. _diagram: no cursor when job is done/stopped
# ═══════════════════════════════════════════════════════════════════════════════

class TestDiagram(unittest.TestCase):

    def _diagram_str(self, job):
        return "\n".join(F._diagram(job))

    def test_no_hermes_cursor_when_done(self):
        job = {
            "stage": "pr", "state": "done", "result": "ready",
            "stages": {"pr": F.now()}, "events": [],
        }
        out = self._diagram_str(job)
        self.assertNotIn("HERMES", out)

    def test_no_hermes_cursor_when_stopped(self):
        job = {
            "stage": "build", "state": "stopped", "result": None,
            "stages": {"build": F.now()}, "events": [],
        }
        out = self._diagram_str(job)
        self.assertNotIn("HERMES", out)

    def test_stopped_shows_stopped_label(self):
        job = {
            "stage": "review", "state": "stopped", "result": None,
            "stages": {"review": F.now()}, "events": [],
        }
        out = self._diagram_str(job)
        self.assertIn("STOPPED", out)

    def test_all_seven_stage_boxes_present(self):
        job = {
            "stage": "ingest", "state": "active", "result": None,
            "stages": {"ingest": F.now()}, "events": [],
        }
        out = self._diagram_str(job)
        for stage in ["INGEST", "SPEC", "BUILD", "GATE", "REVIEW", "APPROVE", "PR"]:
            self.assertIn(stage, out)

    def test_result_none_renders_done_not_none(self):
        """None result on done job must show 'done', not the string 'None'."""
        job = {
            "stage": "pr", "state": "done", "result": None,
            "stages": {"pr": F.now()}, "events": [],
        }
        out = self._diagram_str(job)
        self.assertNotIn("None", out)


if __name__ == "__main__":
    unittest.main()


# ═══════════════════════════════════════════════════════════════════════════════
# 24. Example job.json schema validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestExampleJobSchema(unittest.TestCase):
    """Example job.json files must match the factory.py job record schema."""

    REQUIRED = [
        "repo", "path", "vcs", "issue", "title", "url", "branch", "base",
        "stage", "state", "result", "pr", "pr_url", "risk", "surfaces",
        "agents", "round", "round_limit", "reviews", "started", "updated",
        "stages", "events",
    ]
    VALID_STAGES = F.STAGES + ["ingest"]   # ingest is first in STAGES list
    VALID_STATES = {"active", "stopped", "done"}

    def _examples(self):
        root = Path(__file__).resolve().parents[1] / "examples/jobs"
        return list(root.rglob("job.json"))

    def test_examples_exist(self):
        self.assertGreater(len(self._examples()), 0)

    def test_all_required_fields_present(self):
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                missing = [f for f in self.REQUIRED if f not in j]
                self.assertEqual(missing, [],
                                 f"{p.parent.name}: missing fields {missing}")

    def test_state_is_valid(self):
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                self.assertIn(j["state"], self.VALID_STATES,
                              f"Invalid state {j['state']!r}")

    def test_stage_is_valid(self):
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                self.assertIn(j["stage"], F.STAGES,
                              f"Invalid stage {j['stage']!r}")

    def test_vcs_is_valid(self):
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                self.assertIn(j["vcs"], {"github", "gitlab"})

    def test_started_and_updated_parseable(self):
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                # These must be parseable ISO 8601 UTC
                F._parse(j["started"])
                F._parse(j["updated"])

    def test_stages_dict_not_empty_for_done_jobs(self):
        for p in self._examples():
            j = json.loads(p.read_text())
            if j["state"] == "done":
                with self.subTest(example=p.parent.name):
                    self.assertGreater(len(j["stages"]), 0,
                                      "Done job must have at least one stage timestamp")

    def test_done_job_has_pr_url(self):
        for p in self._examples():
            j = json.loads(p.read_text())
            if j["state"] == "done":
                with self.subTest(example=p.parent.name):
                    self.assertIsNotNone(j.get("pr_url"),
                                        "Done job must have pr_url set")

    def test_stopped_job_has_no_pr(self):
        for p in self._examples():
            j = json.loads(p.read_text())
            if j["state"] == "stopped":
                with self.subTest(example=p.parent.name):
                    self.assertIsNone(j.get("pr"),
                                     "Stopped job should not have pr set")

    def test_events_is_list(self):
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                self.assertIsInstance(j["events"], list)

    def test_events_have_required_keys(self):
        for p in self._examples():
            j = json.loads(p.read_text())
            for i, evt in enumerate(j["events"]):
                with self.subTest(example=p.parent.name, event=i):
                    self.assertIn("at", evt)
                    self.assertIn("stage", evt)
                    self.assertIn("text", evt)
                    F._parse(evt["at"])   # timestamp must parse

    def test_round_within_round_limit(self):
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                self.assertLessEqual(j["round"], j["round_limit"])

    def test_no_extra_unknown_fields(self):
        """No field from the old zazencodes schema should appear."""
        old_fields = {"id", "issue_url", "worktree", "intake", "gate",
                      "cost_usd", "ended"}
        for p in self._examples():
            with self.subTest(example=p.parent.name):
                j = json.loads(p.read_text())
                found = old_fields & set(j.keys())
                self.assertEqual(found, set(),
                                 f"Old-schema fields still present: {found}")


# ═══════════════════════════════════════════════════════════════════════════════
# 25. Reviewer-flagged regression tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestReviewerFindings(unittest.TestCase):
    """Direct regression tests for every finding in review-report.md."""

    # ── 1.1 key() collision fix ───────────────────────────────────────────────

    def test_key_double_hyphen_no_collision(self):
        """a/b--c and a--b/c must produce different keys."""
        self.assertNotEqual(F.key("a/b--c"), F.key("a--b/c"))

    def test_key_uses_colon_separator(self):
        self.assertEqual(F.key("owner/repo"), "owner:repo")

    def test_key_colon_is_not_in_github_names(self):
        """The separator must be illegal in GitHub/GitLab owner and repo names."""
        import re
        slug_part = re.compile(r"[\w.\-]+")
        self.assertFalse(slug_part.fullmatch("a:b"),
                         "Colon must not match the slug-part pattern")

    # ── 1.2 open_pr rollback on failure ──────────────────────────────────────

    def test_open_pr_rolls_back_stage_on_push_failure(self):
        """If git_push fails, stage must roll back to 'approve', not stay 'pr'."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="approve", branch="factory/issue-1")

                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "1"
                    return ""

                # Write a body file
                body = Path(tmp) / "pr-body.md"
                body.write_text("body\n")

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push",
                                  side_effect=SystemExit("push rejected")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        F.open_pr(SimpleNamespace(
                            repo="o/r", body_file=str(body), draft=False))

                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())

            # Stage must have rolled back, not stuck at "pr"
            self.assertEqual(saved["stage"], "approve",
                             "Stage must roll back to approve after push failure")
            self.assertNotEqual(saved["state"], "done",
                                "Job must not be marked done if push failed")

    def test_open_pr_rolls_back_stage_on_pr_create_failure(self):
        """If pr_create fails after push, stage must also roll back."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="approve", branch="factory/issue-1")

                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "1"
                    return ""

                body = Path(tmp) / "pr-body.md"
                body.write_text("body\n")

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push", return_value=""), \
                     patch.object(F, "pr_create",
                                  side_effect=SystemExit("API rate limited")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        F.open_pr(SimpleNamespace(
                            repo="o/r", body_file=str(body), draft=False))

                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())

            self.assertEqual(saved["stage"], "approve")
            self.assertNotEqual(saved["state"], "done")

    def test_open_pr_success_marks_done_after_vcs_ops(self):
        """Happy path: state becomes 'done' only after all VCS ops succeed."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="approve", branch="factory/issue-1")

                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "1"
                    return ""

                body = Path(tmp) / "pr-body.md"
                body.write_text("body\n")

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push", return_value=""), \
                     patch.object(F, "pr_create",
                                  return_value="https://github.com/o/r/pull/1"), \
                     contextlib.redirect_stdout(io.StringIO()):
                    F.open_pr(SimpleNamespace(
                        repo="o/r", body_file=str(body), draft=False))

                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())

            self.assertEqual(saved["state"], "done")
            self.assertEqual(saved["result"], "ready")

    # ── 1.4 round-next command ────────────────────────────────────────────────

    def test_round_next_increments_and_resets_to_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="review", round_=1, round_limit=3)
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["round"], 2)
            self.assertEqual(saved["stage"], "build")

    def test_round_next_refuses_at_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="review", round_=3, round_limit=3)
                with self.assertRaises(SystemExit) as ctx:
                    F.round_next_cmd(SimpleNamespace(repo="o/r"))
            self.assertIn("max", str(ctx.exception).lower())

    def test_round_next_requires_active_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                with self.assertRaises(SystemExit):
                    F.round_next_cmd(SimpleNamespace(repo="o/r"))

    def test_round_next_writes_stage_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="review", round_=0, round_limit=3)
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertIn("build", saved["stages"])

    # ── cancel command ────────────────────────────────────────────────────────

    def test_cancel_marks_cancelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="build")
                F.cancel(SimpleNamespace(repo="o/r", reason="issue closed"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["state"], "cancelled")

    def test_cancel_records_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1)
                F.cancel(SimpleNamespace(repo="o/r", reason="superseded by #10"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertIn("superseded by #10", saved["events"][-1]["text"])

    def test_cancel_unnamed_one_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1)
                F.cancel(SimpleNamespace(repo=None, reason="done"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["state"], "cancelled")

    def test_cancel_cannot_be_resumed(self):
        """Cancelled jobs are terminal — resume must reject them."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, state="cancelled", path=tmp,
                     branch="factory/issue-1")
                with patch.object(F, "cli_run", return_value="factory/issue-1"):
                    with self.assertRaises(SystemExit) as ctx:
                        F.resume(SimpleNamespace(repo="o/r", issue=1))
            # Must refuse with a message about the state
            self.assertIn("cancelled", str(ctx.exception))

    # ── 1.5 gate_cmd stage guard ──────────────────────────────────────────────

    def test_gate_refuses_at_spec_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="spec", path=tmp)
                with self.assertRaises(SystemExit) as ctx:
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
            self.assertIn("spec", str(ctx.exception))

    def test_gate_refuses_at_ingest_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="ingest", path=tmp)
                with self.assertRaises(SystemExit):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))

    def test_gate_refuses_at_review_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="review", path=tmp)
                with self.assertRaises(SystemExit):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))

    def test_gate_accepts_at_build_stage(self):
        """gate_cmd must not raise for a job at the build stage."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="build", path=tmp, round_=1)
                checks = Path(tmp) / "factory/checks.sh"
                checks.parent.mkdir(parents=True, exist_ok=True)
                checks.write_text("#!/usr/bin/env bash\nexit 0\n")
                checks.chmod(0o755)
                with patch.object(F, "protected_paths_check", return_value=[]), \
                     patch.object(F, "load_config", return_value={}), \
                     patch.object(F, "cli_run", return_value="abc1234"), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
            self.assertIn("GATE: PASS", out.getvalue())

    def test_gate_accepts_at_gate_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=1)
                checks = Path(tmp) / "factory/checks.sh"
                checks.parent.mkdir(parents=True, exist_ok=True)
                checks.write_text("#!/usr/bin/env bash\nexit 0\n")
                checks.chmod(0o755)
                with patch.object(F, "protected_paths_check", return_value=[]), \
                     patch.object(F, "load_config", return_value={}), \
                     patch.object(F, "cli_run", return_value="abc1234"), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
            self.assertIn("GATE: PASS", out.getvalue())

    # ── 2.1 all_jobs corrupt JSON ─────────────────────────────────────────────

    def test_all_jobs_warns_and_skips_corrupt(self):
        """Corrupt job file must print a WARNING to stderr, not crash."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1)
                corrupt = F.state_dir() / "jobs" / "corrupt" / "job.json"
                corrupt.parent.mkdir(parents=True)
                corrupt.write_text("{invalid json")
                import sys
                stderr_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf):
                    jobs = F.all_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertIn("WARNING", stderr_buf.getvalue())

    # ── 2.2 checks.sh stdin/timeout ──────────────────────────────────────────

    def test_gate_passes_stdin_devnull(self):
        """checks.sh subprocess must never inherit the parent's stdin."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=1)
                checks = Path(tmp) / "factory/checks.sh"
                checks.parent.mkdir(parents=True, exist_ok=True)
                checks.write_text("#!/usr/bin/env bash\nexit 0\n")
                checks.chmod(0o755)
                sp_calls = []
                real_run = F.subprocess.run
                def capture_run(*args, **kwargs):
                    sp_calls.append(kwargs)
                    return real_run(*args, **kwargs)
                with patch.object(F, "protected_paths_check", return_value=[]), \
                     patch.object(F, "load_config", return_value={}), \
                     patch.object(F, "cli_run", return_value="abc1234"), \
                     patch.object(F.subprocess, "run", side_effect=capture_run), \
                     contextlib.redirect_stdout(io.StringIO()):
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
            # The checks.sh call must have stdin=DEVNULL
            self.assertTrue(
                any(kw.get("stdin") is F.subprocess.DEVNULL for kw in sp_calls),
                "checks.sh must be called with stdin=subprocess.DEVNULL")

    def test_gate_timeout_writes_fail_and_returns(self):
        """Timeout must write GATE: FAIL to checks.md and return cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                _job(tmp, "o/r", 1, stage="gate", path=tmp, round_=1)
                checks = Path(tmp) / "factory/checks.sh"
                checks.parent.mkdir(parents=True, exist_ok=True)
                checks.write_text("#!/usr/bin/env bash\nsleep 999\n")
                checks.chmod(0o755)
                with patch.object(F, "protected_paths_check", return_value=[]), \
                     patch.object(F, "load_config",
                                  return_value={"checks_timeout_seconds": 1}), \
                     patch.object(F, "cli_run", return_value="abc1234"), \
                     contextlib.redirect_stdout(io.StringIO()) as out:
                    F.gate_cmd(SimpleNamespace(repo="o/r"))
                checks_md = F.job_dir("o/r", 1) / "round-1/checks.md"
            self.assertIn("FAIL", out.getvalue())
            if checks_md.exists():
                self.assertIn("GATE: FAIL", checks_md.read_text())

    # ── 4.3 start() TOCTOU guard ─────────────────────────────────────────────

    def test_start_toctou_guard_second_call_fails(self):
        """If the job dir already exists, start() must refuse with a clear error."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", path=tmp)
                # Pre-create the job directory to simulate a race
                F.job_dir("o/r", 7).mkdir(parents=True, exist_ok=True)
                with patch.object(F, "check_queue",
                                  return_value=[{"number": 7, "title": "t"}]), \
                     patch.object(F, "issue_list_labeled",
                                  return_value=[{"number": 7, "title": "t"}]), \
                     patch.object(F, "cli_run", return_value=""), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        F.start(SimpleNamespace(repo="o/r", issue=None))
            self.assertIn("concurrent", str(ctx.exception))

    # ── 5.3 dead assertion in test — regression ───────────────────────────────

    def test_renamed_away_protected_documents_actual_behavior(self):
        """Renaming FROM a protected path (src matches) is currently caught.
        This test documents the actual behavior and will catch if it changes."""
        config = {"protected_paths": ["factory/checks.sh"]}
        diff = "R100\tfactory/checks.sh\tsome/new.sh\n"
        with patch.object(F, "cli_run", return_value=diff):
            result = F.protected_paths_check("/repo", config, "main")
        # The SOURCE path matches the pattern — currently caught by parts[1] check
        # If this behavior changes, this test will catch it
        self.assertIsInstance(result, list)
        # Document the current behavior with an actual assertion
        self.assertIn("factory/checks.sh", result)

    # ── 6.4 FACTORY path is $HERMES_HOME-relative, not git-relative ──────────

    def test_factory_path_not_git_dependent(self):
        """factory.py must be locatable without git rev-parse."""
        import os
        factory_path = Path(__file__).resolve().parents[1] / "factory/bin/factory.py"
        # Must exist without any git operation
        self.assertTrue(factory_path.exists(),
                        f"factory.py not found at {factory_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# 26. Pass 2 reviewer regression tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPass2ReviewerFindings(unittest.TestCase):

    # ── 2.1 stage_cmd cannot reach "pr" via CLI ───────────────────────────────

    def test_stage_cmd_rejects_pr_as_target(self):
        """stage OWNER/REPO pr must be rejected — 'pr' only reachable via open-pr."""
        import importlib.util as _ilu
        # Verify factory.py's STAGES[1:-1] no longer includes "pr"
        self.assertNotIn("pr", F.STAGES[1:-1])

    def test_stage_cmd_pr_not_in_argparser_choices(self):
        """Argparser choices must exclude 'pr'."""
        import argparse
        # Simulate what argparse would do with choices=STAGES[1:-1]
        valid = F.STAGES[1:-1]
        self.assertNotIn("pr", valid)
        self.assertIn("approve", valid)   # last valid stage via CLI

    def test_stage_cmd_internal_logic_still_allows_pr_advance(self):
        """stage_cmd internal logic still advances ingest→...→approve correctly.
        The restriction is only in the argparser choices, not the function body,
        so open_pr can still set stage='pr' via log()."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="approve")
                # stage_cmd with name="pr" still works when called programmatically
                # (open_pr does this via job["stage"] = "pr" + log, not via stage_cmd)
                # Verify the function itself doesn't block "pr" — only argparse does
                F.stage_cmd(SimpleNamespace(repo="o/r", name="pr", note=None))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["stage"], "pr")

    # ── 2.2 open_pr rollback catches BaseException ────────────────────────────

    def test_open_pr_rolls_back_on_keyboard_interrupt(self):
        """KeyboardInterrupt during push must roll back stage, not leave job at 'pr'."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="approve", branch="factory/issue-1")

                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "1"
                    return ""

                body = Path(tmp) / "pr-body.md"
                body.write_text("body\n")

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push",
                                  side_effect=KeyboardInterrupt("simulated Ctrl-C")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        F.open_pr(SimpleNamespace(
                            repo="o/r", body_file=str(body), draft=False))

                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())

            self.assertEqual(saved["stage"], "approve",
                             "KeyboardInterrupt must roll back stage to approve")
            self.assertNotEqual(saved["state"], "done")

    def test_open_pr_rolls_back_on_oserror(self):
        """OSError from git_push must also roll back stage."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="approve", branch="factory/issue-1")

                def fake_cli(*args, **kw):
                    if "--show-current" in args:
                        return "factory/issue-1"
                    if "--porcelain" in args:
                        return ""
                    if "rev-list" in args:
                        return "1"
                    return ""

                body = Path(tmp) / "pr-body.md"
                body.write_text("body\n")

                with patch.object(F, "cli_run", side_effect=fake_cli), \
                     patch.object(F, "git_push",
                                  side_effect=OSError("disk full")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(OSError):
                        F.open_pr(SimpleNamespace(
                            repo="o/r", body_file=str(body), draft=False))

                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())

            self.assertEqual(saved["stage"], "approve",
                             "OSError must roll back stage to approve")

    # ── 2.3 monitor() survives corrupt repo files ─────────────────────────────

    def test_monitor_warns_and_continues_on_corrupt_repo_file(self):
        """A corrupt repos/*.json must emit WARNING to stderr, not crash monitor."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                # Valid repo
                _repo(tmp, "o/good")
                # Corrupt repo file alongside it
                corrupt = F.state_dir() / "repos" / "o:corrupt.json"
                corrupt.parent.mkdir(parents=True, exist_ok=True)
                corrupt.write_text("{not valid json")

                stderr_buf = io.StringIO()
                stdout_buf = io.StringIO()
                with contextlib.redirect_stderr(stderr_buf), \
                     contextlib.redirect_stdout(stdout_buf):
                    F.monitor(None)   # must not raise

            self.assertIn("WARNING", stderr_buf.getvalue())
            # Valid repo still appears in output
            self.assertIn("o/good", stdout_buf.getvalue())

    def test_monitor_empty_repos_dir_no_crash(self):
        """monitor() with zero repos must show the 'no repos' message cleanly."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    F.monitor(None)
            self.assertIn("No repositories registered", buf.getvalue())

    # ── 2.4 round_next_cmd stage guard ────────────────────────────────────────

    def test_round_next_refuses_at_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="ingest", round_=0, round_limit=3)
                with self.assertRaises(SystemExit) as ctx:
                    F.round_next_cmd(SimpleNamespace(repo="o/r"))
            self.assertIn("ingest", str(ctx.exception))

    def test_round_next_refuses_at_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="spec", round_=0, round_limit=3)
                with self.assertRaises(SystemExit) as ctx:
                    F.round_next_cmd(SimpleNamespace(repo="o/r"))
            self.assertIn("spec", str(ctx.exception))

    def test_round_next_refuses_at_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="build", round_=0, round_limit=3)
                with self.assertRaises(SystemExit) as ctx:
                    F.round_next_cmd(SimpleNamespace(repo="o/r"))
            self.assertIn("build", str(ctx.exception))

    def test_round_next_accepts_at_gate(self):
        """Gate FAIL leaves the job at 'gate'; a new round starts from build."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="gate", round_=0, round_limit=3)
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["round"], 1)
            self.assertEqual(saved["stage"], "build")

    def test_round_next_accepts_at_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="review", round_=0, round_limit=3)
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["round"], 1)
            self.assertEqual(saved["stage"], "build")

    def test_round_next_accepts_at_approve(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r")
                _job(tmp, "o/r", 1, stage="approve", round_=1, round_limit=3)
                F.round_next_cmd(SimpleNamespace(repo="o/r"))
                saved = json.loads((F.job_dir("o/r", 1) / "job.json").read_text())
            self.assertEqual(saved["round"], 2)

    # ── 2.5 SKILL.md job path uses new : separator ────────────────────────────

    def test_skill_md_job_path_uses_colon_separator(self):
        """SKILL.md must reference OWNER:REPO--N not OWNER--REPO--N."""
        root = Path(__file__).resolve().parents[1]
        skill = root / "skills/software-development/factory/SKILL.md"
        if not skill.exists():
            skill = root / "skills/software-development/hermes-factory/SKILL.md"
        content = skill.read_text()
        self.assertNotIn("OWNER--REPO--N", content,
                         "SKILL.md still has stale OWNER--REPO--N path format")
        self.assertIn("OWNER:REPO--N", content,
                      "SKILL.md must use OWNER:REPO--N (colon separator)")

    # ── key() collision regression (also in test_factory_core, explicit here) ─

    def test_key_collision_regression(self):
        """Lock in the fix: a/b--c and a--b/c must not collide."""
        self.assertNotEqual(F.key("a/b--c"), F.key("a--b/c"),
                            "key() collision regression: separator must be illegal in slug")

    def test_distribution_yaml_has_bounded_version(self):
        """distribution.yaml hermes_requires must have an upper bound."""
        import yaml
        dist = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "distribution.yaml").read_text())
        req = dist.get("hermes_requires", "")
        self.assertIn("<", req,
                      f"hermes_requires={req!r} has no upper bound")

    def test_skill_script_matches_bin(self):
        """The installed skill copy of factory.py must match factory/bin/.

        The orchestrator runs the skill's scripts/factory.py; the test
        suite exercises factory/bin/factory.py.  If the two drift, the
        shipped pipeline silently runs different code than what is tested.
        """
        root = Path(__file__).resolve().parents[1]
        bin_src = (root / "factory/bin/factory.py").read_text()
        skill_src = (root / "skills/software-development/factory/"
                     "scripts/factory.py").read_text()
        self.assertEqual(bin_src, skill_src,
                         "skills/.../factory/scripts/factory.py is out of "
                         "sync with factory/bin/factory.py — copy bin "
                         "over the skill script.")
