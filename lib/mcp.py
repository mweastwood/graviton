#!/usr/bin/env python3
"""
Model Context Protocol (MCP) Server for Graviton.

Exposes Graviton sidecar control and task supervision tools to Antigravity
and other MCP-compatible clients over standard JSON-RPC 2.0 stdio transport.

Zero external dependencies (Python standard library only).
"""

import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.sidecar import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    ensure_sidecar_running,
    get_sidecar_status,
)

logger = logging.getLogger("graviton.mcp")


class GravitonMCPServer:
    """
    Stdio-based JSON-RPC 2.0 MCP Server for Graviton.
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        auto_start_sidecar: bool = True,
    ):
        self.host = host
        self.port = port
        self.auto_start_sidecar = auto_start_sidecar
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable[[Dict[str, Any]], Tuple[str, bool]]] = {}
        self._register_default_tools()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _http_request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        timeout: float = 5.0,
    ) -> Tuple[int, Dict[str, Any]]:
        """Perform an HTTP request to the Graviton server."""
        if self.auto_start_sidecar:
            success, msg = ensure_sidecar_running(host=self.host, port=self.port)
            if not success:
                return 503, {"error": f"Failed to ensure Graviton sidecar: {msg}"}

        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"User-Agent": "Graviton-MCP/1.0"}
        if data_bytes is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                try:
                    return resp.status, json.loads(body)
                except json.JSONDecodeError:
                    return resp.status, {"raw": body}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                return e.code, json.loads(body)
            except Exception:
                return e.code, {"error": str(e), "raw": body}
        except Exception as e:
            return 503, {"error": f"Failed to connect to Graviton server at {url}: {e}"}

    def _register_default_tools(self):
        # 1. graviton_status
        self._register_tool(
            name="graviton_status",
            description="Query the live status of the Graviton autonomous webhook server, active worker threads, pending tasks, and AI quota pool pacing.",
            input_schema={
                "type": "object",
                "properties": {},
            },
            handler=self._tool_status,
        )

        # 2. graviton_list_tasks
        self._register_tool(
            name="graviton_list_tasks",
            description="List currently active, queued, and recently finished Graviton container supervisor tasks.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of history items to return (default: 20)",
                    }
                },
            },
            handler=self._tool_list_tasks,
        )

        # 3. graviton_get_task
        self._register_tool(
            name="graviton_get_task",
            description="Get detailed execution logs, model thoughts, tool calls, and result for a specific Graviton task ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The unique task identifier (e.g., 'task-1')",
                    },
                },
                "required": ["task_id"],
            },
            handler=self._tool_get_task,
        )

        # 4. graviton_submit_review
        self._register_tool(
            name="graviton_submit_review",
            description="Trigger an autonomous Graviton PR code review inside an isolated ContainerSupervisor sandbox.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo_full_name": {
                        "type": "string",
                        "description": "GitHub repository full name (e.g., 'owner/repo')",
                    },
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number to review",
                    },
                    "repo_name": {
                        "type": "string",
                        "description": "Local repository directory name (optional)",
                    },
                },
                "required": ["repo_full_name", "pr_number"],
            },
            handler=self._tool_submit_review,
        )

        # 5. graviton_submit_task
        self._register_tool(
            name="graviton_submit_task",
            description="Submit an arbitrary coding or debugging task to the Graviton ContainerSupervisor queue.",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The instruction or task prompt to execute",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent persona name ('code_reviewer', 'code_fixer', 'issue_triager', 'pr_drafter')",
                        "default": "code_fixer",
                    },
                    "repo_name": {
                        "type": "string",
                        "description": "Target repository directory name",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "Associated issue or PR identifier (e.g. '#42')",
                    },
                },
                "required": ["prompt"],
            },
            handler=self._tool_submit_task,
        )

        # 6. graviton_abort_task
        self._register_tool(
            name="graviton_abort_task",
            description="Abort a running or queued Graviton task.",
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The ID of the task to abort",
                    }
                },
                "required": ["task_id"],
            },
            handler=self._tool_abort_task,
        )

        # 7. graviton_dashboard
        self._register_tool(
            name="graviton_dashboard",
            description="Retrieve the live Graviton status and task pipeline formatted as a markdown dashboard, and optionally register an Antigravity artifact file path for automatic continuous updates from the server.",
            input_schema={
                "type": "object",
                "properties": {
                    "artifact_path": {
                        "type": "string",
                        "description": "Optional absolute path to an Antigravity artifact file (e.g., '<appDataDir>/brain/<conversation-id>/graviton_dashboard.md') to register for live server updates.",
                    },
                },
            },
            handler=self._tool_dashboard,
        )

    def _register_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[[Dict[str, Any]], Tuple[str, bool]],
    ):
        self._tools[name] = {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
        }
        self._handlers[name] = handler

    # ---------------- Tool Implementations ----------------

    def _tool_status(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        status_code, data = self._http_request("GET", "/health")
        if status_code != 200:
            return f"Failed to query Graviton server: {data}", True

        lines = [
            "⚡ **Graviton Server Status**",
            f"- **Service**: {data.get('service', 'graviton-server')}",
            f"- **Status**: {data.get('status', 'unknown')}",
            f"- **Reviewer Agent**: `{data.get('reviewer_agent')}`",
            f"- **Fixer Agent**: `{data.get('fixer_agent')}`",
        ]
        tasks = data.get("tasks", {})
        if tasks:
            lines.extend([
                "",
                "📊 **Task Pipeline**:",
                f"- Active Workers: {tasks.get('active_workers', 0)} / {tasks.get('max_workers', 0)}",
                f"- Running Tasks: {tasks.get('active_tasks', 0)}",
                f"- Queued Tasks: {tasks.get('queued_tasks', 0)}",
                f"- Completed: {tasks.get('completed_tasks', 0)}",
                f"- Failed: {tasks.get('failed_tasks', 0)}",
            ])
        quota = data.get("quota", {})
        if quota:
            lines.extend([
                "",
                "🎯 **Quota Status**:",
                f"- Current Pool: `{quota.get('current_pool', 'default')}`",
                f"- Gemini Remaining: {quota.get('gemini_remaining_percentage', 'N/A')}%",
                f"- Third-Party Remaining: {quota.get('third_party_remaining_percentage', 'N/A')}%",
            ])
        rc_links = data.get("active_remote_control_urls", {})
        if rc_links:
            lines.extend(["", "🌐 **Active Remote Control Links**:"])
            for tid, url in rc_links.items():
                lines.append(f"- **{tid}**: {url}")
        return "\n".join(lines), False

    def _tool_list_tasks(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        status_code, data = self._http_request("GET", "/tasks")
        if status_code != 200:
            return f"Failed to list Graviton tasks: {data}", True

        active = data.get("active", [])
        queued = data.get("queued", [])
        history = data.get("history", [])

        limit = int(args.get("limit", 20))
        history = history[:limit]

        lines = ["📋 **Graviton Tasks**", ""]
        if active:
            lines.append("▶️ **Running Tasks:**")
            for t in active:
                cid = f" (Conv: `{t.get('conversation_id')}`)" if t.get("conversation_id") else ""
                rc_url = f" [Live: {t.get('remote_control_url')}]" if t.get("remote_control_url") else ""
                prompt_snippet = (t.get("prompt") or "")[:60]
                lines.append(f"- **{t.get('id', '')}** [{t.get('agent', '')}]: {t.get('target_id', '')} - {prompt_snippet}... ({t.get('elapsed_time', 0)}s elapsed){cid}{rc_url}")
            lines.append("")

        if queued:
            lines.append("⏳ **Queued Tasks:**")
            for t in queued:
                prompt_snippet = (t.get("prompt") or "")[:60]
                lines.append(f"- **{t.get('id', '')}** [{t.get('agent', '')}]: {t.get('target_id', '')} - {prompt_snippet}... ({t.get('wait_time', 0)}s waiting)")
            lines.append("")

        if history:
            lines.append("📜 **Recent History:**")
            for t in history:
                icon = "✅" if t.get("status") == "COMPLETED" else "❌"
                prompt_snippet = (t.get("prompt") or "")[:50]
                lines.append(f"- {icon} **{t.get('id', '')}** [{t.get('status', '')}]: {t.get('target_id', '')} - {prompt_snippet}... ({t.get('elapsed_time', 0)}s)")
            lines.append("")

        if not active and not queued and not history:
            lines.append("(No active, queued, or recent tasks found)")

        return "\n".join(lines), False

    def _tool_get_task(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        task_id = args.get("task_id", "").strip()
        if not task_id:
            return "Error: task_id is required", True

        status_code, data = self._http_request("GET", f"/tasks/{task_id}")
        if status_code != 200:
            return f"Task '{task_id}' not found or error occurred: {data.get('error', data)}", True

        lines = [
            f"🔍 **Task Details: {data.get('id')}**",
            f"- **Status**: `{data.get('status')}`",
            f"- **Agent**: `{data.get('agent')}`",
            f"- **Model**: `{data.get('selected_model')}` ({data.get('selected_pool')})",
            f"- **Target**: {data.get('target_id', 'N/A')}",
            f"- **Repo**: {data.get('repo_full_name') or data.get('repo_name') or 'N/A'}",
            f"- **Elapsed**: {data.get('elapsed_time', 0)}s",
        ]
        if data.get("conversation_id"):
            lines.append(f"- **Conversation ID**: `{data.get('conversation_id')}`")
        if data.get("remote_control_url"):
            lines.append(f"- **Remote Control**: {data.get('remote_control_url')}")

        thoughts = data.get("thoughts", [])
        if thoughts:
            lines.extend(["", "🧠 **Agent Thoughts:**"])
            for thought in thoughts[-5:]:
                lines.append(f"> {thought.strip()}")

        tool_calls = data.get("tool_calls", [])
        if tool_calls:
            lines.extend(["", "🛠️ **Tool Calls:**"])
            for tc in tool_calls[-5:]:
                lines.append(f"- `{tc.get('name')}`: `{json.dumps(tc.get('args', {}))[:100]}`")

        logs = data.get("logs", [])
        if logs:
            lines.extend(["", "📜 **Trailing Logs:**", "```text"])
            lines.extend(logs[-25:])
            lines.append("```")

        return "\n".join(lines), False

    def _tool_submit_review(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        repo_full = str(args.get("repo_full_name") or "").strip()
        pr_num = args.get("pr_number")
        if not repo_full:
            return "Error: repo_full_name is required", True
        if pr_num is None:
            return "Error: pr_number is required", True
        try:
            pr_num = int(pr_num)
        except (ValueError, TypeError):
            return f"Error: Invalid 'pr_number': {pr_num}", True

        repo_name = args.get("repo_name") or repo_full.split("/")[-1]

        prompt = (
            f"Review PR #{pr_num}. Inspect all changed files, run tests locally, formulate fixes, and post review to GitHub: "
            f"submit `gh pr review {pr_num} --request-changes` for external PRs (or `gh pr comment {pr_num}` with `/fix` for own PRs). "
            f"Always append `<!-- antigravity-auto-reply -->` and `<!-- graviton:code_reviewer -->`."
        )
        goal_prompt = (
            f"/goal Review PR #{pr_num} on repository {repo_full} thoroughly. "
            f"Inspect all changed files, run tests locally, formulate fixes, and submit the review to GitHub using "
            f"`gh pr review {pr_num} --request-changes` for external PRs or `gh pr comment {pr_num}` with `/fix` for own PRs. "
            f"You MUST execute the `gh` command via run_command to post your review before completing your goal."
        )

        payload = {
            "agent": "code_reviewer",
            "prompt": prompt,
            "goal_prompt": goal_prompt,
            "use_goal": True,
            "target_id": f"#{pr_num}",
            "repo_full_name": repo_full,
            "repo_name": repo_name,
        }
        status_code, data = self._http_request("POST", "/tasks/submit", payload=payload)
        if status_code != 200:
            return f"Failed to submit review task: {data.get('error', data)}", True

        task_id = data.get("task_id")
        return f"🚀 Successfully submitted review task for `{repo_full}#{pr_num}` (Task ID: **{task_id}**). Track progress with `graviton_get_task(task_id='{task_id}')`.", False

    def _tool_submit_task(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return "Error: prompt is required", True

        agent = args.get("agent", "code_fixer")
        repo_name = args.get("repo_name")
        target_id = args.get("target_id")

        payload = {
            "agent": agent,
            "prompt": prompt,
            "goal_prompt": f"/goal {prompt}",
            "use_goal": True,
            "target_id": target_id,
            "repo_name": repo_name,
        }
        status_code, data = self._http_request("POST", "/tasks/submit", payload=payload)
        if status_code != 200:
            return f"Failed to submit task: {data.get('error', data)}", True

        task_id = data.get("task_id")
        return f"🚀 Task submitted to Graviton queue (Task ID: **{task_id}**). Track with `graviton_get_task(task_id='{task_id}')`.", False

    def _tool_abort_task(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        task_id = str(args.get("task_id") or "").strip()
        if not task_id:
            return "Error: task_id is required", True

        status_code, data = self._http_request("POST", f"/tasks/{task_id}/abort")
        if status_code != 200:
            return f"Failed to abort task '{task_id}': {data.get('error', data)}", True

        return f"🛑 Successfully aborted task **{task_id}**.", False

    def _tool_dashboard(self, args: Dict[str, Any]) -> Tuple[str, bool]:
        artifact_path = args.get("artifact_path")
        registration_notes = []
        if artifact_path:
            status_code, resp = self._http_request(
                "POST",
                "/dashboard/register",
                payload={"path": str(artifact_path)},
            )
            if status_code == 200:
                registration_notes.append(f"> [!TIP]\n> **Live Updates Active**: Registered `{artifact_path}` for automatic real-time server updates.")
            else:
                err_msg = resp.get("error", str(resp))
                registration_notes.append(f"> [!WARNING]\n> Could not register artifact path with server: {err_msg}")

        # Fetch formatted dashboard content from server
        status_code, data = self._http_request("GET", "/dashboard/content")
        if status_code == 200 and isinstance(data, dict) and "markdown" in data:
            md = data["markdown"]
            if registration_notes:
                md = "\n\n".join(registration_notes) + "\n\n" + md
            return md, False

        # Fallback to local status query if /dashboard/content is not reachable
        status_text, is_err = self._tool_status(args)
        if registration_notes:
            status_text = "\n\n".join(registration_notes) + "\n\n" + status_text
        return status_text, is_err

    # ---------------- JSON-RPC Protocol Handling ----------------

    def handle_request(self, req: Any) -> Optional[Dict[str, Any]]:
        """Handle a single JSON-RPC request."""
        if not isinstance(req, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32600,
                    "message": "Invalid Request: payload must be a JSON object",
                },
            }

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # Notifications (no id)
        if req_id is None:
            if method == "notifications/initialized":
                logger.info("Client sent notifications/initialized")
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {},
                    },
                    "serverInfo": {
                        "name": "graviton-mcp",
                        "version": "1.0.0",
                    },
                },
            }

        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "tools": list(self._tools.values()),
                },
            }

        if method == "tools/call":
            tool_name = params.get("name")
            arguments = params.get("arguments") or {}
            handler = self._handlers.get(tool_name)
            if not handler:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {
                        "code": -32601,
                        "message": f"Method not found / Unknown tool: '{tool_name}'",
                    },
                }

            try:
                content_text, is_error = handler(arguments)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": content_text,
                            }
                        ],
                        "isError": is_error,
                    },
                }
            except Exception as e:
                logger.exception(f"Error executing tool '{tool_name}'")
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Error executing tool '{tool_name}': {e}",
                            }
                        ],
                        "isError": True,
                    },
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,
                "message": f"Method '{method}' not implemented",
            },
        }

    def run_stdio(self):
        """Run the MCP server listening on stdin, responding on stdout."""
        logger.info("Graviton MCP server listening on stdio...")
        for line in sys.stdin:
            clean = line.strip()
            if not clean:
                continue
            try:
                req = json.loads(clean)
            except json.JSONDecodeError as err:
                resp = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {err}"},
                }
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
                continue

            resp = self.handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Graviton MCP Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Graviton server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Graviton server port")
    parser.add_argument("--no-auto-start", action="store_true", help="Do not automatically launch sidecar")
    args = parser.parse_args()

    # Route logging to stderr so stdout remains clean JSON-RPC
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s [%(levelname)s] %(message)s")

    server = GravitonMCPServer(
        host=args.host,
        port=args.port,
        auto_start_sidecar=not args.no_auto_start,
    )
    server.run_stdio()


if __name__ == "__main__":
    main()
