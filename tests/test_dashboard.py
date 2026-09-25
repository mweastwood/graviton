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
    _render_history_tasks_table,
    format_dashboard_markdown,
    format_duration,
    format_percentage,
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
from lib.quota import QuotaTracker, QuotaWindow
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

    def test_format_dashboard_markdown_shows_5h_and_1w_quota(self):
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {}
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = []

        mock_quota = MagicMock()
        w5_g = QuotaWindow(name="5H", remaining_percentage=85.0, reset_time="2026-09-24T10:00:00Z")
        w1_g = QuotaWindow(name="1W", remaining_percentage=92.5, reset_time="2026-09-30T10:00:00Z")
        w5_c = QuotaWindow(name="5H", remaining_percentage=70.0, reset_time="2026-09-24T12:00:00Z")
        w1_c = QuotaWindow(name="1W", remaining_percentage=98.0, reset_time="2026-09-30T12:00:00Z")

        mock_quota.get_pool_windows.side_effect = lambda pool: (w5_g, w1_g) if pool == "gemini" else (w5_c, w1_c)
        mock_quota.get_pool_remaining_percentage.side_effect = lambda pool: 85.0 if pool == "gemini" else 70.0
        mock_quota.get_info().to_dict.return_value = {
            "quota_pool": "gemini",
            "active_model": "gemini-3.8-flash-medium",
            "remaining_percentage": 85.0,
        }
        mock_quota.get_active_model.side_effect = lambda pool: "gemini-3.8-flash-medium" if pool == "gemini" else "claude-sonnet-4-6"

        md = format_dashboard_markdown(task_manager=mock_tm, quota_tracker=mock_quota)

        self.assertIn("| **Gemini (5H)** | `85%` |", md)
        self.assertIn("| **Gemini (1W)** | `92.5%` |", md)
        self.assertIn("| **Third-Party (5H)** | `70%` |", md)
        self.assertIn("| **Third-Party (1W)** | `98%` |", md)
        self.assertIn("| **Gemini Remaining** | `85.0%` |", md)
        self.assertIn("| **Third-Party Remaining** | `70.0%` |", md)

    def test_parse_dashboard_markdown_extracts_5h_and_1w_quota(self):
        sample_md = """# 🌌 Graviton Live Dashboard

## 🎯 Model Quota & Pacing

| Metric | Value | Details |
| :--- | :--- | :--- |
| **Active Pool** | `gemini` | Configured quota bucket |
| **Active Model** | `gemini-3.8-flash-medium` | Active Gemini / LLM persona |
| **Active Gemini Model** | `gemini-3.8-flash-medium` | Active Gemini model persona |
| **Active Third-Party Model** | `claude-sonnet-4-6` | Active Third-Party model persona |
| **Gemini (5H)** | `85.5%` | Reset: 02h 15m | Pacing: OK |
| **Gemini (1W)** | `92.0%` | Reset: 5d 04h | Pacing: OK |
| **Third-Party (5H)** | `70.0%` | Reset: 01h 30m | Pacing: OK |
| **Third-Party (1W)** | `95.0%` | Reset: 4d 12h | Pacing: OK |
| **Gemini Remaining** | `85.5%` | Live Gemini API capacity |
| **Third-Party Remaining** | `70.0%` | Fallback model capacity |
"""
        parsed = parse_dashboard_markdown(sample_md)
        self.assertEqual(parsed["gemini_5h_pct"], 85.5)
        self.assertEqual(parsed["gemini_1w_pct"], 92.0)
        self.assertEqual(parsed["tp_5h_pct"], 70.0)
        self.assertEqual(parsed["tp_1w_pct"], 95.0)
        self.assertEqual(parsed["third_party_5h_pct"], 70.0)
        self.assertEqual(parsed["third_party_1w_pct"], 95.0)
        self.assertEqual(parsed["gemini_5h_countdown"], "02h 15m")
        self.assertEqual(parsed["gemini_1w_countdown"], "5d 04h")
        self.assertEqual(parsed["third_party_5h_countdown"], "01h 30m")
        self.assertEqual(parsed["third_party_1w_countdown"], "4d 12h")

    def test_render_dashboard_html_contains_5h_and_1w_gauges(self):
        sample_md = """# 🌌 Graviton Live Dashboard

## 🎯 Model Quota & Pacing

| Metric | Value | Details |
| :--- | :--- | :--- |
| **Active Pool** | `gemini` | Configured quota bucket |
| **Active Model** | `gemini-3.8-flash-medium` | Active Gemini / LLM persona |
| **Active Gemini Model** | `gemini-3.8-flash-medium` | Active Gemini model persona |
| **Active Third-Party Model** | `claude-sonnet-4-6` | Active Third-Party model persona |
| **Gemini (5H)** | `85%` | Reset: 02:15:00 | Pacing: OK |
| **Gemini (1W)** | `92%` | Reset: 5d 04h | Pacing: OK |
| **Third-Party (5H)** | `70%` | Reset: 01:30:00 | Pacing: OK |
| **Third-Party (1W)** | `95%` | Reset: 4d 12h | Pacing: OK |
| **Gemini Remaining** | `85%` | Live Gemini API capacity |
| **Third-Party Remaining** | `70%` | Fallback model capacity |
"""
        html_out = render_dashboard_html(sample_md)

        self.assertIn("5-Hour Window (Burst)", html_out)
        self.assertIn("1-Week Window (Weekly)", html_out)
        self.assertIn('id="gemini-5h-bar"', html_out)
        self.assertIn('id="gemini-1w-bar"', html_out)
        self.assertIn('id="tp-5h-bar"', html_out)
        self.assertIn('id="tp-1w-bar"', html_out)
        self.assertIn('id="gemini-5h-pct-label"', html_out)
        self.assertIn('id="gemini-1w-pct-label"', html_out)
        self.assertIn('id="tp-5h-pct-label"', html_out)
        self.assertIn('id="tp-1w-pct-label"', html_out)
        self.assertIn("85%", html_out)
        self.assertIn("92%", html_out)
        self.assertIn("70%", html_out)
        self.assertIn("95%", html_out)

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

    def test_render_dashboard_html_client_regex_escapes_parenthesized_labels(self):
        html_out = render_dashboard_html("# Test MD")
        # Ensure the client-side JavaScript escapes regex special characters for labels
        self.assertIn("const esc = label.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');", html_out)
        self.assertIn("new RegExp(\"\\\\|\\\\s*\\\\*\\\\*\" + esc + \"\\\\*\\\\*\\\\s*\\\\|\\\\s*`?([^`|\\\\n]+)`?\");", html_out)
        self.assertIn("new RegExp(\"\\\\|\\\\s*\\\\*\\\\*\" + esc + \"\\\\*\\\\*\\\\s*\\\\|\\\\s*`?[^`|\\\\n]+`?\\\\s*\\\\|\\\\s*([^|\\\\n]+)\\\\|\");", html_out)

    def test_format_dashboard_markdown_quota_info_dict_fallback(self):
        # When quota_tracker is None but extra_info has quota_info, format_dashboard_markdown falls back to quota_info
        extra = {
            "quota_info": {
                "quota_pool": "gemini",
                "active_model": "gemini-3.8-flash-medium",
                "remaining_percentage": 82.0,
                "gemini_window_5h": {
                    "name": "5H",
                    "remaining_percentage": 82.0,
                    "reset_time": "2026-09-24T12:00:00Z",
                    "reset_countdown": "03h 15m",
                    "pacing_recovery_countdown": "00:00:00",
                    "pacing_status": "OK",
                },
                "gemini_window_1w": {
                    "name": "1W",
                    "remaining_percentage": 95.0,
                    "reset_countdown": "6d 02h",
                    "pacing_status": "OK",
                },
                "claude_window_5h": {
                    "name": "5H",
                    "remaining_percentage": 60.0,
                    "reset_countdown": "01h 45m",
                    "pacing_status": "BEHIND_PACING",
                },
                "claude_window_1w": {
                    "name": "1W",
                    "remaining_percentage": 90.0,
                    "reset_countdown": "4d 10h",
                    "pacing_status": "OK",
                },
            }
        }
        md = format_dashboard_markdown(quota_tracker=None, extra_info=extra)
        self.assertIn("| **Gemini (5H)** | `82%` | Reset: 03h 15m | Pacing: OK |", md)
        self.assertIn("| **Gemini (1W)** | `95%` | Reset: 6d 02h | Pacing: OK |", md)
        self.assertIn("| **Third-Party (5H)** | `60%` | Reset: 01h 45m | Pacing: BEHIND_PACING |", md)
        self.assertIn("| **Third-Party (1W)** | `90%` | Reset: 4d 10h | Pacing: OK |", md)

    def test_format_dashboard_markdown_third_party_pool_fallback(self):
        # When quota_tracker is None and quota_info is third-party pool with generic window_5h / window_1w,
        # Third-party pool quota must not leak into Gemini windows.
        extra = {
            "quota_info": {
                "quota_pool": "claude_gpt",
                "window_5h": {
                    "name": "5H",
                    "remaining_percentage": 55.0,
                    "reset_countdown": "01h 15m",
                    "pacing_status": "OK",
                },
                "window_1w": {
                    "name": "1W",
                    "remaining_percentage": 75.0,
                    "reset_countdown": "3d 08h",
                    "pacing_status": "OK",
                },
            }
        }
        md = format_dashboard_markdown(quota_tracker=None, extra_info=extra)
        # Gemini windows should not take Third-Party window_5h / window_1w
        self.assertIn("| **Gemini (5H)** | `N/A` | N/A |", md)
        self.assertIn("| **Gemini (1W)** | `N/A` | N/A |", md)
        # Third-Party windows should correctly resolve window_5h / window_1w
        self.assertIn("| **Third-Party (5H)** | `55%` | Reset: 01h 15m | Pacing: OK |", md)
        self.assertIn("| **Third-Party (1W)** | `75%` | Reset: 3d 08h | Pacing: OK |", md)

    def test_parse_dashboard_markdown_preserves_pacing_status_in_details(self):
        sample_md = """# 🌌 Graviton Live Dashboard

## 🎯 Model Quota & Pacing

| Metric | Value | Details |
| :--- | :--- | :--- |
| **Active Pool** | `gemini` | Configured quota bucket |
| **Active Model** | `gemini-3.8-flash-medium` | Active Gemini / LLM persona |
| **Active Gemini Model** | `gemini-3.8-flash-medium` | Active Gemini model persona |
| **Active Third-Party Model** | `claude-sonnet-4-6` | Active Third-Party model persona |
| **Gemini (5H)** | `85%` | Reset: 02:15:00 | Pacing: OK |
| **Gemini (1W)** | `92%` | Reset: 5d 04h | Pacing: OK |
| **Third-Party (5H)** | `70%` | Reset: 01:30:00 | Pacing: BEHIND_PACING |
| **Third-Party (1W)** | `95%` | Reset: 4d 12h | Pacing: OK |
| **Gemini Remaining** | `85%` | Live Gemini API capacity |
| **Third-Party Remaining** | `70%` | Fallback model capacity |
"""
        parsed = parse_dashboard_markdown(sample_md)
        self.assertEqual(parsed["gemini_5h_details"], "Reset: 02:15:00 | Pacing: OK")
        self.assertEqual(parsed["gemini_1w_details"], "Reset: 5d 04h | Pacing: OK")
        self.assertEqual(parsed["tp_5h_details"], "Reset: 01:30:00 | Pacing: BEHIND_PACING")
        self.assertEqual(parsed["tp_1w_details"], "Reset: 4d 12h | Pacing: OK")

    def test_render_dashboard_html_extra_info_fallback(self):
        extra = {
            "quota_info": {
                "gemini_5h_remaining_percentage": 78.5,
                "gemini_1w_remaining_percentage": 88.0,
                "third_party_5h_remaining_percentage": 65.0,
                "third_party_1w_remaining_percentage": 92.0,
                "gemini_5h_countdown": "02h 10m",
                "gemini_1w_countdown": "5d 11h",
            }
        }
        html_out = render_dashboard_html("# Test MD", quota_tracker=None, extra_info=extra)
        self.assertIn("78.5%", html_out)
        self.assertIn("88%", html_out)
        self.assertIn("65%", html_out)
        self.assertIn("92%", html_out)
        self.assertIn("Reset: 02h 10m | Pacing: OK", html_out)
        self.assertIn("Reset: 5d 11h | Pacing: OK", html_out)


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
        self.assertIn("Active Gemini Model", METRIC_STR_PATTERNS)
        self.assertIn("Active Third-Party Model", METRIC_STR_PATTERNS)
        self.assertIn("Gemini (5H)", METRIC_STR_PATTERNS)
        self.assertIn("Gemini (1W)", METRIC_STR_PATTERNS)
        self.assertIn("Third-Party (5H)", METRIC_STR_PATTERNS)
        self.assertIn("Third-Party (1W)", METRIC_STR_PATTERNS)

    def test_format_percentage_helper(self):
        """Verify format_percentage handles int, float, string-float, and strings with %."""
        self.assertEqual(format_percentage(100), "100%")
        self.assertEqual(format_percentage(88.0), "88%")
        self.assertEqual(format_percentage(78.5), "78.5%")
        self.assertEqual(format_percentage("78.5"), "78.5%")
        self.assertEqual(format_percentage("78.5%"), "78.5%")
        self.assertEqual(format_percentage("88.0"), "88%")
        self.assertEqual(format_percentage("88.0%"), "88%")
        self.assertEqual(format_percentage("100%"), "100%")
        self.assertEqual(format_percentage(None), "N/A")
        self.assertEqual(format_percentage("N/A"), "N/A")
        self.assertEqual(format_percentage(True), "N/A")

    def test_format_percentage_edge_cases(self):
        """Verify format_percentage handles 'N/A%', 'n/a', 'None', and non-numeric strings without appending %."""
        self.assertEqual(format_percentage("N/A%"), "N/A")
        self.assertEqual(format_percentage("n/a"), "N/A")
        self.assertEqual(format_percentage("None"), "N/A")
        self.assertEqual(format_percentage("null"), "N/A")
        self.assertEqual(format_percentage("unknown"), "N/A")
        self.assertEqual(format_percentage("error"), "N/A")
        self.assertEqual(format_percentage("None%", default="N/A"), "N/A")
        self.assertEqual(format_percentage("invalid", default="--"), "--")

    def test_render_dashboard_html_string_float_and_integer_percentages(self):
        """Verify render_dashboard_html handles string-float and int percentages without ValueError or AttributeError."""
        extra = {
            "quota_info": {
                "gemini_5h_remaining_percentage": "78.5",
                "gemini_1w_remaining_percentage": 88,  # int
                "third_party_5h_remaining_percentage": "65.0%",
                "third_party_1w_remaining_percentage": "92.4",
                "gemini_5h_countdown": "02h 10m",
                "gemini_5h_pacing_status": "BEHIND_PACING",
            }
        }
        html_out = render_dashboard_html("# Test MD", quota_tracker=None, extra_info=extra)
        self.assertIn("78.5%", html_out)
        self.assertIn("88%", html_out)
        self.assertIn("65%", html_out)
        self.assertIn("92.4%", html_out)
        self.assertIn("Pacing: BEHIND_PACING", html_out)

    def test_format_dashboard_markdown_integer_window_percentage_compatibility(self):
        """Verify integer remaining_percentage on QuotaWindow does not cause AttributeError on Python 3.10/3.11."""
        win_int = QuotaWindow(name="5H", duration_seconds=18000)
        win_int.remaining_percentage = 80  # int instead of float
        md = format_dashboard_markdown(extra_info={"quota_info": {"gemini_window_5h": win_int}})
        self.assertIn("| **Gemini (5H)** | `80%` |", md)

    def test_format_dashboard_markdown_reconstructed_dict_preserves_pacing_status(self):
        """Verify format_dashboard_markdown preserves pacing_status in reconstructed window dicts."""
        extra = {
            "quota_info": {
                "gemini_5h_remaining_percentage": 45.0,
                "gemini_5h_countdown": "01h 30m",
                "gemini_5h_reset_time": "2026-09-25T12:00:00Z",
                "gemini_5h_pacing_status": "BEHIND_PACING",
            }
        }
        md = format_dashboard_markdown(quota_tracker=None, extra_info=extra)
        self.assertIn("| **Gemini (5H)** | `45%` | Reset: 01h 30m | Pacing: BEHIND_PACING |", md)

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

    def test_format_dashboard_markdown_third_party_pool_capacity_no_leakage(self):
        """Verify third-party active pool assigns remaining_percentage to Third-Party Remaining and does not leak into Gemini capacity or windows."""
        extra = {
            "quota_info": {
                "quota_pool": "claude_gpt",
                "remaining_percentage": 60.0,
            }
        }
        md = format_dashboard_markdown(quota_tracker=None, extra_info=extra)
        self.assertIn("| **Third-Party Remaining** | `60.0%` |", md)
        self.assertIn("| **Gemini Remaining** | `N/A` |", md)
        self.assertIn("| **Gemini (5H)** | `N/A` | N/A |", md)
        self.assertIn("| **Gemini (1W)** | `N/A` | N/A |", md)
        self.assertIn("| **Third-Party (5H)** | `60%` | Live quota capacity |", md)
        self.assertIn("| **Third-Party (1W)** | `60%` | Live quota capacity |", md)
        self.assertNotIn("N/A%", md)

    def test_format_dashboard_markdown_missing_null_windows_render_cleanly(self):
        """Verify missing/null pool windows (e.g. from QuotaInfo.to_dict()) render as N/A without phantom details or N/A%."""
        extra = {
            "quota_info": {
                "quota_pool": "gemini",
                "remaining_percentage": 75.0,
                "gemini_5h_remaining_percentage": None,
                "gemini_5h_countdown": None,
                "gemini_5h_reset_time": None,
                "gemini_5h_pacing_status": "OK",
                "gemini_1w_remaining_percentage": None,
                "third_party_remaining_percentage": None,
                "third_party_5h_remaining_percentage": None,
                "third_party_1w_remaining_percentage": None,
            }
        }
        md = format_dashboard_markdown(quota_tracker=None, extra_info=extra)
        self.assertIn("| **Gemini Remaining** | `75.0%` |", md)
        self.assertIn("| **Third-Party Remaining** | `N/A` |", md)
        self.assertNotIn("Reset: N/A | Pacing: OK", md)
        self.assertNotIn("N/A%", md)
        self.assertIn("| **Third-Party (5H)** | `N/A` | N/A |", md)
        self.assertIn("| **Third-Party (1W)** | `N/A` | N/A |", md)

    def test_render_dashboard_html_preserves_pacing_status_without_countdown(self):
        """Verify render_dashboard_html preserves BEHIND_PACING status even when countdown is None."""
        extra = {
            "quota_info": {
                "gemini_5h_remaining_percentage": 40.0,
                "gemini_5h_countdown": None,
                "gemini_5h_pacing_status": "BEHIND_PACING",
                "third_party_5h_remaining_percentage": 30.0,
                "third_party_5h_countdown": None,
                "third_party_5h_reset_time": "2026-09-25T17:00:00Z",
                "third_party_5h_pacing_status": "BEHIND_PACING",
            }
        }
        html_out = render_dashboard_html("# Test MD", quota_tracker=None, extra_info=extra)
        self.assertIn("Pacing: BEHIND_PACING", html_out)
        self.assertIn("Reset: N/A | Pacing: BEHIND_PACING", html_out)

    def test_dashboard_template_extract_str_and_details_support_array_labels(self):
        """Verify dashboard HTML template includes label arrays for extractStr and extractDetails."""
        _reset_dashboard_template_cache()
        template = _get_dashboard_template()
        self.assertIn("function extractStr(labels, defVal)", template)
        self.assertIn("function extractDetails(labels, defVal)", template)
        self.assertIn("['Third-Party (5H)', 'Third Party (5H)', 'Third-Party Quota (5H)']", template)

    def test_parse_dashboard_markdown_explicit_na_does_not_fallback(self):
        """Verify explicit N/A window rows parse to None rather than falling back to gemini_pct / tp_pct."""
        md_content = """# 🌌 Graviton Live Dashboard
## System Status & Health
| Metric | Value | Notes |
| :--- | :--- | :--- |
| **Active Pool** | `gemini` | Configured quota bucket |
| **Gemini Remaining** | `85.0%` | Primary capacity |
| **Third-Party Remaining** | `70.0%` | Secondary capacity |
| **Gemini (5H)** | `N/A` | N/A |
| **Gemini (1W)** | `85.0%` | Live Gemini weekly quota |
| **Third-Party (5H)** | `N/A` | N/A |
| **Third-Party (1W)** | `70.0%` | Fallback weekly quota |
"""
        data = parse_dashboard_markdown(md_content)
        self.assertEqual(data["gemini_pct"], 85.0)
        self.assertIsNone(data["gemini_5h_pct"])
        self.assertEqual(data["gemini_1w_pct"], 85.0)
        self.assertEqual(data["tp_pct"], 70.0)
        self.assertIsNone(data["tp_5h_pct"])
        self.assertEqual(data["tp_1w_pct"], 70.0)

        # Contrast with legacy markdown dashboard where window rows are missing
        legacy_md = """# 🌌 Graviton Live Dashboard
## System Status & Health
| Metric | Value | Notes |
| :--- | :--- | :--- |
| **Active Pool** | `gemini` | Configured quota bucket |
| **Gemini Remaining** | `85.0%` | Primary capacity |
| **Third-Party Remaining** | `70.0%` | Secondary capacity |
"""
        legacy_data = parse_dashboard_markdown(legacy_md)
        self.assertEqual(legacy_data["gemini_5h_pct"], 85.0)
        self.assertEqual(legacy_data["gemini_1w_pct"], 85.0)
        self.assertEqual(legacy_data["tp_5h_pct"], 70.0)
        self.assertEqual(legacy_data["tp_1w_pct"], 70.0)

    def test_render_dashboard_html_no_phantom_details_on_null_window(self):
        """Verify render_dashboard_html avoids creating dummy 'Reset: N/A | Pacing: OK' details when a window is null in extra_info."""
        extra = {
            "quota_info": {
                "quota_pool": "claude_gpt",
                "remaining_percentage": 50.0,
                "gemini_5h_remaining_percentage": None,
                "gemini_5h_reset_time": None,
                "gemini_5h_countdown": None,
                "gemini_5h_pacing_status": "OK",
                "gemini_1w_remaining_percentage": None,
                "gemini_1w_reset_time": None,
                "gemini_1w_countdown": None,
                "gemini_1w_pacing_status": "OK",
                "third_party_5h_remaining_percentage": 50.0,
                "third_party_5h_reset_time": "2026-09-25T17:00:00Z",
                "third_party_5h_countdown": "01h 00m",
                "third_party_5h_pacing_status": "OK",
            }
        }
        html_out = render_dashboard_html("# Test MD", quota_tracker=None, extra_info=extra)
        self.assertNotIn("Reset: N/A | Pacing: OK", html_out)
        self.assertIn("Reset: 01h 00m | Pacing: OK", html_out)


if __name__ == "__main__":
    unittest.main()
