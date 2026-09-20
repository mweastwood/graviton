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
    format_dashboard_markdown,
    format_duration,
    render_dashboard_html,
)
from lib.quota import QuotaTracker
from lib.tasks import Task, TaskStatus


class TestDashboardFormatting(unittest.TestCase):
    """Test duration and markdown formatting functions."""

    def setUp(self):
        super().setUp()
        self._models_patcher = patch(
            "lib.quota.fetch_cli_models",
            return_value=(["gemini-3.6-flash-high"], ["claude-sonnet-4-6"]),
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
        tracker = QuotaTracker()
        md = format_dashboard_markdown(quota_tracker=tracker)
        self.assertIn("| **Active Pool** | `gemini` |", md)
        self.assertIn("| **Active Model** | `gemini-3.6-flash-high` |", md)
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


if __name__ == "__main__":
    unittest.main()
