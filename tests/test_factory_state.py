"""Tests for core state machine operations in factory.py."""
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


def make_job(tmp, slug, number, stage="ingest", state="active"):
    job = {
        "repo": slug, "path": tmp, "vcs": "github", "issue": number,
        "title": f"Issue {number}", "url": f"https://example.com/{number}",
        "branch": f"factory/issue-{number}", "base": "main",
        "stage": stage, "state": state, "result": None,
        "pr": None, "pr_url": None,
        "risk": None, "surfaces": [], "agents": {},
        "round": 0, "round_limit": 3, "reviews": {},
        "started": factory.now(), "updated": factory.now(),
        "stages": {stage: factory.now()}, "events": [],
    }
    factory.save(factory.job_dir(slug, number) / "job.json", job)
    return job


class StageAdvanceTests(unittest.TestCase):

    def test_stage_advances_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": tmp})
                make_job(tmp, slug, 1, stage="ingest")
                factory.stage_cmd(SimpleNamespace(repo=slug, name="spec", note=None))
                saved = json.loads((factory.job_dir(slug, 1) / "job.json").read_text())
            self.assertEqual(saved["stage"], "spec")

    def test_stage_refuses_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": tmp})
                make_job(tmp, slug, 1, stage="ingest")
                with self.assertRaises(SystemExit):
                    factory.stage_cmd(SimpleNamespace(repo=slug, name="build", note=None))


class RouteTests(unittest.TestCase):

    def test_route_sets_reviewers(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            config = {
                "routing": {"low": {"code": "haiku"}},
                "surface_reviewers": {"security": {"security": "opus"}},
            }
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": tmp})
                make_job(tmp, slug, 1, stage="build")
                with patch.object(factory, "load_config", return_value=config):
                    factory.route_cmd(SimpleNamespace(
                        repo=slug, risk="low", surfaces="security"))
                saved = json.loads((factory.job_dir(slug, 1) / "job.json").read_text())
            self.assertIn("security", saved["agents"]["reviewers"])

    def test_route_low_risk_no_surfaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            config = {
                "routing": {"low": {"code": "haiku"}},
                "surface_reviewers": {},
            }
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": tmp})
                make_job(tmp, slug, 1, stage="build")
                with patch.object(factory, "load_config", return_value=config):
                    factory.route_cmd(SimpleNamespace(
                        repo=slug, risk="low", surfaces=""))
                saved = json.loads((factory.job_dir(slug, 1) / "job.json").read_text())
            self.assertIn("reviewers", saved["agents"])


class StopTests(unittest.TestCase):

    def test_stop_marks_job_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            slug = "owner/repo"
            with patch.dict(os.environ, HERMES_HOME=tmp):
                factory.save(factory.repo_file(slug), {"repo": slug, "path": tmp})
                make_job(tmp, slug, 1, stage="build")
                factory.stop(SimpleNamespace(repo=slug, reason="test stop"))
                saved = json.loads((factory.job_dir(slug, 1) / "job.json").read_text())
            self.assertEqual(saved["state"], "stopped")


class MonitorTests(unittest.TestCase):

    def test_monitor_runs_without_error(self):
        import io, contextlib
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, HERMES_HOME=tmp):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    factory.monitor(None)
                self.assertIn("HERMES SOFTWARE FACTORY", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
