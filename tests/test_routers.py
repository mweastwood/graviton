"""
Unit tests for lib/routers sub-modules.
"""

import unittest
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

    def test_issue_router_direct(self):
        payload = {"action": "opened", "issue": {"number": 10, "title": "Test Issue", "body": "Issue body"}}
        res = handle_issues_event(payload)
        self.assertEqual(res["status"], "accepted")
        self.assertEqual(res["agent"], "issue_triager")
