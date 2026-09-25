"""
Unit tests for lib.notifications module.
"""

import unittest
from unittest.mock import MagicMock, patch

from lib.notifications import post_task_completion_comment, post_task_start_comment


class TestNotifications(unittest.TestCase):
    @patch("lib.release.post_issue_comment")
    def test_post_task_start_comment_success(self, mock_post):
        mock_post.return_value = True
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "#123"
        task.agent = "code_fixer"
        task.conversation_id = "conv-1"
        task.remote_control_url = "https://rc.example.com/1"

        res = post_task_start_comment(task)
        self.assertTrue(res)
        mock_post.assert_called_once()
        repo, issue, body = mock_post.call_args[0][:3]
        self.assertEqual(repo, "owner/repo")
        self.assertEqual(issue, 123)
        self.assertIn("🚀 **Antigravity Agent `code_fixer` Started**", body)
        self.assertIn("- **Conversation ID**: `conv-1`", body)
        self.assertIn("https://rc.example.com/1", body)

    @patch("lib.release.post_issue_comment")
    def test_post_task_completion_comment_success(self, mock_post):
        mock_post.return_value = True
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "123"
        task.status = "COMPLETED"
        task.agent = "pr_reviewer"
        task.conversation_id = "conv-2"
        task.remote_control_url = "https://rc.example.com/2"
        task.elapsed_time = 42.5

        result = MagicMock()
        result.is_success = True
        result.response = "Detailed fix summary <!-- GOAL_COMPLETE -->"

        res = post_task_completion_comment(task, result=result)
        self.assertTrue(res)
        mock_post.assert_called_once()
        repo, issue, body = mock_post.call_args[0][:3]
        self.assertEqual(repo, "owner/repo")
        self.assertEqual(issue, 123)
        self.assertIn("✅ **Antigravity Agent `pr_reviewer` Finished**", body)
        self.assertIn("42.5s", body)
        self.assertIn("Detailed fix summary", body)
        self.assertNotIn("<!-- GOAL_COMPLETE -->", body)

    @patch("lib.release.post_issue_comment")
    def test_post_task_completion_comment_truncation(self, mock_post):
        mock_post.return_value = True
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "123"
        task.status = "COMPLETED"

        result = MagicMock()
        result.is_success = True
        result.response = "x" * 4000

        post_task_completion_comment(task, result=result)
        body = mock_post.call_args[0][2]
        self.assertIn("*(output truncated)*", body)

    def test_invalid_task_inputs(self):
        self.assertFalse(post_task_start_comment(None))
        self.assertFalse(post_task_completion_comment(None))

        task = MagicMock()
        task.repo_full_name = None
        task.target_id = "#1"
        self.assertFalse(post_task_start_comment(task))
        self.assertFalse(post_task_completion_comment(task))

        task.repo_full_name = "owner/repo"
        task.target_id = "invalid_no_num"
        self.assertFalse(post_task_start_comment(task))
        self.assertFalse(post_task_completion_comment(task))

    @patch("lib.release.post_issue_comment")
    def test_post_task_completion_comment_failed_result_precedence(self, mock_post):
        mock_post.return_value = True
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "123"
        task.status = "COMPLETED"
        task.agent = "agent"
        task.conversation_id = None
        task.remote_control_url = None

        result = MagicMock()
        result.is_success = False
        result.response = None
        result.conversation_id = None
        result.remote_control_url = None

        res = post_task_completion_comment(task, result=result)
        self.assertTrue(res)
        body = mock_post.call_args[0][2]
        self.assertIn("❌ **Antigravity Agent `agent` Finished**", body)

    @patch("lib.release.post_issue_comment")
    def test_post_task_completion_comment_failed_task_status_precedence(self, mock_post):
        mock_post.return_value = True
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "123"
        task.status = "FAILED"
        task.agent = "agent"

        result = MagicMock()
        result.is_success = True
        result.status = "SUCCESS"

        res = post_task_completion_comment(task, result=result)
        self.assertTrue(res)
        body = mock_post.call_args[0][2]
        self.assertIn("❌ **Antigravity Agent `agent` Finished**", body)

    @patch("lib.release.post_issue_comment")
    def test_post_task_completion_comment_failed_result_status_attr(self, mock_post):
        mock_post.return_value = True
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "123"
        task.status = "COMPLETED"
        task.agent = "agent"

        result = MagicMock()
        result.is_success = True
        result.status = "FAILED"

        res = post_task_completion_comment(task, result=result)
        self.assertTrue(res)
        body = mock_post.call_args[0][2]
        self.assertIn("❌ **Antigravity Agent `agent` Finished**", body)

    @patch("lib.release.post_issue_comment")
    def test_post_task_completion_comment_formatting_exception_handled(self, mock_post):
        bad_task = MagicMock()
        bad_task.repo_full_name = "owner/repo"
        bad_task.target_id = "#123"
        type(bad_task).agent = property(lambda self: 1 / 0)

        self.assertFalse(post_task_completion_comment(bad_task))
        self.assertFalse(post_task_start_comment(bad_task))

    def test_reexported_from_tasks(self):
        from lib.tasks import post_task_completion_comment as fn1, post_task_start_comment as fn2
        self.assertIs(fn1, post_task_completion_comment)
        self.assertIs(fn2, post_task_start_comment)

    @patch("lib.release.post_issue_comment")
    def test_exception_handling(self, mock_post):
        mock_post.side_effect = Exception("API failure")
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "#1"

        self.assertFalse(post_task_start_comment(task))
        self.assertFalse(post_task_completion_comment(task))
