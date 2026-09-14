"""
Unit tests for lib/routers sub-modules.
"""

from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from lib.routers import (
    _pr_review_timestamps,
    _pr_review_timestamps_lock,
)
from lib.routers.base import (
    _EXPLICIT_COMMAND_PATTERN,
    clear_pr_review_cache,
    is_pr_created_by_us,
    has_explicit_command,
    _extract_repo_info,
    _build_accepted_response,
    _get_git_remote_repo_names,
    get_server_repo_name,
)
from lib.routers.push_router import handle_ping_event, handle_push_event
from lib.routers.pr_router import (
    handle_pull_request_event,
    handle_pull_request_review_event,
    handle_pull_request_review_comment_event,
)
from lib.routers.issue_router import (
    _FIX_COMMAND_PATTERN,
    _REVIEW_COMMAND_PATTERN,
    handle_issues_event,
    handle_issue_comment_event,
)


class TestSubRoutersDirectImport(unittest.TestCase):

    def test_base_router_helpers(self):
        clear_pr_review_cache()
        self.assertTrue(is_pr_created_by_us({}))
        self.assertTrue(has_explicit_command("/fix bug"))
        self.assertFalse(has_explicit_command("just a regular comment"))

        repo_full, repo_name, clone_url = _extract_repo_info({"repository": {"full_name": "owner/repo"}})
        self.assertEqual(repo_full, "owner/repo")
        self.assertEqual(repo_name, "repo")

        res = _build_accepted_response("test_action", "test_agent", "test_prompt")
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["action"], "test_action")

        self.assertIsNotNone(_pr_review_timestamps)
        self.assertIsNotNone(_pr_review_timestamps_lock)

    def test_push_router_direct(self):
        ping_res = handle_ping_event({"zen": "Keep it simple"})
        self.assertEqual(ping_res["status"], "accepted")
        self.assertEqual(ping_res["zen"], "Keep it simple")

        push_res = handle_push_event({"ref": "refs/heads/main"})
        self.assertEqual(push_res["status"], "accepted")
        self.assertEqual(push_res["action"], "self_update")

    def test_pr_router_direct(self):
        payload = {"action": "opened", "number": 1, "pull_request": {"body": "PR description"}}
        res = handle_pull_request_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["agent"], "code_reviewer")

        review_payload = {
            "action": "submitted",
            "review": {
                "state": "CHANGES_REQUESTED",
                "body": "Fix issues requested <!-- graviton:user -->",
            },
            "pull_request": {
                "number": 1,
                "body": "PR description <!-- graviton:pr_drafter -->",
                "user": {"login": "testuser"},
            },
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        review_res = handle_pull_request_review_event(review_payload)
        self.assertEqual(review_res["status"], "accepted")
        self.assertEqual(review_res["agent"], "code_fixer")

        comment_payload = {
            "action": "created",
            "comment": {
                "body": "Change this line",
                "path": "lib/routers/__init__.py",
                "line": 10,
            },
            "pull_request": {
                "number": 1,
                "body": "PR description <!-- graviton:pr_drafter -->",
                "html_url": "https://github.com/owner/repo/pull/1",
            },
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        comment_res = handle_pull_request_review_comment_event(comment_payload)
        self.assertEqual(comment_res["status"], "accepted")
        self.assertEqual(comment_res["agent"], "code_fixer")

    def test_issue_router_direct(self):
        payload = {"action": "opened", "issue": {"number": 10, "title": "Test Issue", "body": "Issue body"}}
        res = handle_issues_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["agent"], "issue_triager")

        comment_payload = {
            "action": "created",
            "comment": {
                "body": "/fix Action items required: update tests",
            },
            "issue": {
                "number": 10,
                "pull_request": {"html_url": "https://github.com/owner/repo/pull/10"},
                "body": "PR description <!-- graviton:pr_drafter -->",
            },
            "repository": {"full_name": "owner/repo", "name": "repo"},
        }
        comment_res = handle_issue_comment_event(comment_payload)
        self.assertEqual(comment_res["status"], "accepted")
        self.assertEqual(comment_res["agent"], "code_fixer")

    def test_null_fields_in_webhook_payloads(self):
        # 1. Null issue in issues event (opened)
        res_null_issue = handle_issues_event({"action": "opened", "issue": None})
        self.assertEqual(res_null_issue["status"], "accepted")
        self.assertEqual(res_null_issue["agent"], "issue_triager")

        # 2. Null label in issues labeled event
        res_null_label = handle_issues_event({"action": "labeled", "issue": {"number": 1}, "label": None})
        self.assertEqual(res_null_label["status"], "ignored")

        # 3. Valid label with null issue in issues labeled event
        res_null_issue_labeled = handle_issues_event(
            {"action": "labeled", "issue": None, "label": {"name": "ready-for-pr"}}
        )
        self.assertEqual(res_null_issue_labeled["status"], "accepted")
        self.assertEqual(res_null_issue_labeled["agent"], "pr_drafter")

        # 4. Null issue in issue_comment event
        res_comment_null_issue = handle_issue_comment_event(
            {"action": "created", "issue": None, "comment": {"body": "hello"}}
        )
        self.assertEqual(res_comment_null_issue["status"], "accepted")
        self.assertEqual(res_comment_null_issue["agent"], "issue_triager")

        # 5. Issue with null labels and null user in issue_comment event
        res_comment_null_labels = handle_issue_comment_event(
            {
                "action": "created",
                "issue": {"number": 1, "labels": None, "user": None},
                "comment": {"body": "hello"},
            }
        )
        self.assertEqual(res_comment_null_labels["status"], "accepted")
        self.assertEqual(res_comment_null_labels["agent"], "issue_triager")

        # 6. Null comment and null issue in issue_comment event
        res_comment_null_all = handle_issue_comment_event(
            {"action": "created", "issue": None, "comment": None}
        )
        self.assertEqual(res_comment_null_all["status"], "accepted")

        # 7. Null pull_request in pull_request_review_comment event
        res_pr_comment_null_pr = handle_pull_request_review_comment_event(
            {"action": "created", "pull_request": None, "comment": {"body": "test"}}
        )
        self.assertEqual(res_pr_comment_null_pr["status"], "accepted")
        self.assertEqual(res_pr_comment_null_pr["agent"], "code_fixer")

        # 8. Null comment and null pull_request in pull_request_review_comment event
        res_pr_comment_null_all = handle_pull_request_review_comment_event(
            {"action": "created", "pull_request": None, "comment": None}
        )
        self.assertEqual(res_pr_comment_null_all["status"], "accepted")
        self.assertEqual(res_pr_comment_null_all["agent"], "code_fixer")

        # 9. Null pull_request in pull_request event
        res_pr_null_pr = handle_pull_request_event(
            {"action": "opened", "pull_request": None}
        )
        self.assertEqual(res_pr_null_pr["status"], "accepted")
        self.assertEqual(res_pr_null_pr["agent"], "code_reviewer")

        # 10. Null pull_request and null review in pull_request_review event
        res_pr_review_null_all = handle_pull_request_review_event(
            {"action": "submitted", "pull_request": None, "review": None}
        )
        self.assertEqual(res_pr_review_null_all["status"], "ignored")

    def test_precompiled_command_patterns(self):
        self.assertIsInstance(_EXPLICIT_COMMAND_PATTERN, re.Pattern)
        self.assertIsInstance(_FIX_COMMAND_PATTERN, re.Pattern)
        self.assertIsInstance(_REVIEW_COMMAND_PATTERN, re.Pattern)

        # Base explicit command pattern matches
        self.assertTrue(bool(_EXPLICIT_COMMAND_PATTERN.search("/fix bug")))
        self.assertTrue(bool(_EXPLICIT_COMMAND_PATTERN.search("/review bug")))
        self.assertTrue(bool(_EXPLICIT_COMMAND_PATTERN.search("@antigravity please check")))
        self.assertFalse(bool(_EXPLICIT_COMMAND_PATTERN.search("prefix/fix")))
        self.assertFalse(bool(_EXPLICIT_COMMAND_PATTERN.search("prefix/review")))

        # Issue router fix / review command pattern matches
        self.assertTrue(bool(_FIX_COMMAND_PATTERN.search("/fix this issue")))
        self.assertFalse(bool(_FIX_COMMAND_PATTERN.search("/review this issue")))
        self.assertFalse(bool(_FIX_COMMAND_PATTERN.search("see foo/fix")))

        self.assertTrue(bool(_REVIEW_COMMAND_PATTERN.search("/review this pr")))
        self.assertFalse(bool(_REVIEW_COMMAND_PATTERN.search("/fix this pr")))
        self.assertFalse(bool(_REVIEW_COMMAND_PATTERN.search("see foo/review")))


class TestGitRemoteRepoResolution(unittest.TestCase):
    """
    Unit tests for git remote repository name resolution helpers in lib/routers/base.py:
    _get_git_remote_repo_names and get_server_repo_name.
    """

    @patch("subprocess.run")
    def test_git_remote_url_variations(self, mock_sub_run):
        test_cases = [
            # Standard SSH URL
            ("git@github.com:mweastwood/graviton.git\n", "graviton", "mweastwood/graviton"),
            # Standard HTTPS URL
            ("https://github.com/mweastwood/graviton.git\n", "graviton", "mweastwood/graviton"),
            # HTTPS URL without .git suffix
            ("https://github.com/mweastwood/graviton\n", "graviton", "mweastwood/graviton"),
            # HTTPS URL with trailing slashes and whitespace
            ("https://github.com/mweastwood/graviton/\n", "graviton", "mweastwood/graviton"),
            ("https://github.com/mweastwood/graviton.git/\n", "graviton", "mweastwood/graviton"),
            # Port / custom SSH URL
            ("ssh://git@github.com:22/mweastwood/graviton.git\n", "graviton", "mweastwood/graviton"),
            # Single path component / fallback format
            ("/graviton\n", "graviton", None),
            (":graviton.git\n", "graviton", None),
        ]

        dummy_root = Path("/workspace/my-repo")

        for stdout, expected_repo, expected_full in test_cases:
            with self.subTest(stdout=stdout):
                mock_res = MagicMock()
                mock_res.returncode = 0
                mock_res.stdout = stdout
                mock_sub_run.return_value = mock_res

                repo_name, repo_full_name = _get_git_remote_repo_names(dummy_root)
                self.assertEqual(repo_name, expected_repo)
                self.assertEqual(repo_full_name, expected_full)

                server_repo = get_server_repo_name(dummy_root)
                self.assertEqual(server_repo, expected_repo)

    @patch("subprocess.run")
    def test_fallback_non_zero_exit_code(self, mock_sub_run):
        mock_res = MagicMock()
        mock_res.returncode = 128
        mock_res.stdout = ""
        mock_res.stderr = "fatal: not a git repository"
        mock_sub_run.return_value = mock_res

        dummy_root = Path("/custom/fallback-dir")
        repo_name, repo_full_name = _get_git_remote_repo_names(dummy_root)
        self.assertIsNone(repo_name)
        self.assertIsNone(repo_full_name)

        server_repo = get_server_repo_name(dummy_root)
        self.assertEqual(server_repo, "fallback-dir")

    @patch("subprocess.run")
    def test_fallback_empty_or_whitespace_output(self, mock_sub_run):
        for empty_stdout in ["", "   ", "\n", "\t\n  "]:
            with self.subTest(empty_stdout=repr(empty_stdout)):
                mock_res = MagicMock()
                mock_res.returncode = 0
                mock_res.stdout = empty_stdout
                mock_sub_run.return_value = mock_res

                dummy_root = Path("/custom/empty-remote")
                repo_name, repo_full_name = _get_git_remote_repo_names(dummy_root)
                self.assertIsNone(repo_name)
                self.assertIsNone(repo_full_name)

                server_repo = get_server_repo_name(dummy_root)
                self.assertEqual(server_repo, "empty-remote")

    @patch("subprocess.run")
    def test_fallback_subprocess_timeout(self, mock_sub_run):
        mock_sub_run.side_effect = subprocess.TimeoutExpired(cmd=["git"], timeout=5)

        dummy_root = Path("/custom/timeout-repo")
        repo_name, repo_full_name = _get_git_remote_repo_names(dummy_root)
        self.assertIsNone(repo_name)
        self.assertIsNone(repo_full_name)

        server_repo = get_server_repo_name(dummy_root)
        self.assertEqual(server_repo, "timeout-repo")

    @patch("subprocess.run")
    def test_fallback_os_error(self, mock_sub_run):
        mock_sub_run.side_effect = OSError("git command not found")

        dummy_root = Path("/custom/oserror-repo")
        repo_name, repo_full_name = _get_git_remote_repo_names(dummy_root)
        self.assertIsNone(repo_name)
        self.assertIsNone(repo_full_name)

        server_repo = get_server_repo_name(dummy_root)
        self.assertEqual(server_repo, "oserror-repo")

    @patch("subprocess.run")
    def test_default_root_resolution_success(self, mock_sub_run):
        import lib.routers.base as base_mod
        expected_root = Path(base_mod.__file__).resolve().parent.parent.parent

        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "git@github.com:mweastwood/graviton.git\n"
        mock_sub_run.return_value = mock_res

        repo_name, repo_full_name = _get_git_remote_repo_names(repo_root=None)
        self.assertEqual(repo_name, "graviton")
        self.assertEqual(repo_full_name, "mweastwood/graviton")
        mock_sub_run.assert_called_with(
            ["git", "remote", "get-url", "origin"],
            cwd=str(expected_root),
            capture_output=True,
            text=True,
            timeout=5,
        )

        server_repo = get_server_repo_name(repo_root=None)
        self.assertEqual(server_repo, "graviton")


class TestReleaseRouting(unittest.TestCase):

    def test_release_issue_opened(self):
        payload = {
            "action": "opened",
            "issue": {
                "number": 100,
                "title": "🚀 Release Controller",
                "body": "",
                "user": {"login": "alice"},
                "author_association": "OWNER",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issues_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["action"], "release_init")
        self.assertEqual(res["issue_number"], 100)

    def test_release_issue_opened_unauthorized(self):
        payload = {
            "action": "opened",
            "issue": {
                "number": 101,
                "title": "🚀 Release Controller",
                "body": "",
                "user": {"login": "eve"},
                "author_association": "NONE",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issues_event(payload)
        self.assertEqual(res["status"], "ignored")
        self.assertIn("not authorized", res.get("reason", ""))

    def test_release_issue_edited(self):
        payload = {
            "action": "edited",
            "issue": {
                "number": 100,
                "title": "🚀 Release Controller",
                "body": "",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issues_event(payload)
        self.assertEqual(res["status"], "ignored")

    def test_release_comment_patch_authorized(self):
        payload = {
            "action": "created",
            "issue": {
                "number": 100,
                "title": "🚀 Release Controller",
            },
            "comment": {
                "id": 555,
                "body": "patch",
                "user": {"login": "alice"},
                "author_association": "OWNER",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issue_comment_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["action"], "release")
        self.assertEqual(res["release_type"], "patch")
        self.assertEqual(res["command"], "bin/tag.sh patch")
        self.assertEqual(res["branch"], "main")
        self.assertEqual(res["issue_number"], 100)

    def test_release_comment_slash_tag_minor(self):
        payload = {
            "action": "created",
            "issue": {
                "number": 100,
                "title": "Release Tracker",
            },
            "comment": {
                "id": 556,
                "body": "/tag minor",
                "user": {"login": "alice"},
                "author_association": "MEMBER",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issue_comment_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["action"], "release")
        self.assertEqual(res["release_type"], "minor")
        self.assertEqual(res["command"], "bin/tag.sh minor")

    def test_release_comment_help(self):
        payload = {
            "action": "created",
            "issue": {
                "number": 100,
                "title": "Release",
            },
            "comment": {
                "id": 557,
                "body": "help",
                "user": {"login": "alice"},
                "author_association": "OWNER",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issue_comment_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["action"], "release_help")

    def test_release_comment_unrecognized(self):
        payload = {
            "action": "created",
            "issue": {
                "number": 100,
                "title": "Release",
            },
            "comment": {
                "id": 558,
                "body": "foobar",
                "user": {"login": "alice"},
                "author_association": "OWNER",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issue_comment_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["action"], "release_unrecognized")

    def test_release_comment_unauthorized_user(self):
        payload = {
            "action": "created",
            "issue": {
                "number": 100,
                "title": "Release",
            },
            "comment": {
                "id": 559,
                "body": "patch",
                "user": {"login": "stranger"},
                "author_association": "NONE",
            },
            "repository": {"name": "app", "full_name": "owner/app"},
        }
        res = handle_issue_comment_event(payload)
        self.assertEqual(res["status"], "ignored")
        self.assertIn("not authorized", res["reason"])


    @patch("subprocess.run")
    def test_default_root_resolution_failure_falls_back_to_directory_name(self, mock_sub_run):
        import lib.routers.base as base_mod
        expected_root = Path(base_mod.__file__).resolve().parent.parent.parent

        mock_res = MagicMock()
        mock_res.returncode = 1
        mock_res.stdout = ""
        mock_sub_run.return_value = mock_res

        repo_name, repo_full_name = _get_git_remote_repo_names(repo_root=None)
        self.assertIsNone(repo_name)
        self.assertIsNone(repo_full_name)

        server_repo = get_server_repo_name(repo_root=None)
        self.assertEqual(server_repo, expected_root.name)

    def test_default_root_resolution_exception_fallback(self):
        with patch("lib.routers.base.Path", side_effect=RuntimeError("Filesystem resolve failure")):
            repo_name, repo_full_name = _get_git_remote_repo_names(repo_root=None)
            self.assertIsNone(repo_name)
            self.assertIsNone(repo_full_name)

            server_repo = get_server_repo_name(repo_root=None)
            self.assertEqual(server_repo, "graviton")



