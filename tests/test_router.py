"""
Unit tests for lib/router.py
"""

import time
import unittest
from lib.router import (
    route_webhook_event,
    format_event_summary,
    is_pr_created_by_us,
    has_explicit_command,
    handle_ping_event,
    handle_push_event,
    handle_pull_request_event,
    handle_pull_request_review_event,
    handle_pull_request_review_comment_event,
    handle_issues_event,
    handle_issue_comment_event,
    clear_pr_review_cache,
)
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
        self.assertTrue(is_pr_created_by_us({"head": {"ref": "feat/my-feature"}}))
        self.assertTrue(is_pr_created_by_us({"head": {"ref": "fix/some-bug"}}))
        self.assertFalse(is_pr_created_by_us({"user": {"login": "external_dev", "type": "User"}, "head": {"ref": "patch-1"}}))

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
                "body": "Fix null pointer exception",
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

    def test_pull_request_review_bot_changes_requested_accepted(self):
        payload = {
            "action": "submitted",
            "review": {
                "state": "CHANGES_REQUESTED",
                "body": f"Automated review {BOT_MARKER}",
            },
            "pull_request": {"number": 15},
        }
        result = route_webhook_event("pull_request_review", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertEqual(result["pr_number"], 15)
        self.assertIn("Automated review", result["prompt"])

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

    def test_issues_labeled_ready_for_pr_triggers_drafter(self):
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
        self.assertEqual(result["agent"], "pr_drafter")
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

    def test_issue_comment_bot_comment_with_fix_command_accepted(self):
        payload = {
            "action": "created",
            "comment": {
                "body": f"/fix Action items required: please fix test failure {BOT_MARKER}",
            },
            "issue": {
                "number": 15,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/15"},
                "user": {"login": "antigravity-bot", "type": "Bot"},
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertEqual(result["pr_number"], 15)
        self.assertIn("Address comment on PR #15", result["prompt"])

    def test_issue_comment_bot_comment_without_command_ignored(self):
        payload = {
            "action": "created",
            "comment": {
                "body": f"Automated reply {BOT_MARKER}",
            },
            "issue": {
                "number": 15,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/15"},
                "user": {"login": "antigravity-bot", "type": "Bot"},
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "Bot comment dropped")

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

    def test_issue_comment_on_external_pr_with_feat_branch_ignored(self):
        payload = {
            "action": "created",
            "comment": {"body": "Looks suspicious."},
            "issue": {
                "number": 19,
                "pull_request": {
                    "url": "https://api.github.com/repos/org/repo/pulls/19",
                    "html_url": "https://github.com/org/repo/pull/19",
                },
                "user": {"login": "external_dev", "type": "User"},
                "head": {"ref": "feat/new-ui-theme"},
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["reason"], "PR was not created by us")

    def test_issue_comment_on_external_pr_with_fix_command_accepted(self):
        payload = {
            "action": "created",
            "comment": {"body": "/fix please resolve this crash"},
            "issue": {
                "number": 20,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/20"},
                "user": {"login": "external_dev", "type": "User"},
                "head": {"ref": "patch-1"},
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")
        self.assertIn("Address comment on PR #20", result["prompt"])

    def test_issue_comment_on_external_pr_with_antigravity_command_accepted(self):
        payload = {
            "action": "created",
            "comment": {"body": "Hey @antigravity please take a look"},
            "issue": {
                "number": 21,
                "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/21"},
                "user": {"login": "external_dev", "type": "User"},
                "head": {"ref": "patch-1"},
            },
        }
        result = route_webhook_event("issue_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")

    def test_pull_request_review_on_external_pr_with_fix_command_accepted(self):
        payload = {
            "action": "submitted",
            "review": {
                "state": "CHANGES_REQUESTED",
                "body": "/fix resolve missing error handling",
            },
            "pull_request": {
                "number": 22,
                "user": {"login": "external_dev", "type": "User"},
                "head": {"ref": "patch-1"},
            },
        }
        result = route_webhook_event("pull_request_review", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")

    def test_pull_request_review_comment_on_external_pr_with_fix_command_accepted(self):
        payload = {
            "action": "created",
            "comment": {
                "body": "/fix fix variable scope issue",
                "path": "lib/app.py",
                "line": 10,
            },
            "pull_request": {
                "html_url": "https://github.com/org/repo/pull/23",
                "user": {"login": "external_dev", "type": "User"},
                "head": {"ref": "patch-1"},
            },
        }
        result = route_webhook_event("pull_request_review_comment", payload)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["agent"], "code_fixer")

    def test_has_explicit_command_helper(self):
        self.assertTrue(has_explicit_command("/fix please address"))
        self.assertTrue(has_explicit_command("Hey @antigravity check this"))
        self.assertTrue(has_explicit_command("@Antigravity /fix"))
        self.assertFalse(has_explicit_command("Looks good to me"))
        self.assertFalse(has_explicit_command(""))

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

    def test_issue_comment_on_ready_issue_triggers_drafter(self):
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
        self.assertEqual(result["agent"], "pr_drafter")
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

    def test_modular_event_handlers_direct_invocation(self):
        """Test calling the individual modular handler functions directly."""
        # handle_ping_event
        res_ping = handle_ping_event({"zen": "Keep it simple."})
        self.assertEqual(res_ping["status"], "accepted")
        self.assertEqual(res_ping["zen"], "Keep it simple.")

        # handle_push_event
        res_push = handle_push_event({"ref": "refs/heads/main"})
        self.assertEqual(res_push["status"], "accepted")
        self.assertEqual(res_push["action"], "self_update")

        # handle_pull_request_event
        res_pr = handle_pull_request_event({"action": "opened", "number": 10}, default_reviewer="custom_reviewer")
        self.assertEqual(res_pr["status"], "accepted")
        self.assertEqual(res_pr["agent"], "custom_reviewer")

        # handle_pull_request_review_event
        res_pr_rev = handle_pull_request_review_event(
            {
                "action": "submitted",
                "review": {"state": "CHANGES_REQUESTED", "body": "Needs work"},
                "pull_request": {"number": 10},
            },
            default_fixer="custom_fixer",
        )
        self.assertEqual(res_pr_rev["status"], "accepted")
        self.assertEqual(res_pr_rev["agent"], "custom_fixer")

        # handle_pull_request_review_comment_event
        res_comment = handle_pull_request_review_comment_event(
            {
                "action": "created",
                "comment": {"body": "Fix typo", "path": "main.py", "line": 1},
                "pull_request": {"number": 10},
            },
            default_fixer="custom_fixer",
        )
        self.assertEqual(res_comment["status"], "accepted")
        self.assertEqual(res_comment["agent"], "custom_fixer")

        # handle_issues_event
        res_issues = handle_issues_event({"action": "opened", "issue": {"number": 5}}, default_triager="custom_triager")
        self.assertEqual(res_issues["status"], "accepted")
        self.assertEqual(res_issues["agent"], "custom_triager")

        res_issues_labeled = handle_issues_event(
            {"action": "labeled", "label": {"name": "ready-for-pr"}, "issue": {"number": 5}},
            default_drafter="custom_drafter",
        )
        self.assertEqual(res_issues_labeled["status"], "accepted")
        self.assertEqual(res_issues_labeled["agent"], "custom_drafter")

        # handle_issue_comment_event
        res_issue_comment = handle_issue_comment_event(
            {"action": "created", "comment": {"body": "Details"}, "issue": {"number": 5}},
            default_triager="custom_triager",
        )
        self.assertEqual(res_issue_comment["status"], "accepted")
        self.assertEqual(res_issue_comment["agent"], "custom_triager")

        res_issue_comment_draft = handle_issue_comment_event(
            {"action": "created", "comment": {"body": "/draft-pr"}, "issue": {"number": 5}},
            default_drafter="custom_drafter",
        )
        self.assertEqual(res_issue_comment_draft["status"], "accepted")
        self.assertEqual(res_issue_comment_draft["agent"], "custom_drafter")

    def test_router_updates_pr_tracker_on_approval_and_changes_requested(self):
        from lib.pr_tracker import PRTracker
        tracker = PRTracker()

        payload_approved = {
            "action": "submitted",
            "review": {"state": "APPROVED", "body": "LGTM!"},
            "pull_request": {
                "number": 42,
                "title": "Add PR tracking feature",
                "html_url": "https://github.com/mweastwood/graviton/pull/42",
                "user": {"login": "alice_reviewer"},
            },
        }

        route_webhook_event("pull_request_review", payload_approved, pr_tracker=tracker)
        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 42)
        self.assertEqual(approved[0]["title"], "Add PR tracking feature")
        self.assertEqual(approved[0]["author"], "alice_reviewer")
        self.assertEqual(approved[0]["url"], "https://github.com/mweastwood/graviton/pull/42")

        # Now send CHANGES_REQUESTED
        payload_changes = {
            "action": "submitted",
            "review": {"state": "CHANGES_REQUESTED", "body": "Needs fixes"},
            "pull_request": {
                "number": 42,
                "user": {"login": "antigravity-bot"},
            },
        }
        route_webhook_event("pull_request_review", payload_changes, pr_tracker=tracker)
        self.assertEqual(len(tracker.get_approved_prs()), 0)

    def test_router_updates_pr_tracker_on_bot_approval(self):
        from lib.pr_tracker import PRTracker
        tracker = PRTracker()

        payload_bot_approved = {
            "action": "submitted",
            "review": {
                "state": "APPROVED",
                "body": "LGTM! <!-- antigravity-auto-reply -->",
            },
            "pull_request": {
                "number": 59,
                "title": "Fix Empty Approved PRs Panel",
                "html_url": "https://github.com/mweastwood/graviton/pull/59",
                "user": {"login": "code_reviewer"},
            },
        }

        res = route_webhook_event("pull_request_review", payload_bot_approved, pr_tracker=tracker)
        self.assertEqual(res["status"], "ignored")
        self.assertEqual(res["reason"], "Bot self-review event dropped")

        approved = tracker.get_approved_prs()
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["number"], 59)
        self.assertEqual(approved[0]["title"], "Fix Empty Approved PRs Panel")
        self.assertEqual(approved[0]["author"], "code_reviewer")

    def test_router_updates_pr_tracker_on_review_dismissed(self):
        from lib.pr_tracker import PRTracker
        tracker = PRTracker()
        tracker.add_approved_pr(42, "PR Title", "author", "https://example.com/42")

        payload_dismissed = {
            "action": "dismissed",
            "review": {"state": "DISMISSED"},
            "pull_request": {
                "number": 42,
                "user": {"login": "antigravity-bot"},
            },
        }
        route_webhook_event("pull_request_review", payload_dismissed, pr_tracker=tracker)
        self.assertEqual(len(tracker.get_approved_prs()), 0)

    def test_router_updates_pr_tracker_on_pr_closed_or_merged(self):
        from lib.pr_tracker import PRTracker
        tracker = PRTracker()
        tracker.add_approved_pr(42, "PR Title", "author", "https://example.com/42")

        payload_closed = {
            "action": "closed",
            "number": 42,
            "pull_request": {"number": 42, "merged": True},
        }
        route_webhook_event("pull_request", payload_closed, pr_tracker=tracker)
        self.assertEqual(len(tracker.get_approved_prs()), 0)

    def test_pull_request_event_debouncing(self):
        clear_pr_review_cache()

        # 1. First event PR #77 'opened' -> accepted
        payload_opened = {"action": "opened", "number": 77}
        res1 = route_webhook_event("pull_request", payload_opened)
        self.assertEqual(res1["status"], "accepted")
        self.assertEqual(res1["action"], "opened")

        # 2. Rapid 'synchronize' event for PR #77 immediately following -> ignored / debounced
        payload_sync = {"action": "synchronize", "number": 77}
        res2 = route_webhook_event("pull_request", payload_sync)
        self.assertEqual(res2["status"], "ignored")
        self.assertIn("debounced", res2.get("reason", ""))

        # 3. 'synchronize' for a different PR #78 -> accepted
        payload_sync_other = {"action": "synchronize", "number": 78}
        res3 = route_webhook_event("pull_request", payload_sync_other)
        self.assertEqual(res3["status"], "accepted")

        # 4. Rapid second 'synchronize' for PR #78 -> debounced
        res4 = route_webhook_event("pull_request", payload_sync_other)
        self.assertEqual(res4["status"], "ignored")

        # 5. After debounce_window expires (using small window), synchronize should be accepted
        handle_pull_request_event(payload_sync, debounce_window=0.01)
        time.sleep(0.02)
        res6 = handle_pull_request_event(payload_sync, debounce_window=0.01)
        self.assertEqual(res6["status"], "accepted")

    def test_format_event_summary_pull_request(self):
        # pull_request opened
        p1 = {"action": "opened", "number": 12}
        self.assertEqual(format_event_summary("pull_request", p1), "PR #12 (action: opened)")

        # pull_request_review submitted
        p2 = {"action": "submitted", "pull_request": {"number": 15}}
        self.assertEqual(format_event_summary("pull_request_review", p2), "PR #15 (action: submitted)")

        # pull_request_review_comment created with html_url
        p3 = {"action": "created", "pull_request": {"html_url": "https://github.com/org/repo/pull/88"}}
        self.assertEqual(format_event_summary("pull_request_review_comment", p3), "PR #88 (action: created)")

        # pull_request without action
        p4 = {"number": 7}
        self.assertEqual(format_event_summary("pull_request", p4), "PR #7")

    def test_format_event_summary_issues(self):
        p1 = {"action": "opened", "issue": {"number": 62}}
        self.assertEqual(format_event_summary("issues", p1), "Issue #62 (action: opened)")

        p2 = {"action": "labeled", "issue": {"number": 55}, "label": {"name": "ready-for-pr"}}
        self.assertEqual(format_event_summary("issues", p2), "Issue #55 (action: labeled)")

        p3 = {"issue": {"number": 10}}
        self.assertEqual(format_event_summary("issues", p3), "Issue #10")

    def test_format_event_summary_issue_comment(self):
        # Pure issue comment
        p1 = {"action": "created", "issue": {"number": 62}}
        self.assertEqual(format_event_summary("issue_comment", p1), "Issue #62 (action: created)")

        # PR comment via issue_comment event
        p2 = {"action": "created", "issue": {"number": 12, "pull_request": {"url": "https://api.github.com/pulls/12"}}}
        self.assertEqual(format_event_summary("issue_comment", p2), "PR #12 (action: created)")

    def test_format_event_summary_push(self):
        p1 = {"ref": "refs/heads/main"}
        self.assertEqual(format_event_summary("push", p1), "Branch 'refs/heads/main'")

        p2 = {}
        self.assertEqual(format_event_summary("push", p2), "Push")

    def test_format_event_summary_ping(self):
        p1 = {"zen": "Non-blocking is better than blocking."}
        self.assertEqual(format_event_summary("ping", p1), "Ping")

    def test_format_event_summary_fallbacks(self):
        self.assertEqual(format_event_summary("custom_event", {"action": "sync"}), "custom_event (action: sync)")
        self.assertEqual(format_event_summary("custom_event", {}), "custom_event")
        self.assertEqual(format_event_summary("custom_event", None), "custom_event")


if __name__ == "__main__":
    unittest.main()
