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

try:
    from google.antigravity import types as ag_types
except ImportError:
    ag_types = None


def _flush_json(data: Dict[str, Any]) -> None:
    """Write an NDJSON event to stdout and flush immediately."""
    sys.stdout.write(json.dumps(data, default=str) + "\n")
    sys.stdout.flush()


def _extract_from_obj(val: Any) -> Optional[str]:
    """Helper to extract text from a nested prompt or message field."""
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        if "prompt" in val and isinstance(val["prompt"], str):
            return val["prompt"]
        content = val.get("content")
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
        if "text" in val:
            return str(val["text"])
    elif isinstance(val, list):
        parts = []
        for item in val:
            if isinstance(item, dict) and "text" in item:
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return None


def _extract_prompt(payload: Dict[str, Any]) -> str:
    """Extract user prompt text from stream-json prompt events."""
    for field in ("prompt", "message"):
        extracted = _extract_from_obj(payload.get(field))
        if extracted is not None:
            return extracted

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
            thought_cls = getattr(ag_types, "Thought", None)
            is_thought = (
                chunk_type.endswith("Thought")
                or (isinstance(thought_cls, type) and isinstance(chunk, thought_cls))
                or hasattr(chunk, "thought")
                or hasattr(chunk, "thinking")
            )
            tool_result_cls = getattr(ag_types, "ToolResult", None)
            is_tool_result = (
                chunk_type.endswith("ToolResult")
                or (isinstance(tool_result_cls, type) and isinstance(chunk, tool_result_cls))
                or (hasattr(chunk, "name") and (hasattr(chunk, "result") or hasattr(chunk, "error") or hasattr(chunk, "exception")))
            )
            tool_call_cls = getattr(ag_types, "ToolCall", None)
            is_tool_call = (
                (
                    chunk_type.endswith("ToolCall")
                    or (isinstance(tool_call_cls, type) and isinstance(chunk, tool_call_cls))
                    or (hasattr(chunk, "name") and hasattr(chunk, "args"))
                )
                and not is_tool_result
            )
            text_cls = getattr(ag_types, "Text", None)
            is_text = (
                chunk_type.endswith("Text")
                or (isinstance(text_cls, type) and isinstance(chunk, text_cls))
                or (hasattr(chunk, "text") and not is_thought and not is_tool_call and not is_tool_result)
            )

            if is_thought:
                thought_text = (
                    getattr(chunk, "thought", None)
                    or getattr(chunk, "thinking", None)
                    or getattr(chunk, "text", None)
                    or str(chunk)
                )
                _flush_json({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "thought": thought_text,
                    },
                })
            elif is_tool_call:
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
            elif is_tool_result:
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
            elif is_text:
                delta = getattr(chunk, "text", str(chunk))
                text_parts.append(delta)
                _flush_json({
                    "event": "step_update",
                    "step_update": {
                        "step_type": "agent_response",
                        "text_delta": delta,
                    },
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

    global ag_types
    if ag_types is None:
        ag_types = types

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
    if not instructions and args.agent:
        agent_paths = [
            Path.home() / ".gemini" / "config" / "agents" / args.agent / "agent.md",
            Path.home() / ".gemini" / "config" / "agents" / f"{args.agent}.md",
            Path(workspace_dir) / ".agents" / "agents" / args.agent / "agent.md",
            Path(workspace_dir) / ".agents" / "agents" / f"{args.agent}.md",
            Path(workspace_dir) / "plugin" / "agents" / args.agent / "agent.md",
            Path(workspace_dir) / "plugin" / "agents" / f"{args.agent}.md",
        ]
        plugins_dir = Path(workspace_dir) / ".agents" / "plugins"
        if plugins_dir.is_dir():
            for plugin_dir in sorted(plugins_dir.iterdir()):
                if plugin_dir.is_dir():
                    agent_paths.append(plugin_dir / "agents" / args.agent / "agent.md")
                    agent_paths.append(plugin_dir / "agents" / f"{args.agent}.md")

        for ap in agent_paths:
            if ap.is_file():
                try:
                    instructions = ap.read_text(encoding="utf-8")
                    break
                except Exception as e:
                    logger.debug(f"Failed to read agent instructions from {ap}: {e}")

    # Discover skills paths
    skills_paths = []
    default_skills = Path.home() / ".gemini" / "config" / "skills"
    if default_skills.is_dir():
        skills_paths.append(str(default_skills))
    cli_skills = Path(app_data_dir) / "skills"
    if cli_skills.is_dir():
        skills_paths.append(str(cli_skills))
    for ws_sub in [Path(workspace_dir) / ".agents" / "skills", Path(workspace_dir) / "skills", Path(workspace_dir) / "plugin" / "skills"]:
        if ws_sub.is_dir() and str(ws_sub) not in skills_paths:
            skills_paths.append(str(ws_sub))
    plugins_dir = Path(workspace_dir) / ".agents" / "plugins"
    if plugins_dir.is_dir():
        for plugin_dir in sorted(plugins_dir.iterdir()):
            plugin_skills = plugin_dir / "skills"
            if plugin_skills.is_dir() and str(plugin_skills) not in skills_paths:
                skills_paths.append(str(plugin_skills))

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

            if not isinstance(event, dict):
                sys.stderr.write(f"SDK Runner: Expected JSON object on stdin, got {type(event).__name__}\n")
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
