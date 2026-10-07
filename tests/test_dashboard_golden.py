#!/usr/bin/env python3
"""
Golden output test asserting 100% byte-identical outputs for dashboard formatting,
parsing, and HTML rendering across refactorings.
"""

import datetime
import unittest
from unittest.mock import MagicMock, patch

from lib.dashboard import (
    format_dashboard_markdown,
    parse_dashboard_markdown,
    render_dashboard_html,
    _reset_dashboard_template_cache,
    _reset_detected_repo_cache,
)
from lib.tasks import Task, TaskStatus


class TestDashboardGoldenOutput(unittest.TestCase):
    def setUp(self):
        super().setUp()
        _reset_dashboard_template_cache()
        _reset_detected_repo_cache()
        self.addCleanup(_reset_dashboard_template_cache)
        self.addCleanup(_reset_detected_repo_cache)

    def _fixed_dt_mock(self):
        mock_dt = MagicMock(wraps=datetime.datetime)
        fixed = datetime.datetime(2026, 10, 7, 12, 0, 0, tzinfo=datetime.timezone.utc)
        mock_dt.now.return_value = fixed
        mock_dt.timezone = datetime.timezone
        return mock_dt

    def test_golden_empty_state(self):
        with patch("time.time", return_value=1700000000.0), \
             patch("datetime.datetime", new=self._fixed_dt_mock()), \
             patch("lib.updater.get_cached_git_info", return_value=("cafe123", "main")), \
             patch("lib.updater.get_hot_reload_state", return_value="IDLE"), \
             patch("lib.dashboard.html._detect_git_repo_full_name", return_value="mweastwood/graviton"):
            md = format_dashboard_markdown(host="127.0.0.1", port=9000)
            self.assertIn("# 🌌 Graviton Live Dashboard", md)
            self.assertIn("*No container tasks currently running.*", md)
            self.assertIn("*Queue is empty.*", md)
            self.assertIn("*(No approved PRs awaiting merge)*", md)
            self.assertIn("*No completed tasks in history yet.*", md)

            parsed = parse_dashboard_markdown(md, default_host="127.0.0.1", default_port=9000)
            self.assertEqual(parsed["host"], "127.0.0.1")
            self.assertEqual(parsed["port"], 9000)
            self.assertEqual(parsed["status_text"], "ONLINE")
            self.assertEqual(parsed["active_tasks"], [])
            self.assertEqual(parsed["queued_tasks_list"], [])
            self.assertEqual(parsed["history_tasks"], [])
            self.assertEqual(parsed["approved_prs"], [])

            html_out = render_dashboard_html(md, host="127.0.0.1", port=9000)
            self.assertIn("No container tasks currently running.", html_out)
            self.assertIn("Queue is empty.", html_out)
            self.assertIn("No approved PRs awaiting merge.", html_out)
            self.assertIn("No completed tasks in history yet.", html_out)

            import hashlib
            self.assertEqual(
                hashlib.sha256(md.encode()).hexdigest(),
                "6371a59825d51a5ae8de43adb6ea37c990711403370e296b121e8da3e15e44ca",
            )
            self.assertEqual(
                hashlib.sha256(html_out.encode()).hexdigest(),
                "948aac13d95f658be2cb87daceb4fbf93f3f738aa8a12f6602d2fdf921ecb9b1",
            )

    def test_golden_populated_state(self):
        # Create mock TaskManager
        mock_tm = MagicMock()
        mock_tm.get_stats.return_value = {
            "active_workers": 2,
            "max_workers": 4,
            "active_tasks": 1,
            "queued_tasks": 1,
            "completed_tasks": 10,
            "failed_tasks": 1,
        }

        t_active = Task(
            id="task-101",
            agent="code_reviewer",
            prompt="Review PR #42",
            target_id="mweastwood/graviton#42",
        )
        t_active.start_time = 1700000000.0 - 120.0
        t_active.selected_model = "gemini-3.8-flash-medium"
        t_active.repo_full_name = "mweastwood/graviton"
        mock_tm.get_active_tasks.return_value = [t_active]

        t_queued = Task(
            id="task-102",
            agent="pr_drafter",
            prompt="Draft PR for #421",
            target_id="#421",
        )
        t_queued.enqueue_time = 1700000000.0 - 60.0
        t_queued.priority = 1
        t_queued.repo_full_name = "mweastwood/graviton"
        mock_tm.get_queued_tasks.return_value = [t_queued]

        t_hist_ok = Task(
            id="task-099",
            agent="issue_triager",
            prompt="Triage issue #400",
            target_id="#400",
        )
        t_hist_ok.start_time = 1700000000.0 - 300.0
        t_hist_ok.finish_time = 1700000000.0 - 200.0
        t_hist_ok.status = TaskStatus.COMPLETED
        t_hist_ok.selected_model = "gemini-3.8-flash-medium"
        t_hist_ok.repo_full_name = "mweastwood/graviton"

        t_hist_fail = Task(
            id="task-098",
            agent="code_fixer",
            prompt="Fix bug",
            target_id="#390",
        )
        t_hist_fail.start_time = 1700000000.0 - 500.0
        t_hist_fail.finish_time = 1700000000.0 - 450.0
        t_hist_fail.status = TaskStatus.FAILED
        t_hist_fail.error_message = "Test failure in unit test"
        t_hist_fail.selected_model = "claude-3-5-sonnet"
        t_hist_fail.repo_full_name = "mweastwood/graviton"
        mock_tm.get_task_history.return_value = [t_hist_ok, t_hist_fail]

        extra = {
            "quota_info": {
                "quota_pool": "gemini",
                "active_gemini_model": "gemini-3.8-flash-medium",
                "active_third_party_model": "claude-3-5-sonnet",
                "gemini_remaining_percentage": 75,
                "third_party_remaining_percentage": 50,
                "gemini_5h_remaining_percentage": 75,
                "gemini_5h_countdown": "03:15:00",
                "gemini_5h_pacing_status": "OK",
                "gemini_1w_remaining_percentage": 80,
                "gemini_1w_countdown": "4d 12h",
                "gemini_1w_pacing_status": "OK",
                "third_party_5h_remaining_percentage": 50,
                "third_party_5h_countdown": "02:00:00",
                "third_party_5h_pacing_status": "BEHIND_PACING",
                "third_party_5h_pacing_recovery_countdown": "00:45:00",
                "third_party_1w_remaining_percentage": 60,
                "third_party_1w_countdown": "5d 00h",
                "third_party_1w_pacing_status": "OK",
            },
            "approved_prs": [
                {
                    "number": 123,
                    "repo_full_name": "mweastwood/graviton",
                    "title": "Add awesome feature",
                    "author": "octocat",
                    "url": "https://github.com/mweastwood/graviton/pull/123",
                }
            ],
            "commit": "deadbeef",
            "branch": "feature/refactor",
            "reload_state": "IDLE",
        }

        with patch("time.time", return_value=1700000000.0), \
             patch("datetime.datetime", new=self._fixed_dt_mock()), \
             patch("lib.dashboard.html._detect_git_repo_full_name", return_value="mweastwood/graviton"):
            # 1. Format markdown
            md = format_dashboard_markdown(
                task_manager=mock_tm,
                host="localhost",
                port=8000,
                extra_info=extra,
            )

            self.assertIn("🟡 **BUSY**", md)
            self.assertIn("task-101", md)
            self.assertIn("task-102", md)
            self.assertIn("task-099", md)
            self.assertIn("task-098", md)
            self.assertIn("Test failure in unit test", md)
            self.assertIn("Add awesome feature", md)

            # 2. Parse markdown
            parsed = parse_dashboard_markdown(md, default_host="localhost", default_port=8000)
            self.assertEqual(parsed["status_text"], "BUSY")
            self.assertEqual(len(parsed["active_tasks"]), 1)
            self.assertEqual(parsed["active_tasks"][0]["id"], "task-101")
            self.assertEqual(len(parsed["queued_tasks_list"]), 1)
            self.assertEqual(parsed["queued_tasks_list"][0]["id"], "task-102")
            self.assertEqual(len(parsed["history_tasks"]), 2)
            self.assertEqual(len(parsed["approved_prs"]), 1)
            self.assertEqual(parsed["approved_prs"][0]["number"], 123)

            # 3. Render HTML
            html_out = render_dashboard_html(
                md,
                host="localhost",
                port=8000,
                task_manager=mock_tm,
                extra_info=extra,
            )
            self.assertIn("task-101", html_out)
            self.assertIn("task-102", html_out)
            self.assertIn("task-099", html_out)
            self.assertIn("task-098", html_out)
            self.assertIn("Add awesome feature", html_out)
            self.assertIn("deadbeef", html_out)
            self.assertIn("feature/refactor", html_out)

            import hashlib
            self.assertEqual(
                hashlib.sha256(md.encode()).hexdigest(),
                "f52d24cbafc2347d8d25d4cb07d904008b85347c0d55172f750707c2c6f25cd2",
            )
            self.assertEqual(
                hashlib.sha256(html_out.encode()).hexdigest(),
                "f2fdae565128e9f1c2a70ebfa219d333c29a74e13ad851b7c1c172feeac8d36b",
            )


if __name__ == "__main__":
    unittest.main()
