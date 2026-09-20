"""Unit tests for Programmatic Stream-JSON Supervisor (lib/supervisor.py)."""

import io
import json
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.supervisor import (
    ContainerSupervisor,
    StreamSession,
    SupervisorError,
    SupervisorResult,
    SupervisorTimeoutError,
    clean_workspace_dir,
    run_container_goal,
    run_container_turn,
    run_goal_turn,
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
            thoughts=["Let me check"],
            tool_calls=[{"name": "run_command"}],
        )
        self.assertTrue(res.is_success)
        self.assertEqual(res.conversation_id, "conv-123")
        self.assertEqual(res.status, "SUCCESS")
        self.assertEqual(res.response, "All tests passed")
        self.assertEqual(res.thoughts, ["Let me check"])
        self.assertEqual(len(res.tool_calls), 1)

    def test_result_failure_status(self):
        res = SupervisorResult(status="ERROR", error="Model timed out")
        self.assertFalse(res.is_success)

    def test_result_error_with_success_status(self):
        res = SupervisorResult(status="SUCCESS", error="Partial failure")
        self.assertFalse(res.is_success)

    def test_result_goal_complete(self):
        res1 = SupervisorResult(status="SUCCESS", response="Done! <!-- GOAL_COMPLETE -->")
        self.assertTrue(res1.is_goal_complete)

        res2 = SupervisorResult(status="SUCCESS", response="Finished task without marker")
        self.assertTrue(res2.is_goal_complete)

        res3 = SupervisorResult(status="ERROR", response="Something broke <!-- GOAL_COMPLETE -->")
        self.assertTrue(res3.is_goal_complete)

        res4 = SupervisorResult(status="ERROR", response="Failed task", error="Crash")
        self.assertFalse(res4.is_goal_complete)

    def test_result_goal_complete_none_response(self):
        res_none_err = SupervisorResult(status="ERROR", response=None)
        self.assertFalse(res_none_err.is_goal_complete)

        res_none_ok = SupervisorResult(status="SUCCESS", response=None)
        self.assertTrue(res_none_ok.is_goal_complete)

    def test_result_goal_complete_error_with_bare_substring(self):
        res = SupervisorResult(
            status="ERROR",
            response="Error: Agent failed without emitting GOAL_COMPLETE",
            error="Process error",
        )
        self.assertFalse(res.is_goal_complete)

        res2 = SupervisorResult(
            status="ERROR",
            response="Subprocess output mentioning GOAL_COMPLETE keyword",
        )
        self.assertFalse(res2.is_goal_complete)


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

            cmd = mock_popen.call_args_list[0][0][0]
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

    def test_stream_session_custom_command(self):
        custom_cmd = ["docker", "run", "-i", "my-container", "agy", "--stream"]
        session = StreamSession(custom_command=custom_cmd)

        with patch("subprocess.Popen") as mock_popen:
            self.mock_proc.stdout.readline.side_effect = [
                json.dumps({
                    "event": "init",
                    "conversation_id": "docker-conv-id",
                    "init": {},
                }) + "\n",
                "",
            ]
            mock_popen.return_value = self.mock_proc

            conv_id = session.start()
            self.assertEqual(conv_id, "docker-conv-id")

            cmd = mock_popen.call_args[0][0]
            self.assertEqual(cmd, custom_cmd)

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

    def test_receive_turn_step_updates_and_callbacks(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        session.conversation_id = "conv-progress"

        lines = [
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "thought": "I should read the file first.",
                    "text_delta": "Thinking...",
                },
            }) + "\n",
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "tool_name": "view_file",
                    "tool_info": {"path": "/workspace/main.py"},
                    "state": "DONE",
                    "duration_seconds": 0.05,
                },
            }) + "\n",
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "agent_response",
                    "text_delta": "File looks good.",
                },
            }) + "\n",
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Done! <!-- GOAL_COMPLETE -->",
                },
            }) + "\n",
            "",
        ]
        self.mock_proc.stdout.readline.side_effect = lines

        captured_thoughts = []
        captured_tools = []
        captured_chunks = []
        captured_steps = []

        res = session.receive_turn(
            on_thought=lambda th: captured_thoughts.append(th),
            on_tool_call=lambda name, info: captured_tools.append((name, info)),
            on_chunk=lambda chunk: captured_chunks.append(chunk),
            on_step=lambda step: captured_steps.append(step),
        )

        self.assertTrue(res.is_success)
        self.assertTrue(res.is_goal_complete)
        self.assertEqual(res.thoughts, ["I should read the file first."])
        self.assertEqual(captured_thoughts, ["I should read the file first."])
        self.assertEqual(len(res.tool_calls), 1)
        self.assertEqual(res.tool_calls[0]["name"], "view_file")
        self.assertEqual(captured_tools, [("view_file", {"path": "/workspace/main.py"})])
        self.assertIn("Thinking...", captured_chunks)
        self.assertIn("File looks good.", captured_chunks)
        self.assertEqual(len(captured_steps), 3)

    def test_receive_turn_idle_watchdog(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc

        block_event = threading.Event()

        def blocking_readline():
            block_event.wait(timeout=1.0)
            return ""

        self.mock_proc.stdout.readline.side_effect = blocking_readline

        with self.assertRaises(SupervisorTimeoutError) as ctx:
            session.receive_turn(idle_timeout=0.05)
        block_event.set()
        self.assertIn("idle", str(ctx.exception).lower())

    def test_run_goal_formatting(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        session.conversation_id = "conv-goal"

        with patch.object(session, "send_prompt") as mock_send, \
             patch.object(session, "receive_turn") as mock_receive:
            mock_receive.return_value = SupervisorResult(status="SUCCESS", response="<!-- GOAL_COMPLETE -->")

            # Prepends /goal if missing
            res1 = session.run_goal("Run all tests and fix errors")
            mock_send.assert_called_with("/goal Run all tests and fix errors")
            self.assertTrue(res1.is_goal_complete)

            # Preserves /goal if already present
            res2 = session.run_goal("/goal Already has goal")
            mock_send.assert_called_with("/goal Already has goal")
            self.assertTrue(res2.is_goal_complete)

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

    def test_receive_turn_max_duration_timeout(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc

        block_event = threading.Event()

        def blocking_readline():
            block_event.wait(timeout=1.0)
            return ""

        self.mock_proc.stdout.readline.side_effect = blocking_readline

        start_time = time.time()
        with self.assertRaises(SupervisorTimeoutError) as ctx:
            session.receive_turn(max_duration=0.05)
        elapsed = time.time() - start_time
        self.assertLess(elapsed, 0.5)
        block_event.set()
        self.assertIn("ceiling", str(ctx.exception).lower())

    def test_run_turn_disabling_max_duration(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc

        with patch.object(session, "send_prompt"), \
             patch.object(session, "receive_turn") as mock_receive:
            mock_receive.return_value = SupervisorResult(status="SUCCESS")
            session.run_turn("test prompt", max_duration=None)
            mock_receive.assert_called_once()
            _, kwargs = mock_receive.call_args
            self.assertIn("max_duration", kwargs)
            self.assertIsNone(kwargs["max_duration"])

    def test_receive_turn_tool_calls_deduplication(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        session.conversation_id = "conv-dedup"

        lines = [
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "id": "tool-call-1",
                    "tool_name": "run_command",
                    "tool_info": {"CommandLine": "pytest"},
                    "state": "RUNNING",
                },
            }) + "\n",
            json.dumps({
                "event": "step_update",
                "step_update": {
                    "step_type": "tool",
                    "id": "tool-call-1",
                    "tool_name": "run_command",
                    "state": "DONE",
                    "duration_seconds": 1.2,
                },
            }) + "\n",
            json.dumps({
                "event": "result",
                "result": {
                    "status": "SUCCESS",
                    "response": "Done! <!-- GOAL_COMPLETE -->",
                },
            }) + "\n",
            "",
        ]
        self.mock_proc.stdout.readline.side_effect = lines

        res = session.receive_turn()
        self.assertEqual(len(res.tool_calls), 1)
        self.assertEqual(res.tool_calls[0]["id"], "tool-call-1")
        self.assertEqual(res.tool_calls[0]["name"], "run_command")
        self.assertEqual(res.tool_calls[0]["info"], {"CommandLine": "pytest"})
        self.assertEqual(res.tool_calls[0]["state"], "DONE")
        self.assertEqual(res.tool_calls[0]["duration_seconds"], 1.2)

    def test_receive_turn_premature_exit(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        self.mock_proc.returncode = 1
        self.mock_proc.poll.side_effect = [None, 1, 1, 1]
        self.mock_proc.stderr.readline.side_effect = ["Subprocess crashed mid-turn\n", ""]

        block_event = threading.Event()

        def blocking_readline():
            block_event.wait(timeout=1.0)
            return ""

        self.mock_proc.stdout.readline.side_effect = blocking_readline

        with self.assertRaises(SupervisorError) as ctx:
            session.receive_turn(timeout=1.0)

        block_event.set()
        self.assertIn("exited prematurely with code 1", str(ctx.exception))
        self.assertTrue(session._is_closed)
        self.mock_proc.stdin.close.assert_called()
        self.mock_proc.stdout.close.assert_called()
        self.mock_proc.stderr.close.assert_called()

    def test_receive_turn_premature_exit_on_line_none(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        self.mock_proc.returncode = 1
        self.mock_proc.poll.side_effect = [None, 1, 1, 1]
        self.mock_proc.stderr.readline.side_effect = ["Process closed stream\n", ""]
        self.mock_proc.stdout.readline.side_effect = [""]

        with self.assertRaises(SupervisorError) as ctx:
            session.receive_turn(timeout=1.0)

        self.assertIn("exited prematurely with code 1", str(ctx.exception))
        self.assertTrue(session._is_closed)
        self.mock_proc.stdin.close.assert_called()
        self.mock_proc.stdout.close.assert_called()
        self.mock_proc.stderr.close.assert_called()

    def test_get_stderr_alive_process_does_not_call_proc_read(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        self.mock_proc.poll.return_value = None
        session._stderr_lines.append("active warning\n")

        stderr = session.get_stderr()
        self.assertEqual(stderr, "active warning\n")
        self.mock_proc.stderr.read.assert_not_called()

    def test_get_stderr_stopped_process_falls_back_to_read(self):
        session = StreamSession(agy_binary="agy")
        session.proc = self.mock_proc
        self.mock_proc.poll.return_value = 0
        self.mock_proc.stderr.read.return_value = "fallback stderr"

        stderr = session.get_stderr()
        self.assertEqual(stderr, "fallback stderr")
        self.mock_proc.stderr.read.assert_called_once()

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


class TestCleanWorkspaceDir(unittest.TestCase):
    def test_clean_workspace_dir_none(self):
        self.assertTrue(clean_workspace_dir(None))

    def test_clean_workspace_dir_non_existent(self):
        self.assertTrue(clean_workspace_dir("/path/does/not/exist/graviton_test_xyz"))

    def test_clean_workspace_dir_removes_tree(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="graviton-test-clean-"))
        (tmp / "subdir").mkdir()
        (tmp / "subdir" / "file.txt").write_text("hello")
        self.assertTrue(tmp.exists())
        self.assertTrue(clean_workspace_dir(tmp))
        self.assertFalse(tmp.exists())

    def test_clean_workspace_dir_readonly_files(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="graviton-test-clean-ro-"))
        ro_file = tmp / "readonly.txt"
        ro_file.write_text("protected")
        ro_file.chmod(0o400)
        self.assertTrue(clean_workspace_dir(tmp))
        self.assertFalse(tmp.exists())


class TestContainerSupervisor(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.repo_dir = Path(self.tmp_dir.name) / "mock_repo"
        self.repo_dir.mkdir()
        (self.repo_dir / "README.md").write_text("# Mock Repo")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_initialization_defaults(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="test1234",
            base_workspaces_dir=self.tmp_dir.name,
        )
        self.assertEqual(sup.agent_name, "code_reviewer")
        self.assertTrue(sup.remote_control)
        self.assertTrue(sup.dangerously_skip_permissions)
        self.assertEqual(sup.run_id, "test1234")
        self.assertEqual(sup.container_name, "graviton-stream-run-test1234")
        self.assertEqual(sup.temp_workspace, Path(self.tmp_dir.name) / "run-test1234")
        self.assertFalse(sup.is_alive())

    def test_prepare_workspace_cache_restore(self):
        cache_dir = Path(self.tmp_dir.name) / "cache"
        cache_dir.mkdir()
        (cache_dir / "cached_file.txt").write_text("cached content")

        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="cache_test",
            base_workspaces_dir=self.tmp_dir.name,
            cache_dir=cache_dir,
        )
        ws = sup.prepare_workspace()
        self.assertEqual(ws, sup.temp_workspace)
        self.assertTrue((ws / "cached_file.txt").exists())
        self.assertEqual((ws / "cached_file.txt").read_text(), "cached content")

    def test_prepare_workspace_clone_fallback(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="clone_test",
            base_workspaces_dir=self.tmp_dir.name,
        )
        ws = sup.prepare_workspace()
        self.assertTrue(ws.exists())
        self.assertTrue((ws / "README.md").exists())

    def test_build_docker_command(self):
        home_mock = Path(self.tmp_dir.name) / "fake_home"
        ssh_dir = home_mock / ".ssh"
        ssh_dir.mkdir(parents=True)
        gh_dir = home_mock / ".config" / "gh"
        gh_dir.mkdir(parents=True)
        cli_dir = home_mock / ".gemini" / "antigravity-cli"
        cli_dir.mkdir(parents=True)
        (cli_dir / "antigravity-oauth-token").write_text("fake-token")
        (cli_dir / "settings.json").write_text("{}")

        skills_dir = Path(self.tmp_dir.name) / "skills"
        skills_dir.mkdir()

        with patch("pathlib.Path.home", return_value=home_mock):
            sup = ContainerSupervisor(
                repo_dir=self.repo_dir,
                agent_name="tester",
                model="claude-3-sonnet",
                image_name="test-agent-image:custom",
                remote_control=True,
                dangerously_skip_permissions=True,
                extra_args=["--verbose"],
                run_id="cmd_test",
                base_workspaces_dir=self.tmp_dir.name,
                git_user_name="Test User",
                git_user_email="test@example.com",
                github_token="gh_secret_123",
                skills_dir=skills_dir,
                env={"MY_CUSTOM_VAR": "hello"},
                docker_binary="/usr/bin/docker",
                agy_binary="/usr/bin/agy",
            )
            sup.prepare_workspace()
            cmd = sup.build_docker_command()

            # Docker run invocation
            self.assertEqual(cmd[0], "/usr/bin/docker")
            self.assertEqual(cmd[1], "run")
            self.assertIn("-i", cmd)
            self.assertIn("--name", cmd)
            self.assertIn("graviton-stream-run-cmd_test", cmd)
            self.assertIn("--security-opt=no-new-privileges", cmd)

            # Workspace mount
            self.assertIn(f"{sup.temp_workspace.resolve()}:/workspace", cmd)
            self.assertIn("-w", cmd)
            self.assertIn("/workspace", cmd)

            # SSH and gh mounts
            self.assertIn(f"{ssh_dir.resolve()}:/root/.ssh:ro", cmd)
            self.assertIn(f"{gh_dir.resolve()}:/root/.config/gh:ro", cmd)

            # Antigravity CLI isolation: tmpfs and ro credentials
            self.assertIn("/root/.gemini/antigravity-cli:rw,exec", cmd)
            self.assertIn("/root/.gemini/config:rw,exec", cmd)
            self.assertIn(f"{(cli_dir / 'antigravity-oauth-token').resolve()}:/root/.gemini/antigravity-cli/antigravity-oauth-token:ro", cmd)
            self.assertIn(f"{(cli_dir / 'settings.json').resolve()}:/root/.gemini/antigravity-cli/settings.json:ro", cmd)

            # Skills mount
            self.assertIn(f"{skills_dir.resolve()}:/root/.gemini/config/skills:ro", cmd)

            # Environment variables
            self.assertIn("GITHUB_TOKEN=gh_secret_123", cmd)
            self.assertIn("GIT_AUTHOR_NAME=Test User", cmd)
            self.assertIn("GIT_AUTHOR_EMAIL=test@example.com", cmd)
            self.assertIn("ANTIGRAVITY_MODEL=claude-3-sonnet", cmd)
            self.assertIn("MY_CUSTOM_VAR=hello", cmd)

            # Image
            self.assertIn("test-agent-image:custom", cmd)

            # Inner agy command
            self.assertIn("agy", cmd)
            self.assertIn("--input-format", cmd)
            self.assertIn("stream-json", cmd)
            self.assertIn("--output-format", cmd)
            self.assertIn("--dangerously-skip-permissions", cmd)
            self.assertIn("--remote-control", cmd)
            self.assertIn("--agent", cmd)
            self.assertIn("tester", cmd)
            self.assertIn("--model", cmd)
            self.assertIn("claude-3-sonnet", cmd)
            self.assertIn("--verbose", cmd)

    def test_build_docker_command_with_custom_agy_args(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="custom_agy",
            base_workspaces_dir=self.tmp_dir.name,
        )
        sup.prepare_workspace()
        cmd = sup.build_docker_command(agy_args=["custom_binary", "--custom-arg"])
        self.assertIn("custom_binary", cmd)
        self.assertIn("--custom-arg", cmd)

    def test_start_and_lifecycle(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="lifecycle_test",
            base_workspaces_dir=self.tmp_dir.name,
        )

        with patch("lib.supervisor.StreamSession") as mock_session_cls:
            mock_session = MagicMock()
            mock_session.is_alive.return_value = True
            mock_session.start.return_value = "docker-conv-789"
            mock_session.conversation_id = "docker-conv-789"
            mock_session_cls.return_value = mock_session

            conv_id = sup.start(timeout=45.0)
            self.assertEqual(conv_id, "docker-conv-789")
            self.assertEqual(sup.conversation_id, "docker-conv-789")
            self.assertTrue(sup.is_alive())

            # Verify StreamSession was initialized with custom_command
            mock_session_cls.assert_called_once()
            call_kwargs = mock_session_cls.call_args[1]
            self.assertIn("custom_command", call_kwargs)
            self.assertEqual(call_kwargs["cwd"], sup.temp_workspace)
            mock_session.start.assert_called_once_with(timeout=45.0)

            # Calling start again while alive returns conversation_id without respawning
            conv_id2 = sup.start()
            self.assertEqual(conv_id2, "docker-conv-789")
            self.assertEqual(mock_session.start.call_count, 1)

    def test_send_prompt_and_receive_turn(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="turn_test",
            base_workspaces_dir=self.tmp_dir.name,
        )
        mock_session = MagicMock()
        mock_session.is_alive.return_value = True
        expected_result = SupervisorResult(status="SUCCESS", response="Turn complete")
        mock_session.receive_turn.return_value = expected_result
        sup.session = mock_session

        sup.send_prompt("Review the code")
        mock_session.send_prompt.assert_called_once_with("Review the code")

        cb = MagicMock()
        res = sup.receive_turn(timeout=30.0, on_event=cb)
        mock_session.receive_turn.assert_called_once_with(timeout=30.0, on_event=cb)
        self.assertEqual(res, expected_result)

    def test_send_prompt_raises_when_not_alive(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="not_alive",
            base_workspaces_dir=self.tmp_dir.name,
        )
        with self.assertRaises(SupervisorError):
            sup.send_prompt("hello")

    def test_receive_turn_raises_when_not_alive(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="not_alive",
            base_workspaces_dir=self.tmp_dir.name,
        )
        with self.assertRaises(SupervisorError):
            sup.receive_turn()

    def test_run_turn_auto_starts(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="auto_start",
            base_workspaces_dir=self.tmp_dir.name,
        )

        with patch.object(sup, "start") as mock_start, \
             patch.object(sup, "send_prompt") as mock_send, \
             patch.object(sup, "receive_turn") as mock_receive:
            expected = SupervisorResult(status="SUCCESS")
            mock_receive.return_value = expected

            res = sup.run_turn("Do work", timeout=20.0)
            mock_start.assert_called_once()
            mock_send.assert_called_once_with("Do work")
            mock_receive.assert_called_once_with(timeout=20.0, on_event=None)
            self.assertEqual(res, expected)

    def test_cleanup_and_context_manager(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="cleanup_test",
            base_workspaces_dir=self.tmp_dir.name,
        )
        sup.prepare_workspace()
        self.assertTrue(sup.temp_workspace.exists())

        mock_session = MagicMock()
        mock_session.is_alive.return_value = True
        sup.session = mock_session

        with patch("subprocess.run") as mock_run:
            sup.cleanup()

            mock_session.close.assert_called_once()
            # Verify docker rm -f was invoked
            rm_calls = [
                call for call in mock_run.call_args_list
                if len(call[0]) > 0 and "rm" in call[0][0] and "-f" in call[0][0]
            ]
            self.assertTrue(len(rm_calls) > 0)
            self.assertIn("graviton-stream-run-cleanup_test", rm_calls[0][0][0])
            self.assertFalse(sup.temp_workspace.exists())

    def test_context_manager(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="cm_test",
            base_workspaces_dir=self.tmp_dir.name,
        )
        with patch.object(sup, "start") as mock_start, patch.object(sup, "cleanup") as mock_cleanup:
            with sup as active_sup:
                self.assertEqual(active_sup, sup)
                mock_start.assert_called_once()
            mock_cleanup.assert_called_once()

    def test_run_container_turn_wrapper(self):
        with patch("lib.supervisor.ContainerSupervisor") as mock_cls:
            mock_inst = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_inst
            expected_res = SupervisorResult(status="SUCCESS", response="Container done")
            mock_inst.run_turn.return_value = expected_res

            res = run_container_turn(
                prompt="Fix bug",
                repo_dir=self.repo_dir,
                agent_name="patcher",
                model="gemini-1.5-pro",
                timeout=120.0,
            )

            mock_cls.assert_called_once()
            self.assertEqual(mock_cls.call_args[1]["agent_name"], "patcher")
            self.assertEqual(mock_cls.call_args[1]["model"], "gemini-1.5-pro")
            mock_inst.run_turn.assert_called_once_with("Fix bug", timeout=120.0, on_event=None)
            self.assertEqual(res, expected_res)

    def test_container_supervisor_run_goal(self):
        sup = ContainerSupervisor(
            repo_dir=self.repo_dir,
            run_id="goal_sup",
            base_workspaces_dir=self.tmp_dir.name,
        )
        with patch.object(sup, "run_turn") as mock_run_turn:
            expected = SupervisorResult(status="SUCCESS", response="Done <!-- GOAL_COMPLETE -->")
            mock_run_turn.return_value = expected

            res = sup.run_goal("Solve issue #5", timeout=30.0)
            mock_run_turn.assert_called_once_with(
                "/goal Solve issue #5",
                timeout=30.0,
                on_event=None,
                max_duration=1800.0,
            )
            self.assertEqual(res, expected)

    def test_run_goal_turn_and_run_container_goal_wrappers(self):
        with patch("lib.supervisor.StreamSession") as mock_stream_cls:
            mock_inst = MagicMock()
            mock_stream_cls.return_value.__enter__.return_value = mock_inst
            expected_res = SupervisorResult(status="SUCCESS", response="Done")
            mock_inst.run_goal.return_value = expected_res

            res1 = run_goal_turn("Fix tests", agent_name="fixer", timeout=60.0)
            mock_stream_cls.assert_called_once()
            mock_inst.run_goal.assert_called_once_with(
                "Fix tests",
                timeout=60.0,
                on_event=None,
                max_duration=1800.0,
            )
            self.assertEqual(res1, expected_res)

        with patch("lib.supervisor.ContainerSupervisor") as mock_container_cls:
            mock_inst = MagicMock()
            mock_container_cls.return_value.__enter__.return_value = mock_inst
            expected_res = SupervisorResult(status="SUCCESS", response="Done")
            mock_inst.run_goal.return_value = expected_res

            res2 = run_container_goal("Fix tests", repo_dir=self.repo_dir, timeout=60.0)
            mock_container_cls.assert_called_once()
            mock_inst.run_goal.assert_called_once_with(
                "Fix tests",
                timeout=60.0,
                on_event=None,
                max_duration=1800.0,
            )
            self.assertEqual(res2, expected_res)


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

