"""
Integration/HTTP unit tests for bin/graviton-server.py
"""

import importlib.util
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "bin" / "graviton-server.py"

spec = importlib.util.spec_from_file_location("graviton_server", SERVER_SCRIPT)
server_mod = importlib.util.module_from_spec(spec)
sys.modules["graviton_server"] = server_mod
spec.loader.exec_module(server_mod)

GravitonHandler = server_mod.GravitonHandler


class TestGravitonHandler(unittest.TestCase):

    def setUp(self):
        GravitonHandler.secret = ""
        GravitonHandler.default_reviewer = "code_reviewer"
        GravitonHandler.default_fixer = "code_fixer"
        GravitonHandler.default_triager = "issue_triager"
        GravitonHandler.default_drafter = "pr_drafter"
        GravitonHandler.scheduler = None
        GravitonHandler.task_manager = None

    def test_health_check_endpoint(self):
        handler = MagicMock(spec=GravitonHandler)
        handler.task_manager = None
        handler.path = "/health"
        handler.default_reviewer = "code_reviewer"
        handler.default_fixer = "code_fixer"
        handler.default_triager = "issue_triager"
        handler.default_drafter = "pr_drafter"
        GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once()
        args, _ = handler._send_json.call_args
        self.assertEqual(args[0], 200)
        self.assertEqual(args[1]["status"], "ok")
        self.assertEqual(args[1]["drafter_agent"], "pr_drafter")
        self.assertFalse(args[1]["scheduler_enabled"])

    def test_health_check_endpoint_with_scheduler(self):
        mock_scheduler = MagicMock()
        mock_scheduler.is_running.return_value = True
        mock_scheduler.jobs = {"job1": MagicMock()}
        GravitonHandler.scheduler = mock_scheduler

        handler = MagicMock(spec=GravitonHandler)
        handler.path = "/health"
        GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once()
        args, _ = handler._send_json.call_args
        self.assertEqual(args[0], 200)
        self.assertTrue(args[1]["scheduler_enabled"])
        self.assertTrue(args[1]["scheduler_running"])
        self.assertEqual(args[1]["active_jobs"], 1)

    def test_not_found_endpoint(self):
        handler = MagicMock(spec=GravitonHandler)
        handler.task_manager = None
        handler.path = "/invalid"
        GravitonHandler.do_GET(handler)
        handler._send_json.assert_called_once_with(404, {"error": "Not Found"})

    @patch("graviton_server.run_agent_async")
    def test_do_post_valid_pr_event(self, mock_run_async):
        payload = json.dumps({"action": "opened", "number": 7}).encode("utf-8")
        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "pull_request",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_reviewer = "code_reviewer"
        handler.default_fixer = "code_fixer"
        handler.task_manager = None

        GravitonHandler.do_POST(handler)

        mock_run_async.assert_called_once()
        handler._send_json.assert_called_once()
        args, _ = handler._send_json.call_args
        self.assertEqual(args[0], 200)
        self.assertEqual(args[1]["status"], "accepted")
        self.assertEqual(args[1]["agent"], "code_reviewer")

    def test_do_post_with_task_manager(self):
        mock_tm = MagicMock()
        payload = json.dumps({"action": "opened", "number": 7}).encode("utf-8")
        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "pull_request",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_reviewer = "code_reviewer"
        handler.default_fixer = "code_fixer"
        handler.task_manager = mock_tm

        GravitonHandler.do_POST(handler)

        mock_tm.submit_task.assert_called_once_with(
            agent="code_reviewer",
            prompt="Review PR #7. Use --request-changes for any findings or code fixes.",
            target_id="#7",
        )
        handler._send_json.assert_called_once()

    @patch("graviton_server.post_emoji_reaction_async")
    @patch("graviton_server.run_agent_async")
    def test_do_post_triggers_emoji_reaction(self, mock_run_async, mock_post_reaction):
        payload = json.dumps({
            "action": "opened",
            "number": 7,
            "repository": {"full_name": "mweastwood/graviton"},
        }).encode("utf-8")
        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "pull_request",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_reviewer = "code_reviewer"
        handler.task_manager = None

        GravitonHandler.do_POST(handler)

        mock_post_reaction.assert_called_once_with("pull_request", {"action": "opened", "number": 7, "repository": {"full_name": "mweastwood/graviton"}})

    @patch("graviton_server.logger")
    def test_do_post_logging_enriched_context(self, mock_logger):
        payload = json.dumps({
            "action": "opened",
            "issue": {"number": 62, "title": "Enhance event logs", "body": "Details"}
        }).encode("utf-8")
        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "issues",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_triager = "issue_triager"
        handler.task_manager = None

        with patch("graviton_server.run_agent_async"):
            GravitonHandler.do_POST(handler)

        mock_logger.info.assert_any_call("Received GitHub webhook event: issues (Issue #62 (action: opened))")
        mock_logger.info.assert_any_call("Routed webhook event 'issues' (Issue #62 (action: opened)): status=accepted, agent=issue_triager")

    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_main_starts_dashboard_unconditionally(
        self, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_server = MagicMock()
        mock_http.return_value = mock_server
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        with patch("sys.argv", ["graviton-server.py"]):
            server_mod.main()

        mock_dashboard_cls.assert_called_once()
        mock_dashboard_inst.start.assert_called_once()
        mock_dashboard_inst.stop.assert_called_once()

    def test_cli_parser_rejects_dashboard_flag(self):
        with patch("sys.argv", ["graviton-server.py", "--dashboard"]):
            with patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    server_mod.main()

    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_cli_drafter_option_configures_handler(
        self, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_server = MagicMock()
        mock_http.return_value = mock_server
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        with patch("sys.argv", ["graviton-server.py", "--drafter", "custom_drafter"]):
            server_mod.main()

        self.assertEqual(GravitonHandler.default_drafter, "custom_drafter")


if __name__ == "__main__":
    unittest.main()

