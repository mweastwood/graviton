"""Unit tests for Programmatic Stream-JSON Supervisor (lib/supervisor.py)."""

import io
import json
import subprocess
import unittest
from pathlib import Path
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
        self.mock_proc.stdout = io.StringIO()
        self.mock_proc.stderr = io.StringIO()

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
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout.readline.return_value = json.dumps({
                "event": "init",
                "conversation_id": "test-conv-id",
                "init": {},
            }) + "\n"
            mock_popen.return_value = mock_proc

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
            mock_proc = MagicMock()
            mock_proc.poll.return_value = 1
            mock_proc.returncode = 1
            mock_proc.stderr.read.return_value = "Fatal error"
            mock_popen.return_value = mock_proc

            with self.assertRaises(SupervisorError) as ctx:
                session.start(timeout=1.0)
            self.assertIn("exited prematurely", str(ctx.exception))

    def test_start_malformed_init_json(self):
        session = StreamSession(agy_binary="agy")
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout.readline.return_value = "NOT_VALID_JSON\n"
            mock_popen.return_value = mock_proc

            with self.assertRaises(SupervisorError) as ctx:
                session.start(timeout=1.0)
            self.assertIn("Malformed JSON", str(ctx.exception))

    def test_start_unexpected_event(self):
        session = StreamSession(agy_binary="agy")
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout.readline.return_value = json.dumps({"event": "something_else"}) + "\n"
            mock_popen.return_value = mock_proc

            with self.assertRaises(SupervisorError) as ctx:
                session.start(timeout=1.0)
            self.assertIn("Expected 'init' event", str(ctx.exception))

    def test_send_prompt_formatting(self):
        session = StreamSession(agy_binary="agy")
        session.proc = MagicMock()
        session.proc.poll.return_value = None
        session.proc.stdin = MagicMock()

        session.send_prompt("Review PR #42")
        session.proc.stdin.write.assert_called_once()
        sent_line = session.proc.stdin.write.call_args[0][0]
        data = json.loads(sent_line)

        self.assertEqual(data.get("event"), "user")
        self.assertEqual(
            data.get("message", {}).get("content"),
            [{"type": "text", "text": "Review PR #42"}],
        )

    def test_receive_turn_success(self):
        session = StreamSession(agy_binary="agy")
        session.proc = MagicMock()
        session.proc.poll.return_value = None
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
        ]
        session.proc.stdout.readline.side_effect = lines

        captured_events = []
        result = session.receive_turn(on_event=lambda ev: captured_events.append(ev))

        self.assertTrue(result.is_success)
        self.assertEqual(result.response, "Reviewed.")
        self.assertEqual(result.conversation_id, "conv-abc")
        self.assertEqual(len(captured_events), 3)

    def test_receive_turn_timeout(self):
        session = StreamSession(agy_binary="agy")
        session.proc = MagicMock()
        session.proc.poll.return_value = None
        session.proc.stdout.readline.return_value = ""

        with self.assertRaises(SupervisorTimeoutError):
            session.receive_turn(timeout=0.05)

    def test_multi_turn_execution(self):
        session = StreamSession(agy_binary="agy")
        session.proc = MagicMock()
        session.proc.poll.return_value = None
        session.conversation_id = "conv-multi"

        turn1_lines = [
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Turn 1 done"}}) + "\n",
        ]
        turn2_lines = [
            json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "Turn 2 done"}}) + "\n",
        ]

        session.proc.stdout.readline.side_effect = turn1_lines + turn2_lines

        r1 = session.run_turn("First prompt")
        self.assertEqual(r1.response, "Turn 1 done")

        r2 = session.run_turn("Second prompt")
        self.assertEqual(r2.response, "Turn 2 done")

    def test_context_manager(self):
        with patch("subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.poll.return_value = None
            mock_proc.stdout.readline.return_value = json.dumps({"event": "init", "conversation_id": "cm-id"}) + "\n"
            mock_popen.return_value = mock_proc

            with StreamSession() as session:
                self.assertEqual(session.conversation_id, "cm-id")
            mock_proc.stdin.close.assert_called()


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
