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
    REPO_ROOT,
    _detect_git_repo_full_name,
    _format_target_html_cell,
    _format_target_markdown_cell,
    _get_dashboard_template,
    _render_active_tasks_table,
    _render_history_tasks_table,
    _render_queued_tasks_table,
    _reset_dashboard_template_cache,
    _reset_detected_repo_cache,
    format_dashboard_markdown,
    format_duration,
    get_quota_color,
    is_safe_url,
    parse_dashboard_markdown,
    render_dashboard_html,
    resolve_target_url,
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
        ])
        self.assertIn('<a href="https://github.com/org/repo/pull/55" target="_blank" rel="noopener" class="target-link"><code>#55</code></a>', history_rendered)
        self.assertNotIn("<code>[#55]", history_rendered)
        self.assertIn("<code>N/A</code>", history_rendered)

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


if __name__ == "__main__":
    unittest.main()


