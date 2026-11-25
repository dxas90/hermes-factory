"""
Tests for start(): claiming an issue must not leave debris behind.

  - A start() that fails after claiming (issue fetch, git fetch, git switch)
    releases the claim, so the next start() can claim the issue again.
  - A claim folder without job.json older than two minutes is debris and is
    reclaimed; a fresh one is still reported as a concurrent claim.
  - A leftover factory/issue-N branch is refused before claiming, with a
    message that says how to fix it.
"""

import contextlib
import importlib.util
import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_FACTORY_PATH = Path(__file__).resolve().parents[1] / "factory/bin/factory.py"
_spec = importlib.util.spec_from_file_location("factory", _FACTORY_PATH)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)

SLUG = "o/r"
ISSUE = {"number": 1, "title": "Add sub", "body": "", "url": "u", "author": {"login": "a"}}


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class StartTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.clone = root / "clone"
        origin = root / "origin.git"
        _git(root, "init", "-q", "--bare", "-b", "main", str(origin))
        _git(root, "clone", "-q", str(origin), str(self.clone))
        _git(self.clone, "checkout", "-q", "-b", "main")
        (self.clone / "a.txt").write_text("x\n")
        _git(self.clone, "add", "-A")
        _git(self.clone, "commit", "-qm", "init")
        _git(self.clone, "push", "-q", "origin", "main")
        self.env = patch.dict(os.environ, HERMES_HOME=str(self.home), FACTORY_HOME=str(self.home))
        self.env.start()
        F.save(F.repo_file(SLUG), {"repo": SLUG, "path": str(self.clone), "vcs": "github",
                                   "base": "main", "queue": []})
        self.vcs = [patch.object(F, "issue_list_labeled", return_value=[{"number": 1, "title": "Add sub"}]),
                    patch.object(F, "issue_view_full", return_value=ISSUE)]
        for p in self.vcs:
            p.start()

    def tearDown(self):
        for p in self.vcs:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def _start(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            F.start(SimpleNamespace(repo=SLUG))
        return out.getvalue()

    def _claim(self):
        return F.job_dir(SLUG, 1)

    def test_clean_start(self):
        self.assertIn("STARTED issue #1", self._start())
        self.assertTrue((self._claim() / "job.json").exists())

    def test_failure_after_claim_releases_it(self):
        with patch.object(F, "issue_view_full", side_effect=SystemExit("network down")):
            with self.assertRaises(SystemExit):
                self._start()
        self.assertFalse(self._claim().exists(), "claim folder must be released")
        self.assertIn("STARTED issue #1", self._start())

    def test_leftover_branch_refused_before_claiming(self):
        _git(self.clone, "branch", "factory/issue-1")
        with self.assertRaises(SystemExit) as ctx:
            self._start()
        self.assertIn("already exists", str(ctx.exception))
        self.assertIn("git branch -D factory/issue-1", str(ctx.exception))
        self.assertFalse(self._claim().exists(), "nothing may be claimed")

    def test_stale_claim_is_reclaimed(self):
        claim = self._claim()
        claim.mkdir(parents=True)
        old = time.time() - 600
        os.utime(claim, (old, old))
        self.assertIn("STARTED issue #1", self._start())
        self.assertTrue((claim / "job.json").exists())

    def test_fresh_claim_is_still_a_concurrent_claim(self):
        self._claim().mkdir(parents=True)
        with self.assertRaises(SystemExit) as ctx:
            self._start()
        self.assertIn("claimed by a concurrent process", str(ctx.exception))

    def test_claim_with_job_json_is_never_reclaimed(self):
        claim = self._claim()
        claim.mkdir(parents=True)
        (claim / "job.json").write_text('{"repo": "x/y", "issue": 1, "state": "done"}')
        old = time.time() - 600
        os.utime(claim, (old, old))
        with self.assertRaises(SystemExit) as ctx:
            self._start()
        self.assertIn("claimed by a concurrent process", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
