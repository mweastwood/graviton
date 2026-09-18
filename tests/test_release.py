"""
Unit tests for lib/release.py
"""

import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.release import (
    DEFAULT_BRANCH,
    DEFAULT_COMMANDS,
    DEFAULT_RELEASE_ISSUE_PATTERN,
    RELEASE_BOT_TAG,
    clear_release_locks,
    execute_release,
    execute_release_async,
    format_release_help,
    format_release_init,
    get_repo_release_lock,
    is_release_issue,
    is_user_authorized_for_release,
    load_release_config,
    parse_release_command,
    post_issue_comment,
    post_release_help_async,
    post_release_init_async,
    post_release_unrecognized_async,
    resolve_repo_dir,
)


class TestReleaseConfigLoading(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_release_config_none_or_missing(self):
        self.assertIsNone(load_release_config(None))
        self.assertIsNone(load_release_config(self.repo_path / "nonexistent"))
        self.assertIsNone(load_release_config(self.repo_path))

    def test_load_release_config_dot_graviton_json(self):
        config_data = {
            "release": {
                "branch": "prod",
                "allowed_users": ["alice", "bob"],
                "commands": {
                    "patch": "bin/tag.sh --patch",
                    "minor": "bin/tag.sh --minor",
                },
            }
        }
        cfg_file = self.repo_path / ".graviton.json"
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        loaded = load_release_config(self.repo_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get("branch"), "prod")
        self.assertEqual(loaded.get("allowed_users"), ["alice", "bob"])
        self.assertEqual(loaded.get("commands", {}).get("patch"), "bin/tag.sh --patch")

    def test_load_release_config_graviton_json_fallback(self):
        config_data = {
            "release": {
                "branch": "main",
                "commands": {"major": "bin/tag.sh major"},
            }
        }
        cfg_file = self.repo_path / "graviton.json"
        with open(cfg_file, "w", encoding="utf-8") as f:
            json.dump(config_data, f)

        loaded = load_release_config(self.repo_path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.get("commands", {}).get("major"), "bin/tag.sh major")

    def test_load_release_config_invalid_json(self):
        cfg_file = self.repo_path / ".graviton.json"
        cfg_file.write_text("not json", encoding="utf-8")
        self.assertIsNone(load_release_config(self.repo_path))


class TestResolveRepoDir(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.repos_dir = self.base_path / "repos"
        self.repos_dir.mkdir()
        self.app_dir = self.repos_dir / "myapp"
        self.app_dir.mkdir()
        self.repo_root = self.base_path / "graviton"
        self.repo_root.mkdir()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_resolve_repo_dir_from_repos_dir(self):
        resolved = resolve_repo_dir("myapp", repo_root=self.repo_root, repos_dir=self.repos_dir)
        self.assertEqual(resolved, self.app_dir)

    def test_resolve_repo_dir_path_traversal_prevented(self):
        resolved = resolve_repo_dir("../outside", repo_root=self.repo_root, repos_dir=self.repos_dir)
        self.assertIsNone(resolved)

    def test_resolve_repo_dir_fallback_repo_root(self):
        resolved = resolve_repo_dir("graviton", repo_root=self.repo_root, repos_dir=None)
        self.assertEqual(resolved, self.repo_root)


class TestIsReleaseIssue(unittest.TestCase):

    def test_default_pattern_matches(self):
        self.assertTrue(is_release_issue("Release"))
        self.assertTrue(is_release_issue("🚀 Release Controller"))
        self.assertTrue(is_release_issue("Release Tracker"))
        self.assertTrue(is_release_issue("release controller"))
        self.assertTrue(is_release_issue("🚀 Release"))

    def test_default_pattern_non_matches(self):
        self.assertFalse(is_release_issue(""))
        self.assertFalse(is_release_issue("Bug in login screen"))
        self.assertFalse(is_release_issue("Fix release script typo"))
        self.assertFalse(is_release_issue("Add new feature"))
        self.assertFalse(is_release_issue("Release notes for v1.0.0"))
        self.assertFalse(is_release_issue("Release blocker"))
        self.assertFalse(is_release_issue("Release blocker: payment gateway crash"))
        self.assertFalse(is_release_issue("Release checklist"))

    def test_custom_pattern(self):
        cfg = {"issue_pattern": r"^Deploy\s+Bot\b"}
        self.assertTrue(is_release_issue("Deploy Bot", cfg))
        self.assertFalse(is_release_issue("Release Controller", cfg))

    def test_invalid_custom_pattern_falls_back(self):
        cfg = {"issue_pattern": "[invalid("}
        self.assertTrue(is_release_issue("Release Controller", cfg))


class TestParseReleaseCommand(unittest.TestCase):

    def test_default_commands(self):
        cmd, shell_cmd = parse_release_command("patch")
        self.assertEqual(cmd, "patch")
        self.assertEqual(shell_cmd, "bin/tag.sh patch")

        cmd, shell_cmd = parse_release_command("minor")
        self.assertEqual(cmd, "minor")
        self.assertEqual(shell_cmd, "bin/tag.sh minor")

        cmd, shell_cmd = parse_release_command("major")
        self.assertEqual(cmd, "major")
        self.assertEqual(shell_cmd, "bin/tag.sh major")

    def test_slash_and_keyword_prefixes(self):
        cmd, shell_cmd = parse_release_command("/tag patch")
        self.assertEqual(cmd, "patch")
        self.assertEqual(shell_cmd, "bin/tag.sh patch")

        cmd, shell_cmd = parse_release_command("/release minor")
        self.assertEqual(cmd, "minor")
        self.assertEqual(shell_cmd, "bin/tag.sh minor")

        cmd, shell_cmd = parse_release_command("release major")
        self.assertEqual(cmd, "major")
        self.assertEqual(shell_cmd, "bin/tag.sh major")

    def test_backticks_and_whitespace(self):
        cmd, shell_cmd = parse_release_command("  `patch`  ")
        self.assertEqual(cmd, "patch")
        self.assertEqual(shell_cmd, "bin/tag.sh patch")

    def test_help_commands(self):
        self.assertEqual(parse_release_command("help"), ("help", None))
        self.assertEqual(parse_release_command("/help"), ("help", None))
        self.assertEqual(parse_release_command("?"), ("help", None))

    def test_custom_commands_from_config(self):
        cfg = {
            "commands": {
                "beta": "./scripts/tag_beta.sh",
                "prod": "./scripts/tag_prod.sh",
            }
        }
        cmd, shell_cmd = parse_release_command("beta", cfg)
        self.assertEqual(cmd, "beta")
        self.assertEqual(shell_cmd, "./scripts/tag_beta.sh")

        cmd, shell_cmd = parse_release_command("/release prod", cfg)
        self.assertEqual(cmd, "prod")
        self.assertEqual(shell_cmd, "./scripts/tag_prod.sh")

    def test_unrecognized_command(self):
        self.assertEqual(parse_release_command("random text"), (None, None))
        self.assertEqual(parse_release_command(""), (None, None))

    def test_multi_word_comments_rejected(self):
        self.assertEqual(parse_release_command("release patch notes"), (None, None))
        self.assertEqual(parse_release_command("release patch notes for v1.0.0"), (None, None))
        self.assertEqual(parse_release_command("tag minor bug in checkout"), (None, None))
        self.assertEqual(parse_release_command("/release patch notes"), (None, None))
        self.assertEqual(parse_release_command("/tag minor fix"), (None, None))
        self.assertEqual(parse_release_command("patch notes for v1.0.0"), (None, None))


class TestUserAuthorization(unittest.TestCase):

    def test_explicit_allowed_users(self):
        cfg = {"allowed_users": ["mweastwood", "alice"]}
        self.assertTrue(is_user_authorized_for_release("mweastwood", {}, cfg))
        self.assertTrue(is_user_authorized_for_release("@Alice", {}, cfg))
        self.assertFalse(is_user_authorized_for_release("eve", {}, cfg))

    def test_string_allowed_user(self):
        cfg = {"allowed_users": "mweastwood"}
        self.assertTrue(is_user_authorized_for_release("mweastwood", {}, cfg))
        self.assertTrue(is_user_authorized_for_release("@mweastwood", {}, cfg))
        self.assertFalse(is_user_authorized_for_release("eve", {}, cfg))

    def test_author_association_fallback(self):
        payload_owner = {"comment": {"author_association": "OWNER"}}
        self.assertTrue(is_user_authorized_for_release("any_user", payload_owner, {}))

        payload_member = {"comment": {"author_association": "MEMBER"}}
        self.assertTrue(is_user_authorized_for_release("any_user", payload_member, {}))

        payload_contributor = {"comment": {"author_association": "CONTRIBUTOR"}}
        self.assertFalse(is_user_authorized_for_release("contributor_user", payload_contributor, {}))

    def test_issue_author_association_fallback(self):
        payload_owner = {"issue": {"author_association": "OWNER"}}
        self.assertTrue(is_user_authorized_for_release("any_user", payload_owner, {}))

        payload_member = {"issue": {"author_association": "MEMBER"}}
        self.assertTrue(is_user_authorized_for_release("any_user", payload_member, {}))

        payload_collab = {"issue": {"author_association": "COLLABORATOR"}}
        self.assertTrue(is_user_authorized_for_release("any_user", payload_collab, {}))

        payload_contributor = {"issue": {"author_association": "CONTRIBUTOR"}}
        self.assertFalse(is_user_authorized_for_release("contributor_user", payload_contributor, {}))

    def test_repo_owner_fallback(self):
        payload = {"repository": {"owner": {"login": "repo_owner"}}}
        self.assertTrue(is_user_authorized_for_release("repo_owner", payload, {}))
        self.assertFalse(is_user_authorized_for_release("other_user", payload, {}))

    def test_empty_user(self):
        self.assertFalse(is_user_authorized_for_release(None, {}, {}))
        self.assertFalse(is_user_authorized_for_release("", {}, {}))


class TestExecuteRelease(unittest.TestCase):

    def setUp(self):
        clear_release_locks()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_path = Path(self.temp_dir.name)

    def tearDown(self):
        clear_release_locks()
        self.temp_dir.cleanup()

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_success(self, mock_subproc, mock_comment):
        # 1. git status, 2. git checkout, 3. git pull, 4. command
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["bin/tag.sh", "patch"], returncode=0, stdout="Tagged v1.0.1", stderr=""),
        ]

        success = execute_release(
            repo_dir=self.repo_path,
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
            target_branch="main",
        )
        self.assertTrue(success)
        mock_comment.assert_called_once()
        comment_body = mock_comment.call_args[0][2]
        self.assertIn("Patch Release Succeeded!", comment_body)
        self.assertIn("Tagged v1.0.1", comment_body)
        self.assertIn("<!-- antigravity-auto-reply -->", comment_body)
        self.assertIn("<!-- graviton:release -->", comment_body)

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_script_failure(self, mock_subproc, mock_comment):
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["bin/tag.sh", "patch"], returncode=1, stdout="", stderr="Tag already exists"),
        ]

        success = execute_release(
            repo_dir=self.repo_path,
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
            target_branch="main",
        )
        self.assertFalse(success)
        comment_body = mock_comment.call_args[0][2]
        self.assertIn("Patch Release Failed!", comment_body)
        self.assertIn("Tag already exists", comment_body)
        self.assertIn("<!-- antigravity-auto-reply -->", comment_body)
        self.assertIn("<!-- graviton:release -->", comment_body)

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_dirty_working_tree(self, mock_subproc, mock_comment):
        mock_subproc.return_value = subprocess.CompletedProcess(
            args=["git", "status", "--porcelain", "-uno"],
            returncode=0,
            stdout=" M lib/release.py\n",
            stderr="",
        )
        success = execute_release(
            repo_dir=self.repo_path,
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
            target_branch="main",
        )
        self.assertFalse(success)
        mock_comment.assert_called_once()
        comment_body = mock_comment.call_args[0][2]
        self.assertIn("Dirty Working Tree", comment_body)
        self.assertIn("M lib/release.py", comment_body)

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_git_pull_failure(self, mock_subproc, mock_comment):
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=1, stdout="", stderr="Merge conflict"),
        ]

        success = execute_release(
            repo_dir=self.repo_path,
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
            target_branch="main",
        )
        self.assertFalse(success)
        comment_body = mock_comment.call_args[0][2]
        self.assertIn("Failed to fast-forward", comment_body)

    @patch("lib.release.post_issue_comment")
    def test_execute_release_concurrency_lock(self, mock_comment):
        lock = get_repo_release_lock(str(self.repo_path.resolve()))
        lock.acquire()
        try:
            success = execute_release(
                repo_dir=self.repo_path,
                repo_full_name="owner/repo",
                issue_number=42,
                release_type="patch",
                command="bin/tag.sh patch",
            )
            self.assertFalse(success)
            comment_body = mock_comment.call_args[0][2]
            self.assertIn("Release In Progress", comment_body)
        finally:
            lock.release()

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_async_thread(self, mock_subproc, mock_comment):
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["bin/tag.sh", "patch"], returncode=0, stdout="Success", stderr=""),
        ]

        thread = execute_release_async(
            repo_dir=self.repo_path,
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
        )
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        mock_comment.assert_called_once()

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_branch_none_fallback(self, mock_subproc, mock_comment):
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["bin/tag.sh", "patch"], returncode=0, stdout="Success", stderr=""),
        ]

        success = execute_release(
            repo_dir=self.repo_path,
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
            target_branch=None,
        )
        self.assertTrue(success)
        checkout_call = mock_subproc.call_args_list[1]
        self.assertEqual(checkout_call[0][0], ["git", "checkout", DEFAULT_BRANCH])

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_string_pre_flight(self, mock_subproc, mock_comment):
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git diff --quiet"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["bin/tag.sh", "patch"], returncode=0, stdout="Success", stderr=""),
        ]

        success = execute_release(
            repo_dir=self.repo_path,
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
            pre_flight_checks="git diff --quiet",
        )
        self.assertTrue(success)
        self.assertEqual(mock_subproc.call_count, 5)
        pre_flight_call = mock_subproc.call_args_list[3]
        self.assertEqual(pre_flight_call[0][0], "git diff --quiet")

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_string_repo_dir(self, mock_subproc, mock_comment):
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["bin/tag.sh", "patch"], returncode=0, stdout="Tagged v1.0.1", stderr=""),
        ]

        success = execute_release(
            repo_dir=str(self.repo_path),
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
            target_branch="main",
        )
        self.assertTrue(success)
        mock_comment.assert_called_once()
        comment_body = mock_comment.call_args[0][2]
        self.assertIn("Patch Release Succeeded!", comment_body)
        self.assertIn("Tagged v1.0.1", comment_body)

    @patch("lib.release.post_issue_comment")
    @patch("subprocess.run")
    def test_execute_release_async_string_repo_dir(self, mock_subproc, mock_comment):
        mock_subproc.side_effect = [
            subprocess.CompletedProcess(args=["git", "status"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "checkout"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["git", "pull"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["bin/tag.sh", "patch"], returncode=0, stdout="Success", stderr=""),
        ]

        thread = execute_release_async(
            repo_dir=str(self.repo_path),
            repo_full_name="owner/repo",
            issue_number=42,
            release_type="patch",
            command="bin/tag.sh patch",
        )
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        mock_comment.assert_called_once()


class TestPostIssueComment(unittest.TestCase):

    @patch("subprocess.run")
    def test_post_issue_comment_gh_success(self, mock_subproc):
        mock_subproc.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        res = post_issue_comment("owner/repo", 12, "Hello world")
        self.assertTrue(res)
        mock_subproc.assert_called_once()
        cmd = mock_subproc.call_args[0][0]
        body_idx = cmd.index("--body") + 1
        self.assertIn("Hello world", cmd[body_idx])
        self.assertIn("<!-- antigravity-auto-reply -->", cmd[body_idx])
        self.assertIn("<!-- graviton:release -->", cmd[body_idx])

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_post_issue_comment_urllib_fallback(self, mock_subproc, mock_urlopen):
        mock_subproc.side_effect = FileNotFoundError("gh not installed")
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with patch.dict("os.environ", {"GITHUB_TOKEN": "secret_token"}):
            res = post_issue_comment("owner/repo", 12, "Hello world")
            self.assertTrue(res)

    def test_post_issue_comment_empty_args(self):
        self.assertFalse(post_issue_comment("", 12, "text"))
        self.assertFalse(post_issue_comment("owner/repo", 0, "text"))
        self.assertFalse(post_issue_comment("owner/repo", 12, ""))


class TestReleaseBotMarkersAndFormatting(unittest.TestCase):

    def test_format_release_help_contains_bot_marker(self):
        body = format_release_help()
        self.assertIn("<!-- antigravity-auto-reply -->", body)
        self.assertIn("<!-- graviton:release -->", body)

    def test_format_release_init_contains_bot_marker(self):
        body = format_release_init()
        self.assertIn("<!-- antigravity-auto-reply -->", body)
        self.assertIn("<!-- graviton:release -->", body)

    @patch("lib.release.post_issue_comment")
    def test_post_release_help_async_contains_bot_marker(self, mock_comment):
        t = post_release_help_async("owner/repo", 42)
        t.join(timeout=2.0)
        mock_comment.assert_called_once()
        body = mock_comment.call_args[0][2]
        self.assertIn("<!-- antigravity-auto-reply -->", body)
        self.assertIn("<!-- graviton:release -->", body)

    @patch("lib.release.post_issue_comment")
    def test_post_release_init_async_contains_bot_marker(self, mock_comment):
        t = post_release_init_async("owner/repo", 42)
        t.join(timeout=2.0)
        mock_comment.assert_called_once()
        body = mock_comment.call_args[0][2]
        self.assertIn("<!-- antigravity-auto-reply -->", body)
        self.assertIn("<!-- graviton:release -->", body)

    @patch("lib.release.post_issue_comment")
    def test_post_release_unrecognized_async_contains_bot_marker(self, mock_comment):
        t = post_release_unrecognized_async("owner/repo", 42, "unknown_cmd")
        t.join(timeout=2.0)
        mock_comment.assert_called_once()
        body = mock_comment.call_args[0][2]
        self.assertIn("<!-- antigravity-auto-reply -->", body)
        self.assertIn("<!-- graviton:release -->", body)

    @patch("lib.release.post_issue_comment")
    def test_post_release_unrecognized_async_sanitizes_multiline(self, mock_comment):
        multiline_cmd = "first_line `with backticks`\nsecond_line\nthird_line"
        t = post_release_unrecognized_async("owner/repo", 42, multiline_cmd)
        t.join(timeout=2.0)
        mock_comment.assert_called_once()
        body = mock_comment.call_args[0][2]
        self.assertIn("Unrecognized release command: `first_line 'with backticks'`", body)
        self.assertNotIn("second_line", body.split("\n\n")[0])

    @patch("lib.release.post_issue_comment")
    def test_post_release_unrecognized_async_truncates_long_input(self, mock_comment):
        long_cmd = "a" * 120
        t = post_release_unrecognized_async("owner/repo", 42, long_cmd)
        t.join(timeout=2.0)
        mock_comment.assert_called_once()
        body = mock_comment.call_args[0][2]
        expected_sanitized = "a" * 77 + "..."
        self.assertIn(f"Unrecognized release command: `{expected_sanitized}`", body)

    @patch("subprocess.run")
    def test_post_issue_comment_appends_marker_and_does_not_duplicate(self, mock_subproc):
        mock_subproc.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

        # Plain message gets marker appended
        post_issue_comment("owner/repo", 12, "Message without marker")
        cmd_args = mock_subproc.call_args[0][0]
        body_idx = cmd_args.index("--body") + 1
        posted_body = cmd_args[body_idx]
        self.assertIn("<!-- antigravity-auto-reply -->", posted_body)
        self.assertIn("<!-- graviton:release -->", posted_body)

        # Message with existing marker is not duplicated
        mock_subproc.reset_mock()
        already_tagged = "Message with marker\n\n<!-- antigravity-auto-reply -->\n<!-- graviton:release -->"
        post_issue_comment("owner/repo", 12, already_tagged)
        cmd_args = mock_subproc.call_args[0][0]
        body_idx = cmd_args.index("--body") + 1
        posted_body = cmd_args[body_idx]
        self.assertEqual(posted_body.count("<!-- antigravity-auto-reply -->"), 1)


if __name__ == "__main__":
    unittest.main()
