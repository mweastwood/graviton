"""
Unit tests for lib/routers sub-modules.
"""

import unittest
from lib.routers import (
    _pr_review_timestamps,
    _pr_review_timestamps_lock,
)
from lib.routers.base import (
    clear_pr_review_cache,
    is_pr_created_by_us,
    has_explicit_command,
    _extract_repo_info,
    _build_accepted_response,
    get_server_repo_name,
)
from lib.routers.push_router import handle_ping_event, handle_push_event
from lib.routers.pr_router import (
    handle_pull_request_event,
    handle_pull_request_review_event,
    handle_pull_request_review_comment_event,
)
from lib.routers.issue_router import handle_issues_event, handle_issue_comment_event


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


