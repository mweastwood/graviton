"""
Unit tests for lib/pr_tracker.py (PRTracker).
"""

import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.pr_tracker import PRTracker


class TestPRTracker(unittest.TestCase):

    def test_add_and_remove_approved_pr(self):
        tracker = PRTracker()
        self.assertEqual(tracker.get_approved_prs(), [])

        # Add PR #42 with string author and int number
        tracker.add_approved_pr(
            number=42,
            title="Add feature X",
            author="mweastwood",
            url="https://github.com/mweastwood/graviton/pull/42",
        )

        # Add PR #10 with dict author and string number
        tracker.add_approved_pr(
            number="10",
            title="Fix bug Y",
            author={"login": "contributor_bob"},
            url="https://github.com/mweastwood/graviton/pull/10",
        )

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 2)
        # Should be sorted by PR number ascending
        self.assertEqual(approved[0]["number"], 10)
        self.assertEqual(approved[0]["author"], "contributor_bob")
        self.assertEqual(approved[1]["number"], 42)
        self.assertEqual(approved[1]["author"], "mweastwood")

        # Remove PR #10
        tracker.remove_approved_pr(10)
        approved_after = tracker.get_approved_prs()
        self.assertEqual(len(approved_after), 1)
        self.assertEqual(approved_after[0]["number"], 42)

        # Remove invalid PR (non-existent or invalid type)
        tracker.remove_approved_pr(999)
        tracker.remove_approved_pr("invalid")
        self.assertEqual(len(tracker.get_approved_prs()), 1)

        # Add invalid PR number (should be ignored safely)
        tracker.add_approved_pr(number="invalid_num", title="Bad number PR")
        self.assertEqual(len(tracker.get_approved_prs()), 1)

    def test_thread_safety(self):
        tracker = PRTracker()
        threads = []

        def add_prs(start_num):
            for i in range(start_num, start_num + 50):
                tracker.add_approved_pr(
                    number=i,
                    title=f"PR #{i}",
                    author="user",
                    url=f"https://github.com/org/repo/pull/{i}",
                )

        def remove_prs(start_num):
            for i in range(start_num, start_num + 25):
                tracker.remove_approved_pr(i)

        for s in [100, 200, 300]:
            t1 = threading.Thread(target=add_prs, args=(s,))
            t2 = threading.Thread(target=remove_prs, args=(s,))
            threads.extend([t1, t2])

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        approved = tracker.get_approved_prs()
        self.assertTrue(all(isinstance(p["number"], int) for p in approved))

    @patch("subprocess.run")
    def test_sync_github_prs_success(self, mock_run):
        mock_output = [
            {
                "number": 42,
                "title": "Approved PR",
                "url": "https://github.com/org/repo/pull/42",
                "author": {"login": "alice"},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            },
            {
                "number": 43,
                "title": "Draft Approved PR",
                "url": "https://github.com/org/repo/pull/43",
                "author": {"login": "bob"},
                "reviewDecision": "APPROVED",
                "isDraft": True,
            },
            {
                "number": 44,
                "title": "Pending Review PR",
                "url": "https://github.com/org/repo/pull/44",
                "author": {"login": "charlie"},
                "reviewDecision": "REVIEW_REQUIRED",
                "isDraft": False,
            },
        ]
        mock_res = MagicMock()
        mock_res.stdout = json.dumps(mock_output)
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 42)
        self.assertEqual(approved[0]["author"], "alice")

    @patch("subprocess.run")
    def test_sync_github_prs_with_latest_reviews_approval(self, mock_run):
        mock_output = [
            {
                "number": 59,
                "title": "Approved PR via latestReviews",
                "url": "https://github.com/org/repo/pull/59",
                "author": {"login": "bot_reviewer"},
                "reviewDecision": "",
                "isDraft": False,
                "latestReviews": [
                    {"state": "APPROVED", "author": {"login": "bot_reviewer"}}
                ],
            },
        ]
        mock_res = MagicMock()
        mock_res.stdout = json.dumps(mock_output)
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 59)
        self.assertEqual(approved[0]["author"], "bot_reviewer")

        cmd = mock_run.call_args_list[-1][0][0]
        self.assertIn("number,title,url,author,reviewDecision,isDraft,latestReviews", cmd)

    @patch("subprocess.run")
    def test_sync_github_prs_with_latest_reviews_changes_requested_ignored(self, mock_run):
        mock_output = [
            {
                "number": 60,
                "title": "PR with mixed reviews",
                "url": "https://github.com/org/repo/pull/60",
                "author": {"login": "dev"},
                "reviewDecision": "",
                "isDraft": False,
                "latestReviews": [
                    {"state": "APPROVED", "author": {"login": "reviewer1"}},
                    {"state": "CHANGES_REQUESTED", "author": {"login": "reviewer2"}},
                ],
            },
        ]
        mock_res = MagicMock()
        mock_res.stdout = json.dumps(mock_output)
        mock_res.returncode = 0
        mock_run.return_value = mock_res

        tracker = PRTracker()
        tracker.sync_github_prs(repo_root=Path("/tmp"))

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 0)

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh"))
    def test_sync_github_prs_handles_exception(self, mock_run):
        tracker = PRTracker()
        tracker.add_approved_pr(1, "Existing PR", "dev", "https://example.com/1")
        
        # Sync fails, existing state should not crash
        tracker.sync_github_prs()
        self.assertEqual(len(tracker.get_approved_prs()), 1)

    def test_multi_repo_approved_prs_tracking(self):
        tracker = PRTracker()
        tracker.add_approved_pr(42, "Feature Alpha", "alice", "https://github.com/owner/repo-alpha/pull/42", repo_full_name="owner/repo-alpha")
        tracker.add_approved_pr(42, "Feature Beta", "bob", "https://github.com/owner/repo-beta/pull/42", repo_full_name="owner/repo-beta")

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 2)
        self.assertEqual(approved[0]["repo_full_name"], "owner/repo-alpha")
        self.assertEqual(approved[0]["number"], 42)
        self.assertEqual(approved[1]["repo_full_name"], "owner/repo-beta")
        self.assertEqual(approved[1]["number"], 42)

        # Remove PR #42 specifically for owner/repo-alpha
        tracker.remove_approved_pr(42, repo_full_name="owner/repo-alpha")
        remaining = tracker.get_approved_prs()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["repo_full_name"], "owner/repo-beta")

    @patch("subprocess.run")
    def test_sync_github_prs_removesuffix_git(self, mock_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "reddit"
            repo_dir.mkdir()
            (repo_dir / ".git").mkdir()

            mock_git_res = MagicMock()
            mock_git_res.returncode = 0
            mock_git_res.stdout = "https://github.com/owner/reddit.git\n"

            mock_gh_res = MagicMock()
            mock_gh_res.returncode = 0
            mock_gh_res.stdout = json.dumps([{
                "number": 1,
                "title": "PR 1",
                "url": "https://github.com/owner/reddit/pull/1",
                "author": {"login": "dev"},
                "reviewDecision": "APPROVED",
                "isDraft": False,
            }])

            mock_run.side_effect = [mock_git_res, mock_gh_res]

            tracker = PRTracker()
            tracker.sync_github_prs(repos_dir=Path(tmpdir))

            approved = tracker.get_approved_prs()
            self.assertEqual(len(approved), 1)
            self.assertEqual(approved[0]["repo_full_name"], "owner/reddit")
            self.assertEqual(approved[0]["number"], 1)


if __name__ == "__main__":
    unittest.main()
