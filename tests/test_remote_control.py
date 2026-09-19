"""
Unit tests for Antigravity Remote Control integration across Graviton components.
"""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from lib.mcp import GravitonMCPServer
from lib.supervisor import extract_remote_control_url, get_remote_control_instance_name
from lib.tasks import (
    Task,
    TaskManager,
    TaskStatus,
    post_task_completion_comment,
    post_task_start_comment,
)
from lib.tui import TerminalDashboard
from lib.tui_panels import render_task_logs_panel


class TestRemoteControlUrlExtraction(unittest.TestCase):
    def setUp(self):
        # Reset cached instance name
        import lib.supervisor as s
        s._CACHED_INSTANCE_NAME = None

    def test_extract_url_from_event_dict(self):
        event = {"remote_control_url": "https://antigravity.google.com/c/direct-url"}
        url = extract_remote_control_url(event)
        self.assertEqual(url, "https://antigravity.google.com/c/direct-url")

    def test_extract_url_from_stderr(self):
        stderr = "Starting session...\nRemote control active at: https://antigravity.google.com/c/abc-123\nReady."
        url = extract_remote_control_url(None, stderr_lines=stderr)
        self.assertEqual(url, "https://antigravity.google.com/c/abc-123")

    def test_fallback_url_synthesis_with_instance(self):
        url = extract_remote_control_url(
            None,
            conversation_id="conv-456",
            remote_control_enabled=True,
            instance_name="my-cloud-workstation",
        )
        self.assertEqual(
            url,
            "https://antigravity.google.com/c/conv-456?instance=my-cloud-workstation",
        )

    def test_fallback_url_synthesis_without_instance(self):
        url = extract_remote_control_url(
            None,
            conversation_id="conv-789",
            remote_control_enabled=True,
            instance_name="",
        )
        self.assertEqual(url, "https://antigravity.google.com/c/conv-789")

    def test_no_url_when_remote_control_disabled(self):
        url = extract_remote_control_url(
            None,
            conversation_id="conv-789",
            remote_control_enabled=False,
        )
        self.assertIsNone(url)

    @patch.dict("os.environ", {"ANTIGRAVITY_INSTANCE_NAME": "env-instance"})
    def test_get_remote_control_instance_name_from_env(self):
        import lib.supervisor as s
        s._CACHED_INSTANCE_NAME = None
        self.assertEqual(get_remote_control_instance_name(), "env-instance")

    @patch.dict("os.environ", {}, clear=True)
    @patch("lib.supervisor.subprocess.run")
    def test_get_remote_control_instance_name_from_cli(self, mock_run):
        import lib.supervisor as s
        s._CACHED_INSTANCE_NAME = None
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Status: ACTIVE\nInstance Name: cli-instance-alpha\nPort: 443\n",
        )
        name = get_remote_control_instance_name()
        self.assertEqual(name, "cli-instance-alpha")


class TestRemoteControlPRComments(unittest.TestCase):
    @patch("lib.release.post_issue_comment")
    def test_post_task_start_comment_with_remote_url(self, mock_post):
        mock_post.return_value = True
        task = Task(
            id="task-rc-1",
            agent="pr_reviewer",
            prompt="Review changes",
            repo_full_name="mweastwood/graviton",
            target_id="#99",
            selected_model="gemini-3.6-flash-high",
            remote_control_url="https://antigravity.google.com/c/rc-start-123",
        )
        success = post_task_start_comment(task)
        self.assertTrue(success)
        mock_post.assert_called_once()
        repo, issue_num, body = mock_post.call_args[0][:3]
        self.assertEqual(repo, "mweastwood/graviton")
        self.assertEqual(issue_num, 99)
        self.assertIn("Antigravity Agent `pr_reviewer` Started", body)
        self.assertIn("https://antigravity.google.com/c/rc-start-123", body)

    @patch("lib.release.post_issue_comment")
    def test_post_task_completion_comment_with_remote_url(self, mock_post):
        mock_post.return_value = True
        task = Task(
            id="task-rc-2",
            agent="pr_reviewer",
            prompt="Review changes",
            repo_full_name="mweastwood/graviton",
            target_id="#99",
            selected_model="gemini-3.6-flash-high",
            status=TaskStatus.COMPLETED,
            remote_control_url="https://antigravity.google.com/c/rc-comp-456",
        )
        success = post_task_completion_comment(task)
        self.assertTrue(success)
        mock_post.assert_called_once()
        repo, issue_num, body = mock_post.call_args[0][:3]
        self.assertEqual(repo, "mweastwood/graviton")
        self.assertEqual(issue_num, 99)
        self.assertIn("Antigravity Agent `pr_reviewer` Finished", body)
        self.assertIn("https://antigravity.google.com/c/rc-comp-456", body)


class TestTaskManagerRemoteControl(unittest.TestCase):
    def test_task_to_dict_includes_remote_control_url(self):
        task = Task(
            id="t-1",
            agent="code_reviewer",
            prompt="Check code",
            remote_control_url="https://antigravity.google.com/c/conv-xyz",
        )
        d = task.to_dict()
        self.assertEqual(d["remote_control_url"], "https://antigravity.google.com/c/conv-xyz")

    def test_dump_and_restore_queue_state_with_remote_control(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "state.json"
            tm1 = TaskManager(max_workers=0)
            t1 = tm1.submit_task("code_reviewer", "Review PR #10", target_id="#10")
            t1.remote_control_url = "https://antigravity.google.com/c/conv-restored"
            tm1.dump_queue_state(filepath=state_file)
            tm1.stop()

            tm2 = TaskManager(max_workers=0)
            restored_count = tm2.restore_queue_state(filepath=state_file)
            self.assertEqual(restored_count, 1)
            queued = tm2.get_queued_tasks()
            self.assertEqual(queued[0].remote_control_url, "https://antigravity.google.com/c/conv-restored")
            tm2.stop()

    def test_get_stats_active_remote_control_urls(self):
        tm = TaskManager(max_workers=2)
        task = Task(
            id="t-active",
            agent="code_reviewer",
            prompt="Active review",
            status=TaskStatus.RUNNING,
            remote_control_url="https://antigravity.google.com/c/conv-active",
        )
        with tm._lock:
            tm._tasks["t-active"] = task

        stats = tm.get_stats()
        self.assertIn("active_remote_control_urls", stats)
        self.assertEqual(
            stats["active_remote_control_urls"],
            {"t-active": "https://antigravity.google.com/c/conv-active"},
        )
        tm.stop()


class TestMCPRemoteControlTools(unittest.TestCase):
    def setUp(self):
        self.server = GravitonMCPServer(host="127.0.0.1", port=8000, auto_start_sidecar=False)

    @patch.object(GravitonMCPServer, "_http_request")
    def test_mcp_tools_display_remote_control(self, mock_http):
        # 1. Test graviton_status
        mock_http.return_value = (
            200,
            {
                "service": "graviton-server",
                "status": "ok",
                "tasks": {"active_workers": 1, "max_workers": 2, "active_tasks": 1, "queued_tasks": 0},
                "active_remote_control_urls": {"task-mcp-1": "https://antigravity.google.com/c/conv-mcp"},
            },
        )
        req_status = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "graviton_status", "arguments": {}},
        }
        resp = self.server.handle_request(req_status)
        content = resp["result"]["content"][0]["text"]
        self.assertIn("Active Remote Control Links", content)
        self.assertIn("https://antigravity.google.com/c/conv-mcp", content)

        # 2. Test graviton_list_tasks
        mock_http.return_value = (
            200,
            {
                "active": [
                    {
                        "id": "task-mcp-1",
                        "agent": "code_reviewer",
                        "target_id": "repo#10",
                        "elapsed_time": 12.0,
                        "remote_control_url": "https://antigravity.google.com/c/conv-mcp",
                    }
                ],
                "queued": [],
                "history": [],
            },
        )
        req_list = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "graviton_list_tasks", "arguments": {}},
        }
        resp = self.server.handle_request(req_list)
        content = resp["result"]["content"][0]["text"]
        self.assertIn("[Live: https://antigravity.google.com/c/conv-mcp]", content)

        # 3. Test graviton_get_task
        mock_http.return_value = (
            200,
            {
                "id": "task-mcp-1",
                "status": "RUNNING",
                "agent": "code_reviewer",
                "remote_control_url": "https://antigravity.google.com/c/conv-mcp",
                "logs": [],
                "thoughts": [],
                "tool_calls": [],
            },
        )
        req_get = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "graviton_get_task", "arguments": {"task_id": "task-mcp-1"}},
        }
        resp = self.server.handle_request(req_get)
        content = resp["result"]["content"][0]["text"]
        self.assertIn("- **Remote Control**: https://antigravity.google.com/c/conv-mcp", content)


class TestTUIRemoteControl(unittest.TestCase):
    def test_render_task_logs_panel_with_remote_control(self):
        task = Task(
            id="t-tui",
            agent="code_reviewer",
            prompt="Check logs",
            status=TaskStatus.RUNNING,
            remote_control_url="https://antigravity.google.com/c/conv-tui",
        )
        rendered = render_task_logs_panel(width=100, task=task, logs=["output line"])
        self.assertTrue(any("https://antigravity.google.com/c/conv-tui" in l for l in rendered))

    @patch("webbrowser.open")
    def test_dashboard_open_selected_task_remote_control(self, mock_open):
        mock_tm = MagicMock()
        task = Task(
            id="t-open",
            agent="code_reviewer",
            prompt="Check logs",
            status=TaskStatus.RUNNING,
            remote_control_url="https://antigravity.google.com/c/conv-open",
        )
        mock_tm.get_active_tasks.return_value = [task]
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task.return_value = task

        dashboard = TerminalDashboard(task_manager=mock_tm)
        dashboard.focused_panel = "active"
        dashboard.selected_active_index = 0

        self.assertEqual(dashboard.selected_task, task)
        success = dashboard.open_selected_task_remote_control()
        self.assertTrue(success)
        mock_open.assert_called_once_with("https://antigravity.google.com/c/conv-open")


if __name__ == "__main__":
    unittest.main()
