"""
Unit tests for bin/agent_sdk_runner.py (Antigravity Python SDK Agent Runner).
"""

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
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

    def setUp(self):
        super().setUp()
        import bin.agent_sdk_runner as runner_module
        self._orig_ag_types = runner_module.ag_types

    def tearDown(self):
        import bin.agent_sdk_runner as runner_module
        runner_module.ag_types = self._orig_ag_types
        super().tearDown()

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

    def test_parse_args_no_dangerously_skip_permissions(self):
        args = parse_args(["--no-dangerously-skip-permissions"])
        self.assertFalse(args.dangerously_skip_permissions)

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

        # Message is plain string
        self.assertEqual(_extract_prompt({"event": "user", "message": "Execute task"}), "Execute task")

        # Fallback text field
        self.assertEqual(_extract_prompt({"text": "Fallback text"}), "Fallback text")

        # Fallback with non-serializable object uses default=str
        payload_with_path = {"custom": Path("/tmp/test")}
        self.assertIn("/tmp/test", _extract_prompt(payload_with_path))

    def test_flush_json(self):
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            _flush_json({"event": "init", "conversation_id": "test-cid"})
        output = buf.getvalue()
        self.assertTrue(output.endswith("\n"))
        parsed = json.loads(output)
        self.assertEqual(parsed["event"], "init")
        self.assertEqual(parsed["conversation_id"], "test-cid")

    def test_flush_json_with_non_serializable(self):
        """Verify _flush_json protects against TypeError by serializing via default=str."""
        buf = io.StringIO()
        with patch.object(sys, "stdout", buf):
            _flush_json({"event": "step_update", "path": Path("/workspace/file.txt")})
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["path"], "/workspace/file.txt")

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

    def test_run_agent_turn_dict_usage_metadata(self):
        async def _test():
            mock_agent = MagicMock()

            class MockText:
                def __init__(self, text: str):
                    self.text = text

            async def _chunk_iter():
                yield MockText("All tests passed.")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="All tests passed.")
            mock_response.usage_metadata = {
                "prompt_token_count": 120,
                "candidates_token_count": 60,
                "total_token_count": 180,
            }
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Run tests", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["response"], "All tests passed.")
            self.assertEqual(result["num_turns"], 1)
            self.assertEqual(result["usage"]["prompt_tokens"], 120)
            self.assertEqual(result["usage"]["candidates_tokens"], 60)
            self.assertEqual(result["usage"]["total_tokens"], 180)

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

    def test_run_agent_turn_empty_prompt(self):
        async def _test():
            mock_agent = MagicMock()
            mock_agent.chat = AsyncMock()

            # Empty string
            result1 = await run_agent_turn(mock_agent, "", turn_index=1)
            self.assertEqual(result1["status"], "FAILED")
            self.assertEqual(result1["error"], "Empty prompt provided")
            self.assertEqual(result1["response"], "")
            mock_agent.chat.assert_not_called()

            # Whitespace string
            result2 = await run_agent_turn(mock_agent, "   \n\t  ", turn_index=2)
            self.assertEqual(result2["status"], "FAILED")
            self.assertEqual(result2["error"], "Empty prompt provided")
            mock_agent.chat.assert_not_called()

        asyncio.run(_test())

    def test_run_agent_turn_tool_result_streaming(self):
        async def _test():
            mock_agent = MagicMock()

            class MockToolResult:
                def __init__(self, name, result, call_id, error=None):
                    self.name = name
                    self.result = result
                    self.id = call_id
                    self.error = error

            async def _chunk_iter():
                yield MockToolResult("run_command", "0", "call-1", error=None)
                yield MockToolResult("read_file", None, "call-2", error="File not found")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Finished execution.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Execute command", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 2)
            self.assertEqual(tool_events[0]["step_update"]["state"], "DONE")
            self.assertEqual(tool_events[0]["step_update"]["tool_name"], "run_command")
            self.assertEqual(tool_events[0]["step_update"]["id"], "call-1")
            self.assertEqual(tool_events[0]["step_update"]["call_id"], "call-1")
            self.assertEqual(tool_events[0]["step_update"]["tool_call_id"], "call-1")
            self.assertEqual(tool_events[0]["step_update"]["result"], "0")
            self.assertNotIn("error", tool_events[0]["step_update"])

            self.assertEqual(tool_events[1]["step_update"]["state"], "ERROR")
            self.assertEqual(tool_events[1]["step_update"]["tool_name"], "read_file")
            self.assertEqual(tool_events[1]["step_update"]["id"], "call-2")
            self.assertEqual(tool_events[1]["step_update"]["call_id"], "call-2")
            self.assertEqual(tool_events[1]["step_update"]["tool_call_id"], "call-2")
            self.assertEqual(tool_events[1]["step_update"]["error"], "File not found")

        asyncio.run(_test())

    def test_run_agent_turn_raw_string_chunks(self):
        """Verify response.chunks yielding plain str chunks emits text_delta events."""
        async def _test():
            mock_agent = MagicMock()

            async def _chunk_iter():
                yield "Chunk 1 "
                yield "Chunk 2"

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Say chunks", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["response"], "Chunk 1 Chunk 2")

            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            text_events = [l for l in lines if l.get("step_update", {}).get("text_delta")]
            self.assertEqual(len(text_events), 2)
            self.assertEqual(text_events[0]["step_update"]["text_delta"], "Chunk 1 ")
            self.assertEqual(text_events[1]["step_update"]["text_delta"], "Chunk 2")
            self.assertEqual(text_events[0]["step_update"]["step_type"], "agent_response")

        asyncio.run(_test())

    def test_run_agent_turn_tool_result_duration_seconds(self):
        """Verify duration_seconds on tool result chunk is forwarded in step_update."""
        async def _test():
            mock_agent = MagicMock()

            class MockToolResult:
                def __init__(self, name, result, call_id, duration_seconds=None):
                    self.name = name
                    self.result = result
                    self.id = call_id
                    self.duration_seconds = duration_seconds

            async def _chunk_iter():
                yield MockToolResult("run_command", "0", "call-1", duration_seconds=1.25)

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Done.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Execute", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0]["step_update"]["duration_seconds"], 1.25)

        asyncio.run(_test())

    def test_run_agent_turn_enum_tool_name_unwrapping(self):
        """Verify enum tool names are unwrapped to their .value string."""
        async def _test():
            from enum import Enum
            class MockBuiltinTools(Enum):
                RUN_COMMAND = "run_command"
                READ_FILE = "read_file"

            mock_agent = MagicMock()

            class MockToolCall:
                def __init__(self, name, args, call_id):
                    self.name = name
                    self.args = args
                    self.id = call_id

            class MockToolResult:
                def __init__(self, name, result, call_id):
                    self.name = name
                    self.result = result
                    self.id = call_id

            async def _chunk_iter():
                yield MockToolCall(MockBuiltinTools.RUN_COMMAND, {"command": "ls"}, "call-enum-1")
                yield MockToolResult(MockBuiltinTools.RUN_COMMAND, "file.txt", "call-enum-1")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Done.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "List files", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 2)
            self.assertEqual(tool_events[0]["step_update"]["tool_name"], "run_command")
            self.assertEqual(tool_events[1]["step_update"]["tool_name"], "run_command")

        asyncio.run(_test())

    def test_run_agent_turn_tool_call_none_args(self):
        """Verify chunk.args=None produces empty dictionary for tool_info."""
        async def _test():
            mock_agent = MagicMock()

            class MockToolCall:
                def __init__(self, name, args, call_id):
                    self.name = name
                    self.args = args
                    self.id = call_id

            async def _chunk_iter():
                yield MockToolCall("custom_tool", None, "call-none-args")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Done.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Run tool", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0]["step_update"]["tool_name"], "custom_tool")
            self.assertEqual(tool_events[0]["step_update"]["tool_info"], {})

        asyncio.run(_test())

    def test_run_agent_turn_text_delta_fallback(self):
        async def _test():
            mock_agent = MagicMock()

            class MockText:
                def __init__(self, text):
                    self.text = text

            async def _chunk_iter():
                yield MockText("First line. ")
                yield MockText("Second line.")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            # Simulate response.text() returning None/empty
            mock_response.text = AsyncMock(return_value="")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Explain", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["response"], "First line. Second line.")

        asyncio.run(_test())

    def test_main_async_project_and_skills_paths(self):
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                default_skills = fake_home / ".gemini" / "config" / "skills"
                default_skills.mkdir(parents=True)

                app_data = Path(temp_dir) / "appdata"
                cli_skills = app_data / "skills"
                cli_skills.mkdir(parents=True)

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--project", "vertex-project-xyz",
                    "--app-data-dir", str(app_data),
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                mock_sdk.LocalAgentConfig.assert_called_once()
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                self.assertEqual(call_kwargs.get("project"), "vertex-project-xyz")
                skills = call_kwargs.get("skills_paths")
                self.assertIsNotNone(skills)
                self.assertIn(str(default_skills), skills)
                self.assertIn(str(cli_skills), skills)

        asyncio.run(_test())

    def test_main_async_remote_control_handshake(self):
        async def _test():
            mock_sdk = MagicMock()
            mock_agent_instance = MagicMock()
            mock_conversation = MagicMock()
            mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
            mock_agent_instance.conversation = mock_conversation

            mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
            mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

            stdout_mock = io.StringIO()
            args = parse_args([
                "--conversation", "test-conv-uuid-1234567890123456",
                "--remote-control",
            ])

            env = {
                "ANTIGRAVITY_REMOTE_CONTROL_BASE_URL": "https://remote.example.com/",
                "ANTIGRAVITY_INSTANCE_NAME": "instance-node-1",
            }
            with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                 patch.dict(os.environ, env), \
                 patch.object(sys, "stdin", io.StringIO("")), \
                 patch.object(sys, "stdout", stdout_mock):
                exit_code = await main_async(args)

            self.assertEqual(exit_code, 0)
            lines = [json.loads(l) for l in stdout_mock.getvalue().splitlines() if l.strip()]
            self.assertTrue(len(lines) >= 1)
            init_event = lines[0]
            expected_url = "https://remote.example.com/c/test-conv-uuid-1234567890123456?instance=instance-node-1"
            self.assertEqual(init_event["remote_control_url"], expected_url)
            self.assertEqual(init_event["init"]["remote_control_url"], expected_url)

        asyncio.run(_test())

    def test_main_async_no_dangerously_skip_permissions_policies(self):
        async def _test():
            mock_sdk = MagicMock()
            mock_agent_instance = MagicMock()
            mock_conversation = MagicMock()
            mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
            mock_agent_instance.conversation = mock_conversation

            mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
            mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

            args = parse_args([
                "--conversation", "test-conv-uuid-1234567890123456",
                "--no-dangerously-skip-permissions",
            ])

            with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                 patch.object(sys, "stdin", io.StringIO("")), \
                 patch.object(sys, "stdout", io.StringIO()):
                exit_code = await main_async(args)

            self.assertEqual(exit_code, 0)
            mock_sdk.LocalAgentConfig.assert_called_once()
            call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
            self.assertIsNone(call_kwargs.get("policies"))
            mock_sdk.policy.allow_all.assert_not_called()

        asyncio.run(_test())

    def test_run_agent_turn_chunk_classification_subclasses(self):
        """Verify chunks that subclass Thought/Text or end with Thought are properly classified."""
        async def _test():
            mock_agent = MagicMock()

            # Define or mock ag_types
            try:
                from google.antigravity import types as ag_types
            except Exception:
                class MockBaseThought:
                    def __init__(self, text="", step_index=0, **kwargs):
                        self.text = text
                        self.step_index = step_index

                class MockBaseText:
                    def __init__(self, text="", step_index=0, **kwargs):
                        self.text = text
                        self.step_index = step_index

                class MockAgTypes:
                    Thought = MockBaseThought
                    Text = MockBaseText

                ag_types = MockAgTypes

            class SpecialIdea(ag_types.Thought):
                pass

            thought_instance = SpecialIdea(step_index=0, text="Deep thought from subclass")

            # Mock class ending with Thought but not inheriting types.Thought
            class CustomAgentThought:
                def __init__(self, text):
                    self.text = text

            # Subclass of Text
            class CustomTextChunk(ag_types.Text):
                pass

            text_instance = CustomTextChunk(step_index=1, text="Result text")

            async def _chunk_iter():
                yield thought_instance
                yield CustomAgentThought("Custom agent thought")
                yield text_instance

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Result text")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch("bin.agent_sdk_runner.ag_types", ag_types), \
                 patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Think and answer", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            thought_events = [l for l in lines if l.get("step_update", {}).get("thought")]
            text_events = [l for l in lines if l.get("step_update", {}).get("text_delta")]

            # Both SpecialIdea and CustomAgentThought must be classified as thoughts
            self.assertEqual(len(thought_events), 2)
            self.assertEqual(thought_events[0]["step_update"]["thought"], "Deep thought from subclass")
            self.assertEqual(thought_events[1]["step_update"]["thought"], "Custom agent thought")

            # Only CustomTextChunk should be classified as text_delta
            self.assertEqual(len(text_events), 1)
            self.assertEqual(text_events[0]["step_update"]["text_delta"], "Result text")

        asyncio.run(_test())

    def test_run_agent_turn_tool_result_with_text_attribute(self):
        """Verify tool results with a .text attribute are emitted as tool events and not text_delta."""
        async def _test():
            mock_agent = MagicMock()

            class ToolResultChunk:
                def __init__(self, name, result, text, tool_call_id="call-123"):
                    self.name = name
                    self.result = result
                    self.text = text
                    self.id = tool_call_id

            async def _chunk_iter():
                yield ToolResultChunk("run_command", "output data", text="output data text")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Final response")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Execute command", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            text_events = [l for l in lines if l.get("step_update", {}).get("text_delta")]

            self.assertEqual(len(tool_events), 1)
            self.assertEqual(tool_events[0]["step_update"]["tool_name"], "run_command")
            self.assertEqual(tool_events[0]["step_update"]["result"], "output data")
            self.assertEqual(tool_events[0]["step_update"]["state"], "DONE")
            # Must NOT be classified as text_delta
            self.assertEqual(len(text_events), 0)

        asyncio.run(_test())

    def test_main_async_agent_persona_instructions_loading(self):
        """Verify persona instructions are loaded from agent.md when --agent is specified."""
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                agent_dir = fake_home / ".gemini" / "config" / "agents" / "code_fixer"
                agent_dir.mkdir(parents=True)
                agent_md = agent_dir / "agent.md"
                agent_md.write_text("# Code Fixer Agent Persona\nResolve review feedback.", encoding="utf-8")

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--agent", "code_fixer",
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                mock_sdk.LocalAgentConfig.assert_called_once()
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                self.assertEqual(call_kwargs.get("system_instructions"), "# Code Fixer Agent Persona\nResolve review feedback.")

        asyncio.run(_test())

    def test_main_async_agent_persona_flat_md_loading(self):
        """Verify persona instructions are loaded from {agent}.md if directory agent.md does not exist."""
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                agents_dir = fake_home / ".gemini" / "config" / "agents"
                agents_dir.mkdir(parents=True)
                agent_flat_md = agents_dir / "code_reviewer.md"
                agent_flat_md.write_text("# Code Reviewer Persona", encoding="utf-8")

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--agent", "code_reviewer",
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                self.assertEqual(call_kwargs.get("system_instructions"), "# Code Reviewer Persona")

        asyncio.run(_test())

    def test_main_async_agent_persona_precedence(self):
        """Verify explicit --instructions takes precedence over --agent persona file."""
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                agent_dir = fake_home / ".gemini" / "config" / "agents" / "code_fixer"
                agent_dir.mkdir(parents=True)
                (agent_dir / "agent.md").write_text("Persona from file", encoding="utf-8")

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--agent", "code_fixer",
                    "--instructions", "Explicit instruction override",
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                self.assertEqual(call_kwargs.get("system_instructions"), "Explicit instruction override")

        asyncio.run(_test())

    def test_main_async_workspace_skills_discovery(self):
        """Verify repository and workspace-level skills are discovered in skills_paths."""
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                fake_workspace = Path(temp_dir) / "workspace"
                fake_workspace.mkdir(parents=True)

                dot_agents_skills = fake_workspace / ".agents" / "skills"
                dot_agents_skills.mkdir(parents=True)
                ws_skills = fake_workspace / "skills"
                ws_skills.mkdir(parents=True)
                plugin_skills = fake_workspace / "plugin" / "skills"
                plugin_skills.mkdir(parents=True)

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--workspace", str(fake_workspace),
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                skills = call_kwargs.get("skills_paths")
                self.assertIsNotNone(skills)
                self.assertIn(str(dot_agents_skills), skills)
                self.assertIn(str(ws_skills), skills)
                self.assertIn(str(plugin_skills), skills)

        asyncio.run(_test())

    def test_run_agent_turn_tool_result_with_args_and_result_precedence(self):
        """Verify tool result chunks retaining invocation args are classified as DONE and not RUNNING."""
        async def _test():
            mock_agent = MagicMock()

            class MockToolResultWithArgs:
                def __init__(self, name, args, result, call_id):
                    self.name = name
                    self.args = args
                    self.result = result
                    self.id = call_id

            async def _chunk_iter():
                yield MockToolResultWithArgs("run_command", {"command": "ls -la"}, "file1.txt\nfile2.txt", "call-exec-1")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Command output processed.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Run ls", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 1)
            step = tool_events[0]["step_update"]
            self.assertEqual(step["state"], "DONE")
            self.assertEqual(step["tool_name"], "run_command")
            self.assertEqual(step["result"], "file1.txt\nfile2.txt")
            self.assertEqual(step["id"], "call-exec-1")

        asyncio.run(_test())

    def test_run_agent_turn_tool_result_with_args_and_error(self):
        """Verify tool execution chunks that fail with error or exception are classified as ERROR."""
        async def _test():
            mock_agent = MagicMock()

            class MockToolErrorWithArgs:
                def __init__(self, name, args, error, call_id):
                    self.name = name
                    self.args = args
                    self.error = error
                    self.id = call_id

            class MockToolExceptionWithArgs:
                def __init__(self, name, args, exception, call_id):
                    self.name = name
                    self.args = args
                    self.exception = exception
                    self.id = call_id

            async def _chunk_iter():
                yield MockToolErrorWithArgs("read_file", {"path": "/no/such/file"}, "Permission denied", "call-err-1")
                yield MockToolExceptionWithArgs("write_file", {"path": "/read/only"}, RuntimeError("Disk full"), "call-err-2")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Errors encountered.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Perform file ops", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 2)

            step1 = tool_events[0]["step_update"]
            self.assertEqual(step1["state"], "ERROR")
            self.assertEqual(step1["tool_name"], "read_file")
            self.assertEqual(step1["error"], "Permission denied")

            step2 = tool_events[1]["step_update"]
            self.assertEqual(step2["state"], "ERROR")
            self.assertEqual(step2["tool_name"], "write_file")
            self.assertIn("Disk full", step2["error"])

        asyncio.run(_test())

    def test_run_agent_turn_mock_ag_types_safety(self):
        """Verify run_agent_turn functions reliably when ag_types contains mock or non-type attributes."""
        async def _test():
            import bin.agent_sdk_runner as runner_module
            mock_types = MagicMock()
            runner_module.ag_types = mock_types

            mock_agent = MagicMock()

            class CustomThought:
                def __init__(self, text):
                    self.text = text

            class CustomToolCall:
                def __init__(self, name, args, call_id):
                    self.name = name
                    self.args = args
                    self.id = call_id

            class CustomToolResult:
                def __init__(self, name, result, call_id):
                    self.name = name
                    self.result = result
                    self.id = call_id

            class CustomText:
                def __init__(self, text):
                    self.text = text

            async def _chunk_iter():
                yield CustomThought("Safe thought without type collision")
                yield CustomToolCall("check_status", {"verbose": True}, "call-safe-1")
                yield CustomToolResult("check_status", {"ok": True}, "call-safe-1")
                yield CustomText("Turn finished safely.")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Turn finished safely.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Perform safe turn", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["response"], "Turn finished safely.")

            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            thought_events = [l for l in lines if l.get("step_update", {}).get("thought")]
            self.assertEqual(len(thought_events), 1)
            self.assertEqual(thought_events[0]["step_update"]["thought"], "Safe thought without type collision")

            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]
            self.assertEqual(len(tool_events), 2)
            self.assertEqual(tool_events[0]["step_update"]["tool_name"], "check_status")
            self.assertEqual(tool_events[0]["step_update"]["state"], "RUNNING")
            self.assertEqual(tool_events[1]["step_update"]["tool_name"], "check_status")
            self.assertEqual(tool_events[1]["step_update"]["state"], "DONE")

        asyncio.run(_test())

    def test_main_async_synchronizes_ag_types(self):
        """Verify main_async synchronizes the module-level ag_types if initially None."""
        async def _test():
            import bin.agent_sdk_runner as runner_module
            original_ag_types = runner_module.ag_types
            try:
                runner_module.ag_types = None

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args(["--conversation", "test-conv-uuid-1234567890123456"])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                self.assertIsNotNone(runner_module.ag_types)
                self.assertEqual(runner_module.ag_types, mock_sdk.types)
            finally:
                runner_module.ag_types = original_ag_types

        asyncio.run(_test())

    def test_main_async_workspace_plugins_skills_and_agents_discovery(self):
        """Verify plugin skills and agent personas under .agents/plugins are discovered."""
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                fake_workspace = Path(temp_dir) / "workspace"
                fake_workspace.mkdir(parents=True)

                # Workspace plugin structure: .agents/plugins/graviton/skills and .agents/plugins/graviton/agents
                plugin_skills = fake_workspace / ".agents" / "plugins" / "graviton" / "skills"
                plugin_skills.mkdir(parents=True)
                plugin_agents = fake_workspace / ".agents" / "plugins" / "graviton" / "agents"
                plugin_agents.mkdir(parents=True)
                (plugin_agents / "plugin_bot.md").write_text("# Plugin Bot Persona", encoding="utf-8")

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--workspace", str(fake_workspace),
                    "--agent", "plugin_bot",
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                skills = call_kwargs.get("skills_paths")
                self.assertIsNotNone(skills)
                self.assertIn(str(plugin_skills), skills)
                self.assertEqual(call_kwargs.get("system_instructions"), "# Plugin Bot Persona")

        asyncio.run(_test())

    def test_main_async_plugin_flat_agent_md_discovery(self):
        """Verify plugin/agents/{agent}.md is discovered when directory agent.md does not exist."""
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                fake_workspace = Path(temp_dir) / "workspace"
                fake_workspace.mkdir(parents=True)

                plugin_agents = fake_workspace / "plugin" / "agents"
                plugin_agents.mkdir(parents=True)
                (plugin_agents / "custom_agent.md").write_text("# Custom Plugin Agent Persona", encoding="utf-8")

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--workspace", str(fake_workspace),
                    "--agent", "custom_agent",
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                self.assertEqual(call_kwargs.get("system_instructions"), "# Custom Plugin Agent Persona")

        asyncio.run(_test())

    def test_main_async_ignores_non_dict_json_on_stdin(self):
        """Verify non-object JSON on stdin is discarded without terminating the runner loop."""
        async def _test():
            mock_sdk = MagicMock()
            mock_agent_instance = MagicMock()
            mock_conversation = MagicMock()
            mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
            mock_agent_instance.conversation = mock_conversation

            mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
            mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
            mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

            # Supply non-dict JSON lines (array, string, int), followed by a valid user event and then exit
            stdin_content = (
                "[1, 2, 3]\n"
                "\"just a string\"\n"
                "42\n"
                + json.dumps({"event": "user", "prompt": "Process after non-dict JSON"}) + "\n"
                + json.dumps({"event": "exit"}) + "\n"
            )
            stdin_mock = io.StringIO(stdin_content)
            stdout_mock = io.StringIO()
            stderr_mock = io.StringIO()

            args = parse_args(["--conversation", "test-conv-uuid-1234567890123456"])

            with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                 patch.object(sys, "stdin", stdin_mock), \
                 patch.object(sys, "stdout", stdout_mock), \
                 patch.object(sys, "stderr", stderr_mock), \
                 patch("bin.agent_sdk_runner.run_agent_turn", new_callable=AsyncMock) as mock_turn:
                mock_turn.return_value = {
                    "status": "SUCCESS",
                    "response": "Processed successfully",
                    "duration_seconds": 0.2,
                    "num_turns": 1,
                    "usage": {},
                }
                exit_code = await main_async(args)

            self.assertEqual(exit_code, 0)
            stderr_val = stderr_mock.getvalue()
            self.assertIn("Expected JSON object on stdin, got list", stderr_val)
            self.assertIn("Expected JSON object on stdin, got str", stderr_val)
            self.assertIn("Expected JSON object on stdin, got int", stderr_val)

            lines = [json.loads(l) for l in stdout_mock.getvalue().splitlines() if l.strip()]
            event_types = [l.get("event") for l in lines]
            self.assertIn("init", event_types)
            self.assertIn("result", event_types)

        asyncio.run(_test())

    def test_run_agent_turn_thought_chunk_with_thought_attribute(self):
        """Verify thought chunks exposing .thought or .thinking are extracted correctly into the thought event."""
        async def _test():
            mock_agent = MagicMock()

            class CustomThought:
                def __init__(self, thought):
                    self.thought = thought

            class ThinkingThought:
                def __init__(self, thinking):
                    self.thinking = thinking

            class CustomChunkWithThought:
                def __init__(self, thought):
                    self.thought = thought

            class CustomChunkWithThinking:
                def __init__(self, thinking):
                    self.thinking = thinking

            async def _chunk_iter():
                yield CustomThought("Reasoning step 1 using .thought on Thought class")
                yield ThinkingThought("Reasoning step 2 using .thinking on Thought class")
                yield CustomChunkWithThought("Reasoning step 3 using .thought on generic object")
                yield CustomChunkWithThinking("Reasoning step 4 using .thinking on generic object")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Final response.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Think carefully", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            thought_events = [l for l in lines if l.get("step_update", {}).get("thought")]
            self.assertEqual(len(thought_events), 4)
            self.assertEqual(thought_events[0]["step_update"]["thought"], "Reasoning step 1 using .thought on Thought class")
            self.assertEqual(thought_events[1]["step_update"]["thought"], "Reasoning step 2 using .thinking on Thought class")
            self.assertEqual(thought_events[2]["step_update"]["thought"], "Reasoning step 3 using .thought on generic object")
            self.assertEqual(thought_events[3]["step_update"]["thought"], "Reasoning step 4 using .thinking on generic object")

        asyncio.run(_test())

    def test_extract_prompt_nested_prompt_dict(self):
        """Verify nested dictionary and list structures in prompt fields are parsed accurately."""
        # Nested prompt dict with text
        self.assertEqual(_extract_prompt({"prompt": {"text": "Analyze this file"}}), "Analyze this file")

        # Nested prompt dict with content string
        self.assertEqual(_extract_prompt({"prompt": {"content": "Inspect repository"}}), "Inspect repository")

        # Nested prompt dict with content list
        payload_list = {
            "prompt": {
                "content": [
                    {"type": "text", "text": "Refactor "},
                    {"type": "text", "text": "agent module"},
                ]
            }
        }
        self.assertEqual(_extract_prompt(payload_list), "Refactor agent module")

        # Prompt field as list of items
        payload_direct_list = {
            "prompt": [
                {"type": "text", "text": "Run "},
                {"type": "text", "text": "tests"},
            ]
        }
        self.assertEqual(_extract_prompt(payload_direct_list), "Run tests")

        # Nested prompt dict with inner prompt string
        self.assertEqual(_extract_prompt({"prompt": {"prompt": "Inner prompt string"}}), "Inner prompt string")

    def test_main_async_agent_persona_traversal_sanitization(self):
        """Verify that agent names with directory traversal characters are rejected."""
        async def _test():
            with tempfile.TemporaryDirectory() as temp_dir:
                fake_home = Path(temp_dir) / "home"
                fake_home.mkdir(parents=True)
                traversal_target = fake_home / "evil.md"
                traversal_target.write_text("# Malicious persona", encoding="utf-8")

                mock_sdk = MagicMock()
                mock_agent_instance = MagicMock()
                mock_conversation = MagicMock()
                mock_conversation.conversation_id = "test-conv-uuid-1234567890123456"
                mock_agent_instance.conversation = mock_conversation

                mock_sdk.Agent.return_value.__aenter__ = AsyncMock(return_value=mock_agent_instance)
                mock_sdk.Agent.return_value.__aexit__ = AsyncMock(return_value=None)
                mock_sdk.types.SessionContinuationMode.CREATE_OR_RESUME = "create_or_resume"

                args = parse_args([
                    "--conversation", "test-conv-uuid-1234567890123456",
                    "--agent", "../../evil",
                ])

                with patch.dict(sys.modules, {"google.antigravity": mock_sdk}), \
                     patch.object(Path, "home", return_value=fake_home), \
                     patch.object(sys, "stdin", io.StringIO("")), \
                     patch.object(sys, "stdout", io.StringIO()):
                    exit_code = await main_async(args)

                self.assertEqual(exit_code, 0)
                mock_sdk.LocalAgentConfig.assert_called_once()
                call_kwargs = mock_sdk.LocalAgentConfig.call_args.kwargs
                self.assertIsNone(call_kwargs.get("system_instructions"))

        asyncio.run(_test())

    def test_run_agent_turn_chunk_classification_collision_prevention(self):
        """Verify tool calls and tool results with thought/thinking attributes are not misclassified as thoughts."""
        async def _test():
            mock_agent = MagicMock()

            class ToolCallWithThought:
                def __init__(self, name, args, call_id):
                    self.name = name
                    self.args = args
                    self.id = call_id
                    self.thought = "Tool thinking attribute"
                    self.thinking = "Tool thinking step"

            class ToolResultWithThought:
                def __init__(self, name, result, call_id):
                    self.name = name
                    self.result = result
                    self.id = call_id
                    self.thought = "Tool result thought attribute"

            class ThoughtChunkWithCallable:
                def __init__(self, thinking_val):
                    self.thinking = thinking_val

                def thought(self):
                    return "should not be called"

            async def _chunk_iter():
                yield ToolCallWithThought("search_code", {"query": "supervisor"}, "call-col-1")
                yield ToolResultWithThought("search_code", {"found": True}, "call-col-1")
                yield ThoughtChunkWithCallable("Actual thinking string")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Done.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Investigate collisions", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]

            tool_calls = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool" and l.get("step_update", {}).get("state") == "RUNNING"]
            tool_results = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool" and l.get("step_update", {}).get("state") == "DONE"]
            thought_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "agent_response" and "thought" in l.get("step_update", {})]

            self.assertEqual(len(tool_calls), 1)
            self.assertEqual(tool_calls[0]["step_update"]["tool_name"], "search_code")

            self.assertEqual(len(tool_results), 1)
            self.assertEqual(tool_results[0]["step_update"]["result"], {"found": True})

            self.assertEqual(len(thought_events), 1)
            self.assertEqual(thought_events[0]["step_update"]["thought"], "Actual thinking string")

        asyncio.run(_test())

    def test_run_agent_turn_tool_call_with_none_result_not_misclassified(self):
        """Verify tool calls with result=None or error=None are emitted as RUNNING tool calls with tool_info."""
        async def _test():
            mock_agent = MagicMock()

            class CustomToolCallChunk:
                def __init__(self, name, args, call_id):
                    self.name = name
                    self.args = args
                    self.id = call_id
                    self.result = None
                    self.error = None

            async def _chunk_iter():
                yield CustomToolCallChunk("execute_bash", {"cmd": "pytest"}, "call-none-res-1")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="Command executed.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Run bash", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            tool_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "tool"]

            self.assertEqual(len(tool_events), 1)
            step = tool_events[0]["step_update"]
            self.assertEqual(step["state"], "RUNNING")
            self.assertEqual(step["tool_name"], "execute_bash")
            self.assertEqual(step["tool_info"], {"cmd": "pytest"})
            self.assertEqual(step["id"], "call-none-res-1")

        asyncio.run(_test())

    def test_run_agent_turn_text_chunk_with_none_or_callable_thought_not_misclassified(self):
        """Verify text chunks with thought=None or thought as a method are emitted as text_delta."""
        async def _test():
            mock_agent = MagicMock()

            class CustomTextChunkWithNoneField:
                def __init__(self, text):
                    self.text = text
                    self.thought = None
                    self.thinking = None

            class CustomTextChunkWithMethod:
                def __init__(self, text):
                    self.text = text

                def thought(self):
                    return "method representation"

            async def _chunk_iter():
                yield CustomTextChunkWithNoneField("First part of answer. ")
                yield CustomTextChunkWithMethod("Second part of answer.")

            mock_response = MagicMock()
            mock_response.chunks = _chunk_iter()
            mock_response.text = AsyncMock(return_value="First part of answer. Second part of answer.")
            mock_agent.chat = AsyncMock(return_value=mock_response)

            buf = io.StringIO()
            with patch.object(sys, "stdout", buf):
                result = await run_agent_turn(mock_agent, "Answer question", turn_index=1)

            self.assertEqual(result["status"], "SUCCESS")
            lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
            thought_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "agent_response" and "thought" in l.get("step_update", {})]
            text_events = [l for l in lines if l.get("step_update", {}).get("step_type") == "agent_response" and "text_delta" in l.get("step_update", {})]

            self.assertEqual(len(thought_events), 0)
            self.assertEqual(len(text_events), 2)
            self.assertEqual(text_events[0]["step_update"]["text_delta"], "First part of answer. ")
            self.assertEqual(text_events[1]["step_update"]["text_delta"], "Second part of answer.")

        asyncio.run(_test())


if __name__ == "__main__":
    unittest.main()

