"""
Unit tests for lib/router.py
"""

import unittest
from lib.router import route_webhook_event, is_pr_created_by_us
from lib.security import BOT_MARKER


class TestRouter(unittest.TestCase):

    def test_is_pr_created_by_us_helper(self):
        self.assertTrue(is_pr_created_by_us({}))
        self.assertTrue(is_pr_created_by_us({"number": 1}))
        self.assertTrue(is_pr_created_by_us({"user": {"login": "antigravity-bot", "type": "Bot"}}))
        self.assertTrue(is_pr_created_by_us({"body": f"PR description {BOT_MARKER}"}))
        self.assertTrue(is_pr_created_by_us({"head": {"ref": "fix/some-bug"}}))
        self.assertTrue(is_pr_created_by_us({"created_by_us": True}))

        self.assertFalse(is_pr_created_by_us({"created_by_us": False}))
        self.assertFalse(is_pr_created_by_us({"user": {"login": "external_dev", "type": "User"}}))
        self.assertFalse(is_pr_created_by_us({"user": {"login": "external_dev", "type": "User"}, "head": {"ref": "feat/my-feature"}}))
        self.assertFalse(is_pr_created_by_us({"user": {"login": "external_dev", "type": "User"}, "head": {"ref": "fix/some-bug"}}))

    def test_ping_event(self):
        payload = {"zen": "Non-blocking is better than blocking."}
        result = route_webhook_event("ping", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["action"], "ping")
        self.assertEqual(result["zen"], "Non-blocking is better than blocking.")

    def test_pull_request_opened(self):
        payload = {"action": "opened", "number": 42}
        result = route_webhook_event("pull_request", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_reviewer")
        self.assertEqual(result["pr_number"], 42)
        self.assertIn("Review PR #42", result["prompt"])

    def test_pull_request_synchronize(self):
        payload = {"action": "synchronize", "number": 101}
        result = route_webhook_event("pull_request", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_reviewer")

    def test_pull_request_closed_ignored(self):
        payload = {"action": "closed", "number": 42}
        result = route_webhook_event("pull_request", payload)
        self.assertEqual(result["status"], "ignored")

    def test_pull_request_review_changes_requested(self):
        payload = {
            "action": "submitted",
            "review": {
                "state": "CHANGES_REQUESTED",
                "body": f"Fix null pointer exception {BOT_MARKER}",
            },
            "pull_request": {"number": 15, "user": {"login": "antigravity-bot"}},
        }
        result = route_webhook_event("pull_request_review", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertIn("Fix null pointer exception", result["prompt"])

    def test_pull_request_review_not_created_by_us_ignored(self):
        payload = {
            "action": "submitted",
            "review": {
                "state": "CHANGES_REQUESTED",
                "body": "Fix null pointer exception",
            },
            "pull_request": {"number": 15, "user": {"login": "external_dev", "type": "User"}},
        }
        result = route_webhook_event("pull_request_review", payload)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "PR was not created by us")

    def test_pull_request_review_approved_ignored(self):
        payload = {
            "action": "submitted",
            "review": {"state": "APPROVED", "body": "Looks good to me!"},
            "pull_request": {"number": 15},
        }
        result = route_webhook_event("pull_request_review", payload)
        self.assertEqual(result["status"], "ignored")

    def test_pull_request_review_bot_comment_ignored(self):
        payload = {
            "action": "submitted",
            "review": {
                "state": "COMMENTED",
                "body": f"Automated reply {BOT_MARKER}",
            },
            "pull_request": {"number": 15},
        }
        result = route_webhook_event("pull_request_review", payload)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "Bot self-review event dropped")

    def test_pull_request_review_comment_created(self):
        payload = {
            "action": "created",
            "comment": {
                "body": "Use constant instead of hardcoded string",
                "path": "lib/server.py",
                "line": 42,
            },
            "pull_request": {
                "html_url": "https://github.com/org/repo/pull/88",
                "user": {"login": "antigravity-bot"},
            },
        }
        result = route_webhook_event("pull_request_review_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertEqual(result["file"], "lib/server.py")
        self.assertEqual(result["line"], 42)

    def test_pull_request_review_comment_not_created_by_us_ignored(self):
        payload = {
            "action": "created",
            "comment": {
                "body": "Use constant instead of hardcoded string",
                "path": "lib/server.py",
                "line": 42,
            },
            "pull_request": {
                "html_url": "https://github.com/org/repo/pull/88",
                "user": {"login": "external_dev", "type": "User"},
            },
        }
        result = route_webhook_event("pull_request_review_comment", payload)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "PR was not created by us")

    def test_pull_request_review_comment_bot_ignored(self):
        payload = {
            "action": "created",
            "comment": {
                "body": f"Fixed in commit abc {BOT_MARKER}",
                "path": "lib/server.py",
                "line": 42,
            },
            "pull_request": {"html_url": "https://github.com/org/repo/pull/88"},
        }
        result = route_webhook_event("pull_request_review_comment", payload)
        self.assertEqual(result["status"], "ignored")

    def test_issues_opened_triggers_triager(self):
        payload = {
            "action": "opened",
            "issue": {
                "number": 55,
                "title": "Add support for user profile avatars",
                "body": "Users should be able to upload PNG/JPG avatars.",
            },
        }
        result = route_webhook_event("issues", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "issue_triager")
        self.assertEqual(result["issue_number"], 55)
        self.assertIn("Triage Issue #55", result["prompt"])

    def test_issues_labeled_ready_for_pr_triggers_fixer(self):
        payload = {
            "action": "labeled",
            "label": {"name": "ready-for-pr"},
            "issue": {
                "number": 55,
                "title": "Add support for user profile avatars",
                "body": "Approved design spec.",
            },
        }
        result = route_webhook_event("issues", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertEqual(result["issue_number"], 55)
        self.assertIn("Draft initial PR", result["prompt"])

    def test_issues_labeled_other_ignored(self):
        payload = {
            "action": "labeled",
            "label": {"name": "bug"},
            "issue": {"number": 55},
        }
        result = route_webhook_event("issues", payload)
        self.assertEqual(result["status"], "ignored")

    def test_issue_comment_on_pr_human_comment_triggers_fixer(self):
        payload = {
            "action": "created",
            "comment": {"body": "Please add error handling here as well."},
            "issue": {
                "number": 12,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/12", "html_url": "https://github.com/org/repo/pull/12"},
                "user": {"login": "antigravity-bot", "type": "Bot"},
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertIn("Address comment on PR #12", result["prompt"])

    def test_issue_comment_on_pr_not_created_by_us_ignored(self):
        payload = {
            "action": "created",
            "comment": {"body": "Please add error handling here as well."},
            "issue": {
                "number": 12,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/12", "html_url": "https://github.com/org/repo/pull/12"},
                "user": {"login": "external_dev", "type": "User"},
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "PR was not created by us")

    def test_issue_comment_on_pure_issue_triggers_triager(self):
        payload = {
            "action": "created",
            "comment": {"body": "Here are the reproduction steps you asked for."},
            "issue": {"number": 55},  # No pull_request key
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "issue_triager")
        self.assertIn("Continue triage on Issue #55", result["prompt"])

    def test_issue_comment_on_ready_issue_triggers_fixer(self):
        payload = {
            "action": "created",
            "comment": {"body": "@antigravity /draft-pr start working"},
            "issue": {
                "number": 55,
                "labels": [{"name": "ready-for-pr"}],
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertIn("Draft initial PR for Issue #55", result["prompt"])

    def test_push_main_event(self):
        payload = {"ref": "refs/heads/main"}
        result = route_webhook_event("push", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["action"], "self_update")
        self.assertEqual(result["ref"], "refs/heads/main")

    def test_push_master_event(self):
        payload = {"ref": "refs/heads/master"}
        result = route_webhook_event("push", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["action"], "self_update")
        self.assertEqual(result["ref"], "refs/heads/master")

    def test_push_feature_branch_ignored(self):
        payload = {"ref": "refs/heads/feat/some-feature"}
        result = route_webhook_event("push", payload)
        self.assertEqual(result["status"], "ignored")

    def test_unknown_event_type(self):
        result = route_webhook_event("unknown_event", {})
        self.assertEqual(result["status"], "ignored")


if __name__ == "__main__":
    unittest.main()

