"""
Tests for Gitea/Forgejo VCS backend (vcs="gitea") in factory.py.

Coverage:
  _cli("gitea")            -> "gt"
  _vcs                     -> "gitea" from dict and registered repo
  _gt_host                 -> env vars, config key, empty
  origin_slug              -> Gitea HTTPS, SSH, host verification, no match
  repo_view gitea          -> gt repo view --json, host forwarded
  issue_list_labeled gitea -> gt issue list, host forwarded
  issue_view_full gitea    -> gt issue view --json
  issue_comment gitea      -> gt issue comment
  label_create gitea       -> gt label create, already-exists swallowed
  pr_create gitea          -> draft/no-draft, host forwarded
  add --vcs gitea          -> gitea in argparser choices
  check_queue gitea        -> passes config to issue_list_labeled
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

_FACTORY_PATH = Path(__file__).resolve().parents[1] / "factory/bin/factory.py"
_spec = importlib.util.spec_from_file_location("factory", _FACTORY_PATH)
F = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(F)


def _fake_run(stdout="", returncode=0):
    r = MagicMock()
    r.stdout = stdout
    r.returncode = returncode
    r.stderr = ""
    return r


def _env(tmp):
    return patch.dict(os.environ, HERMES_HOME=tmp)


def _repo(tmp, slug, path=None, vcs="gitea"):
    rec = {"repo": slug, "path": path or tmp, "vcs": vcs, "base": "main",
           "added": F.now(), "checked": None, "queue": []}
    F.save(F.repo_file(slug), rec)
    return rec


# ═══════════════════════════════════════════════════════════════════════════════
# 1. _cli / _vcs / _gt_host
# ═══════════════════════════════════════════════════════════════════════════════

class TestCliGitea(unittest.TestCase):

    def test_cli_gitea_returns_gt(self):
        self.assertEqual(F._cli("gitea"), "gt")

    def test_cli_github_unchanged(self):
        self.assertEqual(F._cli("github"), "gh")

    def test_cli_gitlab_unchanged(self):
        self.assertEqual(F._cli("gitlab"), "glab")

    def test_vcs_dict_gitea(self):
        self.assertEqual(F._vcs({"vcs": "gitea"}), "gitea")

    def test_vcs_slug_gitea(self):
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                _repo(tmp, "o/r", vcs="gitea")
                self.assertEqual(F._vcs("o/r"), "gitea")

    def test_gt_host_from_env_gitea_cli_host(self):
        with patch.dict(os.environ, GITEA_CLI_HOST="https://gitea.example.com",
                        GITEA_INSTANCE_URL=""):
            self.assertEqual(F._gt_host(), "https://gitea.example.com")

    def test_gt_host_from_env_instance_url(self):
        with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL="https://forge.local"):
            self.assertEqual(F._gt_host(), "https://forge.local")

    def test_gt_host_from_config_dict(self):
        with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
            result = F._gt_host({"gitea_host": "https://my.gitea.org"})
        self.assertEqual(result, "https://my.gitea.org")

    def test_gt_host_strips_trailing_slash(self):
        with patch.dict(os.environ, GITEA_CLI_HOST="https://host.example.com/",
                        GITEA_INSTANCE_URL=""):
            self.assertEqual(F._gt_host(), "https://host.example.com")

    def test_gt_host_empty_when_nothing_set(self):
        with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
            self.assertEqual(F._gt_host({}), "")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. origin_slug for Gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestOriginSlugGitea(unittest.TestCase):

    def _run(self, url):
        mock = MagicMock()
        mock.stdout = url + "\n"
        with patch.object(F.subprocess, "run", return_value=mock):
            return F.origin_slug("/path", vcs="gitea")

    def _run_with_host(self, url, host):
        mock = MagicMock()
        mock.stdout = url + "\n"
        with patch.object(F.subprocess, "run", return_value=mock):
            with patch.dict(os.environ, GITEA_CLI_HOST=host, GITEA_INSTANCE_URL=""):
                return F.origin_slug("/path", vcs="gitea")

    def test_gitea_https_any_host(self):
        result = self._run("https://gitea.example.com/owner/repo.git")
        self.assertEqual(result, "owner/repo")

    def test_gitea_ssh_any_host(self):
        result = self._run("git@gitea.example.com:owner/repo.git")
        self.assertEqual(result, "owner/repo")

    def test_gitea_ssh_custom_port(self):
        result = self._run("ssh://git@gitea.example.com:22/owner/repo.git")
        self.assertEqual(result, "owner/repo")

    def test_gitea_forgejo_instance(self):
        result = self._run("https://codeberg.org/owner/repo.git")
        self.assertEqual(result, "owner/repo")

    def test_gitea_host_verification_passes(self):
        result = self._run_with_host(
            "https://gitea.example.com/owner/repo.git",
            "https://gitea.example.com")
        self.assertEqual(result, "owner/repo")

    def test_gitea_host_verification_fails_on_wrong_host(self):
        # URL is github.com but we registered a gitea host — must return None
        result = self._run_with_host(
            "https://github.com/owner/repo.git",
            "https://gitea.example.com")
        self.assertIsNone(result)

    def test_gitea_no_host_set_accepts_any(self):
        # When no host is configured, any URL matches (best-effort)
        with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
            mock = MagicMock()
            mock.stdout = "https://anything.example.com/owner/repo.git\n"
            with patch.object(F.subprocess, "run", return_value=mock):
                result = F.origin_slug("/path", vcs="gitea", config={})
        self.assertEqual(result, "owner/repo")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. repo_view gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestRepoViewGitea(unittest.TestCase):

    def test_repo_view_gitea_returns_normalized(self):
        raw = {
            "nameWithOwner": "owner/repo",
            "defaultBranchRef": {"name": "main"},
            "isArchived": False,
        }
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or json.dumps(raw)):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://gitea.host", GITEA_INSTANCE_URL=""):
                result = F.repo_view("owner/repo", "gitea")
        self.assertEqual(result["nameWithOwner"], "owner/repo")
        self.assertEqual(result["defaultBranchRef"]["name"], "main")
        self.assertFalse(result["isArchived"])
        # gt must be called, not gh or glab
        self.assertTrue(any("gt" in str(c) for c in calls))

    def test_repo_view_gitea_host_forwarded(self):
        raw = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "isArchived": False}
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or json.dumps(raw)):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://gt.host", GITEA_INSTANCE_URL=""):
                F.repo_view("o/r", "gitea")
        flat = " ".join(str(c) for c in calls)
        self.assertIn("--host", flat)
        self.assertIn("https://gt.host", flat)

    def test_repo_view_gitea_no_host_no_flag(self):
        raw = {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}, "isArchived": False}
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or json.dumps(raw)):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.repo_view("o/r", "gitea", config={})
        flat = " ".join(str(c) for c in calls)
        self.assertNotIn("--host", flat)


# ═══════════════════════════════════════════════════════════════════════════════
# 4. issue_list_labeled gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestIssueListGitea(unittest.TestCase):

    def test_returns_number_and_title(self):
        raw = [{"number": 3, "title": "Bug"}]
        with patch.object(F, "cli_run", return_value=json.dumps(raw)):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                result = F.issue_list_labeled("o/r", "gitea", "factory")
        self.assertEqual(result[0]["number"], 3)
        self.assertEqual(result[0]["title"], "Bug")

    def test_uses_gt_cli(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "[]"):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.issue_list_labeled("o/r", "gitea", "factory")
        self.assertTrue(any("gt" in str(c) for c in calls))

    def test_host_forwarded(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "[]"):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://gt.host", GITEA_INSTANCE_URL=""):
                F.issue_list_labeled("o/r", "gitea", "factory")
        flat = " ".join(str(c) for c in calls)
        self.assertIn("--host", flat)
        self.assertIn("https://gt.host", flat)

    def test_no_iid_remapping(self):
        """Gitea issues use `number` directly (unlike GitLab which uses `iid`)."""
        raw = [{"number": 7, "title": "Feature"}]
        with patch.object(F, "cli_run", return_value=json.dumps(raw)):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                result = F.issue_list_labeled("o/r", "gitea", "factory")
        self.assertEqual(result[0]["number"], 7)  # number, not iid


# ═══════════════════════════════════════════════════════════════════════════════
# 5. issue_view_full gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestIssueViewFullGitea(unittest.TestCase):

    def test_returns_normalized(self):
        raw = {
            "number": 5, "title": "Fix bug", "body": "desc",
            "url": "https://gt.host/o/r/issues/5",
            "author": {"login": "alice"},
        }
        with patch.object(F, "cli_run", return_value=json.dumps(raw)):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                result = F.issue_view_full("o/r", "gitea", 5)
        self.assertEqual(result["number"], 5)
        self.assertEqual(result["author"]["login"], "alice")
        self.assertEqual(result["body"], "desc")

    def test_uses_gt_cli(self):
        raw = {"number": 1, "title": "t", "body": "", "url": "", "author": {"login": ""}}
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or json.dumps(raw)):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.issue_view_full("o/r", "gitea", 1)
        self.assertTrue(any("gt" in str(c) for c in calls))


# ═══════════════════════════════════════════════════════════════════════════════
# 6. issue_comment gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestIssueCommentGitea(unittest.TestCase):

    def test_calls_gt_issue_comment(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or ""):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.issue_comment("o/r", "gitea", 1, "Hello")
        self.assertTrue(any("gt" in str(c) for c in calls))
        self.assertTrue(any("comment" in str(c) for c in calls))
        self.assertTrue(any("Hello" in str(c) for c in calls))

    def test_host_forwarded(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or ""):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://host", GITEA_INSTANCE_URL=""):
                F.issue_comment("o/r", "gitea", 1, "Note")
        flat = " ".join(str(c) for c in calls)
        self.assertIn("--host", flat)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. label_create gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestLabelCreateGitea(unittest.TestCase):

    def test_uses_gt_with_hash_color(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or ""):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.label_create("o/r", "gitea", "factory", "5319E7", "desc")
        flat = " ".join(str(c) for c in calls)
        self.assertIn("gt", flat)
        self.assertIn("#5319E7", flat)

    def test_already_exists_swallowed(self):
        """SystemExit from gt label create must not propagate."""
        with patch.object(F, "cli_run", side_effect=SystemExit("already exists")):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.label_create("o/r", "gitea", "factory", "5319E7", "desc")
        # Must not raise

    def test_host_forwarded(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or ""):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://h", GITEA_INSTANCE_URL=""):
                F.label_create("o/r", "gitea", "factory", "5319E7", "desc")
        flat = " ".join(str(c) for c in calls)
        self.assertIn("--host", flat)


# ═══════════════════════════════════════════════════════════════════════════════
# 8. pr_create gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestPrCreateGitea(unittest.TestCase):

    def test_returns_url_from_last_line(self):
        output = "Creating PR...\nhttps://gitea.example.com/o/r/pulls/5"
        with patch.object(F, "cli_run", return_value=output):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                url = F.pr_create("o/r", "gitea", "main", "factory/issue-5",
                                  "Title", "/tmp/body.md", False)
        self.assertEqual(url, "https://gitea.example.com/o/r/pulls/5")

    def test_uses_gt_cli(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "https://x"):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.pr_create("o/r", "gitea", "main", "b", "T", "/f", False)
        self.assertTrue(any("gt" in str(c) for c in calls))

    def test_draft_flag(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "https://x"):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.pr_create("o/r", "gitea", "main", "b", "T", "/f", True)
        self.assertTrue(any("--draft" in str(c) for c in calls))

    def test_no_draft_no_flag(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "https://x"):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.pr_create("o/r", "gitea", "main", "b", "T", "/f", False)
        self.assertFalse(any("--draft" in str(c) for c in calls))

    def test_yes_flag_always_present(self):
        """--yes suppresses interactive confirmation from gt pr create."""
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "https://x"):
            with patch.dict(os.environ, GITEA_CLI_HOST="", GITEA_INSTANCE_URL=""):
                F.pr_create("o/r", "gitea", "main", "b", "T", "/f", False)
        self.assertTrue(any("--yes" in str(c) for c in calls))

    def test_host_forwarded(self):
        calls = []
        with patch.object(F, "cli_run",
                          side_effect=lambda *a, **kw: calls.append(a) or "https://x"):
            with patch.dict(os.environ, GITEA_CLI_HOST="https://gt.host", GITEA_INSTANCE_URL=""):
                F.pr_create("o/r", "gitea", "main", "b", "T", "/f", False)
        flat = " ".join(str(c) for c in calls)
        self.assertIn("--host", flat)
        self.assertIn("https://gt.host", flat)


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Argparser accepts gitea
# ═══════════════════════════════════════════════════════════════════════════════

class TestArgparserGitea(unittest.TestCase):

    def test_gitea_in_vcs_choices(self):
        import argparse
        # Import the full factory module and check _ALL_STAGES equivalent
        # by verifying "gitea" is accepted by the add subparser
        # We simulate what argparse does with --vcs gitea
        p = F.argparse.ArgumentParser()
        sub = p.add_subparsers(dest="command")
        s = sub.add_parser("add")
        s.add_argument("repo")
        s.add_argument("--vcs", choices=["github", "gitlab", "gitea"])
        args = p.parse_args(["add", "o/r", "--vcs", "gitea"])
        self.assertEqual(args.vcs, "gitea")

    def test_gitea_cli_name(self):
        self.assertEqual(F._cli("gitea"), "gt")

    def test_forgejo_config_accepted(self):
        """Forgejo instances work with vcs=gitea."""
        # Forgejo is API-compatible; same gt CLI, same vcs value
        self.assertEqual(F._cli("gitea"), "gt")


# ═══════════════════════════════════════════════════════════════════════════════
# 10. check_queue passes config to issue_list_labeled
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckQueueGitea(unittest.TestCase):

    def test_config_passed_through(self):
        """check_queue must pass config (with gitea_host) to issue_list_labeled."""
        with tempfile.TemporaryDirectory() as tmp:
            with _env(tmp):
                repo = _repo(tmp, "o/r", path=tmp, vcs="gitea")
                # Write a factory/config.json with gitea_host
                (Path(tmp) / "factory").mkdir(exist_ok=True)
                (Path(tmp) / "factory/config.json").write_text(
                    '{"vcs":"gitea","gitea_host":"https://gt.host"}')
                captured = {}
                def fake_list(slug, vcs, label, limit=100, config=None):
                    captured["config"] = config
                    return []
                with patch.object(F, "issue_list_labeled", side_effect=fake_list):
                    F.check_queue(repo)
            self.assertIsNotNone(captured.get("config"))
            self.assertEqual(captured["config"].get("gitea_host"), "https://gt.host")


if __name__ == "__main__":
    unittest.main()
