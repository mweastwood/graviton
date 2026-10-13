"""
Unit and integration tests for repository status polling, issue tracking,
dashboard section rendering, and server endpoints.
"""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.pr_tracker import PRTracker, RepoStatusTracker, RepoTracker
from lib.dashboard.markdown import format_dashboard_markdown
from lib.dashboard.parser import parse_dashboard_markdown
from lib.dashboard.html import render_dashboard_html


class TestPRTrackerRepoStatus(unittest.TestCase):
    def setUp(self):
        self.tracker = PRTracker()

    def test_open_prs_management(self):
        # Adding open PRs
        pr1 = {
            "number": 10,
            "title": "Add feature X",
            "repo_full_name": "owner/repo1",
            "author": "alice",
            "url": "https://github.com/owner/repo1/pull/10",
            "review_decision": "REVIEW_REQUIRED",
            "is_draft": False,
        }
        pr2 = {
            "number": 20,
            "title": "Fix bug Y",
            "repo_full_name": "owner/repo2",
            "author": "bob",
            "url": "https://github.com/owner/repo2/pull/20",
            "review_decision": "APPROVED",
            "is_draft": False,
        }
        self.tracker.add_open_pr(pr1)
        self.tracker.add_open_pr(pr2)

        all_prs = self.tracker.get_open_prs()
        self.assertEqual(len(all_prs), 2)

        repo1_prs = self.tracker.get_open_prs("owner/repo1")
        self.assertEqual(len(repo1_prs), 1)
        self.assertEqual(repo1_prs[0]["number"], 10)

        # Removing open PR
        self.tracker.remove_open_pr("owner/repo1", 10)
        self.assertEqual(len(self.tracker.get_open_prs("owner/repo1")), 0)
        self.assertEqual(len(self.tracker.get_open_prs()), 1)

    def test_open_issues_management(self):
        issue1 = {
            "number": 101,
            "title": "Bug in worker thread",
            "repo_full_name": "owner/repo1",
            "author": "charlie",
            "url": "https://github.com/owner/repo1/issues/101",
            "labels": ["bug", "high-priority"],
            "state": "OPEN",
        }
        issue2 = {
            "number": 102,
            "title": "Feature proposal: Webhook retry",
            "repo_full_name": "owner/repo2",
            "author": "david",
            "url": "https://github.com/owner/repo2/issues/102",
            "labels": ["enhancement"],
            "state": "OPEN",
        }
        self.tracker.add_open_issue(issue1)
        self.tracker.add_open_issue(issue2)

        all_issues = self.tracker.get_open_issues()
        self.assertEqual(len(all_issues), 2)

        repo1_issues = self.tracker.get_open_issues("owner/repo1")
        self.assertEqual(len(repo1_issues), 1)
        self.assertEqual(repo1_issues[0]["number"], 101)

        # Removing open issue
        self.tracker.remove_open_issue("owner/repo1", 101)
        self.assertEqual(len(self.tracker.get_open_issues("owner/repo1")), 0)
        self.assertEqual(len(self.tracker.get_open_issues()), 1)

    def test_get_repo_status_aggregation(self):
        self.tracker.add_open_pr({
            "number": 1,
            "title": "PR 1",
            "repo_full_name": "owner/repo1",
            "author": "alice",
            "url": "https://github.com/owner/repo1/pull/1",
        })
        self.tracker.add_approved_pr({
            "number": 2,
            "title": "Approved PR 2",
            "repo_full_name": "owner/repo1",
            "author": "bob",
            "url": "https://github.com/owner/repo1/pull/2",
        })
        self.tracker.add_open_issue({
            "number": 50,
            "title": "Issue 50",
            "repo_full_name": "owner/repo1",
            "author": "carol",
            "url": "https://github.com/owner/repo1/issues/50",
        })

        status = self.tracker.get_repo_status()
        self.assertIn("repos", status)
        self.assertIn("summary", status)
        self.assertEqual(status["summary"]["total_repos"], 1)
        self.assertEqual(status["summary"]["total_open_prs"], 1)
        self.assertEqual(status["summary"]["total_approved_prs"], 1)
        self.assertEqual(status["summary"]["total_open_issues"], 1)

        repo1 = status["repos"]["owner/repo1"]
        self.assertEqual(len(repo1["open_prs"]), 1)
        self.assertEqual(len(repo1["approved_prs"]), 1)
        self.assertEqual(len(repo1["open_issues"]), 1)

    @patch("subprocess.run")
    def test_sync_issues_single_repo(self, mock_run):
        mock_git_remote = MagicMock(returncode=0, stdout="https://github.com/test-owner/test-repo.git\n")
        raw_issues = [
            {
                "number": 42,
                "title": "Flaky test in task manager",
                "url": "https://github.com/test-owner/test-repo/issues/42",
                "author": {"login": "tester"},
                "labels": [{"name": "bug"}],
                "state": "OPEN",
                "createdAt": "2026-10-10T12:00:00Z",
                "updatedAt": "2026-10-11T12:00:00Z",
            }
        ]
        mock_gh = MagicMock(returncode=0, stdout=json.dumps(raw_issues))
        mock_run.side_effect = [mock_git_remote, mock_gh]

        repo_name, issues = self.tracker._sync_issues_directory(Path("/fake/repo"))
        self.assertEqual(repo_name, "test-owner/test-repo")
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["number"], 42)
        self.assertEqual(issues[0]["author"], "tester")
        self.assertEqual(issues[0]["labels"], ["bug"])

    @patch("subprocess.run")
    def test_sync_github_issues_caching(self, mock_run):
        mock_git_remote = MagicMock(returncode=0, stdout="git@github.com:org/repo.git\n")
        raw_issues = [{"number": 1, "title": "Issue 1", "url": "url", "author": {"login": "user"}, "labels": []}]
        mock_gh = MagicMock(returncode=0, stdout=json.dumps(raw_issues))
        mock_run.side_effect = [mock_git_remote, mock_gh]

        # First sync
        issues = self.tracker.sync_github_issues(repo_root=Path("/tmp"))
        self.assertEqual(len(issues), 1)
        self.assertEqual(mock_run.call_count, 2)

        # Second sync before TTL should hit cache
        cached = self.tracker.sync_github_issues(repo_root=Path("/tmp"))
        self.assertEqual(len(cached), 1)
        self.assertEqual(mock_run.call_count, 2)

        # Force sync bypasses cache
        mock_run.side_effect = [mock_git_remote, mock_gh]
        forced = self.tracker.sync_github_issues(repo_root=Path("/tmp"), force=True)
        self.assertEqual(len(forced), 1)
        self.assertEqual(mock_run.call_count, 4)

    def test_background_polling_lifecycle(self):
        sync_calls = []

        def on_sync_cb(status):
            sync_calls.append(status)

        with patch.object(self.tracker, "sync_repository_status") as mock_sync:
            mock_sync.return_value = {
                "repos": {},
                "summary": {"total_repos": 0, "total_open_prs": 0, "total_approved_prs": 0, "total_open_issues": 0},
            }

            self.assertFalse(self.tracker.is_polling())
            self.tracker.start_background_polling(poll_interval=0.05, on_sync=on_sync_cb)
            self.assertTrue(self.tracker.is_polling())

            # Allow loop to execute at least once
            time.sleep(0.15)

            self.tracker.stop_background_polling()
            self.assertFalse(self.tracker.is_polling())
            self.assertGreaterEqual(mock_sync.call_count, 1)
            self.assertGreaterEqual(len(sync_calls), 1)

    def test_tracker_aliases(self):
        self.assertIs(RepoTracker, PRTracker)
        self.assertIs(RepoStatusTracker, PRTracker)


class TestRepoStatusDashboard(unittest.TestCase):
    def test_markdown_and_parsing(self):
        extra_info = {
            "open_prs": [
                {
                    "repo_full_name": "owner/repo1",
                    "number": 15,
                    "title": "Support multi-repo polling",
                    "author": "alice",
                    "url": "https://github.com/owner/repo1/pull/15",
                    "review_decision": "APPROVED",
                    "is_draft": False,
                }
            ],
            "open_issues": [
                {
                    "repo_full_name": "owner/repo1",
                    "number": 88,
                    "title": "Need live polling dashboard",
                    "author": "bob",
                    "url": "https://github.com/owner/repo1/issues/88",
                    "labels": ["enhancement"],
                }
            ],
        }

        md = format_dashboard_markdown(host="127.0.0.1", port=8000, extra_info=extra_info)
        self.assertIn("## 📦 Repository Status", md)
        self.assertIn("Support multi-repo polling", md)
        self.assertIn("Need live polling dashboard", md)

        parsed = parse_dashboard_markdown(md, default_host="127.0.0.1", default_port=8000)
        self.assertEqual(len(parsed.get("open_prs", [])), 1)
        self.assertEqual(parsed["open_prs"][0]["number"], 15)
        self.assertEqual(len(parsed.get("open_issues", [])), 1)
        self.assertEqual(parsed["open_issues"][0]["number"], 88)

    def test_html_rendering(self):
        extra_info = {
            "open_prs": [
                {
                    "repo_full_name": "owner/repo1",
                    "number": 15,
                    "title": "Support multi-repo polling",
                    "author": "alice",
                    "url": "https://github.com/owner/repo1/pull/15",
                    "review_decision": "APPROVED",
                    "is_draft": False,
                }
            ],
            "open_issues": [
                {
                    "repo_full_name": "owner/repo1",
                    "number": 88,
                    "title": "Need live polling dashboard",
                    "author": "bob",
                    "url": "https://github.com/owner/repo1/issues/88",
                    "labels": ["enhancement"],
                }
            ],
        }

        md = format_dashboard_markdown(host="127.0.0.1", port=8000, extra_info=extra_info)
        html = render_dashboard_html(md, host="127.0.0.1", port=8000, extra_info=extra_info)

        self.assertIn("📦 Repository Status (PRs &amp; Issues)", html)
        self.assertIn("1 PRs", html)
        self.assertIn("1 Issues", html)
        self.assertIn("Support multi-repo polling", html)
        self.assertIn("Need live polling dashboard", html)


class TestRepoStatusServerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import importlib.util
        import sys
        repo_root = Path(__file__).resolve().parent.parent
        server_path = repo_root / "bin" / "graviton-server.py"
        spec = importlib.util.spec_from_file_location("graviton_server_test", server_path)
        cls.server_mod = importlib.util.module_from_spec(spec)
        sys.modules["graviton_server_test"] = cls.server_mod
        spec.loader.exec_module(cls.server_mod)
        cls.GravitonHandler = cls.server_mod.GravitonHandler

    def test_repo_status_endpoint_with_tracker(self):
        handler = MagicMock(spec=self.GravitonHandler)
        mock_pt = MagicMock(spec=PRTracker)
        expected_status = {
            "repos": {"owner/repo": {"open_prs": [], "approved_prs": [], "open_issues": []}},
            "summary": {"total_repos": 1, "total_open_prs": 0, "total_approved_prs": 0, "total_open_issues": 0},
        }
        mock_pt.get_repo_status.return_value = expected_status
        handler.pr_tracker = mock_pt
        handler.path = "/repo/status"

        self.GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once_with(200, expected_status)

    def test_repo_status_endpoint_without_tracker(self):
        handler = MagicMock(spec=self.GravitonHandler)
        handler.pr_tracker = None
        handler.path = "/repo/status"

        self.GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once()
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 200)
        self.assertIn("repos", body)
        self.assertIn("summary", body)
        self.assertEqual(body["summary"]["total_repos"], 0)

    def test_health_check_includes_repo_status(self):
        handler = MagicMock(spec=self.GravitonHandler)
        handler.task_manager = None
        handler.quota_tracker = None
        mock_pt = MagicMock(spec=PRTracker)
        mock_pt.get_repo_status.return_value = {
            "summary": {"total_repos": 3, "total_open_prs": 5, "total_approved_prs": 2, "total_open_issues": 7}
        }
        handler.pr_tracker = mock_pt
        handler.path = "/health"
        handler.default_reviewer = "code_reviewer"
        handler.default_fixer = "code_fixer"
        handler.default_triager = "issue_triager"
        handler.default_drafter = "pr_drafter"

        self.GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once()
        code, body = handler._send_json.call_args[0]
        self.assertEqual(code, 200)
        self.assertIn("repo_status", body)
        self.assertEqual(body["repo_status"]["summary"]["total_repos"], 3)


if __name__ == "__main__":
    unittest.main()
