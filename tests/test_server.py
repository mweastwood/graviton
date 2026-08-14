"""
Integration/HTTP unit tests for bin/graviton-server.py
"""

import importlib.util
import json
import signal
import subprocess
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
        server_mod._is_shutting_down = False
        server_mod._shutdown_thread = None
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
            repo_full_name=None,
            repo_name=None,
            clone_url=None,
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
        handler.repos_dir = None
        handler.quota_tracker = None

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

    def test_do_post_with_multi_repo_task_manager(self):
        mock_tm = MagicMock()
        payload = json.dumps({
            "action": "opened",
            "number": 12,
            "repository": {
                "name": "repo-alpha",
                "full_name": "owner/repo-alpha",
                "clone_url": "https://github.com/owner/repo-alpha.git",
            },
        }).encode("utf-8")

        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "pull_request",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_reviewer = "code_reviewer"
        handler.task_manager = mock_tm

        GravitonHandler.do_POST(handler)

        mock_tm.submit_task.assert_called_once_with(
            agent="code_reviewer",
            prompt="Review PR #12 in owner/repo-alpha. Use --request-changes for any findings or code fixes.",
            target_id="#12",
            repo_full_name="owner/repo-alpha",
            repo_name="repo-alpha",
            clone_url="https://github.com/owner/repo-alpha.git",
        )

    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_cli_repos_dir_defaults_to_starting_directory(
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

        self.assertEqual(GravitonHandler.repos_dir, Path("~/graviton-repos").expanduser().resolve())

    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_cli_projects_dir_option(
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

        with patch("sys.argv", ["graviton-server.py", "--projects-dir", "/tmp/custom_projects"]):
            server_mod.main()

        self.assertEqual(GravitonHandler.repos_dir, Path("/tmp/custom_projects").resolve())

    @patch("graviton_server.post_emoji_reaction_async")
    @patch("subprocess.run")
    @patch("graviton_server.run_agent_async")
    def test_do_post_direct_execution_auto_clones_missing_repo(self, mock_run_async, mock_sub_run, mock_post_reaction):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            expected_repo_dir = repos_dir / "repo-alpha"

            def mock_sub_run_impl(cmd, **kwargs):
                if "clone" in cmd:
                    expected_repo_dir.mkdir(parents=True, exist_ok=True)
                res = MagicMock()
                res.returncode = 0
                res.stdout = ""
                return res

            mock_sub_run.side_effect = mock_sub_run_impl

            payload = json.dumps({
                "action": "opened",
                "number": 12,
                "repository": {
                    "name": "repo-alpha",
                    "full_name": "owner/repo-alpha",
                    "clone_url": "https://github.com/owner/repo-alpha.git",
                },
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
            handler.repos_dir = repos_dir

            GravitonHandler.do_POST(handler)

            mock_sub_run.assert_any_call(
                ["git", "clone", "--", "https://github.com/owner/repo-alpha.git", str(expected_repo_dir)],
                check=True,
                capture_output=True,
                text=True,
            )
            mock_run_async.assert_called_once()
            handler._send_json.assert_called_once()
            args, _ = handler._send_json.call_args
            self.assertEqual(args[0], 200)

    @patch("subprocess.run")
    @patch("graviton_server.run_agent_async")
    def test_do_post_direct_execution_clone_failure_returns_500(self, mock_run_async, mock_sub_run):
        import tempfile
        import subprocess
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            mock_sub_run.side_effect = subprocess.CalledProcessError(1, ["git", "clone"], stderr="Clone error")

            payload = json.dumps({
                "action": "opened",
                "number": 12,
                "repository": {
                    "name": "repo-alpha",
                    "full_name": "owner/repo-alpha",
                    "clone_url": "https://github.com/owner/repo-alpha.git",
                },
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
            handler.repos_dir = repos_dir

            GravitonHandler.do_POST(handler)

            mock_run_async.assert_not_called()
            handler._send_json.assert_called_once()
            args, _ = handler._send_json.call_args
            self.assertEqual(args[0], 500)
            self.assertIn("Failed to auto-clone repository", args[1]["error"])

    @patch("graviton_server.run_agent_async")
    def test_do_post_direct_execution_non_existent_repo_returns_400(self, mock_run_async):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"

            payload = json.dumps({
                "action": "opened",
                "number": 12,
                "repository": {
                    "name": "repo-alpha",
                },
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
            handler.repos_dir = repos_dir

            GravitonHandler.do_POST(handler)

            mock_run_async.assert_not_called()
            handler._send_json.assert_called_once()
            args, _ = handler._send_json.call_args
            self.assertEqual(args[0], 400)
            self.assertIn("does not exist", args[1]["error"])

    @patch("graviton_server.run_agent_async")
    def test_do_post_direct_execution_path_traversal_rejected(self, mock_run_async):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            repos_dir.mkdir()

            payload = json.dumps({
                "action": "opened",
                "number": 12,
                "repository": {
                    "name": "/tmp/bad",
                },
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
            handler.repos_dir = repos_dir

            GravitonHandler.do_POST(handler)

            mock_run_async.assert_not_called()
            handler._send_json.assert_called_once()
            args, _ = handler._send_json.call_args
            self.assertEqual(args[0], 400)
            self.assertIn("attempting path traversal", args[1]["error"])

    @patch("graviton_server.run_agent_async")
    def test_do_post_direct_execution_valid_repo_name(self, mock_run_async):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            repos_dir.mkdir()
            bad_dir = repos_dir / "bad"
            bad_dir.mkdir()

            payload = json.dumps({
                "action": "opened",
                "number": 12,
                "repository": {
                    "name": "bad",
                },
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
            handler.repos_dir = repos_dir

            GravitonHandler.do_POST(handler)

            mock_run_async.assert_called_once()
            exec_cwd = mock_run_async.call_args[0][3]
            self.assertEqual(exec_cwd.resolve(), bad_dir.resolve())

    @patch("graviton_server.run_agent_async")
    def test_do_post_direct_execution_path_traversal_aborted(self, mock_run_async):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            repos_dir.mkdir()

            payload = json.dumps({
                "action": "opened",
                "number": 12,
                "repository": {
                    "name": "..",
                },
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
            handler.repos_dir = repos_dir

            GravitonHandler.do_POST(handler)

            mock_run_async.assert_not_called()
            handler._send_json.assert_called_once()
            args, _ = handler._send_json.call_args
            self.assertEqual(args[0], 400)
            self.assertIn("attempting path traversal", args[1]["error"])

    @patch("graviton_server.post_emoji_reaction_async")
    def test_do_post_with_task_manager_pacing_rejection(self, mock_post_reaction):
        mock_tm = MagicMock()
        mock_tm.submit_task.side_effect = RuntimeError("Cannot accept new task: quota pacing is behind limit")

        payload = json.dumps({"action": "opened", "number": 15}).encode("utf-8")
        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "pull_request",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_reviewer = "code_reviewer"
        handler.task_manager = mock_tm

        GravitonHandler.do_POST(handler)

        mock_tm.submit_task.assert_called_once()
        mock_post_reaction.assert_not_called()
        handler._send_json.assert_called_once_with(200, {"status": "ignored", "reason": "behind_quota_pacing"})

    @patch("graviton_server.post_emoji_reaction_async")
    def test_do_post_with_task_manager_runtime_error_503(self, mock_post_reaction):
        mock_tm = MagicMock()
        mock_tm.submit_task.side_effect = RuntimeError("Cannot accept new task: task acceptance is paused")

        payload = json.dumps({"action": "opened", "number": 16}).encode("utf-8")
        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "pull_request",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_reviewer = "code_reviewer"
        handler.task_manager = mock_tm

        GravitonHandler.do_POST(handler)

        mock_tm.submit_task.assert_called_once()
        mock_post_reaction.assert_not_called()
        handler._send_json.assert_called_once_with(503, {"error": "Cannot accept new task: task acceptance is paused"})

    @patch("graviton_server.post_emoji_reaction_async")
    @patch("graviton_server.run_agent_async")
    def test_do_post_direct_execution_pacing_rejection(self, mock_run_async, mock_post_reaction):
        mock_qt = MagicMock()
        mock_qt.is_behind_pacing.return_value = True

        payload = json.dumps({"action": "opened", "number": 17}).encode("utf-8")
        handler = MagicMock(spec=GravitonHandler)
        handler.headers = {
            "Content-Length": str(len(payload)),
            "X-GitHub-Event": "pull_request",
        }
        handler.rfile = BytesIO(payload)
        handler.secret = ""
        handler.default_reviewer = "code_reviewer"
        handler.task_manager = None
        handler.quota_tracker = mock_qt

        GravitonHandler.do_POST(handler)

        mock_run_async.assert_not_called()
        mock_post_reaction.assert_not_called()
        handler._send_json.assert_called_once_with(200, {"status": "ignored", "reason": "behind_quota_pacing"})

    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    @patch("graviton_server.signal.signal")
    def test_server_signal_shutdown_non_blocking(
        self, mock_signal_func, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_server = MagicMock()
        mock_http.return_value = mock_server

        registered_handlers = {}

        def fake_signal(sig, handler):
            registered_handlers[sig] = handler

        mock_signal_func.side_effect = fake_signal

        def fake_serve_forever():
            handler = registered_handlers.get(signal.SIGINT)
            self.assertIsNotNone(handler)
            handler(signal.SIGINT, None)

        mock_server.serve_forever.side_effect = fake_serve_forever

        with patch("sys.argv", ["graviton-server.py"]):
            server_mod.main()

        mock_dashboard_inst.stop.assert_called_once()
        mock_tm_inst.stop.assert_called_once()
        mock_server.server_close.assert_called_once()
        mock_server.shutdown.assert_not_called()

    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    @patch("graviton_server.signal.signal")
    def test_signal_handler_registered_before_dashboard_start(
        self, mock_signal_func, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_server = MagicMock()
        mock_http.return_value = mock_server
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        call_order = []

        def track_signal(sig, handler):
            call_order.append(("signal", sig))

        def track_dashboard_start():
            call_order.append(("dashboard_start",))

        mock_signal_func.side_effect = track_signal
        mock_dashboard_inst.start.side_effect = track_dashboard_start

        with patch("sys.argv", ["graviton-server.py"]):
            server_mod.main()

        self.assertIn(("signal", signal.SIGINT), call_order)
        self.assertIn(("signal", signal.SIGTERM), call_order)
        self.assertIn(("dashboard_start",), call_order)

        sigint_index = call_order.index(("signal", signal.SIGINT))
        sigterm_index = call_order.index(("signal", signal.SIGTERM))
        dashboard_index = call_order.index(("dashboard_start",))

        self.assertLess(sigint_index, dashboard_index)
        self.assertLess(sigterm_index, dashboard_index)

    def test_cli_argument_quit_grace_period(self):
        with patch("sys.argv", ["graviton-server.py", "--quit-grace-period", "5.5"]):
            with patch("bin.graviton-server.HTTPServer" if "bin.graviton-server" in sys.modules else "graviton_server.HTTPServer") as mock_http:
                mock_server = MagicMock()
                mock_http.return_value = mock_server
                mock_server.serve_forever.side_effect = KeyboardInterrupt
                with patch("graviton_server.TerminalDashboard") as mock_dash:
                    server_mod.main()
                    mock_dash.assert_called_once()
                    _, kwargs = mock_dash.call_args
                    self.assertEqual(kwargs.get("quit_grace_period"), 5.5)

    def test_graceful_shutdown_workflow_server_module(self):
        mock_tm = MagicMock()
        mock_sched = MagicMock()
        mock_httpd = MagicMock()
        mock_dash = MagicMock()

        t = server_mod.graceful_shutdown(
            task_manager=mock_tm,
            scheduler=mock_sched,
            dashboard=mock_dash,
            httpd=mock_httpd,
            grace_period=0.01,
        )
        t.join(timeout=2.0)

        mock_dash.graceful_shutdown.assert_called_once_with(timeout=None, grace_period=0.01)

    def test_graceful_shutdown_without_dashboard(self):
        mock_tm = MagicMock()
        mock_sched = MagicMock()
        mock_httpd = MagicMock()

        t = server_mod.graceful_shutdown(
            task_manager=mock_tm,
            scheduler=mock_sched,
            dashboard=None,
            httpd=mock_httpd,
            grace_period=0.01,
        )
        t.join(timeout=2.0)

        mock_tm.drain_active_tasks.assert_called_once()
        mock_tm.dump_queue_state.assert_called_once()
        mock_sched.stop.assert_called_once()
        mock_httpd.shutdown.assert_called_once()
        mock_httpd.server_close.assert_called_once()
        mock_tm.stop.assert_called_once()

    def test_shutdown_signal_handler_non_blocking(self):
        registered_handlers = {}

        def mock_signal_func(sig, handler):
            registered_handlers[sig] = handler

        with patch("signal.signal", side_effect=mock_signal_func):
            with patch("sys.argv", ["graviton-server.py"]):
                with patch("bin.graviton-server.HTTPServer" if "bin.graviton-server" in sys.modules else "graviton_server.HTTPServer") as mock_http:
                    mock_server = MagicMock()
                    mock_http.return_value = mock_server
                    mock_server.serve_forever.side_effect = KeyboardInterrupt
                    with patch("graviton_server.TerminalDashboard") as mock_dash:
                        with patch("graviton_server.graceful_shutdown") as mock_gs:
                            mock_thread = MagicMock()
                            mock_gs.return_value = mock_thread
                            server_mod.main()

                            self.assertIn(signal.SIGINT, registered_handlers)
                            handler = registered_handlers[signal.SIGINT]
                            # Call signal handler
                            handler(signal.SIGINT, None)
                            mock_gs.assert_called_once()
                            # Verify thread.join was not called inside signal handler
                            mock_thread.join.assert_not_called()

    def test_graceful_shutdown_headless_double_trigger_guard(self):
        server_mod._is_shutting_down = False
        server_mod._shutdown_thread = None
        mock_tm = MagicMock()
        mock_sched = MagicMock()
        mock_httpd = MagicMock()

        t1 = server_mod.graceful_shutdown(
            task_manager=mock_tm,
            scheduler=mock_sched,
            dashboard=None,
            httpd=mock_httpd,
            grace_period=0.01,
        )
        t2 = server_mod.graceful_shutdown(
            task_manager=mock_tm,
            scheduler=mock_sched,
            dashboard=None,
            httpd=mock_httpd,
            grace_period=0.01,
        )

        self.assertIs(t1, t2)
        t1.join(timeout=2.0)

    def test_signal_triggered_shutdown_persists_queue(self):
        import tempfile
        from lib.tasks import TaskManager
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            tm = TaskManager(cwd=tmp_path)
            task = tm.submit_task("code_reviewer", "Persist signal task", target_id="owner/repo#100")
            mock_sched = MagicMock()
            mock_httpd = MagicMock()

            t = server_mod.graceful_shutdown(
                task_manager=tm,
                scheduler=mock_sched,
                dashboard=None,
                httpd=mock_httpd,
                grace_period=0.01,
            )
            t.join(timeout=3.0)

            state_file = tmp_path / ".graviton_queue_state.json"
            self.assertTrue(state_file.exists())

            new_tm = TaskManager(cwd=tmp_path)
            restored_count = new_tm.restore_queue_state()
            self.assertEqual(restored_count, 1)
            restored = new_tm.get_task(task.id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.prompt, "Persist signal task")
            new_tm.stop()

    @patch("subprocess.Popen")
    def test_start_smee_listener_valid_url(self, mock_popen):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        res = server_mod.start_smee_listener("https://smee.io/test-channel", 8000)
        self.assertEqual(res, mock_proc)
        mock_popen.assert_called_once_with(
            [
                str(server_mod.RUN_LISTENER_SCRIPT),
                "https://smee.io/test-channel",
                "8000",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def test_start_smee_listener_empty_url(self):
        res = server_mod.start_smee_listener("", 8000)
        self.assertIsNone(res)

    @patch("os.access", return_value=False)
    def test_start_smee_listener_non_executable_script(self, mock_access):
        res = server_mod.start_smee_listener("https://smee.io/test-channel", 8000)
        self.assertIsNone(res)

    @patch("graviton_server.start_smee_listener")
    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_main_launches_and_cleans_up_smee_listener(
        self, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls, mock_start_listener
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_server = MagicMock()
        mock_http.return_value = mock_server
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_start_listener.return_value = mock_proc

        with patch("sys.argv", ["graviton-server.py", "--smee-url", "https://smee.io/test-channel"]):
            server_mod.main()

        mock_start_listener.assert_called_once_with("https://smee.io/test-channel", 8000)
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=2)

    @patch("graviton_server.start_smee_listener")
    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_main_smee_url_from_env_var(
        self, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls, mock_start_listener
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_server = MagicMock()
        mock_http.return_value = mock_server
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_start_listener.return_value = mock_proc

        with patch.dict("os.environ", {"SMEE_URL": "https://smee.io/env-channel"}):
            with patch("sys.argv", ["graviton-server.py"]):
                server_mod.main()

        mock_start_listener.assert_called_once_with("https://smee.io/env-channel", 8000)

    @patch("graviton_server.start_smee_listener")
    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_main_smee_listener_timeout_triggers_kill(
        self, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls, mock_start_listener
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_server = MagicMock()
        mock_http.return_value = mock_server
        mock_server.serve_forever.side_effect = KeyboardInterrupt

        import subprocess
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="smee", timeout=2), None]
        mock_start_listener.return_value = mock_proc

        with patch("sys.argv", ["graviton-server.py", "--smee-url", "https://smee.io/test-channel"]):
            server_mod.main()

        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        self.assertEqual(mock_proc.wait.call_count, 2)

    @patch("graviton_server.start_smee_listener")
    @patch("graviton_server.TerminalDashboard")
    @patch("graviton_server.HTTPServer")
    @patch("graviton_server.TaskManager")
    @patch("graviton_server.QuotaTracker")
    @patch("graviton_server.PRTracker")
    def test_main_cleans_up_smee_listener_on_init_exception(
        self, mock_pr, mock_quota, mock_tm, mock_http, mock_dashboard_cls, mock_start_listener
    ):
        mock_tm_inst = MagicMock()
        mock_tm_inst.restore_queue_state.return_value = 0
        mock_tm.return_value = mock_tm_inst
        mock_dashboard_inst = MagicMock()
        mock_dashboard_cls.return_value = mock_dashboard_inst
        mock_http.side_effect = OSError("[Errno 98] Address already in use")

        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_start_listener.return_value = mock_proc

        with patch("sys.argv", ["graviton-server.py", "--smee-url", "https://smee.io/test-channel"]):
            with self.assertRaises(OSError):
                server_mod.main()

        mock_start_listener.assert_called_once_with("https://smee.io/test-channel", 8000)
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=2)


if __name__ == "__main__":
    unittest.main()




