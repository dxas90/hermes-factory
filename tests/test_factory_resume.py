"""Tests for resume — adapted from zazencodes/zazencodes-season-3."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location(
    "factory",
    Path(__file__).resolve().parents[1] / "factory/bin/factory.py")
factory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(factory)


class ResumeTests(unittest.TestCase):

    def test_resume_preserves_work_and_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            job  = {
                "repo": slug, "path": "/clone", "vcs": "github",
                "issue": 7, "branch": "factory/issue-7",
                "state": "stopped", "stage": "gate", "events": [], "updated": "old",
            }
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": "/clone"})
                factory.save(factory.job_dir(slug, 7) / "job.json", job)
                with patch.object(factory, "cli_run",
                                  return_value="factory/issue-7") as mock_run:
                    factory.resume(SimpleNamespace(repo=slug, issue=7))
                saved = json.loads((factory.job_dir(slug, 7) / "job.json").read_text())
            self.assertEqual(saved["state"], "active")
            self.assertEqual(saved["stage"], "gate")
            mock_run.assert_called_once_with(
                "git", "branch", "--show-current", cwd="/clone")

    def test_resume_rejects_wrong_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": "/clone"})
                factory.save(factory.job_dir(slug, 7) / "job.json", {
                    "repo": slug, "path": "/clone", "vcs": "github",
                    "issue": 7, "branch": "factory/issue-7",
                    "state": "stopped", "stage": "gate", "events": [],
                })
                with patch.object(factory, "cli_run", return_value="main"):
                    with self.assertRaises(SystemExit):
                        factory.resume(SimpleNamespace(repo=slug, issue=7))

    def test_resume_rejects_non_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": "/clone"})
                factory.save(factory.job_dir(slug, 7) / "job.json", {
                    "repo": slug, "path": "/clone", "vcs": "github",
                    "issue": 7, "branch": "factory/issue-7",
                    "state": "done", "stage": "pr", "events": [],
                })
                with self.assertRaises(SystemExit) as ctx:
                    factory.resume(SimpleNamespace(repo=slug, issue=7))
                self.assertIn("done", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
