#!/usr/bin/env python3
"""
Unit tests for Graviton Live Dashboard Generator and Auto-Updater (lib/dashboard.py).
"""

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.dashboard import (
    DashboardUpdater,
    _get_dashboard_template,
    _reset_dashboard_template_cache,
    _render_active_tasks_table,
    _render_approved_prs_table,
    _render_history_tasks_table,
    format_dashboard_markdown,
    format_duration,
    get_quota_color,
    is_safe_url,
    parse_dashboard_markdown,
    render_dashboard_html,
    SERVER_PATTERN,
    STATUS_PATTERN,
    WORKERS_PATTERN,
    METRIC_INT_PATTERNS,
    METRIC_STR_PATTERNS,
)
from lib.quota import QuotaTracker
from lib.tasks import Task, TaskStatus


class TestDashboardFormatting(unittest.TestCase):
    """Test duration and markdown formatting functions."""

    def setUp(self):
        super().setUp()
        self._models_patcher = patch(
            "lib.quota.fetch_cli_models",
            return_value=(["gemini-3.8-flash-medium"], ["claude-sonnet-4-6"]),
        )
        self._models_patcher.start()
        self.addCleanup(self._models_patcher.stop)

    def test_format_duration(self):
        self.assertEqual(format_duration(None), "0s")
        self.assertEqual(format_duration(-5), "0s")
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(42), "42s")
        self.assertEqual(format_duration(135), "2m 15s")
        self.assertEqual(format_duration(3665), "1h 1m")

    def test_format_dashboard_markdown_empty_state(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 0,
            "max_workers": 4,
            "active_tasks": 0,
            "queued_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 0,
        }
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []
        mock_tm._draining = False
        mock_tm._paused = False

        mock_quota = MagicMock()
        mock_quota.get_info().to_dict.return_value = {
            "current_pool": "gemini",
            "selected_model": "gemini-2.5-pro",
            "gemini_remaining_percentage": 95,
            "third_party_remaining_percentage": 100,
        }

        md = format_dashboard_markdown(
            task_manager=mock_tm,
            quota_tracker=mock_quota,
            host="127.0.0.1",
            port=9000,
        )

        self.assertIn("# 🌌 Graviton Live Dashboard", md)
        self.assertIn("🟢 **ONLINE**", md)
        self.assertIn("http://127.0.0.1:9000/dashboard", md)
        self.assertIn("| **Active Workers** | `0 / 4` |", md)
        self.assertIn("*No container tasks currently running.*", md)
        self.assertIn("*Queue is empty.*", md)
        self.assertIn("| **Active Pool** | `gemini` |", md)
        self.assertIn("| **Gemini Remaining** | `95%` |", md)

    def test_format_dashboard_markdown_with_real_quota_tracker(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = QuotaTracker(state_path=Path(tmpdir) / ".graviton_model_selection.json")
            md = format_dashboard_markdown(quota_tracker=tracker)
            self.assertIn("| **Active Pool** | `gemini` |", md)
            self.assertIn("| **Active Model** | `gemini-3.8-flash-medium` |", md)
            self.assertIn("| **Gemini Remaining** | `100.0%` |", md)
            self.assertIn("| **Third-Party Remaining** | `100.0%` |", md)

            # Test active model override and pool switching
            tracker.set_active_model("gemini", "gemini-2.5-pro")
            md2 = format_dashboard_markdown(quota_tracker=tracker)
            self.assertIn("| **Active Model** | `gemini-2.5-pro` |", md2)

            tracker.quota_pool = "claude"
            md3 = format_dashboard_markdown(quota_tracker=tracker)
            self.assertIn("| **Active Pool** | `claude` |", md3)
            self.assertIn("| **Active Model** | `claude-sonnet-4-6` |", md3)

    def test_format_dashboard_markdown_with_tasks_and_remote_control(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 1,
            "max_workers": 2,
            "active_tasks": 1,
            "queued_tasks": 1,
            "completed_tasks": 5,
            "failed_tasks": 1,
        }

        active_task = Task(
            id="task-101",
            agent="code_reviewer",
            prompt="Review PR #42",
            target_id="#42",
            repo_full_name="owner/repo",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 30.0,
            remote_control_url="https://antigravity.google.com/c/conv-101",
        )
        queued_task = Task(
            id="task-102",
            agent="code_fixer",
            prompt="Fix bug",
            target_id="#43",
            status=TaskStatus.QUEUED,
            priority=2,
            enqueue_time=time.time() - 10.0,
        )
        completed_task = Task(
            id="task-100",
            agent="code_fixer",
            prompt="Completed task",
            target_id="#40",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 100.0,
            finish_time=time.time() - 40.0,
            remote_control_url="https://antigravity.google.com/c/conv-100",
        )

        mock_tm.get_active_tasks.return_value = [active_task]
        mock_tm.get_queued_tasks.return_value = [queued_task]
        mock_tm.get_task_history.return_value = [completed_task]

        md = format_dashboard_markdown(
            task_manager=mock_tm,
            host="localhost",
            port=8000,
        )

        self.assertIn("🟡 **BUSY**", md)
        self.assertIn("`task-101`", md)
        self.assertIn("[Remote Control 🌐](https://antigravity.google.com/c/conv-101)", md)
        self.assertIn("`task-102`", md)
        self.assertIn("`task-100`", md)
        self.assertIn("✅ `COMPLETED`", md)

    def test_render_dashboard_html(self):
        md = "# Sample Markdown"
        html_out = render_dashboard_html(md, host="localhost", port=8000)
        self.assertIn("<!DOCTYPE html>", html_out)
        self.assertIn("Graviton Live Dashboard", html_out)
        self.assertIn("# Sample Markdown", html_out)
        self.assertIn("/dashboard/content", html_out)

    def test_get_quota_color_thresholds(self):
        # > 50%: Green (#3fb950)
        self.assertEqual(get_quota_color(100.0), "#3fb950")
        self.assertEqual(get_quota_color(50.1), "#3fb950")
        # 20% - 50%: Amber (#d29922)
        self.assertEqual(get_quota_color(50.0), "#d29922")
        self.assertEqual(get_quota_color(20.0), "#d29922")
        # < 20%: Red (#f85149)
        self.assertEqual(get_quota_color(19.9), "#f85149")
        self.assertEqual(get_quota_color(0.0), "#f85149")
        # None / N/A: Accent (#58a6ff)
        self.assertEqual(get_quota_color(None), "#58a6ff")

    def test_parse_dashboard_markdown_empty_and_busy(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 2, "max_workers": 4, "active_tasks": 1,
            "queued_tasks": 3, "completed_tasks": 8, "failed_tasks": 2,
        }
        mock_tm.get_active_tasks.return_value = [
            Task(id="task-1", agent="code_reviewer", prompt="Review", target_id="#1", status=TaskStatus.RUNNING, start_time=time.time() - 20)
        ]
        mock_tm.get_queued_tasks.return_value = [
            Task(id="task-2", agent="code_fixer", prompt="Fix", target_id="#2", priority=1, status=TaskStatus.QUEUED, enqueue_time=time.time() - 10)
        ]
        mock_tm.get_task_history.return_value = [
            Task(id="task-0", agent="code_reviewer", prompt="Prev", target_id="#0", status=TaskStatus.COMPLETED, start_time=time.time() - 100, finish_time=time.time() - 50)
        ]
        mock_tm._draining = False
        mock_tm._paused = False

        md = format_dashboard_markdown(task_manager=mock_tm, host="127.0.0.1", port=9000)
        parsed = parse_dashboard_markdown(md, default_host="localhost", default_port=8000)

        self.assertEqual(parsed["host"], "127.0.0.1")
        self.assertEqual(parsed["port"], 9000)
        self.assertEqual(parsed["status_text"], "BUSY")
        self.assertEqual(parsed["status_class"], "busy")
        self.assertEqual(parsed["active_workers"], 2)
        self.assertEqual(parsed["max_workers"], 4)
        self.assertEqual(parsed["running_tasks"], 1)
        self.assertEqual(parsed["queued_tasks"], 3)
        self.assertEqual(parsed["completed_tasks"], 8)
        self.assertEqual(parsed["failed_tasks"], 2)
        self.assertEqual(len(parsed["active_tasks"]), 1)
        self.assertEqual(parsed["active_tasks"][0]["id"], "task-1")
        self.assertEqual(len(parsed["queued_tasks_list"]), 1)
        self.assertEqual(parsed["queued_tasks_list"][0]["id"], "task-2")
        self.assertEqual(len(parsed["history_tasks"]), 1)
        self.assertEqual(parsed["history_tasks"][0]["id"], "task-0")

    def test_render_dashboard_html_rich_elements_and_kpi(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 1, "max_workers": 2, "active_tasks": 1,
            "queued_tasks": 0, "completed_tasks": 12, "failed_tasks": 0,
        }
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []
        mock_tm._draining = False
        mock_tm._paused = False

        md = format_dashboard_markdown(task_manager=mock_tm, host="localhost", port=8000)
        html_out = render_dashboard_html(md, host="localhost", port=8000, task_manager=mock_tm)

        self.assertIn("<!DOCTYPE html>", html_out)
        self.assertIn("<title>Graviton Live Dashboard</title>", html_out)
        self.assertIn("class=\"container\"", html_out)
        self.assertIn("🌌 Graviton Live Dashboard", html_out)
        self.assertIn("badge-busy", html_out)
        self.assertIn("Auto-refreshing (3s)", html_out)
        self.assertIn("Active Workers", html_out)
        self.assertIn("1 / 2", html_out)
        self.assertIn("Running Tasks", html_out)
        self.assertIn("Queued Tasks", html_out)
        self.assertIn("Completed Tasks", html_out)
        self.assertIn("Failed Tasks", html_out)
        self.assertIn("Model Quota &amp; Pacing", html_out)
        self.assertIn("Gemini API Capacity", html_out)
        self.assertIn("Third-Party (Claude) Capacity", html_out)
        self.assertIn("class=\"markdown-view\"", html_out)
        self.assertIn("setInterval(refreshDashboard, 3000)", html_out)

    def test_render_dashboard_html_with_active_tasks_and_remote_control(self):
        active_task = Task(
            id="task-live-1",
            agent="code_reviewer",
            prompt="Review PR #99",
            target_id="#99",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 45.0,
            remote_control_url="https://antigravity.google.com/c/live-sess-1",
        )
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 1, "max_workers": 2, "active_tasks": 1}
        mock_tm.get_active_tasks.return_value = [active_task]
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []

        md = format_dashboard_markdown(task_manager=mock_tm)
        html_out = render_dashboard_html(md, task_manager=mock_tm)

        self.assertIn("task-live-1", html_out)
        self.assertIn("code_reviewer", html_out)
        self.assertIn("#99", html_out)
        self.assertIn("https://antigravity.google.com/c/live-sess-1", html_out)
        self.assertIn("🌐 Remote Control", html_out)
        self.assertIn("target=\"_blank\"", html_out)
        self.assertIn("rel=\"noopener\"", html_out)

    def test_render_dashboard_html_with_queued_tasks_priority_badge(self):
        queued_task = Task(
            id="task-queue-9",
            agent="code_fixer",
            prompt="Fix test",
            target_id="repo#10",
            status=TaskStatus.QUEUED,
            priority=1,
            enqueue_time=time.time() - 15.0,
        )
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 0, "max_workers": 2, "active_tasks": 0, "queued_tasks": 1}
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = [queued_task]
        mock_tm.get_task_history.return_value = []

        md = format_dashboard_markdown(task_manager=mock_tm)
        html_out = render_dashboard_html(md, task_manager=mock_tm)

        self.assertIn("task-queue-9", html_out)
        self.assertIn("code_fixer", html_out)
        self.assertIn("repo#10", html_out)
        self.assertIn("priority-badge", html_out)
        self.assertIn("P1", html_out)

    def test_render_dashboard_html_with_history_tasks_and_details(self):
        completed_task = Task(
            id="task-hist-1",
            agent="pr_drafter",
            prompt="Draft PR",
            target_id="#55",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 60.0,
            finish_time=time.time() - 10.0,
            remote_control_url="https://antigravity.google.com/c/sess-hist",
        )
        failed_task = Task(
            id="task-hist-2",
            agent="code_fixer",
            prompt="Failing fix",
            target_id="#56",
            status=TaskStatus.FAILED,
            start_time=time.time() - 30.0,
            finish_time=time.time() - 5.0,
            error_message="Compilation failed on line 42",
        )
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 0, "max_workers": 2, "completed_tasks": 1, "failed_tasks": 1}
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = [completed_task, failed_task]

        md = format_dashboard_markdown(task_manager=mock_tm)
        html_out = render_dashboard_html(md, task_manager=mock_tm)

        self.assertIn("task-hist-1", html_out)
        self.assertIn("status-completed", html_out)
        self.assertIn("https://antigravity.google.com/c/sess-hist", html_out)
        self.assertIn("task-hist-2", html_out)
        self.assertIn("status-failed", html_out)
        self.assertIn("Compilation failed on line 42", html_out)

    def test_render_dashboard_html_empty_states(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {}
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []

        md = format_dashboard_markdown(task_manager=mock_tm)
        html_out = render_dashboard_html(md, task_manager=mock_tm)

        self.assertIn("No container tasks currently running.", html_out)
        self.assertIn("Queue is empty.", html_out)
        self.assertIn("No completed tasks in history yet.", html_out)

    def test_render_dashboard_html_xss_sanitization(self):
        xss_task = Task(
            id="<script>alert(1)</script>",
            agent="<img src=x onerror=alert(2)>",
            prompt="Prompt",
            target_id="<b>bold</b>",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 10.0,
        )
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 1, "max_workers": 1, "active_tasks": 1}
        mock_tm.get_active_tasks.return_value = [xss_task]
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []

        md = format_dashboard_markdown(task_manager=mock_tm)
        html_out = render_dashboard_html(md, task_manager=mock_tm)

        self.assertNotIn("<script>alert(1)</script>", html_out)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html_out)
        self.assertNotIn("<img src=x onerror=alert(2)>", html_out)
        self.assertIn("&lt;img src=x onerror=alert(2)&gt;", html_out)
        self.assertNotIn("<b>bold</b>", html_out)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", html_out)

    def test_is_safe_url(self):
        self.assertFalse(is_safe_url(None))
        self.assertFalse(is_safe_url(""))
        self.assertFalse(is_safe_url("   "))
        self.assertFalse(is_safe_url("javascript:alert(1)"))
        self.assertFalse(is_safe_url("JAVASCRIPT:alert(1)"))
        self.assertFalse(is_safe_url("data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg=="))
        self.assertFalse(is_safe_url("vbscript:msgbox(1)"))
        self.assertFalse(is_safe_url(12345))  # type: ignore
        self.assertFalse(is_safe_url(3.14))  # type: ignore
        self.assertFalse(is_safe_url(["https://example.com"]))  # type: ignore
        self.assertFalse(is_safe_url({"url": "https://example.com"}))  # type: ignore
        self.assertFalse(is_safe_url(object()))  # type: ignore
        self.assertFalse(is_safe_url(True))  # type: ignore
        self.assertFalse(is_safe_url(False))  # type: ignore
        self.assertTrue(is_safe_url("http://localhost:8000/session/1"))
        self.assertTrue(is_safe_url("https://antigravity.google.com/c/123"))

    def test_render_dashboard_html_javascript_url_not_rendered(self):
        malicious_active = Task(
            id="task-xss-1",
            agent="code_reviewer",
            prompt="Malicious task",
            target_id="#99",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 30.0,
            remote_control_url="javascript:alert(document.cookie)",
        )
        malicious_history = Task(
            id="task-xss-2",
            agent="code_fixer",
            prompt="Malicious history",
            target_id="#100",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 60.0,
            finish_time=time.time() - 10.0,
            remote_control_url="javascript:alert('pwned')",
        )
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 1, "max_workers": 1, "active_tasks": 1, "completed_tasks": 1}
        mock_tm.get_active_tasks.return_value = [malicious_active]
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = [malicious_history]

        md = format_dashboard_markdown(task_manager=mock_tm)
        html_out = render_dashboard_html(md, task_manager=mock_tm)

        self.assertNotIn('href="javascript:', html_out)
        self.assertNotIn("href='javascript:", html_out)
        self.assertNotIn('<a href="javascript', html_out)
        self.assertIn("Pending...", html_out)
        self.assertIn('class="error-snippet"', html_out)

        # Direct table rendering verification
        active_rendered = _render_active_tasks_table([{
            "id": "t1", "agent": "a", "target": "b", "elapsed": "1s", "status": "RUNNING",
            "remote_control_url": "javascript:alert(1)"
        }])
        self.assertNotIn("<a ", active_rendered)
        self.assertIn("Pending...", active_rendered)

        history_rendered = _render_history_tasks_table([{
            "id": "t2", "agent": "a", "target": "b", "duration": "1s", "status": "COMPLETED",
            "remote_control_url": "javascript:alert(1)", "details": "Remote Control"
        }])
        self.assertNotIn("<a ", history_rendered)

    def test_parse_dashboard_markdown_status_false_positives(self):
        # Markdown where status header is ONLINE, but prompt, target, or error mentions BUSY / PAUSED / DRAINING
        sample_md = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE** | **Mode**: HEADLESS\n\n"
            "## 🚀 Active Container Tasks (1)\n\n"
            "| Task ID | Agent | Target | Elapsed | Status | Remote Control |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-busy` | `code_fixer` | `Fix BUSY loop in worker` | 10s | 🔄 RUNNING | [Remote Control 🌐](http://localhost:8000/c/1) |\n\n"
            "## 📜 Recent Task Execution History (1)\n\n"
            "| Task ID | Agent | Target | Duration | Status | Details |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-paused` | `code_fixer` | `Repo` | 5s | ❌ FAILED | Worker was PAUSED unexpectedly |\n"
        )
        parsed = parse_dashboard_markdown(sample_md)
        self.assertEqual(parsed["status_text"], "ONLINE")
        self.assertEqual(parsed["status_class"], "online")
        self.assertEqual(parsed["status_icon"], "🟢")

    def test_parse_dashboard_markdown_table_separator_styles(self):
        # Various separator line dash and colon combinations
        sample_md = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE** | **Mode**: HEADLESS\n\n"
            "## 🚀 Active Container Tasks (1)\n\n"
            "| Task ID | Agent | Target | Elapsed | Status | Remote Control |\n"
            "|:---|:---|:---|:---|:---|:---|\n"
            "| `task-1` | `agent-1` | `target-1` | 12s | 🔄 RUNNING | [Remote Control 🌐](https://example.com/rc) |\n\n"
            "## ⏳ Queued Tasks (1)\n\n"
            "| Task ID | Agent | Target | Priority | Wait Time |\n"
            "| :---: | :---: | :---: | :---: | :---: |\n"
            "| `task-2` | `agent-2` | `target-2` | P1 | 30s |\n\n"
            "## 📜 Recent Task Execution History (1)\n\n"
            "| Task ID | Agent | Target | Duration | Status | Details |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| `task-3` | `agent-3` | `target-3` | 45s | ✅ COMPLETED | Finished |\n"
        )
        parsed = parse_dashboard_markdown(sample_md)
        self.assertEqual(len(parsed["active_tasks"]), 1)
        self.assertEqual(parsed["active_tasks"][0]["id"], "task-1")
        self.assertEqual(parsed["active_tasks"][0]["remote_control_url"], "https://example.com/rc")
        self.assertEqual(len(parsed["queued_tasks_list"]), 1)
        self.assertEqual(parsed["queued_tasks_list"][0]["id"], "task-2")
        self.assertEqual(len(parsed["history_tasks"]), 1)
        self.assertEqual(parsed["history_tasks"][0]["id"], "task-3")


class TestDashboardUpdater(unittest.TestCase):
    """Test DashboardUpdater file registration and auto-writing loop."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_register_and_unregister_targets(self):
        updater = DashboardUpdater(host="127.0.0.1", port=8000)
        target1 = self.dir_path / "dashboard1.md"
        target2 = self.dir_path / "dashboard2.md"

        self.assertTrue(updater.register_target(target1))
        self.assertEqual(len(updater.get_targets()), 1)
        self.assertIn(str(target1.resolve()), updater.get_targets())

        self.assertTrue(updater.register_target(target2))
        self.assertEqual(len(updater.get_targets()), 2)

        self.assertTrue(updater.unregister_target(target1))
        self.assertEqual(len(updater.get_targets()), 1)
        self.assertNotIn(str(target1.resolve()), updater.get_targets())

        self.assertFalse(updater.unregister_target(self.dir_path / "nonexistent.md"))

    def test_update_now_writes_to_targets(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 0, "max_workers": 1}
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []

        updater = DashboardUpdater(task_manager=mock_tm, host="127.0.0.1", port=8000)
        target = self.dir_path / "sub" / "live_dashboard.md"
        updater.register_target(target)

        content = updater.update_now()
        self.assertIsNotNone(content)
        self.assertTrue(target.exists())
        file_text = target.read_text(encoding="utf-8")
        self.assertIn("# 🌌 Graviton Live Dashboard", file_text)
        self.assertEqual(file_text, content)

    def test_background_updater_loop_runs_and_stops(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 0, "max_workers": 1}
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []

        updater = DashboardUpdater(
            task_manager=mock_tm,
            update_interval=0.1,
            min_interval=0.05,
        )
        target = self.dir_path / "loop_dashboard.md"
        updater.register_target(target)

        updater.start()
        # Verify thread started
        self.assertTrue(updater._running)
        time.sleep(0.25)
        updater.trigger_update()
        time.sleep(0.1)
        updater.stop()

        self.assertFalse(updater._running)
        self.assertTrue(target.exists())
        self.assertIn("# 🌌 Graviton Live Dashboard", target.read_text(encoding="utf-8"))

    def test_get_markdown_does_not_write_to_targets(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {"active_workers": 0, "max_workers": 1}
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []

        updater = DashboardUpdater(task_manager=mock_tm, host="127.0.0.1", port=8000)
        target = self.dir_path / "read_only_dashboard.md"
        updater.register_target(target)

        content = updater.get_markdown()
        self.assertIsNotNone(content)
        self.assertIn("# 🌌 Graviton Live Dashboard", content)
        # Verify read-only get_markdown did NOT write to target file on disk
        self.assertFalse(target.exists())

    def test_write_to_target_temp_file_cleanup_on_failure(self):
        updater = DashboardUpdater(host="127.0.0.1", port=8000)
        target = self.dir_path / "fail_dashboard.md"

        with patch.object(Path, "replace", side_effect=OSError("Disk full or permission denied")):
            result = updater._write_to_target(target, "# Failed Write Content")
            self.assertFalse(result)

        # Target should not exist
        self.assertFalse(target.exists())
        # Any temporary files matching the pattern should have been unlinked/cleaned up
        tmp_files = list(self.dir_path.glob("fail_dashboard.md.tmp.*"))
        self.assertEqual(tmp_files, [])

    def test_write_to_target_includes_pid_and_thread_id(self):
        updater = DashboardUpdater(host="127.0.0.1", port=8000)
        target = self.dir_path / "ident_dashboard.md"

        captured_tmp = []

        def mock_replace(src_path, dest_path):
            captured_tmp.append(str(src_path))
            return dest_path

        with patch.object(Path, "replace", autospec=True, side_effect=mock_replace):
            result = updater._write_to_target(target, "# Ident Content")
            self.assertTrue(result)

        self.assertEqual(len(captured_tmp), 1)
        expected_suffix = f".tmp.{os.getpid()}.{threading.get_ident()}"
        self.assertTrue(captured_tmp[0].endswith(expected_suffix))

    def test_parse_dashboard_markdown_model_selection_fields(self):
        md = "# Dashboard\n| **Active Pool** | `gemini` |\n| **Active Model** | `gemini-3.6-flash-high` |"
        parsed = parse_dashboard_markdown(md)
        self.assertIn("available_gemini_models", parsed)
        self.assertIn("available_third_party_models", parsed)
        self.assertEqual(parsed["active_gemini_model"], "gemini-3.6-flash-high")
        self.assertIn("gemini-3.6-flash-high", parsed["available_gemini_models"])

    def test_render_dashboard_html_model_dropdown_elements(self):
        tracker = QuotaTracker()
        tracker.available_gemini_models = ["gemini-3.8-flash-medium", "gemini-3.6-flash-high"]
        tracker.available_third_party_models = ["claude-sonnet-4-6", "gpt-oss-120b-medium"]
        tracker.set_active_model("gemini", "gemini-3.6-flash-high")
        tracker.set_active_model("third_party", "claude-sonnet-4-6")

        md = format_dashboard_markdown(quota_tracker=tracker)
        html_out = render_dashboard_html(md, quota_tracker=tracker)

        self.assertIn('id="gemini-model-select"', html_out)
        self.assertIn('id="tp-model-select"', html_out)
        self.assertIn('data-pool="gemini"', html_out)
        self.assertIn('data-pool="third_party"', html_out)
        self.assertIn('value="gemini-3.6-flash-high" selected', html_out)
        self.assertIn('value="claude-sonnet-4-6" selected', html_out)
        self.assertIn('id="toast-container"', html_out)
        self.assertIn('/api/model', html_out)

    def test_format_dashboard_markdown_exposes_both_active_models(self):
        tracker = QuotaTracker()
        tracker.set_active_model("gemini", "gemini-3.6-flash-high")
        tracker.set_active_model("third_party", "claude-opus-4-6-thinking")
        md = format_dashboard_markdown(quota_tracker=tracker)

        self.assertIn("| **Active Gemini Model** | `gemini-3.6-flash-high` |", md)
        self.assertIn("| **Active Third-Party Model** | `claude-opus-4-6-thinking` |", md)

        parsed = parse_dashboard_markdown(md)
        self.assertEqual(parsed["active_gemini_model"], "gemini-3.6-flash-high")
        self.assertEqual(parsed["active_third_party_model"], "claude-opus-4-6-thinking")

    def test_render_dashboard_html_whitespace_active_model_not_prepended(self):
        md = "# Dashboard\n| **Active Pool** | `gemini` |\n| **Active Model** | `gemini-3.8-flash-medium` |"
        parsed = parse_dashboard_markdown(md)
        parsed["active_gemini_model"] = "   "
        parsed["active_third_party_model"] = ""

        html_out = render_dashboard_html(md)
        self.assertNotIn('<option value="   "', html_out)
        self.assertNotIn('<option value=""', html_out)


    def test_format_dashboard_markdown_with_approved_prs(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = [
            {
                "number": 42,
                "repo_full_name": "owner/repo",
                "title": "Add feature X",
                "author": "octocat",
                "url": "https://github.com/owner/repo/pull/42",
            }
        ]
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("## 🔀 Approved Pull Requests (Ready to Merge)", md)
        self.assertIn("| [`#42`](https://github.com/owner/repo/pull/42) | `owner/repo` | Add feature X | `@octocat` | [View PR ↗](https://github.com/owner/repo/pull/42) |", md)

    def test_format_dashboard_markdown_approved_prs_empty(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = []
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("## 🔀 Approved Pull Requests (Ready to Merge)", md)
        self.assertIn("*(No approved PRs awaiting merge)*", md)

    def test_format_dashboard_markdown_approved_prs_unknown_repo_renders_unadorned_dash(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = [
            {
                "number": 42,
                "repo_full_name": "-",
                "title": "Missing repo PR",
                "author": "octocat",
                "url": "https://github.com/owner/repo/pull/42",
            }
        ]
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("## 🔀 Approved Pull Requests (Ready to Merge)", md)
        self.assertIn("| [`#42`](https://github.com/owner/repo/pull/42) | - | Missing repo PR | `@octocat` | [View PR ↗](https://github.com/owner/repo/pull/42) |", md)
        self.assertNotIn("`-`", md)

        parsed = parse_dashboard_markdown(md)
        self.assertEqual(len(parsed["approved_prs"]), 1)
        self.assertEqual(parsed["approved_prs"][0]["number"], 42)
        self.assertEqual(parsed["approved_prs"][0]["repo_full_name"], "-")

    def test_render_dashboard_html_with_approved_prs(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = [
            {
                "number": 105,
                "repo_full_name": "google/graviton",
                "title": "Support 3.8 flash medium",
                "author": "mweastwood",
                "url": "https://github.com/google/graviton/pull/105",
            }
        ]
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        html_out = render_dashboard_html(md, pr_tracker=mock_pr_tracker)

        self.assertIn("<h2>🔀 Approved Pull Requests (Ready to Merge)</h2>", html_out)
        self.assertIn('id="approved-prs-count"', html_out)
        self.assertIn("1 Ready", html_out)
        self.assertIn('<code>#105</code>', html_out)
        self.assertIn('<code>google/graviton</code>', html_out)
        self.assertIn('Support 3.8 flash medium', html_out)
        self.assertIn('<span class="author-badge">@mweastwood</span>', html_out)
        self.assertIn('href="https://github.com/google/graviton/pull/105"', html_out)
        self.assertIn('View PR ↗', html_out)

    def test_render_dashboard_html_approved_prs_empty(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = []
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        html_out = render_dashboard_html(md, pr_tracker=mock_pr_tracker)

        self.assertIn("<h2>🔀 Approved Pull Requests (Ready to Merge)</h2>", html_out)
        self.assertIn('0 Ready', html_out)
        self.assertIn("No approved PRs awaiting merge.", html_out)

    def test_parse_dashboard_markdown_approved_prs(self):
        md = (
            "# Dashboard\n\n"
            "## 🔀 Approved Pull Requests (Ready to Merge)\n\n"
            "| PR # | Repository | Title | Author | URL |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| [`#77`](https://github.com/test/repo/pull/77) | `test/repo` | Great PR | `@alice` | [View PR ↗](https://github.com/test/repo/pull/77) |\n"
        )
        parsed = parse_dashboard_markdown(md)
        self.assertIn("approved_prs", parsed)
        self.assertEqual(len(parsed["approved_prs"]), 1)
        pr = parsed["approved_prs"][0]
        self.assertEqual(pr["number"], 77)
        self.assertEqual(pr["repo_full_name"], "test/repo")
        self.assertEqual(pr["title"], "Great PR")
        self.assertEqual(pr["author"], "alice")
        self.assertEqual(pr["url"], "https://github.com/test/repo/pull/77")

    def test_dashboard_updater_with_pr_tracker(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = [
            {
                "number": 88,
                "repo_full_name": "owner/repo",
                "title": "PR Title",
                "author": "bob",
                "url": "https://github.com/owner/repo/pull/88",
            }
        ]
        updater = DashboardUpdater(pr_tracker=mock_pr_tracker)
        md = updater.get_markdown()
        self.assertIn("## 🔀 Approved Pull Requests (Ready to Merge)", md)
        self.assertIn("`#88`", md)

    def test_render_dashboard_html_client_js_regex_capture_index(self):
        html_out = render_dashboard_html("# Dashboard")
        self.assertIn("urlMatch[2].trim()", html_out)
        self.assertNotIn("prUrl = urlMatch[1].trim()", html_out)
        self.assertIn("(author && author !== '-')", html_out)

    def test_format_dashboard_markdown_empty_author(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = [
            {
                "number": 99,
                "repo_full_name": "owner/repo",
                "title": "PR with empty author",
                "author": "",
                "url": "https://github.com/owner/repo/pull/99",
            }
        ]
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("| [`#99`](https://github.com/owner/repo/pull/99) | `owner/repo` | PR with empty author | - | [View PR ↗](https://github.com/owner/repo/pull/99) |", md)
        self.assertNotIn("@-", md)

    def test_format_dashboard_markdown_title_newline_sanitization(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = [
            {
                "number": 100,
                "repo_full_name": "owner/repo",
                "title": "Multi\r\nline\ntitle | with pipe",
                "author": "dev",
                "url": "https://github.com/owner/repo/pull/100",
            }
        ]
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("Multi  line title - with pipe", md)
        self.assertNotIn("\nline", md)
        self.assertNotIn("\r", md)

    def test_format_dashboard_markdown_extra_info_fallback(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = []
        extra_info = {
            "approved_prs": [
                {
                    "number": 101,
                    "repo_full_name": "owner/repo",
                    "title": "Fallback PR",
                    "author": "fallback-user",
                    "url": "https://github.com/owner/repo/pull/101",
                }
            ]
        }
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker, extra_info=extra_info)
        self.assertIn("Fallback PR", md)
        self.assertIn("`#101`", md)

    def test_render_dashboard_html_extra_info_fallback(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = []
        extra_info = {
            "approved_prs": [
                {
                    "number": 102,
                    "repo_full_name": "owner/repo",
                    "title": "HTML Fallback PR",
                    "author": "html-user",
                    "url": "https://github.com/owner/repo/pull/102",
                }
            ]
        }
        html_out = render_dashboard_html("# Dashboard", pr_tracker=mock_pr_tracker, extra_info=extra_info)
        self.assertIn("HTML Fallback PR", html_out)
        self.assertIn("1 Ready", html_out)

    def test_parse_dashboard_markdown_approved_prs_raw_url(self):
        md = (
            "# Dashboard\n\n"
            "## 🔀 Approved Pull Requests (Ready to Merge)\n\n"
            "| PR # | Repository | Title | Author | URL |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| `#105` | - | Raw URL PR | `@octocat` | https://github.com/custom/repo/pull/105 |\n"
        )
        parsed = parse_dashboard_markdown(md)
        self.assertIn("approved_prs", parsed)
        self.assertEqual(len(parsed["approved_prs"]), 1)
        pr = parsed["approved_prs"][0]
        self.assertEqual(pr["number"], 105)
        self.assertEqual(pr["url"], "https://github.com/custom/repo/pull/105")
        self.assertEqual(pr["author"], "octocat")
        self.assertEqual(pr["title"], "Raw URL PR")

    def test_approved_prs_dict_author_support(self):
        approved = [
            {
                "number": 106,
                "repo_full_name": "owner/repo",
                "title": "Dict Author PR",
                "author": {"login": "octocat"},
                "url": "https://github.com/owner/repo/pull/106",
            }
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("`@octocat`", md)
        self.assertNotIn("{'login'", md)
        self.assertNotIn("&#x27;", md)

        html_table = _render_approved_prs_table(approved)
        self.assertIn('<span class="author-badge">@octocat</span>', html_table)
        self.assertNotIn("{&#x27;login&#x27;", html_table)

    def test_approved_prs_sanitization_pipes_and_newlines(self):
        approved = [
            {
                "number": 107,
                "repo_full_name": "owner|with|pipe\nnewline\rrepo",
                "title": "Title|pipe\r\nnewline",
                "author": "user|pipe\nnewline",
                "url": "",
            }
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        approved_lines = [line for line in md.splitlines() if line.startswith("|") and ("#107" in line)]
        self.assertEqual(len(approved_lines), 1)
        row = approved_lines[0]
        self.assertNotIn("\n", row)
        self.assertNotIn("\r", row)
        cells = [c.strip() for c in row.split("|")[1:-1]]
        self.assertEqual(len(cells), 5)
        self.assertEqual(cells[1], "`owner-with-pipe newline repo`")
        self.assertEqual(cells[2], "Title-pipe  newline")
        self.assertEqual(cells[3], "`@user-pipe newline`")

        parsed = parse_dashboard_markdown(md)
        self.assertEqual(len(parsed["approved_prs"]), 1)
        self.assertEqual(parsed["approved_prs"][0]["number"], 107)

    def test_render_dashboard_html_client_js_pr_num_escaped(self):
        html_out = render_dashboard_html("# Dashboard")
        self.assertIn("const digitsMatch = r[0].match(/\\d+/);", html_out)
        self.assertIn("prNum = digitsMatch ? digitsMatch[0] : escapeHtml(r[0].replace(/[`#]/g, '').trim());", html_out)
        self.assertIn("const rawRepo = r[1].replace(/`/g, '').trim();", html_out)
        self.assertIn("if (!prUrl && rawRepo && rawRepo !== '-' && hasNum)", html_out)

    def test_render_approved_prs_table_no_double_html_encoding_in_fallback_url(self):
        approved = [
            {
                "number": 108,
                "repo_full_name": "owner&org/repo&project",
                "title": "Ampersand Repo",
                "author": "bob",
                "url": "",
            }
        ]
        html_out = _render_approved_prs_table(approved)
        self.assertIn('href="https://github.com/owner&amp;org/repo&amp;project/pull/108"', html_out)
        self.assertNotIn("&amp;amp;", html_out)

    def test_parse_dashboard_markdown_approved_prs_rejects_unsafe_urls(self):
        # 1. Unsafe scheme in column 0 markdown link brackets
        md_col0 = (
            "# Dashboard\n\n"
            "## 🔀 Approved Pull Requests (Ready to Merge)\n\n"
            "| PR # | Repository | Title | Author | URL |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| [`#101`](javascript:alert(1)) | - | XSS PR | `@alice` | - |\n"
        )
        parsed0 = parse_dashboard_markdown(md_col0)
        self.assertIn("approved_prs", parsed0)
        self.assertEqual(len(parsed0["approved_prs"]), 1)
        self.assertEqual(parsed0["approved_prs"][0]["number"], 101)
        self.assertEqual(parsed0["approved_prs"][0]["url"], "")

        # 2. Unsafe scheme in column 5 markdown link brackets
        md_col5 = (
            "# Dashboard\n\n"
            "## 🔀 Approved Pull Requests (Ready to Merge)\n\n"
            "| PR # | Repository | Title | Author | URL |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| `#102` | - | XSS Link PR | `@bob` | [View PR ↗](javascript:alert(2)) |\n"
        )
        parsed5 = parse_dashboard_markdown(md_col5)
        self.assertIn("approved_prs", parsed5)
        self.assertEqual(len(parsed5["approved_prs"]), 1)
        self.assertEqual(parsed5["approved_prs"][0]["number"], 102)
        self.assertEqual(parsed5["approved_prs"][0]["url"], "")

        # 3. Candidate unsafe URL is rejected and safe repository fallback is used
        md_fallback = (
            "# Dashboard\n\n"
            "## 🔀 Approved Pull Requests (Ready to Merge)\n\n"
            "| PR # | Repository | Title | Author | URL |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| [`#103`](data:text/html,evil) | `custom/repo` | Fallback PR | `@charlie` | [Link](javascript:void(0)) |\n"
        )
        parsed_fallback = parse_dashboard_markdown(md_fallback)
        self.assertEqual(len(parsed_fallback["approved_prs"]), 1)
        self.assertEqual(parsed_fallback["approved_prs"][0]["url"], "https://github.com/custom/repo/pull/103")

    def test_format_dashboard_markdown_pr_num_pipes_and_newlines_sanitization(self):
        approved = [
            {
                "number": "42 | pipe\r\nnewline",
                "repo_full_name": "owner/repo",
                "title": "Pipe PR",
                "author": "octocat",
                "url": "",
            }
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        row_lines = [l for l in md.splitlines() if l.startswith("|") and ("Pipe PR" in l)]
        self.assertEqual(len(row_lines), 1)
        row = row_lines[0]
        self.assertNotIn("\n", row)
        self.assertNotIn("\r", row)
        # Should NOT split into more than 5 columns
        cells = [c.strip() for c in row.split("|")[1:-1]]
        self.assertEqual(len(cells), 5)
        self.assertIn("#42 - pipenewline", cells[0])

        parsed = parse_dashboard_markdown(md)
        self.assertEqual(len(parsed["approved_prs"]), 1)
        self.assertEqual(parsed["approved_prs"][0]["number"], 42)

    def test_approved_prs_author_leading_at_normalization(self):
        approved = [
            {
                "number": 109,
                "repo_full_name": "owner/repo",
                "title": "At Author PR",
                "author": "@octocat",
                "url": "https://github.com/owner/repo/pull/109",
            },
            {
                "number": 110,
                "repo_full_name": "owner/repo",
                "title": "Double At Author PR",
                "author": "@@multi_at",
                "url": "https://github.com/owner/repo/pull/110",
            },
            {
                "number": 111,
                "repo_full_name": "owner/repo",
                "title": "Dict At Author PR",
                "author": {"login": "@dict_user"},
                "url": "https://github.com/owner/repo/pull/111",
            },
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("`@octocat`", md)
        self.assertNotIn("`@@octocat`", md)
        self.assertIn("`@multi_at`", md)
        self.assertNotIn("`@@multi_at`", md)
        self.assertIn("`@dict_user`", md)
        self.assertNotIn("`@@dict_user`", md)

        html_table = _render_approved_prs_table(approved)
        self.assertIn('<span class="author-badge">@octocat</span>', html_table)
        self.assertNotIn('<span class="author-badge">@@octocat</span>', html_table)
        self.assertIn('<span class="author-badge">@multi_at</span>', html_table)
        self.assertNotIn('<span class="author-badge">@@multi_at</span>', html_table)
        self.assertIn('<span class="author-badge">@dict_user</span>', html_table)
        self.assertNotIn('<span class="author-badge">@@dict_user</span>', html_table)

    def test_render_dashboard_html_client_js_url_scheme_validation(self):
        html_out = render_dashboard_html("# Dashboard")
        self.assertIn("const candUrl = prMatch[2].trim();", html_out)
        self.assertIn("if (isSafeUrl(candUrl))", html_out)
        self.assertIn("const candUrl = urlMatch[2].trim();", html_out)

    def test_parse_dashboard_markdown_pr_title_contains_pr_number(self):
        md = (
            "# Dashboard\n\n"
            "## 🔀 Approved Pull Requests (Ready to Merge)\n\n"
            "| PR # | Repository | Title | Author | URL |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| [`#42`](https://github.com/owner/repo/pull/42) | `owner/repo` | fix: resolve conflict with PR #100 | `@alice` | [View PR ↗](https://github.com/owner/repo/pull/42) |\n"
        )
        parsed = parse_dashboard_markdown(md)
        self.assertIn("approved_prs", parsed)
        self.assertEqual(len(parsed["approved_prs"]), 1)
        self.assertEqual(parsed["approved_prs"][0]["number"], 42)
        self.assertEqual(parsed["approved_prs"][0]["title"], "fix: resolve conflict with PR #100")

    def test_client_js_parsetablerows_header_filtering(self):
        html_out = render_dashboard_html("# Dashboard")
        self.assertIn("if (parts[0] !== 'Metric' && parts[0] !== 'Task ID' && parts[0] !== 'PR #')", html_out)
        self.assertNotIn("!line.includes('PR #')", html_out)

    def test_approved_prs_dict_author_none_login_and_empty_dict(self):
        approved = [
            {
                "number": 201,
                "repo_full_name": "owner/repo",
                "title": "Deleted User PR",
                "author": {"login": None},
                "url": "https://github.com/owner/repo/pull/201",
            },
            {
                "number": 202,
                "repo_full_name": "owner/repo",
                "title": "Empty Dict User PR",
                "author": {},
                "url": "https://github.com/owner/repo/pull/202",
            },
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("`#201`", md)
        self.assertIn("`#202`", md)
        self.assertNotIn("None", md)

        html_table = _render_approved_prs_table(approved)
        self.assertIn("<code>#201</code>", html_table)
        self.assertIn("<code>#202</code>", html_table)
        self.assertNotIn("@None", html_table)

    def test_approved_prs_none_pr_number_handling(self):
        approved = [
            {
                "number": None,
                "repo_full_name": "owner/repo",
                "title": "PR with None number",
                "author": "carol",
                "url": "",
            },
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertNotIn("#None", md)
        self.assertNotIn("pull/None", md)

        html_table = _render_approved_prs_table(approved)
        self.assertNotIn("#None", html_table)
        self.assertNotIn("pull/None", html_table)
        self.assertIn('<span class="text-muted">-</span>', html_table)

    def test_approved_prs_none_title_in_html(self):
        approved = [
            {
                "number": 203,
                "repo_full_name": "owner/repo",
                "title": None,
                "author": "dave",
                "url": "https://github.com/owner/repo/pull/203",
            },
        ]
        html_table = _render_approved_prs_table(approved)
        self.assertIn('<span class="pr-title"></span>', html_table)
        self.assertNotIn('<span class="pr-title">None</span>', html_table)

    def test_approved_prs_url_sanitization_pipes(self):
        approved = [
            {
                "number": 204,
                "repo_full_name": "owner/repo",
                "title": "Pipe URL PR",
                "author": "eve",
                "url": "https://github.com/owner/repo/pull/204|extra_pipe",
            },
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        # Verify markdown row has exactly 5 columns
        row = [line for line in md.splitlines() if line.startswith("|") and "#204" in line][0]
        cells = [c.strip() for c in row.split("|")[1:-1]]
        self.assertEqual(len(cells), 5)
        self.assertNotIn("|", cells[0])
        self.assertNotIn("|", cells[4])

        parsed = parse_dashboard_markdown(md)
        self.assertEqual(len(parsed["approved_prs"]), 1)
        self.assertEqual(parsed["approved_prs"][0]["number"], 204)
        self.assertNotIn("|", parsed["approved_prs"][0]["url"])

    def test_approved_prs_skips_non_dict_elements(self):
        approved = [
            None,
            "not-a-dict",
            12345,
            {
                "number": 301,
                "repo_full_name": "owner/repo",
                "title": "Valid PR",
                "author": "alice",
                "url": "https://github.com/owner/repo/pull/301",
            },
            ["list", "item"],
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved

        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("#301", md)
        self.assertIn("Valid PR", md)

        html_out = _render_approved_prs_table(approved)
        self.assertIn("#301", html_out)
        self.assertIn("Valid PR", html_out)

        # Verify all non-dict elements renders empty state, not empty table headers
        all_non_dict = [None, "invalid", 42]
        mock_pr_tracker.get_approved_prs.return_value = all_non_dict
        md_non_dict = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("*(No approved PRs awaiting merge)*", md_non_dict)
        self.assertNotIn("| PR # |", md_non_dict)

        html_non_dict = _render_approved_prs_table(all_non_dict)
        self.assertIn("empty-card", html_non_dict)
        self.assertNotIn("data-table", html_non_dict)

    def test_approved_prs_non_string_title_handling(self):
        approved = [
            {
                "number": 501,
                "repo_full_name": "owner/repo",
                "title": 12345,
                "author": "tester",
                "url": "https://github.com/owner/repo/pull/501",
            },
            {
                "number": 502,
                "repo_full_name": "owner/repo",
                "title": True,
                "author": "tester",
                "url": "https://github.com/owner/repo/pull/502",
            },
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("12345", md)
        self.assertIn("True", md)

        html_out = _render_approved_prs_table(approved)
        self.assertIn("12345", html_out)
        self.assertIn("True", html_out)

    def test_approved_prs_empty_state_in_render_dashboard_html_for_non_dict(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = [None, "invalid", 999]
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        html_out = render_dashboard_html(md, pr_tracker=mock_pr_tracker)
        self.assertIn("0 Ready", html_out)
        self.assertIn("No approved PRs awaiting merge.", html_out)

    def test_approved_prs_none_pr_number_no_zero_or_pull_zero_url(self):
        approved = [
            {
                "number": None,
                "repo_full_name": "owner/repo",
                "title": "PR with None number",
                "author": "alice",
                "url": "",
            },
        ]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        # Should not synthesize /pull/ or /pull/0
        self.assertNotIn("pull/0", md)
        self.assertNotIn("`#0`", md)
        self.assertIn("| - | `owner/repo` | PR with None number | `@alice` | - |", md)

        html_out = _render_approved_prs_table(approved)
        self.assertNotIn("pull/0", html_out)
        self.assertNotIn("<code>#0</code>", html_out)

        # Round trip via parse_dashboard_markdown with cell '-'
        parsed = parse_dashboard_markdown(md)
        for item in parsed.get("approved_prs", []):
            self.assertIsNone(item["number"])
            self.assertNotIn("pull/0", item.get("url", ""))

        # Also verify when raw_num is "0", 0, "-", or "None", has_num is False in both markdown and HTML
        for bad_num in ("0", 0, "-", "None"):
            bad_approved = [{"number": bad_num, "repo_full_name": "owner/repo", "title": "Test PR", "author": "alice"}]
            mock_pr_tracker.get_approved_prs.return_value = bad_approved
            bad_md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
            self.assertNotIn(f"pull/{bad_num}", bad_md)
            self.assertNotIn(f"`#{bad_num}`", bad_md)
            self.assertIn("| - | `owner/repo` | Test PR | `@alice` | - |", bad_md)

            bad_html = _render_approved_prs_table(bad_approved)
            self.assertNotIn(f"pull/{bad_num}", bad_html)
            self.assertNotIn(f"#{bad_num}</code>", bad_html)

    def test_render_dashboard_html_client_script_has_num_logic(self):
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = []
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        html_out = render_dashboard_html(md, pr_tracker=mock_pr_tracker)
        self.assertIn("const hasNum = Boolean(prNum && prNum !== '-' && prNum !== 'None' && prNum !== '0');", html_out)
        self.assertIn("if (!prUrl && rawRepo && rawRepo !== '-' && hasNum) {", html_out)

    def test_is_safe_url_and_markdown_link_injection_prevention(self):
        self.assertTrue(is_safe_url("https://github.com/mweastwood/graviton/pull/1"))
        self.assertTrue(is_safe_url("http://example.com/test"))
        self.assertFalse(is_safe_url("javascript:alert(1)"))
        self.assertFalse(is_safe_url("https://example.com/path) [Click](https://evil.com"))
        self.assertFalse(is_safe_url("https://example.com/path <script>"))
        self.assertFalse(is_safe_url("https://example.com/path\"quote"))
        self.assertFalse(is_safe_url("https://example.com/path'quote"))
        self.assertFalse(is_safe_url("https://example.com/path\nnewline"))
        self.assertFalse(is_safe_url("https://example.com/path\rreturn"))
        self.assertFalse(is_safe_url("https://example.com/path\ttab"))
        self.assertFalse(is_safe_url("   "))
        self.assertFalse(is_safe_url(None))

        # Markdown URL injection test: parenthesis encoded so link is not prematurely terminated
        approved = [{
            "number": 401,
            "repo_full_name": "owner/repo",
            "title": "Injection Test",
            "author": "hacker",
            "url": "https://github.com/repo/pull/1(subpath)",
        }]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertIn("%28subpath%29", md)

        # And malicious URL with closing parenthesis and spaces/quotes is rejected by is_safe_url
        approved_malicious = [{
            "number": 402,
            "repo_full_name": "owner/repo",
            "title": "Malicious Test",
            "author": "hacker",
            "url": "https://github.com/repo/pull/1) [Injected](javascript:alert(1))",
        }]
        mock_pr_tracker.get_approved_prs.return_value = approved_malicious
        md_malicious = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertNotIn("javascript:alert(1)", md_malicious)
        self.assertNotIn("[Injected]", md_malicious)

    def test_approved_prs_markdown_backtick_sanitization(self):
        approved = [{
            "number": "4`0`2",
            "repo_full_name": "owner/`repo`",
            "title": "Backtick test",
            "author": "dev`user",
            "url": "https://github.com/owner/repo/pull/402",
        }]
        mock_pr_tracker = MagicMock()
        mock_pr_tracker.get_approved_prs.return_value = approved
        md = format_dashboard_markdown(pr_tracker=mock_pr_tracker)
        self.assertNotIn("owner/`repo`", md)
        self.assertIn("`owner/repo`", md)
        self.assertIn("`#402`", md)
        self.assertIn("`@devuser`", md)


class TestDashboardTemplateLoaderAndOptimization(unittest.TestCase):
    """Test template decoupling, caching, fallback mechanism, and regex optimization."""

    def setUp(self):
        _reset_dashboard_template_cache()

    def tearDown(self):
        _reset_dashboard_template_cache()

    def test_template_loader_loads_external_html(self):
        template = _get_dashboard_template()
        self.assertTrue(template.startswith("<!DOCTYPE html>"))
        self.assertIn("<!DOCTYPE html>", template)
        self.assertIn("Graviton Live Dashboard", template)
        self.assertIn("{effective_host}", template)
        self.assertIn("{active_workers}", template)

    def test_template_loader_caching(self):
        t1 = _get_dashboard_template()
        t2 = _get_dashboard_template()
        self.assertIs(t1, t2)

    def test_template_loader_fallback_on_missing_file(self):
        with patch.object(Path, "is_file", return_value=False):
            _reset_dashboard_template_cache()
            template = _get_dashboard_template()
            self.assertIn("<!DOCTYPE html>", template)
            self.assertIn("{effective_host}", template)

    def test_precompiled_regex_patterns_exist_and_match(self):
        self.assertIsNotNone(SERVER_PATTERN.search("**Server**: `localhost:8000`"))
        self.assertIsNotNone(STATUS_PATTERN.search("**Status**: 🟢 **ONLINE**"))
        self.assertIsNotNone(WORKERS_PATTERN.search("| **Active Workers** | `2 / 4` |"))
        self.assertIn("Running Tasks", METRIC_INT_PATTERNS)
        self.assertIn("Active Pool", METRIC_STR_PATTERNS)

    def test_template_js_truncates_raw_detail_before_escape_html(self):
        template = _get_dashboard_template()
        self.assertIn("const rawDetail = r[5].replace(/`/g, '').trim();", template)
        self.assertIn("const truncRaw = rawDetail.length > 40 ? rawDetail.substring(0, 40) + '...' : rawDetail;", template)
        self.assertIn("const cleanDetail = escapeHtml(rawDetail);", template)
        self.assertIn("const trunc = escapeHtml(truncRaw);", template)

    def test_possible_paths_has_no_duplicates_and_correct_subdirectories(self):
        from lib.dashboard import REPO_ROOT
        expected_template_path = REPO_ROOT / "templates" / "dashboard" / "dashboard.html"
        self.assertTrue(expected_template_path.is_file(), f"Template file does not exist at {expected_template_path}")
        template = _get_dashboard_template()
        self.assertIn("<!DOCTYPE html>", template)
        self.assertIn("Graviton Live Dashboard", template)

    def test_template_js_handles_finish_status(self):
        template = _get_dashboard_template()
        self.assertIn("r[4].toLowerCase().includes('finish')", template)


if __name__ == "__main__":
    unittest.main()


