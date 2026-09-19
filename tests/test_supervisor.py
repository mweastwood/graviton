"""Unit tests for Programmatic Stream-JSON Supervisor (lib/supervisor.py)."""

import io
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from lib.supervisor import (
    StreamSession,
    SupervisorError,
    SupervisorResult,
    SupervisorTimeoutError,
    run_stream_turn,
)


class TestSupervisorResult(unittest.TestCase):
    def test_result_properties(self):
        res = SupervisorResult(
            conversation_id="conv-123",
            status="SUCCESS",
            response="All tests passed",
            duration_seconds=1.5,
            num_turns=1,
            usage={"input_tokens": 10, "output_tokens": 20},
        )
        self.assertTrue(res.is_success)
        self.assertEqual(res.conversation_id, "conv-123")
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.response, "All tests passed")

    def test_result_failure_status(self):
        res = SupervisorResult(status="ERROR", error="Model timed out")
        self.assertFalse(res.is_success)

    def test_result_error_with_success_status(self):
        res = SupervisorResult(status="SUCCESS", error="Partial failure")
        self.assertFalse(res.is_success)


class TestStreamSession(unittest.TestCase):
    def setUp(self):
        self.mock_proc = MagicMock()
        self.mock_proc.poll.return_value = None
        self.mock_proc.returncode = 0
        self.mock_proc.stdin = MagicMock()
        self.mock_proc.stdout = MagicMock()
        self.mock_proc.stderr = MagicMock()

    def test_command_line_args_generation(self):
        session = StreamSession(
            agent_name="code_reviewer",
            model="gemini-pro",
            cwd="/tmp/repo",
            remote_control=True,
            dangerously_skip_permissions=True,
            extra_args=["--effort", "high"],
            agy_binary="/usr/bin/agy",
        )

        with patch("subprocess.Popen") as mock_popen:
            self.mock_proc.stdout.readline.side_effect = [
                json.dumps({
                    "event": "init",
                    "conversation_id": "test-conv-id",
                    "init": {},
                }) + "\n",
                "",
            ]
            mock_popen.return_value = self.mock_proc

            conv_id = session.start()
            self.assertEqual(conv_id, "test-conv-id")

            cmd = mock_popen.call_args[0][0]
            self.assertIn("/usr/bin/agy", cmd)
            self.assertIn("--input-format", cmd)
            self.assertIn("stream-json", cmd)
            self.assertIn("--output-format", cmd)
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertIn("--remote-control", cmd)
            self.assertIn("--agent", cmd)
            self.assertIn("code_reviewer", cmd)
            self.assertIn("--model", cmd)
            self.assertIn("gemini-pro", cmd)
            self.assertIn("--effort", cmd)
            self.assertIn("high", cmd)

    def test_start_premature_exit(self):
        session = StreamSession(agy_binary="agy")
        with patch("subprocess.Popen") as mock_popen:
            self.mock_proc.poll.return_value = 1
            self.mock_proc.returncode = 1
            self.mock_proc.stderr.readline.side_effect = ["Fatal error\n", ""]
            self.mock_proc.stderr.read.return_value = "Fatal error"
            self.mock_proc.stdout.readline.side_effect = [""]
            mock_popen.return_value = self.mock_proc

            with self.assertRaises(SupervisorError) as ctx:
                session.start(timeout=1.0)
            self.assertIn("exited prematurely", str(ctx.exception))
            self.assertTrue(session._is_closed)
            self.mock_proc.stdin.close.assert_called()

    def test_start_premature_exit_on_line_none(self):
        session = StreamSession(agy_binary="agy")
        with patch("subprocess.Popen") as mock_popen:
            self.mock_proc.poll.return_value = 1
            self.mock_proc.returncode = 1
            self.mock_proc.stderr.readline.side_effect = ["Process exited\n", ""]
            self.mock_proc.stderr.read.return_value = "Process exited"
            self.mock_proc.stdout.readline.side_effect = [""]
            mock_popen.return_value = self.mock_proc

            with self.assertRaises(SupervisorError) as ctx:
                session.start(timeout=1.0)
            self.assertIn("exited prematurely", str(ctx.exception))
            self.assertTrue(session._is_closed)
            self.mock_proc.stdin.close.assert_called()

    def test_start_malformed_init_json(self):
        session = StreamSession(agy_binary="agy")
        with patch("subprocess.Popen") as mock_popen:
            self.mock_proc.stdout.readline.side_effect = ["NOT_VALID_JSON\n", ""]
            mock_popen.return_value = self.mock_proc

            with self.assertRaises(SupervisorError) as ctx:
                session.start(timeout=1.0)
            self.assertIn("Malformed JSON", str(ctx.exception))

    def test_start_unexpected_event(self):
        session = StreamSession(agy_binary="agy")
        with patch("subprocess.Popen") as mock_popen:
            self.mock_proc.stdout.readline.side_effect = [
                json.dumps({"event": "something_else"}) + "\n",
                "",
            ]
            mock_popen.return_value = self.mock_proc

            with self.assertRaises(SupervisorError) as ctx:
                session.start(timeout=1.0)
            self.assertIn("Expected 'init' event", str(ctx.exception))

    def test_start_timeout_blocking(self):
        session = StreamSession(agy_binary="agy")
        with patch("subprocess.Popen") as mock_popen:
            block_event = threading.Event()

            def blocking_readline():
                block_event.wait(timeout=1.0)
                return ""

            self.mock_proc.stdout.readline.side_effect = blocking_readline
            mock_popen.return_value = self.mock_proc

            start_time = time.time()
            with self.assertRaises(SupervisorTimeoutError):
                session.start(timeout=0.05)
            elapsed = time.time() - start_time
            self.assertLess(elapsed, 0.5)
            block_event.set()

    def test_send_prompt_formatting(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc

        session.send_prompt("Review PR #42")
        self.mock_proc.stdin.write.assert_called_once()
        sent_line = self.mock_proc.stdin.write.call_args[0][0]
        data = json.loads(sent_line)

        self.assertEqual(data.get("event"), "user")
        self.assertEqual(
            data.get("message", {}).get("content"),
            [{"type": "text", "text": "Review PR #42"}],
        )

    def test_receive_turn_success(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        session.conversation_id = "conv-abc"

        lines = [
            json.dumps({"event": "step_update", "step_update": {"step_index": 0, "state": "DONE"}}) + "\n",
            json.dumps({"event": "step_update", "step_update": {"step_index": 1, "text_delta": "Reviewed."}}) + "\n",
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Reviewed.",
                    "duration_seconds": 2.5,
                    "num_turns": 1,
                    "usage": {"total_tokens": 100},
                },
            }) + "\n",
            "",
        ]
        self.mock_proc.stdout.readline.side_effect = lines

        captured_events = []
        result = session.receive_turn(on_event=lambda ev: captured_events.append(ev))

        self.assertTrue(result.is_success)
        self.assertEqual(result.response, "Reviewed.")
        self.assertEqual(result.conversation_id, "conv-abc")
        self.assertEqual(len(captured_events), 3)

    def test_receive_turn_timeout(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc

        # Simulate a blocking/stalled pipe: readline blocks longer than timeout
        block_event = threading.Event()

        def blocking_readline():
            block_event.wait(timeout=1.0)
            return ""

        self.mock_proc.stdout.readline.side_effect = blocking_readline

        start_time = time.time()
        with self.assertRaises(SupervisorTimeoutError):
            session.receive_turn(timeout=0.05)
        elapsed = time.time() - start_time
        self.assertLess(elapsed, 0.5)
        block_event.set()

    def test_receive_turn_null_metrics(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        session.conversation_id = "conv-null"

        lines = [
            json.dumps({
                "event": "result",
                "result": {
                    "status": None,
                    "response": None,
                    "duration_seconds": None,
                    "num_turns": None,
                    "usage": None,
                    "error": None,
                },
            }) + "\n",
            "",
        ]
        self.mock_proc.stdout.readline.side_effect = lines

        result = session.receive_turn(timeout=1.0)
        self.assertTrue(result.is_success)
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.response, "")
        self.assertIsInstance(result.duration_seconds, float)
        self.assertEqual(result.num_turns, 1)
        self.assertEqual(result.usage, {})

    def test_multi_turn_execution(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        session.conversation_id = "conv-multi"

        turn1_lines = [
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Turn 1 done"}}) + "\n",
        ]
        turn2_lines = [
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Turn 2 done"}}) + "\n",
            "",
        ]

        self.mock_proc.stdout.readline.side_effect = turn1_lines + turn2_lines

        r1 = session.run_turn("First prompt")
        self.assertEqual(r1.response, "Turn 1 done")

        r2 = session.run_turn("Second prompt")
        self.assertEqual(r2.response, "Turn 2 done")

    def test_context_manager(self):
        with patch("subprocess.Popen") as mock_popen:
            self.mock_proc.stdout.readline.side_effect = [
                json.dumps({"event": "init", "conversation_id": "cm-id"}) + "\n",
                "",
            ]
            mock_popen.return_value = self.mock_proc

            with StreamSession() as session:
                self.assertEqual(session.conversation_id, "cm-id")
            self.mock_proc.stdin.close.assert_called()

    def test_close_graceful_exit(self):
        session = StreamSession()
        session.proc = self.mock_proc
        self.mock_proc.wait.return_value = 0

        session.close(timeout=1.0)
        self.mock_proc.stdin.close.assert_called()
        self.mock_proc.stdout.close.assert_called()
        self.mock_proc.stderr.close.assert_called()
        self.mock_proc.wait.assert_called_with(timeout=1.0)
        self.mock_proc.terminate.assert_not_called()
        self.mock_proc.kill.assert_not_called()

    def test_close_escalation_terminate(self):
        session = StreamSession()
        session.proc = self.mock_proc
        self.mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="agy", timeout=1.0),
            0,
        ]

        session.close(timeout=1.0)
        self.mock_proc.terminate.assert_called_once()
        self.mock_proc.kill.assert_not_called()

    def test_close_escalation_kill(self):
        session = StreamSession()
        session.proc = self.mock_proc
        self.mock_proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="agy", timeout=1.0),
            subprocess.TimeoutExpired(cmd="agy", timeout=2.0),
            0,
        ]

        session.close(timeout=1.0)
        self.mock_proc.terminate.assert_called_once()
        self.mock_proc.kill.assert_called_once()

    def test_drain_stdout_retries_on_queue_full(self):
        session = StreamSession()
        session.proc = self.mock_proc
        self.mock_proc.stdout.readline.side_effect = ["event 1\n", ""]

        call_count = 0
        def fake_put(item, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise queue.Full()
            return None

        with patch.object(session._stdout_queue, "put", side_effect=fake_put):
            session._drain_stdout()
            self.assertGreaterEqual(call_count, 2)

    def test_drain_stdout_exits_retry_loop_when_closed(self):
        session = StreamSession()
        session.proc = self.mock_proc
        self.mock_proc.stdout.readline.side_effect = ["event 1\n", ""]

        def fake_put(item, timeout=None):
            session._is_closed = True
            raise queue.Full()

        with patch.object(session._stdout_queue, "put", side_effect=fake_put):
            session._drain_stdout()

    def test_run_stream_turn_wrapper(self):
        with patch("lib.supervisor.StreamSession") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_instance
            expected_res = SupervisorResult(status="SUCCESS", response="turn output")
            mock_instance.run_turn.return_value = expected_res

            res = run_stream_turn(
                prompt="Test prompt",
                agent_name="issue_triager",
                model="gemini",
                cwd="/tmp",
                remote_control=True,
                timeout=12.0,
                extra_args=["--verbose"],
            )

            mock_cls.assert_called_once_with(
                agent_name="issue_triager",
                model="gemini",
                cwd="/tmp",
                remote_control=True,
                dangerously_skip_permissions=True,
                extra_args=["--verbose"],
                agy_binary=None,
                env=None,
            )
            mock_instance.run_turn.assert_called_once_with("Test prompt", timeout=12.0, on_event=None)
            self.assertEqual(res, expected_res)

    def test_run_stream_turn_wrapper_custom_parameters(self):
        with patch("lib.supervisor.StreamSession") as mock_cls:
            mock_instance = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_instance
            expected_res = SupervisorResult(status="SUCCESS", response="turn output")
            mock_instance.run_turn.return_value = expected_res

            res = run_stream_turn(
                prompt="Custom prompt",
                agent_name="coder",
                model="gemini-exp",
                cwd="/custom/dir",
                remote_control=False,
                dangerously_skip_permissions=False,
                timeout=60.0,
                extra_args=["--flag"],
                agy_binary="/custom/bin/agy",
                env={"CUSTOM_KEY": "val"},
            )

            mock_cls.assert_called_once_with(
                agent_name="coder",
                model="gemini-exp",
                cwd="/custom/dir",
                remote_control=False,
                dangerously_skip_permissions=False,
                extra_args=["--flag"],
                agy_binary="/custom/bin/agy",
                env={"CUSTOM_KEY": "val"},
            )
            mock_instance.run_turn.assert_called_once_with("Custom prompt", timeout=60.0, on_event=None)
            self.assertEqual(res, expected_res)


class TestSupervisorIntegration(unittest.TestCase):
    def test_live_stream_session(self):
        try:
            res = run_stream_turn("say test_ok", timeout=15.0)
            self.assertTrue(res.is_success)
            self.assertIn("test_ok", res.response.lower())
            self.assertIsNotNone(res.conversation_id)
        except (SupervisorError, FileNotFoundError) as e:
            self.skipTest(f"agy not available for live integration test: {e}")


if __name__ == "__main__":
    unittest.main()
