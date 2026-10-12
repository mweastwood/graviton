#!/usr/bin/env python3
"""
Antigravity Python SDK Agent Runner for Graviton.

Acts as a direct drop-in replacement for `agy --input-format stream-json --output-format stream-json`.
Drives autonomous, multi-turn agent execution via `google.antigravity.Agent` while exposing
the bidirectional NDJSON stream-json protocol over standard I/O.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("graviton.sdk_runner")


def _flush_json(data: Dict[str, Any]) -> None:
    """Write an NDJSON event to stdout and flush immediately."""
    sys.stdout.write(json.dumps(data, default=str) + "\n")
    sys.stdout.flush()


def _extract_prompt(payload: Dict[str, Any]) -> str:
    """Extract user prompt text from stream-json prompt events."""
    if "prompt" in payload and isinstance(payload["prompt"], str):
        return payload["prompt"]

    msg = payload.get("message")
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    parts.append(str(item["text"]))
                elif isinstance(item, str):
                    parts.append(item)
            return "".join(parts)
        if "text" in msg:
            return str(msg["text"])

    if "text" in payload:
        return str(payload["text"])

    return json.dumps(payload, default=str)


async def run_agent_turn(
    agent: Any,
    prompt: str,
    turn_index: int,
) -> Dict[str, Any]:
    """Execute a single agent chat turn and stream events over stdout."""
    start_time = time.time()
    if not prompt or not prompt.strip():
        return {
            "status": "FAILED",
            "error": "Empty prompt provided",
            "response": "",
            "duration_seconds": 0.0,
            "num_turns": turn_index,
            "usage": {},
        }

    try:
        response = await agent.chat(prompt)

        # Stream chunks, thoughts, and tool executions in real-time
        text_parts = []
        async for chunk in response.chunks:
            chunk_type = type(chunk).__name__
            if chunk_type in ("Thought", "MockThought"):
                _flush_json({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "thought": getattr(chunk, "text", str(chunk)),
                    },
                })
            elif chunk_type in ("Text", "MockText") or (hasattr(chunk, "text") and not hasattr(chunk, "args")):
                delta = getattr(chunk, "text", str(chunk))
                text_parts.append(delta)
                _flush_json({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "text_delta": delta,
                    },
                })
            elif chunk_type in ("ToolCall", "MockToolCall") or (hasattr(chunk, "name") and hasattr(chunk, "args")):
                raw_name = getattr(chunk, "name", "")
                tool_name = getattr(raw_name, "value", str(raw_name))
                tool_id = (
                    getattr(chunk, "id", None)
                    or getattr(chunk, "call_id", None)
                    or getattr(chunk, "tool_call_id", None)
                )
                _flush_json({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "tool",
                        "tool_name": tool_name,
                        "tool_info": getattr(chunk, "args", None) or {},
                        "id": tool_id,
                        "call_id": tool_id,
                        "tool_call_id": tool_id,
                        "state": "RUNNING",
                    },
                })
            elif chunk_type in ("ToolResult", "MockToolResult") or (hasattr(chunk, "name") and hasattr(chunk, "result")):
                raw_name = getattr(chunk, "name", "")
                tool_name = getattr(raw_name, "value", str(raw_name))
                err = getattr(chunk, "error", None) or getattr(chunk, "exception", None)
                state = "ERROR" if err else "DONE"
                tool_id = (
                    getattr(chunk, "id", None)
                    or getattr(chunk, "call_id", None)
                    or getattr(chunk, "tool_call_id", None)
                )
                step_data: Dict[str, Any] = {
                    "step_type": "tool",
                    "tool_name": tool_name,
                    "id": tool_id,
                    "call_id": tool_id,
                    "tool_call_id": tool_id,
                    "state": state,
                }
                res = getattr(chunk, "result", None)
                if res is not None:
                    step_data["result"] = res
                if err is not None:
                    step_data["error"] = str(err)
                _flush_json({
                    "event": "step_update",
                    "step_update": step_data,
                })

        final_text = await response.text()
        if not final_text and text_parts:
            final_text = "".join(text_parts)
        elapsed = time.time() - start_time

        # Extract usage metadata if available
        usage_dict: Dict[str, Any] = {}
        usage = getattr(response, "usage_metadata", None)
        if usage:
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_token_count", 0),
                "candidates_tokens": getattr(usage, "candidates_token_count", 0),
                "total_tokens": getattr(usage, "total_token_count", 0),
            }

        return {
            "status": "SUCCESS",
            "response": final_text,
            "duration_seconds": round(elapsed, 4),
            "num_turns": turn_index,
            "usage": usage_dict,
        }

    except Exception as err:
        elapsed = time.time() - start_time
        logger.exception(f"Error during agent execution turn {turn_index}: {err}")
        return {
            "status": "FAILED",
            "error": str(err),
            "response": "",
            "duration_seconds": round(elapsed, 4),
            "num_turns": turn_index,
            "usage": {},
        }


async def main_async(args: argparse.Namespace) -> int:
    """Async main routine setting up the Antigravity Agent and processing turns."""
    try:
        from google.antigravity import (
            Agent,
            CapabilitiesConfig,
            LocalAgentConfig,
            policy,
            types,
        )
    except ImportError as e:
        sys.stderr.write(f"FATAL: google-antigravity SDK is not installed: {e}\n")
        sys.stderr.flush()
        return 1

    workspace_dir = os.path.abspath(args.workspace or os.getcwd())
    app_data_dir = args.app_data_dir or os.environ.get("ANTIGRAVITY_CLI_DIR") or str(Path.home() / ".gemini" / "antigravity-cli")
    save_dir = args.save_dir or str(Path(app_data_dir) / "brain")

    # Generate or reuse conversation ID (must be at least 32 characters)
    cid = args.conversation or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
    if not cid or len(cid) < 32:
        cid = str(uuid.uuid4())

    model_name = args.model or os.environ.get("ANTIGRAVITY_MODEL") or os.environ.get("MODEL_NAME")
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    project_id = args.project or os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")

    # Configure autonomous execution policies and capabilities
    policies = [policy.allow_all()] if args.dangerously_skip_permissions else None

    # Load agent persona instructions if provided
    instructions = args.instructions or os.environ.get("ANTIGRAVITY_SYSTEM_INSTRUCTIONS")

    skills_paths = []
    default_skills = Path.home() / ".gemini" / "config" / "skills"
    if default_skills.is_dir():
        skills_paths.append(str(default_skills))
    cli_skills = Path(app_data_dir) / "skills"
    if cli_skills.is_dir():
        skills_paths.append(str(cli_skills))

    agent_config = LocalAgentConfig(
        conversation_id=cid,
        session_continuation_mode=types.SessionContinuationMode.CREATE_OR_RESUME,
        workspaces=[workspace_dir],
        app_data_dir=app_data_dir,
        save_dir=save_dir,
        model=model_name,
        api_key=api_key,
        project=project_id,
        skills_paths=skills_paths or None,
        capabilities=CapabilitiesConfig(),
        policies=policies,
        system_instructions=instructions,
    )

    turn_count = 0

    async with Agent(agent_config) as agent:
        # 1. Output initial handshake event for StreamSession
        resolved_cid = getattr(agent.conversation, "conversation_id", None) or cid
        rc_url = None
        if args.remote_control:
            base_url = os.environ.get("ANTIGRAVITY_REMOTE_CONTROL_BASE_URL", "https://antigravity.google.com").rstrip("/")
            inst = os.environ.get("ANTIGRAVITY_INSTANCE_NAME", "")
            suffix = f"?instance={inst.strip()}" if inst and inst.strip() else ""
            rc_url = f"{base_url}/c/{resolved_cid}{suffix}"

        init_payload = {
            "event": "init",
            "conversation_id": resolved_cid,
            "remote_control_url": rc_url,
            "init": {
                "runner": "antigravity-sdk",
                "workspace": workspace_dir,
                "model": model_name,
                "remote_control_url": rc_url,
            },
        }
        _flush_json(init_payload)

        loop = asyncio.get_running_loop()

        # 2. Main multi-turn NDJSON read loop from stdin
        while True:
            # Read a line from stdin asynchronously
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                # EOF reached (parent closed stdin)
                break

            stripped = line.strip()
            if not stripped:
                continue

            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as err:
                sys.stderr.write(f"SDK Runner: Invalid JSON on stdin: {err}\n")
                sys.stderr.flush()
                continue

            event_type = event.get("event")
            if event_type in ("user", "prompt"):
                turn_count += 1
                prompt_text = _extract_prompt(event)
                result_data = await run_agent_turn(agent, prompt_text, turn_count)
                _flush_json({
                    "event": "result",
                    "result": result_data,
                })
            elif event_type == "ping":
                _flush_json({"event": "pong"})
            elif event_type in ("exit", "close", "quit"):
                break

    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse CLI flags while tolerating raw agy CLI arguments."""
    parser = argparse.ArgumentParser(description="Antigravity Python SDK Agent Runner for Graviton")
    parser.add_argument("--input-format", default="stream-json", help="Input format (default: stream-json)")
    parser.add_argument("--output-format", default="stream-json", help="Output format (default: stream-json)")
    parser.add_argument(
        "--dangerously-skip-permissions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Auto-approve tool permissions",
    )
    parser.add_argument("--remote-control", action="store_true", help="Enable remote control registration")
    parser.add_argument("--model", type=str, default=None, help="Target model name")
    parser.add_argument("--project", type=str, default=None, help="Project name or ID")
    parser.add_argument("--agent", type=str, default=None, help="Agent persona name")
    parser.add_argument("--conversation", type=str, default=None, help="Conversation UUID")
    parser.add_argument("--workspace", type=str, default=None, help="Target workspace path")
    parser.add_argument("--app-data-dir", type=str, default=None, help="Antigravity CLI app data directory")
    parser.add_argument("--save-dir", type=str, default=None, help="Directory to save conversation trajectories")
    parser.add_argument("--instructions", type=str, default=None, help="System instructions")

    # Use parse_known_args to gracefully ignore flags like --effort, --verbose, etc.
    args, _ = parser.parse_known_args(argv)
    return args


def main() -> None:
    args = parse_args(sys.argv[1:])
    exit_code = asyncio.run(main_async(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
