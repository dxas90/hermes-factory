"""
Tests for feedback rounds on an open PR/MR.

  reopen   : only a done job with a PR/MR, its branch checked out, no other
             active job; becomes active at build with a fresh round budget
  open-pr  : on a reopened job, pushes the branch (force-with-lease) and keeps
             the same PR/MR instead of creating a new one; refuses to
             overwrite commits someone else pushed meanwhile
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_FACTORY_PATH = Path(__file__).resolve().parents[1] / "factory/bin/factory.py"
_spec = importlib.util.spec_from_file_location("factory", _FACTORY_PATH)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)

SLUG = "o/r"
PR = "https://forge.example/o/r/pulls/8"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True, env={**os.environ, **GIT_ENV}).stdout.strip()


class ReopenTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        _git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        _git(root, "clone", "-q", str(self.origin), str(self.clone))
        _git(self.clone, "checkout", "-q", "-b", "main")
        (self.clone / "factory").mkdir()
        (self.clone / "factory/config.json").write_text('{"max_rounds": 3}')
        (self.clone / "a.txt").write_text("base\n")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "init")
        _git(self.clone, "push", "-q", "origin", "main")
        _git(self.clone, "switch", "-q", "-c", "factory/issue-5")
        (self.clone / "docs.md").write_text("v1\n")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "docs v1")
        _git(self.clone, "push", "-q", "--set-upstream", "origin", "factory/issue-5")
        self.env = patch.dict(os.environ, HERMES_HOME=str(root / "home"),
                              FACTORY_HOME=str(root / "home"))
        self.env.start()
        F.save(F.repo_file(SLUG), {"repo": SLUG, "path": str(self.clone), "vcs": "gitea",
                                   "base": "main", "queue": []})
        self.job = {"repo": SLUG, "path": str(self.clone), "vcs": "gitea", "issue": 5,
                    "title": "docs", "url": "u", "branch": "factory/issue-5", "base": "main",
                    "stage": "pr", "state": "done", "result": "ready", "pr": PR, "pr_url": PR,
                    "risk": "low", "surfaces": [], "agents": {}, "round": 1, "round_limit": 3,
                    "reviews": {}, "started": F.now(), "updated": F.now(),
                    "stages": {"pr": F.now()}, "events": []}
        self._save()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _save(self, **changes):
        self.job.update(changes)
        F.save(F.job_dir(SLUG, 5) / "job.json", self.job)

    def _load(self):
        return json.loads((F.job_dir(SLUG, 5) / "job.json").read_text())

    def _run(self, func, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            func(SimpleNamespace(repo=SLUG, **kw))
        return out.getvalue()

    # ── reopen ──────────────────────────────────────────────────────────────

    def test_reopen_done_job(self):
        out = self._run(F.reopen, issue=5, reason="new comment on the PR")
        job = self._load()
        self.assertEqual((job["state"], job["stage"], job["round"]), ("active", "build", 2))
        self.assertEqual(job["round_limit"], 4)  # 3 fresh rounds: 2, 3, 4
        self.assertEqual(job["pr_url"], PR)
        self.assertIn("REOPENED #5", out)
        self.assertIn("new comment on the PR", job["events"][-1]["text"])

    def test_reopen_refuses_without_pr(self):
        self._save(pr=None, pr_url=None)
        with self.assertRaises(SystemExit) as ctx:
            self._run(F.reopen, issue=5, reason="x")
        self.assertIn("no PR/MR", str(ctx.exception))

    def test_reopen_refuses_stopped_or_active(self):
        for state in ("stopped", "active", "cancelled"):
            self._save(state=state)
            with self.assertRaises(SystemExit):
                self._run(F.reopen, issue=5, reason="x")

    def test_reopen_refuses_other_branch(self):
        _git(self.clone, "switch", "-q", "main")
        with self.assertRaises(SystemExit) as ctx:
            self._run(F.reopen, issue=5, reason="x")
        self.assertIn("Check out factory/issue-5", str(ctx.exception))

    def test_reopen_refuses_when_another_job_is_active(self):
        other = dict(self.job, issue=6, state="active", stage="build", branch="factory/issue-6")
        F.save(F.job_dir(SLUG, 6) / "job.json", other)
        with self.assertRaises(SystemExit) as ctx:
            self._run(F.reopen, issue=5, reason="x")
        self.assertIn("already active", str(ctx.exception))

    # ── open-pr on a reopened job ──────────────────────────────────────────

    def _feedback_round(self):
        """Reopen, move main forward, rebase, commit the feedback, reach approve."""
        self._run(F.reopen, issue=5, reason="feedback")
        _git(self.clone, "switch", "-q", "main")
        (self.clone / "a.txt").write_text("base moved\n")
        _git(self.clone, "commit", "-qam", "main moved")
        _git(self.clone, "push", "-q", "origin", "main")
        _git(self.clone, "switch", "-q", "factory/issue-5")
        _git(self.clone, "rebase", "-q", "origin/main")
        (self.clone / "docs.md").write_text("v2 with feedback\n")
        _git(self.clone, "commit", "-qam", "docs v2")
        job = self._load()
        job["stage"] = "approve"
        F.save(F.job_dir(SLUG, 5) / "job.json", job)
        body = F.job_dir(SLUG, 5) / "pr-body.md"
        body.write_text("summary")
        return body

    def test_open_pr_updates_existing_pr(self):
        body = self._feedback_round()
        with patch.object(F, "pr_create", side_effect=AssertionError("must not create a PR")):
            out = self._run(F.open_pr, body_file=str(body), draft=False)
        job = self._load()
        self.assertIn(f"Updated PR/MR for #5: {PR}", out)
        self.assertEqual((job["state"], job["pr_url"], job["result"]), ("done", PR, "ready"))
        remote = _git(self.origin, "log", "--format=%s", "factory/issue-5")
        self.assertEqual(remote.splitlines()[:3], ["docs v2", "docs v1", "main moved"])

    def test_open_pr_refuses_to_overwrite_foreign_push(self):
        body = self._feedback_round()
        other = Path(self.tmp.name) / "other"
        _git(Path(self.tmp.name), "clone", "-q", "-b", "factory/issue-5", str(self.origin), str(other))
        (other / "review.md").write_text("pushed by a reviewer\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-qm", "reviewer commit")
        _git(other, "push", "-q", "origin", "factory/issue-5")
        with patch.object(F, "pr_create", side_effect=AssertionError("must not create a PR")):
            with self.assertRaises(SystemExit):
                self._run(F.open_pr, body_file=str(body), draft=False)
        job = self._load()
        self.assertEqual((job["state"], job["stage"]), ("active", "approve"))
        self.assertIn("reviewer commit", _git(self.origin, "log", "--format=%s", "factory/issue-5"))

    def test_first_open_still_creates_pr(self):
        self._save(pr=None, pr_url=None, state="active", stage="approve")
        body = F.job_dir(SLUG, 5) / "pr-body.md"
        body.write_text("summary")
        with patch.object(F, "pr_create", return_value=PR) as create:
            out = self._run(F.open_pr, body_file=str(body), draft=False)
        create.assert_called_once()
        self.assertIn(f"PR/MR for #5: {PR}", out)


if __name__ == "__main__":
    unittest.main()
