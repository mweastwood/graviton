"""
Unit tests for lib/pr_tracker.py and webhook PRTracker integration.
"""

import unittest
from unittest.mock import MagicMock, patch

from lib.pr_tracker import PRTracker
from lib.router import route_webhook_event


class TestPRTracker(unittest.TestCase):

    def setUp(self):
        self.tracker = PRTracker()

    def test_mark_approved_and_get_approved_prs(self):
        self.tracker.mark_approved(
            pr_number=42,
            title="feat: add approved PR panel",
            author="mweastwood",
            url="https://github.com/mweastwood/graviton/pull/42",
        )
        self.tracker.mark_approved(
            pr_number=10,
            title="fix: resolve memory leak",
            author="contributor",
            url="https://github.com/mweastwood/graviton/pull/10",
        )

        approved = self.tracker.get_approved_prs()
        self.assertEqual(len(approved), 2)
        # Verify sorting by PR number ascending
        self.assertEqual(approved[0]["number"], 10)
        self.assertEqual(approved[1]["number"], 42)
        self.assertEqual(approved[1]["author"], "mweastwood")
        self.assertEqual(approved[1]["title"], "feat: add approved PR panel")

    def test_remove_pr_and_mark_changes_requested(self):
        self.tracker.mark_approved(42, "feat: awesome", "author", "https://url")
        self.assertEqual(len(self.tracker.get_approved_prs()), 1)

        self.tracker.mark_changes_requested(42)
        self.assertEqual(len(self.tracker.get_approved_prs()), 0)

        self.tracker.mark_approved(15, "feat: test", "author", "https://url")
        self.assertEqual(len(self.tracker.get_approved_prs()), 1)

        self.tracker.remove_pr(15)
        self.assertEqual(len(self.tracker.get_approved_prs()), 0)

    def test_webhook_event_approved_pr_flow(self):
        # 1. pull_request_review APPROVED event adds PR to tracker
        payload_approved = {
            "action": "submitted",
            "review": {"state": "APPROVED"},
            "pull_request": {
                "number": 42,
                "title": "Display approved PRs in TUI",
                "user": {"login": "mweastwood"},
                "html_url": "https://github.com/mweastwood/graviton/pull/42",
            },
        }
        res1 = route_webhook_event("pull_request_review", payload_approved, pr_tracker=self.tracker)
        approved = self.tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 42)
        self.assertEqual(approved[0]["author"], "mweastwood")
        self.assertEqual(approved[0]["title"], "Display approved PRs in TUI")

        # 2. pull_request_review CHANGES_REQUESTED event removes PR from tracker
        payload_changes = {
            "action": "submitted",
            "review": {"state": "CHANGES_REQUESTED", "body": "Please add tests"},
            "pull_request": {
                "number": 42,
                "user": {"login": "mweastwood"},
            },
        }
        res2 = route_webhook_event("pull_request_review", payload_changes, pr_tracker=self.tracker)
        self.assertEqual(len(self.tracker.get_approved_prs()), 0)

    def test_webhook_event_closed_pr_flow(self):
        self.tracker.mark_approved(42, "PR Title", "Author", "https://url")
        self.assertEqual(len(self.tracker.get_approved_prs()), 1)

        payload_closed = {
            "action": "closed",
            "pull_request": {
                "number": 42,
                "merged": True,
            },
        }
        route_webhook_event("pull_request", payload_closed, pr_tracker=self.tracker)
        self.assertEqual(len(self.tracker.get_approved_prs()), 0)

    @patch("subprocess.run")
    def test_sync_from_gh(self, mock_run):
        mock_output = """[
            {
                "number": 35,
                "title": "feat: add feature",
                "author": {"login": "dev_user"},
                "url": "https://github.com/mweastwood/graviton/pull/35",
                "reviewDecision": "APPROVED",
                "isDraft": false
            },
            {
                "number": 36,
                "title": "draft feature",
                "author": "dev_user2",
                "url": "https://github.com/mweastwood/graviton/pull/36",
                "reviewDecision": "APPROVED",
                "isDraft": true
            },
            {
                "number": 37,
                "title": "unapproved feature",
                "author": "dev_user3",
                "url": "https://github.com/mweastwood/graviton/pull/37",
                "reviewDecision": "REVIEW_REQUIRED",
                "isDraft": false
            }
        ]"""
        mock_res = MagicMock()
        mock_res.stdout = mock_output
        mock_run.return_value = mock_res

        self.tracker.sync_from_gh()
        approved = self.tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 35)
        self.assertEqual(approved[0]["author"], "dev_user")
        self.assertEqual(approved[0]["title"], "feat: add feature")


if __name__ == "__main__":
    unittest.main()
