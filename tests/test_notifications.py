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
    def test_exception_handling(self, mock_post):
        mock_post.side_effect = Exception("API failure")
        task = MagicMock()
        task.repo_full_name = "owner/repo"
        task.target_id = "#1"

        self.assertFalse(post_task_start_comment(task))
        self.assertFalse(post_task_completion_comment(task))
