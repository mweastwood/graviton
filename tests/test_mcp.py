#!/usr/bin/env python3
"""
Unit tests for lib/mcp.py (Graviton MCP Server).
"""

import io
import json
import unittest
from unittest.mock import MagicMock, patch

from lib.mcp import GravitonMCPServer


class TestGravitonMCPServer(unittest.TestCase):

    def setUp(self):
        self.server = GravitonMCPServer(host="127.0.0.1", port=8000, auto_start_sidecar=False)

    def test_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "test-client", "version": "1.0"}},
        }
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["id"], 1)
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertEqual(resp["result"]["serverInfo"]["name"], "graviton-mcp")

    def test_ping(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        resp = self.server.handle_request(req)
        self.assertEqual(resp["id"], 2)
        self.assertEqual(resp["result"], {})

    def test_notifications(self):
        req = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        resp = self.server.handle_request(req)
        self.assertIsNone(resp)

    def test_tools_list(self):
        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}
        resp = self.server.handle_request(req)
        self.assertEqual(resp["id"], 3)
        tools = resp["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        expected = {
            "graviton_status",
            "graviton_list_tasks",
            "graviton_get_task",
            "graviton_submit_review",
            "graviton_submit_task",
            "graviton_abort_task",
        }
        self.assertTrue(expected.issubset(tool_names))

    def test_unknown_method(self):
        req = {"jsonrpc": "2.0", "id": 4, "method": "unknown_function"}
        resp = self.server.handle_request(req)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    def test_call_unknown_tool(self):
        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "non_existent_tool", "arguments": {}},
        }
        resp = self.server.handle_request(req)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], -32601)

    @patch.object(GravitonMCPServer, "_http_request")
    def test_tool_graviton_status(self, mock_http):
        mock_http.return_value = (
            200,
            {
                "service": "graviton-server",
                "status": "ok",
                "reviewer_agent": "code_reviewer",
                "fixer_agent": "code_fixer",
                "tasks": {"active_workers": 1, "max_workers": 2, "active_tasks": 1, "queued_tasks": 0},
                "quota": {"current_pool": "gemini", "gemini_remaining_percentage": 95.0},
            },
        )
        req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "graviton_status", "arguments": {}},
        }
        resp = self.server.handle_request(req)
        self.assertFalse(resp["result"]["isError"])
        content = resp["result"]["content"][0]["text"]
        self.assertIn("Graviton Server Status", content)
        self.assertIn("code_reviewer", content)
        self.assertIn("Active Workers: 1 / 2", content)
        self.assertIn("95.0%", content)

    @patch.object(GravitonMCPServer, "_http_request")
    def test_tool_graviton_list_tasks(self, mock_http):
        mock_http.return_value = (
            200,
            {
                "active": [
                    {
                        "id": "task-1",
                        "agent": "code_reviewer",
                        "prompt": "Review PR #42",
                        "target_id": "#42",
                        "elapsed_time": 12.3,
                        "conversation_id": "conv-999",
                    }
                ],
                "queued": [],
                "history": [],
            },
        )
        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "graviton_list_tasks", "arguments": {}},
        }
        resp = self.server.handle_request(req)
        self.assertFalse(resp["result"]["isError"])
        content = resp["result"]["content"][0]["text"]
        self.assertIn("task-1", content)
        self.assertIn("#42", content)
        self.assertIn("conv-999", content)

    @patch.object(GravitonMCPServer, "_http_request")
    def test_tool_graviton_get_task(self, mock_http):
        mock_http.return_value = (
            200,
            {
                "id": "task-1",
                "status": "COMPLETED",
                "agent": "code_reviewer",
                "selected_model": "gemini-custom",
                "selected_pool": "gemini",
                "elapsed_time": 25.0,
                "thoughts": ["Examining diff in server.py"],
                "tool_calls": [{"name": "view_file", "args": {"file": "server.py"}}],
                "logs": ["Starting review", "Review complete"],
            },
        )
        req = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "graviton_get_task", "arguments": {"task_id": "task-1"}},
        }
        resp = self.server.handle_request(req)
        self.assertFalse(resp["result"]["isError"])
        content = resp["result"]["content"][0]["text"]
        self.assertIn("task-1", content)
        self.assertIn("Examining diff in server.py", content)
        self.assertIn("view_file", content)

    @patch.object(GravitonMCPServer, "_http_request")
    def test_tool_graviton_submit_review(self, mock_http):
        mock_http.return_value = (200, {"status": "submitted", "task_id": "task-50"})
        req = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "graviton_submit_review",
                "arguments": {"repo_full_name": "owner/myrepo", "pr_number": 10},
            },
        }
        resp = self.server.handle_request(req)
        self.assertFalse(resp["result"]["isError"])
        content = resp["result"]["content"][0]["text"]
        self.assertIn("task-50", content)
        self.assertIn("owner/myrepo#10", content)

    @patch.object(GravitonMCPServer, "_http_request")
    def test_tool_graviton_abort_task(self, mock_http):
        mock_http.return_value = (200, {"status": "aborted", "task_id": "task-50"})
        req = {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "graviton_abort_task", "arguments": {"task_id": "task-50"}},
        }
        resp = self.server.handle_request(req)
        self.assertFalse(resp["result"]["isError"])
        content = resp["result"]["content"][0]["text"]
        self.assertIn("task-50", content)

    @patch("lib.mcp.ensure_sidecar_running")
    def test_http_request_fast_failure_on_sidecar_error(self, mock_ensure):
        mock_ensure.return_value = (False, "Daemon failed to start: port busy")
        server = GravitonMCPServer(host="127.0.0.1", port=8000, auto_start_sidecar=True)
        status_code, data = server._http_request("GET", "/health")
        self.assertEqual(status_code, 503)
        self.assertIn("Failed to ensure Graviton sidecar: Daemon failed to start: port busy", data.get("error", ""))

    def test_tool_submit_review_validation(self):
        # Missing repo_full_name
        req = {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "graviton_submit_review", "arguments": {"pr_number": 5}},
        }
        resp = self.server.handle_request(req)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("repo_full_name is required", resp["result"]["content"][0]["text"])

        # Missing pr_number
        req["params"]["arguments"] = {"repo_full_name": "owner/repo"}
        resp = self.server.handle_request(req)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("pr_number is required", resp["result"]["content"][0]["text"])

        # Invalid pr_number
        req["params"]["arguments"] = {"repo_full_name": "owner/repo", "pr_number": "abc"}
        resp = self.server.handle_request(req)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("Invalid 'pr_number'", resp["result"]["content"][0]["text"])

    def test_tool_abort_task_validation(self):
        req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "tools/call",
            "params": {"name": "graviton_abort_task", "arguments": {"task_id": "   "}},
        }
        resp = self.server.handle_request(req)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("task_id is required", resp["result"]["content"][0]["text"])

    def test_tool_submit_task_validation(self):
        req = {
            "jsonrpc": "2.0",
            "id": 13,
            "method": "tools/call",
            "params": {"name": "graviton_submit_task", "arguments": {"prompt": ""}},
        }
        resp = self.server.handle_request(req)
        self.assertTrue(resp["result"]["isError"])
        self.assertIn("prompt is required", resp["result"]["content"][0]["text"])

    @patch.object(GravitonMCPServer, "_http_request")
    def test_tool_call_null_arguments(self, mock_http):
        mock_http.return_value = (200, {"active": [], "queued": [], "history": []})
        req = {
            "jsonrpc": "2.0",
            "id": 14,
            "method": "tools/call",
            "params": {"name": "graviton_list_tasks", "arguments": None},
        }
        resp = self.server.handle_request(req)
        self.assertIsNotNone(resp)
        self.assertEqual(resp["id"], 14)
        self.assertFalse(resp["result"]["isError"])
        self.assertIn("No active, queued, or recent tasks found", resp["result"]["content"][0]["text"])

    @patch.object(GravitonMCPServer, "_http_request")
    def test_tool_list_tasks_defensive_prompt(self, mock_http):
        mock_http.return_value = (
            200,
            {
                "active": [{"id": "task-1", "agent": "worker", "prompt": None}],
                "queued": [{"id": "task-2", "agent": "worker"}],
                "history": [{"id": "task-3", "status": "COMPLETED", "prompt": None}],
            },
        )
        req = {
            "jsonrpc": "2.0",
            "id": 15,
            "method": "tools/call",
            "params": {"name": "graviton_list_tasks", "arguments": {}},
        }
        resp = self.server.handle_request(req)
        self.assertFalse(resp["result"]["isError"])
        content = resp["result"]["content"][0]["text"]
        self.assertIn("task-1", content)
        self.assertIn("task-2", content)
        self.assertIn("task-3", content)

    def test_non_dict_request_payload(self):
        # List payload
        resp = self.server.handle_request([1, 2, 3])
        self.assertIsNotNone(resp)
        self.assertEqual(resp["error"]["code"], -32600)
        self.assertIsNone(resp["id"])

        # String payload
        resp = self.server.handle_request("foo")
        self.assertIsNotNone(resp)
        self.assertEqual(resp["error"]["code"], -32600)
        self.assertIsNone(resp["id"])

    def test_stdio_loop(self):
        input_data = (
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        fake_stdin = io.StringIO(input_data)
        fake_stdout = io.StringIO()

        with patch("sys.stdin", fake_stdin), patch("sys.stdout", fake_stdout):
            self.server.run_stdio()

        output_lines = [json.loads(line) for line in fake_stdout.getvalue().strip().splitlines() if line.strip()]
        self.assertEqual(len(output_lines), 1)
        self.assertEqual(output_lines[0]["id"], 1)
        self.assertEqual(output_lines[0]["result"], {})

    def test_stdio_loop_non_dict_payload(self):
        input_data = (
            json.dumps([1, 2, 3]) + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 99, "method": "ping"}) + "\n"
        )
        fake_stdin = io.StringIO(input_data)
        fake_stdout = io.StringIO()

        with patch("sys.stdin", fake_stdin), patch("sys.stdout", fake_stdout):
            self.server.run_stdio()

        output_lines = [json.loads(line) for line in fake_stdout.getvalue().strip().splitlines() if line.strip()]
        self.assertEqual(len(output_lines), 2)
        self.assertEqual(output_lines[0]["error"]["code"], -32600)
        self.assertIsNone(output_lines[0]["id"])
        self.assertEqual(output_lines[1]["id"], 99)
        self.assertEqual(output_lines[1]["result"], {})


if __name__ == "__main__":
    unittest.main()
