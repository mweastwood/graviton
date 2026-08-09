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

    @patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "gh"))
    def test_sync_github_prs_handles_exception(self, mock_run):
        tracker = PRTracker()
        tracker.add_approved_pr(1, "Existing PR", "dev", "https://example.com/1")
        
        # Sync fails, existing state should not crash
        tracker.sync_github_prs()
        self.assertEqual(len(tracker.get_approved_prs()), 1)


if __name__ == "__main__":
    unittest.main()
