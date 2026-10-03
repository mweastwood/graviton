#!/usr/bin/env python3
"""
Unit tests for Graviton Live Dashboard Generator and Auto-Updater (lib/dashboard.py).
"""

import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.dashboard import (
    DashboardUpdater,
    REPO_ROOT,
    _detect_git_repo_full_name,
    _extract_countdown,
    _format_target_html_cell,
    _format_target_markdown_cell,
    _get_dashboard_template,
    _render_active_tasks_table,
    _render_approved_prs_table,
    _render_history_tasks_table,
    _render_queued_tasks_table,
    _reset_dashboard_template_cache,
    _reset_detected_repo_cache,
    format_dashboard_markdown,
    format_duration,
    format_percentage,
    get_quota_color,
    is_safe_url,
    parse_countdown_to_seconds,
    calculate_target_pacing_from_details,
    parse_dashboard_markdown,
    render_dashboard_html,
    resolve_target_url,
    SERVER_PATTERN,
    STATUS_PATTERN,
    WORKERS_PATTERN,
    METRIC_INT_PATTERNS,
    METRIC_STR_PATTERNS,
)
from lib.quota import QuotaInfo, QuotaTracker, QuotaWindow
from lib.tasks import Task, TaskStatus


class TestDashboardFormatting(unittest.TestCase):
    """Test duration and markdown formatting functions."""

    def setUp(self):
        super().setUp()
        _reset_detected_repo_cache()
        self.addCleanup(_reset_detected_repo_cache)
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

    def test_format_dashboard_markdown_with_clickable_targets(self):
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
            repo_full_name="owner/repo",
            status=TaskStatus.QUEUED,
            priority=2,
            enqueue_time=time.time() - 10.0,
        )
        completed_task = Task(
            id="task-100",
            agent="code_fixer",
            prompt="Completed task",
            target_id="#40",
            repo_full_name="owner/repo",
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
        self.assertIn("[`#42`](https://github.com/owner/repo/pull/42)", md)
        self.assertNotIn("Remote Control", md)
        self.assertIn("`task-102`", md)
        self.assertIn("[`#43`](https://github.com/owner/repo/pull/43)", md)
        self.assertIn("`task-100`", md)
        self.assertIn("[`#40`](https://github.com/owner/repo/pull/40)", md)
        self.assertIn("✅ `COMPLETED`", md)

    def test_format_dashboard_markdown_target_cell_normalization(self):
        """Verify backticked and markdown-linked targets do not produce double-backticks or nested links."""
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 1,
            "max_workers": 2,
            "active_tasks": 1,
            "queued_tasks": 1,
            "completed_tasks": 1,
            "failed_tasks": 0,
        }
        active_task = Task(
            id="task-backtick",
            agent="code_reviewer",
            prompt="Review PR #42",
            target_id="`#42`",
            repo_full_name="owner/repo",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 30.0,
        )
        queued_task = Task(
            id="task-mdlink",
            agent="issue_triager",
            prompt="Triage issue",
            target_id="[#99](https://github.com/owner/repo/issues/99)",
            repo_full_name="owner/repo",
            status=TaskStatus.QUEUED,
            priority=1,
            enqueue_time=time.time() - 10.0,
        )
        history_task = Task(
            id="task-hyphen",
            agent="pr_drafter",
            prompt="Draft PR",
            target_id="`owner/repo#pr-77`",
            repo_full_name="owner/repo",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 50.0,
            finish_time=time.time() - 10.0,
        )
        mock_tm.get_active_tasks.return_value = [active_task]
        mock_tm.get_queued_tasks.return_value = [queued_task]
        mock_tm.get_task_history.return_value = [history_task]

        md = format_dashboard_markdown(task_manager=mock_tm)
        self.assertNotIn("``", md)
        self.assertNotIn("[[", md)
        self.assertIn("[`#42`](https://github.com/owner/repo/pull/42)", md)
        self.assertIn("[`#99`](https://github.com/owner/repo/issues/99)", md)
        self.assertIn("[`owner/repo#pr-77`](https://github.com/owner/repo/pull/77)", md)

    def test_format_dashboard_markdown_history_error_message_pipe_escaping(self):
        """Verify pipe characters in error messages are escaped and newlines replaced to preserve table structure."""
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 0,
            "max_workers": 2,
            "active_tasks": 0,
            "queued_tasks": 0,
            "completed_tasks": 0,
            "failed_tasks": 1,
        }
        mock_tm.get_active_tasks.return_value = []
        mock_tm.get_queued_tasks.return_value = []
        history_task = Task(
            id="task-err-pipe",
            agent="code_fixer",
            prompt="Fix bug",
            target_id="#42",
            repo_full_name="owner/repo",
            status=TaskStatus.FAILED,
            start_time=time.time() - 30.0,
            finish_time=time.time() - 10.0,
            error_message="ValueError: expected a | b or c\r\nsecond line",
        )
        mock_tm.get_task_history.return_value = [history_task]

        md = format_dashboard_markdown(task_manager=mock_tm)
        # Verify pipe is escaped in markdown table row and newlines are sanitized
        self.assertIn("ValueError: expected a \\| b or c  second...", md)
        self.assertNotIn("c\r\nsecond line", md)

        # Verify parsing table rows does not split into an extra 7th column
        parsed = parse_dashboard_markdown(md)
        self.assertEqual(len(parsed["history_tasks"]), 1)
        h = parsed["history_tasks"][0]
        self.assertEqual(h["id"], "task-err-pipe")
        self.assertEqual(h["agent"], "code_fixer")
        self.assertEqual(h["status"], "FAILED")
        self.assertIn("ValueError: expected a | b or c  second", h["details"])

        # Test short error message under 40 characters
        history_task.error_message = "Err: a | b\r\nc"
        md_short = format_dashboard_markdown(task_manager=mock_tm)
        self.assertIn("`Err: a \\| b  c`", md_short)
        parsed_short = parse_dashboard_markdown(md_short)
        self.assertEqual(parsed_short["history_tasks"][0]["details"], "Err: a | b  c")

    def test_render_dashboard_html(self):
        md = "# Sample Markdown"
        html_out = render_dashboard_html(md, host="localhost", port=8000)
        self.assertIn("<!DOCTYPE html>", html_out)
        self.assertIn("Graviton Live Dashboard", html_out)
        self.assertIn("/dashboard/content", html_out)
        self.assertIn("(pr|pulls?|issues?)", html_out)
        self.assertIn("[\\s#:]+", html_out)

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

    def test_render_dashboard_html_with_active_tasks_and_clickable_target(self):
        active_task = Task(
            id="task-live-1",
            agent="code_reviewer",
            prompt="Review PR #99",
            target_id="#99",
            repo_full_name="owner/repo",
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
        self.assertIn('href="https://github.com/owner/repo/pull/99"', html_out)
        self.assertIn('class="target-link"', html_out)
        self.assertNotIn("🌐 Remote Control", html_out)
        self.assertNotIn("<th>Remote Control</th>", html_out)
        self.assertNotIn("https://antigravity.google.com/c/live-sess-1", html_out)
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
            repo_full_name="owner/repo",
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
            repo_full_name="owner/repo",
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
        self.assertIn('href="https://github.com/owner/repo/issues/55"', html_out)
        self.assertNotIn("Remote Session", html_out)
        self.assertNotIn("https://antigravity.google.com/c/sess-hist", html_out)
        self.assertIn("task-hist-2", html_out)
        self.assertIn("status-failed", html_out)
        self.assertIn('href="https://github.com/owner/repo/pull/56"', html_out)
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
            target_id="javascript:alert(1)",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 30.0,
            remote_control_url="javascript:alert(document.cookie)",
        )
        malicious_history = Task(
            id="task-xss-2",
            agent="code_fixer",
            prompt="Malicious history",
            target_id="javascript:alert(2)",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 60.0,
            finish_time=time.time() - 10.0,
            remote_control_url="javascript:alert('pwned')",
            error_message="Test error",
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
        self.assertIn('class="error-snippet"', html_out)

        # Direct table rendering verification
        active_rendered = _render_active_tasks_table([{
            "id": "t1", "agent": "a", "target": "javascript:alert(1)", "elapsed": "1s", "status": "RUNNING",
            "remote_control_url": "javascript:alert(1)"
        }])
        self.assertNotIn("<a ", active_rendered)

        history_rendered = _render_history_tasks_table([{
            "id": "t2", "agent": "a", "target": "javascript:alert(2)", "duration": "1s", "status": "COMPLETED",
            "remote_control_url": "javascript:alert(1)", "details": "Finished"
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

    def test_resolve_target_url_various_formats(self):
        # Full repo#number format
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#123", agent="code_reviewer"),
            "https://github.com/octocat/Hello-World/pull/123",
        )
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#123", agent="issue_triager"),
            "https://github.com/octocat/Hello-World/issues/123",
        )
        # pr_drafter routes bare targets or issue targets to issues/
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#123", agent="pr_drafter"),
            "https://github.com/octocat/Hello-World/issues/123",
        )
        self.assertEqual(
            resolve_target_url("#42", repo="my-org/my-repo", agent="pr_drafter"),
            "https://github.com/my-org/my-repo/issues/42",
        )
        self.assertEqual(
            resolve_target_url("42", repo="my-org/my-repo", agent="pr_drafter"),
            "https://github.com/my-org/my-repo/issues/42",
        )
        self.assertEqual(
            resolve_target_url("PR #123", repo="my-org/my-repo", agent="pr_drafter"),
            "https://github.com/my-org/my-repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#123", agent="code_fixer"),
            "https://github.com/octocat/Hello-World/pull/123",
        )
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#123", agent="arbitrary_agent"),
            "https://github.com/octocat/Hello-World/pull/123",
        )

        # Flexible prefixes and spacing (PR #123, Issue #45, PR 123, Issue 45)
        self.assertEqual(
            resolve_target_url("PR #123", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("Issue #45", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/issues/45",
        )
        self.assertEqual(
            resolve_target_url("PR 123", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("Issue 45", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/issues/45",
        )
        self.assertEqual(
            resolve_target_url("owner/repo PR #123"),
            "https://github.com/owner/repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("owner/repo Issue #45"),
            "https://github.com/owner/repo/issues/45",
        )
        self.assertEqual(
            resolve_target_url("owner/repo PR 123"),
            "https://github.com/owner/repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("owner/repo Issue 45"),
            "https://github.com/owner/repo/issues/45",
        )

        # Bare #number with explicit repo
        self.assertEqual(
            resolve_target_url("#42", repo="my-org/my-repo", agent="code_reviewer"),
            "https://github.com/my-org/my-repo/pull/42",
        )
        self.assertEqual(
            resolve_target_url("42", repo="my-org/my-repo", agent="issue_triager"),
            "https://github.com/my-org/my-repo/issues/42",
        )

        # Direct HTTP/HTTPS URLs and bare github.com
        self.assertEqual(
            resolve_target_url("https://github.com/foo/bar/pull/99"),
            "https://github.com/foo/bar/pull/99",
        )
        self.assertEqual(
            resolve_target_url("http://github.com/foo/bar/issues/100"),
            "http://github.com/foo/bar/issues/100",
        )
        self.assertEqual(
            resolve_target_url("github.com/foo/bar/pull/99"),
            "https://github.com/foo/bar/pull/99",
        )
        self.assertEqual(
            resolve_target_url("github.com/foo/bar/issues/100"),
            "https://github.com/foo/bar/issues/100",
        )
        self.assertEqual(
            resolve_target_url("github.com/owner/repo"),
            "https://github.com/owner/repo",
        )

        # Whole repository
        self.assertEqual(
            resolve_target_url("owner/repo"),
            "https://github.com/owner/repo",
        )

        # Markdown links
        self.assertEqual(
            resolve_target_url("[#42](https://github.com/owner/repo/pull/42)"),
            "https://github.com/owner/repo/pull/42",
        )

        # Backticked targets
        self.assertEqual(
            resolve_target_url("`#42`", repo="my-org/my-repo", agent="code_reviewer"),
            "https://github.com/my-org/my-repo/pull/42",
        )
        self.assertEqual(
            resolve_target_url("`octocat/Hello-World#123`", agent="code_reviewer"),
            "https://github.com/octocat/Hello-World/pull/123",
        )
        self.assertEqual(
            resolve_target_url("`octocat/Hello-World#123`", agent="issue_triager"),
            "https://github.com/octocat/Hello-World/issues/123",
        )
        self.assertEqual(
            resolve_target_url("`owner/repo`"),
            "https://github.com/owner/repo",
        )
        self.assertEqual(
            resolve_target_url("`https://github.com/foo/bar/pull/99`"),
            "https://github.com/foo/bar/pull/99",
        )

        # Hyphenated target formats
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#pr-123"),
            "https://github.com/octocat/Hello-World/pull/123",
        )
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#pull-123"),
            "https://github.com/octocat/Hello-World/pull/123",
        )
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#issue-123"),
            "https://github.com/octocat/Hello-World/issues/123",
        )
        self.assertEqual(
            resolve_target_url("octocat/Hello-World#issues-123"),
            "https://github.com/octocat/Hello-World/issues/123",
        )
        self.assertEqual(
            resolve_target_url("#pr-123", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("#issue-123", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/issues/123",
        )
        self.assertEqual(
            resolve_target_url("pr-123", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("issue-123", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/issues/123",
        )
        self.assertEqual(
            resolve_target_url("`#pr-123`", repo="my-org/my-repo"),
            "https://github.com/my-org/my-repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("`owner/repo#issue-456`"),
            "https://github.com/owner/repo/issues/456",
        )

        # Substring collision resistance with repo names containing "pr", "pull", or "issue"
        self.assertEqual(
            resolve_target_url("spring-projects/spring-boot#123", agent="issue_triager"),
            "https://github.com/spring-projects/spring-boot/issues/123",
        )
        self.assertEqual(
            resolve_target_url("spring-projects/spring-boot#123", agent="pr_drafter"),
            "https://github.com/spring-projects/spring-boot/issues/123",
        )
        self.assertEqual(
            resolve_target_url("expressjs/express#123", agent="issue_triager"),
            "https://github.com/expressjs/express/issues/123",
        )
        self.assertEqual(
            resolve_target_url("cypress-io/cypress#123", agent="issue_triager"),
            "https://github.com/cypress-io/cypress/issues/123",
        )
        self.assertEqual(
            resolve_target_url("owner/pulley#123", agent="issue_triager"),
            "https://github.com/owner/pulley/issues/123",
        )
        self.assertEqual(
            resolve_target_url("org/enterprise-app#123", agent="issue_triager"),
            "https://github.com/org/enterprise-app/issues/123",
        )
        self.assertEqual(
            resolve_target_url("owner/issue-tracker#123", agent="code_reviewer"),
            "https://github.com/owner/issue-tracker/pull/123",
        )
        self.assertEqual(
            resolve_target_url("owner/issue-tracker#123", agent="code_fixer"),
            "https://github.com/owner/issue-tracker/pull/123",
        )
        # Explicit prefix overrides agent defaults on repos with substrings
        self.assertEqual(
            resolve_target_url("spring-projects/spring-boot PR #123", agent="issue_triager"),
            "https://github.com/spring-projects/spring-boot/pull/123",
        )
        self.assertEqual(
            resolve_target_url("owner/issue-tracker Issue #123", agent="code_reviewer"),
            "https://github.com/owner/issue-tracker/issues/123",
        )

        # Path-style targets (e.g. owner/repo/pull/123, owner/repo/issues/123)
        self.assertEqual(
            resolve_target_url("owner/repo/pull/123"),
            "https://github.com/owner/repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("owner/repo/issues/123"),
            "https://github.com/owner/repo/issues/123",
        )
        self.assertEqual(
            resolve_target_url("spring-projects/spring-boot/pull/123"),
            "https://github.com/spring-projects/spring-boot/pull/123",
        )
        self.assertEqual(
            resolve_target_url("spring-projects/spring-boot/issues/123"),
            "https://github.com/spring-projects/spring-boot/issues/123",
        )
        self.assertEqual(
            resolve_target_url("owner/issue-tracker/pull/456"),
            "https://github.com/owner/issue-tracker/pull/456",
        )

        # Path notation with repo and plural pulls
        self.assertEqual(
            resolve_target_url("owner/repo/pulls/123"),
            "https://github.com/owner/repo/pull/123",
        )
        self.assertEqual(
            resolve_target_url("owner/repo/pull/123"),
            "https://github.com/owner/repo/pull/123",
        )

        # Bare path targets with eff_repo (pull/123, pulls/123, issues/123, pr/123)
        self.assertEqual(
            resolve_target_url("pull/1234", repo="owner/repo"),
            "https://github.com/owner/repo/pull/1234",
        )
        self.assertEqual(
            resolve_target_url("pulls/1234", repo="owner/repo"),
            "https://github.com/owner/repo/pull/1234",
        )
        self.assertEqual(
            resolve_target_url("issues/456", repo="owner/repo"),
            "https://github.com/owner/repo/issues/456",
        )
        self.assertEqual(
            resolve_target_url("issue/456", repo="owner/repo"),
            "https://github.com/owner/repo/issues/456",
        )
        self.assertEqual(
            resolve_target_url("pr/789", repo="owner/repo"),
            "https://github.com/owner/repo/pull/789",
        )
        self.assertEqual(
            resolve_target_url("`pull/1234`", repo="owner/repo"),
            "https://github.com/owner/repo/pull/1234",
        )
        self.assertEqual(
            resolve_target_url("`issues/456`", repo="owner/repo"),
            "https://github.com/owner/repo/issues/456",
        )

        # Invalid repo parameter validation
        self.assertIsNone(resolve_target_url("#42", repo="invalid_no_slash"))
        self.assertIsNone(resolve_target_url("#42", repo="owner/repo/extra"))
        self.assertIsNone(resolve_target_url("42", repo="invalid_no_slash"))
        self.assertIsNone(resolve_target_url("PR #123", repo="invalid_no_slash"))
        self.assertIsNone(resolve_target_url("Issue #123", repo="invalid_no_slash"))
        self.assertIsNone(resolve_target_url("pull/123", repo="invalid_no_slash"))
        self.assertIsNone(resolve_target_url("issues/123", repo="owner/repo/extra"))

        # Unsafe / non-target inputs
        self.assertIsNone(resolve_target_url(None))
        self.assertIsNone(resolve_target_url(""))
        self.assertIsNone(resolve_target_url("   "))
        self.assertIsNone(resolve_target_url("N/A"))
        self.assertIsNone(resolve_target_url("javascript:alert(1)"))
        self.assertIsNone(resolve_target_url("[click](javascript:alert(1))"))
        self.assertIsNone(resolve_target_url("random text without issue"))

    def test_format_target_html_cell(self):
        # Markdown link unwrapping
        self.assertEqual(
            _format_target_html_cell("[#42](https://github.com/owner/repo/pull/42)"),
            '<a href="https://github.com/owner/repo/pull/42" target="_blank" rel="noopener" class="target-link"><code>#42</code></a>',
        )
        self.assertEqual(
            _format_target_html_cell("[`#42`](https://github.com/owner/repo/pull/42)"),
            '<a href="https://github.com/owner/repo/pull/42" target="_blank" rel="noopener" class="target-link"><code>#42</code></a>',
        )
        self.assertEqual(
            _format_target_html_cell("`[#42](https://github.com/owner/repo/pull/42)`"),
            '<a href="https://github.com/owner/repo/pull/42" target="_blank" rel="noopener" class="target-link"><code>#42</code></a>',
        )
        self.assertEqual(
            _format_target_html_cell(" `[#42](https://github.com/owner/repo/pull/42)` "),
            '<a href="https://github.com/owner/repo/pull/42" target="_blank" rel="noopener" class="target-link"><code>#42</code></a>',
        )
        self.assertEqual(
            _format_target_html_cell("`[`#42`](https://github.com/owner/repo/pull/42)`"),
            '<a href="https://github.com/owner/repo/pull/42" target="_blank" rel="noopener" class="target-link"><code>#42</code></a>',
        )
        # Pipe unescaping in HTML target cell labels
        self.assertEqual(
            _format_target_html_cell("feature\\|branch"),
            "<code>feature|branch</code>",
        )
        self.assertEqual(
            _format_target_html_cell("`feature\\|branch`"),
            "<code>feature|branch</code>",
        )
        self.assertEqual(
            _format_target_html_cell("[feature\\|branch](https://github.com/owner/repo/pull/1)"),
            '<a href="https://github.com/owner/repo/pull/1" target="_blank" rel="noopener" class="target-link"><code>feature|branch</code></a>',
        )
        self.assertEqual(
            _format_target_html_cell("`[feature\\|branch](https://github.com/owner/repo/pull/1)`"),
            '<a href="https://github.com/owner/repo/pull/1" target="_blank" rel="noopener" class="target-link"><code>feature|branch</code></a>',
        )
        # Empty / None / fallback cases
        self.assertEqual(_format_target_html_cell(None), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell(""), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("   "), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("N/A"), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("-"), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("None"), "<code>N/A</code>")
        # Backticked placeholders
        self.assertEqual(_format_target_html_cell("`None`"), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("`-`"), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("`N/A`"), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("``"), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("`   `"), "<code>N/A</code>")
        # Empty label guard
        self.assertEqual(_format_target_html_cell("[ ](https://github.com/owner/repo/pull/42)"), "<code>N/A</code>")
        self.assertEqual(_format_target_html_cell("[` `](https://github.com/owner/repo/pull/42)"), "<code>N/A</code>")
        # Standard target string with agent resolution
        self.assertEqual(
            _format_target_html_cell("owner/repo#123", agent="issue_triager"),
            '<a href="https://github.com/owner/repo/issues/123" target="_blank" rel="noopener" class="target-link"><code>owner/repo#123</code></a>',
        )
        # Explicit target_url override
        self.assertEqual(
            _format_target_html_cell("Custom Label", target_url="https://github.com/foo/bar/pull/1"),
            '<a href="https://github.com/foo/bar/pull/1" target="_blank" rel="noopener" class="target-link"><code>Custom Label</code></a>',
        )
        # Unresolvable target string
        self.assertEqual(
            _format_target_html_cell("arbitrary non-target string"),
            "<code>arbitrary non-target string</code>",
        )
        # Newline and carriage return sanitization
        self.assertEqual(
            _format_target_html_cell("feat\r\nbranch"),
            "<code>feat  branch</code>",
        )
        self.assertEqual(
            _format_target_html_cell("[feat\nbranch](https://github.com/owner/repo/pull/1)"),
            '<a href="https://github.com/owner/repo/pull/1" target="_blank" rel="noopener" class="target-link"><code>feat branch</code></a>',
        )

    def test_format_target_markdown_cell_type_safety(self):
        # None and non-string handling
        self.assertEqual(_format_target_markdown_cell(None, None), "`N/A`")
        self.assertEqual(_format_target_markdown_cell(None, "https://github.com/owner/repo/pull/1"), "[`N/A`](https://github.com/owner/repo/pull/1)")
        self.assertEqual(_format_target_markdown_cell("", None), "`N/A`")
        self.assertEqual(_format_target_markdown_cell("   ", None), "`N/A`")
        self.assertEqual(_format_target_markdown_cell("``", None), "`N/A`")
        self.assertEqual(_format_target_markdown_cell(1234, None), "`1234`")
        self.assertEqual(_format_target_markdown_cell(1234, "https://github.com/owner/repo/pull/1234"), "[`1234`](https://github.com/owner/repo/pull/1234)")
        # Escaped pipes
        self.assertEqual(_format_target_markdown_cell("feat | fix", None), "`feat \\| fix`")
        self.assertEqual(_format_target_markdown_cell("feat | fix", "https://github.com/owner/repo/pull/1"), "[`feat \\| fix`](https://github.com/owner/repo/pull/1)")
        # Newline and carriage return sanitization
        self.assertEqual(_format_target_markdown_cell("line1\nline2", None), "`line1 line2`")
        self.assertEqual(_format_target_markdown_cell("line1\r\nline2", None), "`line1  line2`")
        self.assertEqual(_format_target_markdown_cell("line1\rline2", None), "`line1 line2`")
        self.assertEqual(
            _format_target_markdown_cell("line1\nline2", "https://github.com/owner/repo/pull/1"),
            "[`line1 line2`](https://github.com/owner/repo/pull/1)",
        )

    def test_parse_dashboard_markdown_escaped_pipe_in_table_rows(self):
        md = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE**\n\n"
            "## 🚀 Active Container Tasks (1)\n\n"
            "| Task ID | Agent | Target | Elapsed | Status |\n"
            "|:---|:---|:---|:---|:---|\n"
            "| `task-1` | `code_reviewer` | [`feat \\| fix`](https://github.com/owner/repo/pull/1) | 42s | 🔄 Running |\n\n"
            "## ⏳ Queued Tasks (1)\n\n"
            "| Task ID | Agent | Target | Priority | Wait Time |\n"
            "|:---|:---|:---|:---|:---|\n"
            "| `task-2` | `code_fixer` | [`fix \\| patch`](https://github.com/owner/repo/pull/2) | P1 | 5s |\n\n"
            "## 📜 Recent Task Execution History (1)\n\n"
            "| Task ID | Agent | Target | Duration | Status | Details |\n"
            "|:---|:---|:---|:---|:---|:---|\n"
            "| `task-3` | `codebase_auditor` | [`audit \\| check`](https://github.com/owner/repo/pull/3) | 1m 20s | ✅ Completed | Finished |\n"
        )
        parsed = parse_dashboard_markdown(md)

        # Active task checks (no shifted columns)
        self.assertEqual(len(parsed["active_tasks"]), 1)
        act = parsed["active_tasks"][0]
        self.assertEqual(act["id"], "task-1")
        self.assertEqual(act["agent"], "code_reviewer")
        self.assertEqual(act["target"], "feat | fix")
        self.assertEqual(act["target_url"], "https://github.com/owner/repo/pull/1")
        self.assertEqual(act["elapsed"], "42s")
        self.assertEqual(act["status"], "Running")

        # Queued task checks
        self.assertEqual(len(parsed["queued_tasks_list"]), 1)
        q = parsed["queued_tasks_list"][0]
        self.assertEqual(q["id"], "task-2")
        self.assertEqual(q["agent"], "code_fixer")
        self.assertEqual(q["target"], "fix | patch")
        self.assertEqual(q["target_url"], "https://github.com/owner/repo/pull/2")
        self.assertEqual(q["priority"], "P1")
        self.assertEqual(q["wait_time"], "5s")

        # History task checks
        self.assertEqual(len(parsed["history_tasks"]), 1)
        h = parsed["history_tasks"][0]
        self.assertEqual(h["id"], "task-3")
        self.assertEqual(h["agent"], "codebase_auditor")
        self.assertEqual(h["target"], "audit | check")
        self.assertEqual(h["target_url"], "https://github.com/owner/repo/pull/3")
        self.assertEqual(h["duration"], "1m 20s")
        self.assertEqual(h["status"], "Completed")
        self.assertEqual(h["details"], "Finished")

        # Render HTML with escaped pipes
        html_out = render_dashboard_html(md)
        self.assertIn("feat | fix", html_out)
        self.assertIn("fix | patch", html_out)
        self.assertIn("audit | check", html_out)

    def test_parse_dashboard_markdown_multiline_target_sanitization(self):
        # When target contains newlines or carriage returns, _format_target_markdown_cell
        # strips/replaces them, ensuring the generated markdown table row does not split
        # across multiple lines, which allows parse_dashboard_markdown to parse active tasks cleanly.
        multiline_target = "Task prompt with\r\nmultiple\nlines"
        cell = _format_target_markdown_cell(multiline_target, "https://github.com/owner/repo/pull/1")
        self.assertNotIn("\n", cell)
        self.assertNotIn("\r", cell)

        md = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE**\n\n"
            "## 🚀 Active Container Tasks (1)\n\n"
            "| Task ID | Agent | Target | Elapsed | Status |\n"
            "|:---|:---|:---|:---|:---|\n"
            f"| `task-1` | `code_reviewer` | {cell} | 42s | 🔄 Running |\n\n"
        )
        parsed = parse_dashboard_markdown(md)
        self.assertEqual(len(parsed["active_tasks"]), 1)
        self.assertEqual(parsed["active_tasks"][0]["id"], "task-1")
        self.assertEqual(parsed["active_tasks"][0]["agent"], "code_reviewer")
        self.assertEqual(parsed["active_tasks"][0]["target"], "Task prompt with  multiple lines")
        self.assertEqual(parsed["active_tasks"][0]["target_url"], "https://github.com/owner/repo/pull/1")

    def test_table_rendering_target_unwrapping_and_fallback(self):
        active_rendered = _render_active_tasks_table([
            {"id": "t1", "agent": "code_reviewer", "target": "[#42](https://github.com/org/repo/pull/42)", "elapsed": "1s", "status": "RUNNING"},
            {"id": "t2", "agent": "code_reviewer", "target": "", "elapsed": "1s", "status": "RUNNING"},
        ])
        self.assertIn('<a href="https://github.com/org/repo/pull/42" target="_blank" rel="noopener" class="target-link"><code>#42</code></a>', active_rendered)
        self.assertNotIn("<code>[#42]", active_rendered)
        self.assertIn("<code>N/A</code>", active_rendered)

        queued_rendered = _render_queued_tasks_table([
            {"id": "q1", "agent": "issue_triager", "target": "[#99](https://github.com/org/repo/issues/99)", "priority": "1", "wait_time": "5s"},
            {"id": "q2", "agent": "issue_triager", "target": None, "priority": "1", "wait_time": "5s"},
        ])
        self.assertIn('<a href="https://github.com/org/repo/issues/99" target="_blank" rel="noopener" class="target-link"><code>#99</code></a>', queued_rendered)
        self.assertNotIn("<code>[#99]", queued_rendered)
        self.assertIn("<code>N/A</code>", queued_rendered)

        history_rendered = _render_history_tasks_table([
            {"id": "h1", "agent": "code_fixer", "target": "[#55](https://github.com/org/repo/pull/55)", "duration": "10s", "status": "COMPLETED", "details": "Finished"},
            {"id": "h2", "agent": "code_fixer", "target": "N/A", "duration": "10s", "status": "COMPLETED", "details": "Finished"},
            {"id": "h3", "agent": "code_reviewer", "target": "N/A", "duration": "5s", "status": "COMPLETED", "details": "Remote Session"},
        ])
        self.assertIn('<a href="https://github.com/org/repo/pull/55" target="_blank" rel="noopener" class="target-link"><code>#55</code></a>', history_rendered)
        self.assertNotIn("<code>[#55]", history_rendered)
        self.assertIn("<code>N/A</code>", history_rendered)
        self.assertIn('<span class="text-muted">Finished</span>', history_rendered)
        self.assertNotIn("Remote Session", history_rendered)
        self.assertNotIn("error-snippet", history_rendered)

    def test_format_target_markdown_cell(self):
        # Escape pipe characters to preserve table syntax
        self.assertEqual(
            _format_target_markdown_cell("feat | fix", "https://github.com/owner/repo/pull/1"),
            "[`feat \\| fix`](https://github.com/owner/repo/pull/1)",
        )
        self.assertEqual(
            _format_target_markdown_cell("a|b|c", None),
            "`a\\|b\\|c`",
        )
        # Strips existing markdown or backticks cleanly
        self.assertEqual(
            _format_target_markdown_cell("[`#42`](https://github.com/foo/bar)", "https://github.com/foo/bar"),
            "[`#42`](https://github.com/foo/bar)",
        )
        self.assertEqual(
            _format_target_markdown_cell("`#42`", "https://github.com/foo/bar"),
            "[`#42`](https://github.com/foo/bar)",
        )
        self.assertEqual(
            _format_target_markdown_cell("`#42`", None),
            "`#42`",
        )
        # Reject unsafe schemes
        self.assertEqual(
            _format_target_markdown_cell("#42", "javascript:alert(1)"),
            "`#42`",
        )
        self.assertEqual(
            _format_target_markdown_cell("#42", "data:text/html,<script>alert(1)</script>"),
            "`#42`",
        )
        # Delimiter encoding for parentheses
        self.assertEqual(
            _format_target_markdown_cell("#42", "https://github.com/owner/repo/pull/1(subpath)"),
            "[`#42`](https://github.com/owner/repo/pull/1%28subpath%29)",
        )

    def test_parse_dashboard_markdown_clickable_targets(self):
        sample_md = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE** | **Mode**: HEADLESS\n\n"
            "## 🚀 Active Container Tasks\n\n"
            "| Task ID | Agent | Target | Elapsed | Status |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-1` | `code_reviewer` | [`#42`](https://github.com/owner/repo/pull/42) | 12s | 🔄 RUNNING |\n\n"
            "## ⏳ Queued Tasks\n\n"
            "| Task ID | Agent | Target | Priority | Queued Duration |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-2` | `issue_triager` | [`#43`](https://github.com/owner/repo/issues/43) | 1 | 30s |\n\n"
            "## 📜 Recent Task Execution History\n\n"
            "| Task ID | Agent | Target | Duration | Status | Details |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-3` | `pr_drafter` | [`#44`](https://github.com/owner/repo/pull/44) | 45s | ✅ COMPLETED | Finished |\n"
        )
        parsed = parse_dashboard_markdown(sample_md)
        self.assertEqual(len(parsed["active_tasks"]), 1)
        self.assertEqual(parsed["active_tasks"][0]["id"], "task-1")
        self.assertEqual(parsed["active_tasks"][0]["target"], "#42")
        self.assertEqual(parsed["active_tasks"][0]["target_url"], "https://github.com/owner/repo/pull/42")

        self.assertEqual(len(parsed["queued_tasks_list"]), 1)
        self.assertEqual(parsed["queued_tasks_list"][0]["id"], "task-2")
        self.assertEqual(parsed["queued_tasks_list"][0]["target"], "#43")
        self.assertEqual(parsed["queued_tasks_list"][0]["target_url"], "https://github.com/owner/repo/issues/43")

        self.assertEqual(len(parsed["history_tasks"]), 1)
        self.assertEqual(parsed["history_tasks"][0]["id"], "task-3")
        self.assertEqual(parsed["history_tasks"][0]["target"], "#44")
        self.assertEqual(parsed["history_tasks"][0]["target_url"], "https://github.com/owner/repo/pull/44")
        self.assertEqual(parsed["history_tasks"][0]["details"], "Finished")

    def test_parse_dashboard_markdown_5_column_history(self):
        sample_md = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE** | **Mode**: HEADLESS\n\n"
            "## 📜 Recent Task Execution History\n\n"
            "| Task ID | Agent | Target | Duration | Status |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-comp` | `pr_drafter` | [`#44`](https://github.com/owner/repo/pull/44) | 45s | ✅ COMPLETED |\n"
            "| `task-fail` | `code_reviewer` | `#45` | 10s | ❌ FAILED |\n"
        )
        parsed = parse_dashboard_markdown(sample_md)
        self.assertEqual(len(parsed["history_tasks"]), 2)
        self.assertEqual(parsed["history_tasks"][0]["id"], "task-comp")
        self.assertEqual(parsed["history_tasks"][0]["status"], "COMPLETED")
        self.assertEqual(parsed["history_tasks"][0]["details"], "Finished")
        self.assertEqual(parsed["history_tasks"][1]["id"], "task-fail")
        self.assertEqual(parsed["history_tasks"][1]["status"], "FAILED")
        self.assertEqual(parsed["history_tasks"][1]["details"], "Finished")

        # Verify rendered HTML does not render the status string as an error-snippet under details
        rendered_html = _render_history_tasks_table(parsed["history_tasks"])
        self.assertIn('<span class="text-muted">Finished</span>', rendered_html)
        self.assertNotIn('class="error-snippet"', rendered_html)
        self.assertNotIn('title="COMPLETED"', rendered_html)

    def test_detect_git_repo_full_name(self):
        # 1. Test GITHUB_REPOSITORY environment variable
        _reset_detected_repo_cache()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "env-owner/env-repo"}):
            self.assertEqual(_detect_git_repo_full_name(), "env-owner/env-repo")
            # Caching check: even if env changes, cached value is retained until reset
            with patch.dict(os.environ, {"GITHUB_REPOSITORY": "other/repo"}):
                self.assertEqual(_detect_git_repo_full_name(), "env-owner/env-repo")

        # 1b. Test invalid GITHUB_REPOSITORY environment variable is ignored/rejected
        _reset_detected_repo_cache()
        mock_proc_git = MagicMock()
        mock_proc_git.returncode = 0
        mock_proc_git.stdout = "https://github.com/valid-owner/valid-repo.git\n"
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "invalid;repo/injection\n"}):
            with patch("subprocess.run", return_value=mock_proc_git):
                self.assertEqual(_detect_git_repo_full_name(), "valid-owner/valid-repo")

        # 2. Reset cache and test git remote origin URL (HTTPS) with cwd=str(REPO_ROOT)
        _reset_detected_repo_cache()
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/git-owner/git-repo.git\n"
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run", return_value=mock_proc) as mock_subproc:
            self.assertEqual(_detect_git_repo_full_name(), "git-owner/git-repo")
            mock_subproc.assert_called_once_with(
                ["git", "config", "--get", "remote.origin.url"],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=1,
            )

        # 3. Test git remote origin URL (SSH)
        _reset_detected_repo_cache()
        mock_proc.stdout = "git@github.com:ssh-owner/ssh-repo.git\n"
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run", return_value=mock_proc):
            self.assertEqual(_detect_git_repo_full_name(), "ssh-owner/ssh-repo")

        # 4. Test git failure / no remote
        _reset_detected_repo_cache()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run", return_value=mock_proc):
            self.assertIsNone(_detect_git_repo_full_name())

        # 5. Test trailing slash git remote URLs
        _reset_detected_repo_cache()
        mock_proc.returncode = 0
        mock_proc.stdout = "https://github.com/trailing-owner/trailing-repo/\n"
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run", return_value=mock_proc):
            self.assertEqual(_detect_git_repo_full_name(), "trailing-owner/trailing-repo")

        _reset_detected_repo_cache()
        mock_proc.stdout = "https://github.com/trailing-owner/trailing-repo.git/\n"
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run", return_value=mock_proc):
            self.assertEqual(_detect_git_repo_full_name(), "trailing-owner/trailing-repo")

        _reset_detected_repo_cache()
        mock_proc.stdout = "git@github.com:ssh-trailing/ssh-repo/\n"
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run", return_value=mock_proc):
            self.assertEqual(_detect_git_repo_full_name(), "ssh-trailing/ssh-repo")

        _reset_detected_repo_cache()
        mock_proc.stdout = "git@github.com:ssh-trailing/ssh-repo.git/\n"
        with patch.dict(os.environ, {}, clear=True), patch("subprocess.run", return_value=mock_proc):
            self.assertEqual(_detect_git_repo_full_name(), "ssh-trailing/ssh-repo")

        # Cleanup cache after test
        _reset_detected_repo_cache()

    def test_render_dashboard_html_default_repo_handling(self):
        # When repo cannot be detected, defaultRepo in JS template is empty string
        _reset_detected_repo_cache()
        with patch("lib.dashboard._detect_git_repo_full_name", return_value=None):
            html_out = render_dashboard_html("# 🌌 Graviton Live Dashboard\n\n**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE**\n")
            self.assertIn('const defaultRepo = "";', html_out)
            self.assertNotIn('const defaultRepo = "None";', html_out)

        # When repo is detected, defaultRepo is populated
        _reset_detected_repo_cache()
        with patch("lib.dashboard._detect_git_repo_full_name", return_value="my-org/my-repo"):
            html_out = render_dashboard_html("# 🌌 Graviton Live Dashboard\n\n**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE**\n")
            self.assertIn('const defaultRepo = "my-org/my-repo";', html_out)

        # When repo detection returns invalid/malicious string, it is sanitized to empty string
        _reset_detected_repo_cache()
        with patch("lib.dashboard._detect_git_repo_full_name", return_value='"; alert("xss");//'):
            html_out = render_dashboard_html("# 🌌 Graviton Live Dashboard\n\n**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE**\n")
            self.assertIn('const defaultRepo = "";', html_out)
            self.assertNotIn('alert("xss")', html_out)

        _reset_detected_repo_cache()


class TestDashboardUpdater(unittest.TestCase):
    """Test DashboardUpdater file registration and auto-writing loop."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _wait_for_condition(condition, timeout=3.0, interval=0.01):
        start = time.time()
        while time.time() - start < timeout:
            try:
                if condition():
                    return True
            except Exception:
                pass
            time.sleep(interval)
        try:
            return bool(condition())
        except Exception:
            return False

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
        self.assertTrue(
            self._wait_for_condition(
                lambda: target.exists() and target.stat().st_size > 0,
                timeout=3.0,
            ),
            "Target file was not created by background loop",
        )

        initial_update_ts = updater._last_update_ts
        updater.trigger_update()

        self.assertTrue(
            self._wait_for_condition(
                lambda: updater._last_update_ts > initial_update_ts,
                timeout=3.0,
            ),
            "Triggered update did not execute in background loop",
        )

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
        self.assertIn("parts[0] !== 'PR #' && !parts[0].startsWith('PR #')", html_out)
        self.assertNotIn("!line.includes('PR #')", html_out)

    def test_client_js_section_header_parsing_avoids_history_misattribution(self):
        html_out = render_dashboard_html("# Dashboard")
        self.assertIn("const firstLine = sec.split('\\n')[0].trim();", html_out)
        self.assertIn("if (firstLine.includes('Active Container Tasks')) activeSec = sec;", html_out)
        self.assertIn("else if (firstLine.includes('Queued Tasks')) queuedSec = sec;", html_out)
        self.assertIn("else if (firstLine.includes('Approved Pull Requests') || firstLine.includes('Ready to Merge')) approvedSec = sec;", html_out)
        self.assertIn("else if (firstLine.includes('Recent Task Execution History')) historySec = sec;", html_out)
        self.assertNotIn("sec.includes('Approved Pull Requests')", html_out)

        # Verify that task execution history items mentioning "Approved Pull Requests"
        # are not misattributed as approved PRs
        md = (
            "# Dashboard\n\n"
            "## 📜 Recent Task Execution History\n\n"
            "| Task ID | Agent | Target | Duration | Status | Summary |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-101` | `code_reviewer` | `PR #383` | 1m 20s | ✅ Success | Support Approved Pull Requests section |\n"
        )
        parsed = parse_dashboard_markdown(md)
        self.assertEqual(len(parsed.get("approved_prs", [])), 0)
        self.assertEqual(len(parsed.get("history_tasks", [])), 1)
        self.assertEqual(parsed["history_tasks"][0]["id"], "task-101")
        self.assertIn("Approved Pull Requests", parsed["history_tasks"][0]["details"])

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
        self.assertIn("const rawDetail = detailRaw.replace(/`/g, '').trim();", template)
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
        self.assertIn("rawStatus.toLowerCase().includes('finish')", template)

    def test_template_js_client_side_update_regexes(self):
        template = _get_dashboard_template()
        # Verify status and updated match regexes have valid markdown escaping (not double backslashes)
        self.assertIn(r"md.match(/\*\*Status\*\*:\s*([^\n|&]+)/)", template)
        self.assertIn(r"md.match(/\*Last updated:\s*([^(]+)/)", template)
        self.assertIn(r"md.match(/\|\s*\*\*Active Workers\*\*\s*\|\s*`?(\d+)\s*\/\s*(\d+)`?/)", template)
        # Verify RegExp string constructor escaping is for literal pipe and asterisks
        self.assertIn(r'new RegExp("\\|\\s*\\*\\*" + label + "\\*\\*\\s*\\|\\s*`?(\\d+)`?")', template)
        self.assertIn(r'new RegExp("\\|\\s*\\*\\*" + esc + "\\*\\*\\s*\\|\\s*`?([^`|\\n]+)`?")', template)
        # Verify markdown link regex does not look for literal backslashes
        self.assertIn(r"rawStr.match(/\[(.*?)\]\((.*?)\)/)", template)

    def test_client_side_regexes_evaluate_generated_dashboard_markdown(self):
        """
        End-to-end verification that the client-side JavaScript regexes in dashboard.html
        correctly match and parse markdown produced by format_dashboard_markdown().
        """
        mock_tm = MagicMock()
        mock_tm._draining = False
        mock_tm._paused = False
        mock_tm.get_stats.return_value = {
            "active_workers": 2,
            "max_workers": 4,
            "active_tasks": 1,
            "queued_tasks": 3,
            "completed_tasks": 10,
            "failed_tasks": 1,
        }

        # Active task with remote control url
        t1 = MagicMock()
        t1.id = "task-001"
        t1.agent = "code_reviewer"
        t1.target_id = "PR #100"
        t1.start_time = time.time() - 30
        t1.status = "RUNNING"
        t1.remote_control_url = "https://antigravity.google.com/session/task-001"
        t1.repo_full_name = "owner/repo"
        mock_tm.get_active_tasks.return_value = [t1]
        mock_tm.get_queued_tasks.return_value = []

        # History task with remote control url
        h1 = MagicMock()
        h1.id = "task-000"
        h1.agent = "code_fixer"
        h1.target_id = "PR #99"
        h1.start_time = time.time() - 100
        h1.finish_time = time.time() - 40
        h1.status = TaskStatus.COMPLETED
        h1.remote_control_url = "https://antigravity.google.com/session/task-000"
        h1.error_message = None
        h1.repo_full_name = "owner/repo"
        mock_tm.get_task_history.return_value = [h1]

        mock_qt = MagicMock()
        mock_qt.get_info.return_value.to_dict.return_value = {
            "quota_pool": "gemini",
            "selected_model": "gemini-3.1-pro-high",
            "active_gemini_model": "gemini-3.1-pro-high",
            "active_third_party_model": "claude-3-5-sonnet",
            "gemini_remaining_percentage": 85,
            "third_party_remaining_percentage": 92,
        }
        mock_qt.quota_pool = "gemini"
        mock_qt.get_active_model.side_effect = lambda pool=None: "gemini-3.1-pro-high" if pool == "gemini" else "claude-3-5-sonnet"
        mock_qt.get_pool_remaining_percentage.side_effect = lambda pool=None: 85 if pool == "gemini" else 92

        md = format_dashboard_markdown(
            task_manager=mock_tm,
            quota_tracker=mock_qt,
            host="127.0.0.1",
            port=8000,
        )

        # 1. Status & Updated
        status_match = re.search(r"\*\*Status\*\*:\s*([^\n|&]+)", md)
        self.assertIsNotNone(status_match)
        self.assertIn("BUSY", status_match.group(1))

        updated_match = re.search(r"\*Last updated:\s*([^(]+)", md)
        self.assertIsNotNone(updated_match)
        self.assertIn("UTC", updated_match.group(1))

        # 2. KPI Metrics - Active Workers
        workers_match = re.search(r"\|\s*\*\*Active Workers\*\*\s*\|\s*`?(\d+)\s*\/\s*(\d+)`?", md)
        self.assertIsNotNone(workers_match)
        self.assertEqual(int(workers_match.group(1)), 2)
        self.assertEqual(int(workers_match.group(2)), 4)

        # 2. KPI Metrics - extractInt equivalent
        def extract_int(label):
            pattern = r"\|\s*\*\*" + re.escape(label) + r"\*\*\s*\|\s*`?(\d+)`?"
            m = re.search(pattern, md)
            return int(m.group(1)) if m else 0

        self.assertEqual(extract_int("Running Tasks"), 1)
        self.assertEqual(extract_int("Queued Tasks"), 3)
        self.assertEqual(extract_int("Completed Tasks"), 10)
        self.assertEqual(extract_int("Failed Tasks"), 1)

        # 3. Quota & Model - extractStr equivalent
        def extract_str(label, def_val):
            pattern = r"\|\s*\*\*" + re.escape(label) + r"\*\*\s*\|\s*`?([^`|\n]+)`?"
            m = re.search(pattern, md)
            return m.group(1).strip() if m else def_val

        self.assertEqual(extract_str("Active Pool", "default"), "gemini")
        self.assertEqual(extract_str("Active Model", "default"), "gemini-3.1-pro-high")
        self.assertEqual(extract_str("Active Gemini Model", ""), "gemini-3.1-pro-high")
        self.assertEqual(extract_str("Active Third-Party Model", ""), "claude-3-5-sonnet")
        self.assertEqual(extract_str("Gemini Remaining", "N/A"), "85%")
        self.assertEqual(extract_str("Third-Party Remaining", "N/A"), "92%")

        # 4. Table Markdown Links (active tasks & history tasks)
        active_lines = [l for l in md.splitlines() if "task-001" in l]
        self.assertEqual(len(active_lines), 1)
        active_cells = [c.strip() for c in active_lines[0].split("|")[1:-1]]
        self.assertEqual(active_cells[0], "`task-001`")
        self.assertEqual(active_cells[1], "`code_reviewer`")
        self.assertEqual(active_cells[2], "-")
        active_link_match = re.search(r"\[(.*?)\]\((.*?)\)", active_cells[3])
        self.assertIsNotNone(active_link_match)
        self.assertEqual(active_link_match.group(1), "`PR #100`")
        self.assertEqual(active_link_match.group(2), "https://github.com/owner/repo/pull/100")
        self.assertEqual(len(active_cells), 6)

        history_lines = [l for l in md.splitlines() if "task-000" in l]
        self.assertEqual(len(history_lines), 1)
        history_cells = [c.strip() for c in history_lines[0].split("|")[1:-1]]
        self.assertEqual(history_cells[0], "`task-000`")
        self.assertEqual(history_cells[1], "`code_fixer`")
        self.assertEqual(history_cells[2], "-")
        history_link_match = re.search(r"\[(.*?)\]\((.*?)\)", history_cells[3])
        self.assertIsNotNone(history_link_match)
        self.assertEqual(history_link_match.group(1), "`PR #99`")
        self.assertEqual(history_link_match.group(2), "https://github.com/owner/repo/pull/99")
        self.assertEqual(history_cells[6], "Finished")

        # 5. ONLINE status verification when no active tasks
        mock_tm.get_active_tasks.return_value = []
        md_online = format_dashboard_markdown(
            task_manager=mock_tm,
            quota_tracker=mock_qt,
            host="127.0.0.1",
            port=8000,
        )
        status_match_online = re.search(r"\*\*Status\*\*:\s*([^\n|&]+)", md_online)
        self.assertIsNotNone(status_match_online)
        self.assertIn("ONLINE", status_match_online.group(1))

        # 6. DRAINING and PAUSED status verification
        mock_tm._draining = True
        md_draining = format_dashboard_markdown(
            task_manager=mock_tm,
            quota_tracker=mock_qt,
            host="127.0.0.1",
            port=8000,
        )
        status_match_draining = re.search(r"\*\*Status\*\*:\s*([^\n|&]+)", md_draining)
        self.assertIsNotNone(status_match_draining)
        self.assertIn("DRAINING", status_match_draining.group(1))

        mock_tm._draining = False
        mock_tm._paused = True
        md_paused = format_dashboard_markdown(
            task_manager=mock_tm,
            quota_tracker=mock_qt,
            host="127.0.0.1",
            port=8000,
        )
        status_match_paused = re.search(r"\*\*Status\*\*:\s*([^\n|&]+)", md_paused)
        self.assertIsNotNone(status_match_paused)
        self.assertIn("PAUSED", status_match_paused.group(1))

    def test_format_dashboard_markdown_with_model_column(self):
        """Verify format_dashboard_markdown includes Model column in active and history tables."""
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 1,
            "max_workers": 2,
            "active_tasks": 1,
            "queued_tasks": 0,
            "completed_tasks": 2,
            "failed_tasks": 0,
        }

        active_task = Task(
            id="task-act",
            agent="code_reviewer",
            prompt="Review PR #50",
            target_id="#50",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 25.0,
            selected_model="gemini-3.8-flash-medium",
        )
        history_task1 = Task(
            id="task-hist1",
            agent="code_fixer",
            prompt="Fix bug",
            target_id="#51",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 80.0,
            finish_time=time.time() - 20.0,
            selected_model="claude-3-5-sonnet",
        )
        history_task2 = Task(
            id="task-hist2",
            agent="pr_drafter",
            prompt="Draft PR",
            target_id="#52",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 50.0,
            finish_time=time.time() - 10.0,
            selected_model=None,
        )

        mock_tm.get_active_tasks.return_value = [active_task]
        mock_tm.get_queued_tasks.return_value = []
        mock_tm.get_task_history.return_value = [history_task1, history_task2]

        md = format_dashboard_markdown(task_manager=mock_tm)

        # Check Active Tasks table headers and rows
        self.assertIn("| Task ID | Agent | Model | Target | Elapsed | Status |", md)
        self.assertIn("| `task-act` | `code_reviewer` | `gemini-3.8-flash-medium` | [`#50`](", md)

        # Check History table headers and rows
        self.assertIn("| Task ID | Agent | Model | Target | Duration | Status | Details |", md)
        self.assertIn("| `task-hist1` | `code_fixer` | `claude-3-5-sonnet` | [`#51`](", md)
        self.assertIn("| `task-hist2` | `pr_drafter` | - | [`#52`](", md)

    def test_parse_dashboard_markdown_with_and_without_model_column(self):
        """Verify parse_dashboard_markdown handles both 7-column (with Model) and 6-column (legacy) tables."""
        # 7-column markdown
        md_7col = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE** | **Mode**: HEADLESS\n\n"
            "## 🚀 Active Container Tasks (1)\n\n"
            "| Task ID | Agent | Model | Target | Elapsed | Status | Remote Control |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-1` | `code_reviewer` | `gemini-3.8-flash-medium` | `#10` | 15s | 🔄 RUNNING | [Remote Control 🌐](https://example.com/rc1) |\n\n"
            "## 📜 Recent Task Execution History (1)\n\n"
            "| Task ID | Agent | Model | Target | Duration | Status | Details |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-2` | `code_fixer` | `claude-3-5-sonnet` | `#11` | 42s | ✅ COMPLETED | Finished |\n"
        )
        parsed_7 = parse_dashboard_markdown(md_7col)
        self.assertEqual(len(parsed_7["active_tasks"]), 1)
        self.assertEqual(parsed_7["active_tasks"][0]["id"], "task-1")
        self.assertEqual(parsed_7["active_tasks"][0]["agent"], "code_reviewer")
        self.assertEqual(parsed_7["active_tasks"][0]["model"], "gemini-3.8-flash-medium")
        self.assertEqual(parsed_7["active_tasks"][0]["target"], "#10")
        self.assertEqual(parsed_7["active_tasks"][0]["remote_control_url"], "https://example.com/rc1")

        self.assertEqual(len(parsed_7["history_tasks"]), 1)
        self.assertEqual(parsed_7["history_tasks"][0]["id"], "task-2")
        self.assertEqual(parsed_7["history_tasks"][0]["agent"], "code_fixer")
        self.assertEqual(parsed_7["history_tasks"][0]["model"], "claude-3-5-sonnet")
        self.assertEqual(parsed_7["history_tasks"][0]["target"], "#11")

        # 6-column markdown (legacy backwards compatibility)
        md_6col = (
            "# 🌌 Graviton Live Dashboard\n\n"
            "**Server**: `localhost:8000` | **Status**: 🟢 **ONLINE** | **Mode**: HEADLESS\n\n"
            "## 🚀 Active Container Tasks (1)\n\n"
            "| Task ID | Agent | Target | Elapsed | Status | Remote Control |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-3` | `code_reviewer` | `#12` | 10s | 🔄 RUNNING | [Remote Control 🌐](https://example.com/rc2) |\n\n"
            "## 📜 Recent Task Execution History (1)\n\n"
            "| Task ID | Agent | Target | Duration | Status | Details |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- |\n"
            "| `task-4` | `code_fixer` | `#13` | 30s | ✅ COMPLETED | Finished |\n"
        )
        parsed_6 = parse_dashboard_markdown(md_6col)
        self.assertEqual(len(parsed_6["active_tasks"]), 1)
        self.assertEqual(parsed_6["active_tasks"][0]["id"], "task-3")
        self.assertEqual(parsed_6["active_tasks"][0]["model"], "-")
        self.assertEqual(parsed_6["active_tasks"][0]["target"], "#12")

        self.assertEqual(len(parsed_6["history_tasks"]), 1)
        self.assertEqual(parsed_6["history_tasks"][0]["id"], "task-4")
        self.assertEqual(parsed_6["history_tasks"][0]["model"], "-")
        self.assertEqual(parsed_6["history_tasks"][0]["target"], "#13")

    def test_render_html_tables_with_model_column(self):
        """Verify _render_active_tasks_table and _render_history_tasks_table render Model column."""
        from lib.dashboard import _render_active_tasks_table, _render_history_tasks_table

        active_data = [
            {
                "id": "task-act-1",
                "agent": "code_reviewer",
                "model": "gemini-3.8-flash-medium",
                "target": "#101",
                "elapsed": "20s",
                "status": "RUNNING",
                "remote_control_url": "https://example.com/rc",
            },
            {
                "id": "task-act-2",
                "agent": "code_fixer",
                "model": "-",
                "target": "#102",
                "elapsed": "5s",
                "status": "RUNNING",
                "remote_control_url": None,
            },
        ]
        active_html = _render_active_tasks_table(active_data)
        self.assertIn("<th>Model</th>", active_html)
        self.assertIn("<code>gemini-3.8-flash-medium</code>", active_html)
        self.assertIn('<span class="text-muted">-</span>', active_html)

        history_data = [
            {
                "id": "task-hist-1",
                "agent": "code_fixer",
                "model": "claude-3-5-sonnet",
                "target": "#103",
                "duration": "1m",
                "status": "COMPLETED",
                "details": "Finished",
                "remote_control_url": None,
            }
        ]
        history_html = _render_history_tasks_table(history_data)
        self.assertIn("<th>Model</th>", history_html)
        self.assertIn("<code>claude-3-5-sonnet</code>", history_html)

    def test_client_js_renders_model_column_header(self):
        """Verify render_dashboard_html client JS contains Model column header in active and history tables."""
        html_out = render_dashboard_html("# Test Dashboard")
        self.assertIn("<th>Task ID</th><th>Agent</th><th>Model</th><th>Target</th><th>Elapsed</th><th>Status</th>", html_out)
        self.assertIn("<th>Task ID</th><th>Agent</th><th>Model</th><th>Target</th><th>Duration</th><th>Status</th><th>Details</th>", html_out)

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

    def test_render_dashboard_html_na_submeters_render_zero_width_and_neutral_color(self):
        """Verifies explicit N/A window submeters render with style="width: 0%; background-color: #58a6ff;" instead of overall pool bar width."""
        extra = {
            "quota_info": {
                "quota_pool": "gemini",
                "remaining_percentage": 85.0,
                "gemini_remaining_percentage": 85.0,
                "gemini_5h_remaining_percentage": None,
                "gemini_5h_reset_time": None,
                "gemini_5h_countdown": None,
                "gemini_5h_pacing_status": "OK",
                "gemini_1w_remaining_percentage": None,
                "gemini_1w_reset_time": None,
                "gemini_1w_countdown": None,
                "gemini_1w_pacing_status": "OK",
                "third_party_remaining_percentage": 75.0,
                "third_party_5h_remaining_percentage": None,
                "third_party_5h_reset_time": None,
                "third_party_5h_countdown": None,
                "third_party_5h_pacing_status": "OK",
                "third_party_1w_remaining_percentage": None,
                "third_party_1w_reset_time": None,
                "third_party_1w_countdown": None,
                "third_party_1w_pacing_status": "OK",
            }
        }
        md = """# 🌌 Graviton Live Dashboard
## System Status & Health
| Metric | Value | Notes |
| :--- | :--- | :--- |
| **Active Pool** | `gemini` | Configured quota bucket |
| **Active Model** | `gemini-3.6-flash-high` | Active Gemini / LLM persona |
| **Active Gemini Model** | `gemini-3.6-flash-high` | Active Gemini model persona |
| **Active Third-Party Model** | `claude-sonnet-4-6` | Active Third-Party model persona |
| **Gemini (5H)** | `N/A` | N/A |
| **Gemini (1W)** | `N/A` | N/A |
| **Third-Party (5H)** | `N/A` | N/A |
| **Third-Party (1W)** | `N/A` | N/A |
| **Gemini Remaining** | `85.0%` | Live Gemini API capacity |
| **Third-Party Remaining** | `75.0%` | Fallback model capacity |
"""
        html_out = render_dashboard_html(md, quota_tracker=None, extra_info=extra)
        self.assertIn('id="gemini-5h-bar" class="meter-fill" style="width: 0%; background-color: #58a6ff;"', html_out)
        self.assertIn('id="gemini-1w-bar" class="meter-fill" style="width: 0%; background-color: #58a6ff;"', html_out)
        self.assertIn('id="tp-5h-bar" class="meter-fill" style="width: 0%; background-color: #58a6ff;"', html_out)
        self.assertIn('id="tp-1w-bar" class="meter-fill" style="width: 0%; background-color: #58a6ff;"', html_out)

    def test_format_dashboard_markdown_quota_window_object_none_metrics_no_phantom_details(self):
        """Verifies QuotaWindow objects with None metrics do not emit phantom Reset: N/A | Pacing: OK details."""
        tracker = QuotaTracker()
        tracker.gemini_window_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=None)
        tracker.gemini_window_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=None)
        tracker.claude_window_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=None)
        tracker.claude_window_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=None)
        md_out = format_dashboard_markdown(quota_tracker=tracker)
        self.assertNotIn("Reset: N/A | Pacing: OK", md_out)
        self.assertIn("| **Gemini (5H)** | `N/A` | N/A |", md_out)
        self.assertIn("| **Gemini (1W)** | `N/A` | N/A |", md_out)
        self.assertIn("| **Third-Party (5H)** | `N/A` | N/A |", md_out)
        self.assertIn("| **Third-Party (1W)** | `N/A` | N/A |", md_out)

    def test_format_dashboard_markdown_and_html_none_remaining_percentage_renders_na_not_none_percent(self):
        """Verifies that QuotaInfo(remaining_percentage=None) renders N/A instead of None% in markdown and HTML across both pools."""
        for pool in ("gemini", "claude"):
            info = QuotaInfo(remaining_percentage=None, quota_pool=pool)
            extra = {"quota_info": info.to_dict()}
            md_out = format_dashboard_markdown(extra_info=extra)
            self.assertNotIn("None%", md_out)
            self.assertIn("| **Gemini Remaining** | `N/A` | Live Gemini API capacity |", md_out)
            self.assertIn("| **Third-Party Remaining** | `N/A` | Fallback model capacity |", md_out)

            html_out = render_dashboard_html(md_out, quota_tracker=None, extra_info=extra)
            self.assertNotIn("None%", html_out)
            self.assertNotIn("none%", html_out.lower())
            self.assertIn('id="gemini-pct-label" style="display: none; color: #58a6ff;">N/A</span>', html_out)
            self.assertIn('id="tp-pct-label" style="display: none; color: #58a6ff;">N/A</span>', html_out)

    def test_render_dashboard_html_quota_tracker_none_window_metrics_no_phantom_details(self):
        """Verifies that render_dashboard_html with QuotaTracker containing None window metrics avoids emitting Reset: N/A | Pacing: OK details."""
        tracker = QuotaTracker()
        tracker.gemini_window_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=None)
        tracker.gemini_window_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=None)
        tracker.claude_window_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=None)
        tracker.claude_window_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=None)

        # Case 1: with empty markdown content
        html_out_empty = render_dashboard_html("", quota_tracker=tracker)
        self.assertNotIn("Reset: N/A | Pacing: OK", html_out_empty)
        self.assertIn('<div class="meter-sub" id="gemini-5h-details">Live Gemini burst quota</div>', html_out_empty)
        self.assertIn('<div class="meter-sub" id="gemini-1w-details">Live Gemini weekly quota</div>', html_out_empty)
        self.assertIn('<div class="meter-sub" id="tp-5h-details">Fallback burst quota</div>', html_out_empty)
        self.assertIn('<div class="meter-sub" id="tp-1w-details">Fallback weekly quota</div>', html_out_empty)

        # Case 2: with markdown generated from the same uninitialized quota tracker
        md_out = format_dashboard_markdown(quota_tracker=tracker)
        html_out = render_dashboard_html(md_out, quota_tracker=tracker)
        self.assertNotIn("Reset: N/A | Pacing: OK", html_out)

    def test_format_dashboard_markdown_quota_tracker_inactive_pool_none_windows_renders_na_not_leaked_percentage(self):
        """Verifies that format_dashboard_markdown with QuotaTracker does not leak active pool percentage into inactive pool."""
        # Active pool is gemini at 42.0%, but Claude windows are uninitialized / None
        tracker = QuotaTracker(quota_pool="gemini", remaining_percentage=42.0)
        tracker.claude_window_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=None)
        tracker.claude_window_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=None)

        md_out = format_dashboard_markdown(quota_tracker=tracker)
        self.assertIn("| **Gemini Remaining** | `42.0%` |", md_out)
        self.assertIn("| **Third-Party Remaining** | `N/A` |", md_out)
        self.assertNotIn("| **Third-Party Remaining** | `42", md_out)

        html_out = render_dashboard_html(md_out, quota_tracker=tracker)
        self.assertIn('id="tp-pct-label" style="display: none; color: #58a6ff;">N/A</span>', html_out)

    def test_template_js_escaped_pipe_and_target_resolution(self):
        template = _get_dashboard_template()
        # Escaped pipe splitting
        self.assertIn("line.split(/(?<!\\\\)\\|/).slice(1, -1)", template)
        self.assertIn("c.trim().replace(/\\\\\\|/g, '|')", template)
        # Target resolution regexes
        self.assertIn("(pr|pulls?|issues?)", template)
        self.assertIn("repoPathMatch", template)
        self.assertIn("repoDelimMatch", template)
        # Backtick placeholder stripping
        self.assertIn("rawStr.replace(/`/g, '').trim()", template)

    def test_template_js_section_and_line_splitting_and_target_unescaping(self):
        template = _get_dashboard_template()
        # Section header splitting uses /^##\s+/m (not faulty /^##\\s+/m)
        self.assertIn(r"md.split(/^##\s+/m)", template)
        self.assertNotIn(r"md.split(/^##\\s+/m)", template)
        # Line splitting uses /\r?\n/ (not faulty split('\\n'))
        self.assertIn(r"sec.split(/\r?\n/).map", template)
        self.assertNotIn(r"sec.split('\\n')", template)
        # Target cell pipe and newline unescaping/sanitization in client-side formatTargetCell
        self.assertIn(r"label = label.replace(/\\\|/g, '|').replace(/[\r\n]+/g, ' ');", template)

    def test_template_js_model_column_row_length_guards(self):
        template = _get_dashboard_template()
        # Active tasks 5-column backwards compatibility guard
        self.assertIn("r.length >= 6 && (activeSec.includes('| Model |')", template)
        self.assertIn("const statusRaw = (hasModel ? r[5] : r[4]) || '';", template)
        # History tasks 5-column backwards compatibility guard
        self.assertIn("r.length >= 7 || (r.length === 6 && historySec.includes('| Model |'))", template)
        self.assertIn("const rawStatus = (hasModel ? r[5] : r[4]) || '';", template)

    def test_parse_countdown_to_seconds(self):
        self.assertEqual(parse_countdown_to_seconds("02:15:00"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("02h 15m"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("5d 04h"), 446400.0)
        self.assertEqual(parse_countdown_to_seconds("5D 04H"), 446400.0)
        self.assertEqual(parse_countdown_to_seconds("02H 15M"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("45s"), 45.0)
        self.assertEqual(parse_countdown_to_seconds("45S"), 45.0)
        self.assertEqual(parse_countdown_to_seconds("5d 04h 30s"), 446430.0)
        self.assertEqual(parse_countdown_to_seconds("00:00:00"), 0.0)
        self.assertIsNone(parse_countdown_to_seconds("N/A"))
        self.assertIsNone(parse_countdown_to_seconds(None))
        # Backtick stripping
        self.assertEqual(parse_countdown_to_seconds("`02:15:00`"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("`02h 15m`"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("`5d 04h`"), 446400.0)
        self.assertEqual(parse_countdown_to_seconds("`00:00:00`"), 0.0)
        # Countdowns with descriptive suffixes or words (e.g. est, remaining, approx)
        self.assertEqual(parse_countdown_to_seconds("02:15:00 est"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("02:15:00 remaining"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("02:15:00 approx"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("`02:15:00` est"), 8100.0)
        self.assertEqual(parse_countdown_to_seconds("02:15 est"), 135.0)
        for word in ("paused", "pending", "invalid", "suspended", "resumed", "closed"):
            self.assertIsNone(parse_countdown_to_seconds(word))

    def test_extract_countdown(self):
        # Case insensitivity
        self.assertEqual(_extract_countdown("Reset: 02:30:00 | Pacing: OK"), "02:30:00")
        self.assertEqual(_extract_countdown("reset: 02:30:00 | pacing: ok"), "02:30:00")
        self.assertEqual(_extract_countdown("RESET: 02:30:00 | PACING: OK"), "02:30:00")
        # Backtick stripping
        self.assertEqual(_extract_countdown("Reset: `02:30:00` | Pacing: OK"), "02:30:00")
        self.assertEqual(_extract_countdown("reset: `02:30:00`"), "02:30:00")
        self.assertEqual(_extract_countdown("RESET: `02:30:00`"), "02:30:00")
        # Edge cases
        self.assertIsNone(_extract_countdown(None))
        self.assertIsNone(_extract_countdown(""))
        self.assertIsNone(_extract_countdown("Reset: N/A | Pacing: OK"))
        self.assertIsNone(_extract_countdown("reset: none"))
        self.assertIsNone(_extract_countdown("No reset details"))

    def test_calculate_target_pacing_from_details(self):
        # 2.5 hours remaining in 5-hour window -> 50%
        self.assertEqual(calculate_target_pacing_from_details("Reset: 02:30:00 | Pacing: OK", 18000.0), 50.0)
        # Case insensitivity for reset prefix
        self.assertEqual(calculate_target_pacing_from_details("reset: 02:30:00 | pacing: ok", 18000.0), 50.0)
        self.assertEqual(calculate_target_pacing_from_details("RESET: 02:30:00 | PACING: OK", 18000.0), 50.0)
        # Backtick stripping in reset details
        self.assertEqual(calculate_target_pacing_from_details("Reset: `02:30:00` | Pacing: OK", 18000.0), 50.0)
        self.assertEqual(calculate_target_pacing_from_details("reset: `02:30:00` | pacing: ok", 18000.0), 50.0)
        # Countdowns with descriptive suffixes in reset details
        self.assertEqual(calculate_target_pacing_from_details("Reset: 02:30:00 est | Pacing: OK", 18000.0), 50.0)
        self.assertEqual(calculate_target_pacing_from_details("Reset: `02:30:00` remaining | Pacing: OK", 18000.0), 50.0)
        # N/A reset details
        self.assertIsNone(calculate_target_pacing_from_details("Reset: N/A | Pacing: OK", 18000.0))
        self.assertIsNone(calculate_target_pacing_from_details(None, 18000.0))
        # Non-countdown words in reset details return None
        for word in ("paused", "pending", "invalid", "suspended", "resumed", "closed"):
            self.assertIsNone(calculate_target_pacing_from_details(f"Reset: {word} | Pacing: OK", 18000.0))

    def test_render_dashboard_html_contains_pacing_marks(self):
        _reset_dashboard_template_cache()
        sample_md = """# 🌌 Graviton Live Dashboard

## 🎯 Model Quota & Pacing

| Metric | Value | Details |
| :--- | :--- | :--- |
| **Active Pool** | `gemini` | Configured quota bucket |
| **Active Model** | `gemini-3.8-flash-medium` | Active Gemini / LLM persona |
| **Active Gemini Model** | `gemini-3.8-flash-medium` | Active Gemini model persona |
| **Active Third-Party Model** | `claude-sonnet-4-6` | Active Third-Party model persona |
| **Gemini (5H)** | `85%` | Reset: 02:30:00 | Pacing: OK |
| **Gemini (1W)** | `92%` | Reset: 5d 04h | Pacing: OK |
| **Third-Party (5H)** | `70%` | Reset: 01:15:00 | Pacing: OK |
| **Third-Party (1W)** | `95%` | Reset: 4d 12h | Pacing: OK |
| **Gemini Remaining** | `85%` | Live Gemini API capacity |
| **Third-Party Remaining** | `70%` | Fallback model capacity |
"""
        html_out = render_dashboard_html(sample_md)
        self.assertIn('id="gemini-5h-pacing-mark"', html_out)
        self.assertIn('id="gemini-1w-pacing-mark"', html_out)
        self.assertIn('id="tp-5h-pacing-mark"', html_out)
        self.assertIn('id="tp-1w-pacing-mark"', html_out)
        # Verify calculated left percentage, title, and aria-label
        self.assertIn('id="gemini-5h-pacing-mark" class="pacing-mark" style="left: 50.0%;" title="Target Pacing: 50% (perfect pacing limit)" aria-label="Target Pacing: 50% (perfect pacing limit)"', html_out)

    def test_template_js_pacing_marks_auto_update(self):
        _reset_dashboard_template_cache()
        template = _get_dashboard_template()
        self.assertIn(".pacing-mark", template)
        self.assertIn("updatePacingMark('gemini-5h-pacing-mark'", template)
        self.assertIn("updatePacingMark('gemini-1w-pacing-mark'", template)
        self.assertIn("updatePacingMark('tp-5h-pacing-mark'", template)
        self.assertIn("updatePacingMark('tp-1w-pacing-mark'", template)
        self.assertIn("mark.setAttribute('aria-label', titleText)", template)


if __name__ == "__main__":
    unittest.main()
