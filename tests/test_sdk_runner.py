"""
Unit tests for bin/agent_sdk_runner.py (Antigravity Python SDK Agent Runner).
"""

import asyncio
import io
import json
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from bin.agent_sdk_runner import (
    _extract_prompt,
    _flush_json,
    main_async,
    parse_args,
    run_agent_turn,
)


class TestAgentSdkRunner(unittest.TestCase):
    """Test suite for SDK agent runner helper functions and async loop."""

    def test_parse_args_defaults(self):
        args = parse_args([])
        self.assertEqual(args.input_format, "stream-json")
        self.assertEqual(args.output_format, "stream-json")
        self.assertTrue(args.dangerously_skip_permissions)
        self.assertFalse(args.remote_control)
        self.assertIsNone(args.model)
        self.assertIsNone(args.project)
        self.assertIsNone(args.conversation)

    def test_parse_args_custom(self):
        argv = [
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--model", "gemini-3.8-flash-medium",
            "--project", "test-project-123",
            "--agent", "code_reviewer",
            "--conversation", "abc12345-6789-abcd-ef01-23456789abcd",
            "--workspace", "/tmp/test-workspace",
            "--remote-control",
            "--unknown-custom-flag", "value",
        ]
        args = parse_args(argv)
        self.assertEqual(args.model, "gemini-3.8-flash-medium")
        self.assertEqual(args.project, "test-project-123")
        self.assertEqual(args.agent, "code_reviewer")
        self.assertEqual(args.conversation, "abc12345-6789-abcd-ef01-23456789abcd")
        self.assertEqual(args.workspace, "/tmp/test-workspace")
        self.assertTrue(args.remote_control)

    def test_extract_prompt(self):
        # Direct prompt field
        self.assertEqual(_extract_prompt({"prompt": "Hello world"}), "Hello world")

        # Standard stream-json message content list
        payload = {
            "event": "user",
            "message": {
                "content": [
                    {"type": "text", "text": "Fix this bug"},
                ]
            },
        }
        self.assertEqual(_extract_prompt(payload), "Fix this bug")

        # Message with direct text string
        self.assertEqual(_extract_prompt({"message": {"text": "Simple prompt"}}), "Simple prompt")

        # Message content is plain string
        self.assertEqual(_extract_prompt({"message": {"content": "Direct content string"}}), "Direct content string")

        # Fallback text field
        self.assertEqual(_extract_prompt({"text": "Fallback text"}), "Fallback text")

    def test_flush_json(self):
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            _flush_json({"event": "init", "conversation_id": "test-cid"})
        output = buf.getvalue()
        self.assertTrue(output.endswith("\n"))
        parsed = json.loads(output)
        self.assertEqual(parsed["event"], "init")
        self.assertEqual(parsed["conversation_id"], "test-cid")

    def test_run_agent_turn_success(self):
        async def _test():
            mock_agent = MagicMock()

            # Mock chunks
            class MockText:
                def __init__(self, text):
                    self.text = text

            class MockThought:
                def __init__(self, text):
                    self.text = text

            class MockToolCall:
                def __init__(self, name, args, call_id):
                    self.name = name
                    self.args = args
                    self.id = call_id

            async def _chunk_iter():
                yield MockThought("Thinking about the solution...")
                yield MockToolCall("run_command", {"command": "pytest"}, "call-1")
                yield MockText("All tests passed.")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="All tests passed.")
            mock_usage = MagicMock()
            mock_usage.prompt_token_count = 100
            mock_usage.candidates_token_count = 50
            mock_usage.total_token_count = 150
            mock_response.usage_metadata = mock_usage

            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Run tests", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["response"], "All tests passed.")
            self.assertEqual(result["num_turns"], 1)
            self.assertEqual(result["usage"]["total_tokens"], 150)

            # Check streamed NDJSON events
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            event_types = [l.get("event") for l in lines]
            self.assertIn("step_update", event_types)

            # Check thought was streamed
            thought_events = [l for l in lines if l.get("step_update", {}).get("thought")]
            self.assertEqual(len(thought_events), 1)
            self.assertEqual(thought_events[0]["step_update"]["thought"], "Thinking about the solution...")

            # Check tool call was streamed
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0]["step_update"]["tool_name"], "run_command")

        asyncio.run(_test())

    def test_run_agent_turn_exception(self):
        async def _test():
            mock_agent = MagicMock()
            mock_agent.chat = AsyncMock(side_effect=RuntimeError("Model quota exceeded"))

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Do work", turn_index=2)

            self.assertEqual(result["status"], "FAILED")
            self.assertIn("Model quota exceeded", result["error"])
            self.assertEqual(result["num_turns"], 2)

        asyncio.run(_test())

    def test_main_async_missing_sdk_import(self):
        args = parse_args([])
        with patch.dict(sys.modules, {"google.antigravity": None}):
            err_buf = io.StringIO()
            with patch.object(sys, "stderr", err_buf):
                exit_code = asyncio.run(main_async(args))
            self.assertEqual(exit_code, 1)
            self.assertIn("google-antigravity SDK is not installed", err_buf.getvalue())

    def test_main_async_full_lifecycle(self):
        async def _test():
            mock_sdk = MagicMock()
            mock_agent_instance = MagicMock()
            mock_conversation = MagicMock()
            mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
            mock_agent_instance.conversation = mock_conversation

            # Async context manager mock for Agent
            mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
            mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

            # Mock stdin with a prompt line followed by EOF
            stdin_content = json.dumps({"event": "user", "prompt": "Hello"}) + "\n"
            stdin_mock = io.StringIO(stdin_content)
            stdout_mock = io.StringIO()

            args = parse_args(["--conversation", "test-conv-uuid-1234567890123456"])

            with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                 patch.object(sys, "stdin", stdin_mock), \
                 patch.object(sys, "stdout", stdout_mock), \
                 patch("bin.agent_sdk_runner.run_agent_turn", new_callable=AsyncMock) as mock_turn:
                mock_turn.return_value = {
                    "status": "SUCCESS",
                    "response": "Hello back!",
                    "duration_seconds": 0.5,
                    "num_turns": 1,
                    "usage": {},
                }
                exit_code = await main_async(args)

            self.assertEqual(exit_code, 0)
            lines = [json.loads(l) for l in stdout_mock.getvalue().splitlines() if l.strip()]

            # Line 1: init event
            self.assertEqual(lines[0]["event"], "init")
            self.assertEqual(lines[0]["conversation_id"], "test-conv-uuid-1234567890123456")

            # Line 2: result event
            self.assertEqual(lines[1]["event"], "result")
            self.assertEqual(lines[1]["result"]["status"], "SUCCESS")
            self.assertEqual(lines[1]["result"]["response"], "Hello back!")

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()
