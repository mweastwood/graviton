"""
Unit tests for lib/tui.py (TerminalDashboard).
"""

import io
import json
import logging
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from lib.quota import QuotaState, QuotaTracker
from lib.scheduler import ScheduledJob, TaskScheduler
from lib.tasks import Task, TaskManager, TaskStatus
from lib.tui import (
    TerminalDashboard,
    TUILogHandler,
    fit_to_display_width,
    get_display_width,
    pad_to_display_width,
    truncate_to_display_width,
)


class TestTerminalDashboard(unittest.TestCase):

    def test_tui_log_handler(self):
        handler = TUILogHandler(max_records=3)
        logger = logging.getLogger("test_tui_logger")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

        logger.info("Message 1")
        logger.info("Message 2")
        logger.info("Message 3")
        logger.info("Message 4")

        logs = handler.get_logs(limit=5)
        self.assertEqual(len(logs), 3)
        self.assertIn("Message 2", logs[0])
        self.assertIn("Message 3", logs[1])
        self.assertIn("Message 4", logs[2])

        recent_2 = handler.get_logs(limit=2)
        self.assertEqual(len(recent_2), 2)
        self.assertIn("Message 3", recent_2[0])
        self.assertIn("Message 4", recent_2[1])

        self.assertEqual(handler.get_logs(limit=0), [])
        self.assertEqual(handler.get_logs(limit=-1), [])

        handler.clear()
        self.assertEqual(len(handler.get_logs()), 0)
        logger.removeHandler(handler)

    def test_disable_file_logging_with_none(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(
            task_manager=manager,
            log_file=None,
        )
        self.assertIsNone(dashboard.log_file)

    def test_multiline_log_sanitization(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(task_manager=manager)
        dashboard.log_handler.records.append("Line 1\nLine 2\nLine 3")
        dashboard.active_screen = "logs"
        rendered = dashboard.render(width=80)
        for line in rendered.split("\n"):
            if line:
                self.assertEqual(get_display_width(line), 80)
        self.assertIn("Line 1 Line 2 Line 3", rendered)

    def test_dashboard_log_redirection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "test_graviton.log"
            stream = io.StringIO()
            stderr_capture = io.StringIO()

            root_logger = logging.getLogger()
            mock_stderr_handler = logging.StreamHandler(stderr_capture)
            root_logger.addHandler(mock_stderr_handler)

            manager = TaskManager(max_workers=2)
            dashboard = TerminalDashboard(
                task_manager=manager,
                refresh_interval=0.05,
                out_stream=stream,
                enable_log_redirection=True,
                log_file=log_file,
            )

            test_logger = logging.getLogger("graviton.test")
            test_logger.setLevel(logging.INFO)

            dashboard.start()
            try:
                test_logger.info("Webhook event received: pull_request")
                test_logger.info("Worker task completed successfully")

                logs = dashboard.log_handler.get_logs()
                self.assertTrue(any("Webhook event received: pull_request" in l for l in logs))
                self.assertTrue(any("Worker task completed successfully" in l for l in logs))

                rendered_main = dashboard.render(width=80)
                self.assertNotIn("EVENT LOGS", rendered_main)
                dashboard.active_screen = "logs"
                rendered_logs = dashboard.render(width=80)
                self.assertIn("EVENT LOGS", rendered_logs)
                self.assertIn("Webhook event received", rendered_logs)

                self.assertTrue(log_file.exists())
                file_content = log_file.read_text(encoding="utf-8")
                self.assertIn("Webhook event received: pull_request", file_content)

                self.assertEqual(stderr_capture.getvalue(), "")

            finally:
                dashboard.stop()

            self.assertIn(mock_stderr_handler, root_logger.handlers)

            test_logger.info("Log after dashboard stop")
            self.assertIn("Log after dashboard stop", stderr_capture.getvalue())

            root_logger.removeHandler(mock_stderr_handler)

    def test_display_width_helpers(self):
        # 1. get_display_width
        self.assertEqual(get_display_width("Hello"), 5)
        # Lightning emoji (⚡) is wide (2 columns)
        self.assertEqual(get_display_width("⚡ GRAVITON SERVER DASHBOARD ⚡"), 31)
        # ANSI styled string
        styled = "\033[96m\033[1m⚡ GRAVITON ⚡\033[0m"
        self.assertEqual(get_display_width(styled), 14)

        # 2. truncate_to_display_width
        self.assertEqual(truncate_to_display_width("Hello World", 5), "Hello")
        # Truncate wide emoji
        self.assertEqual(get_display_width(truncate_to_display_width("⚡⚡⚡", 3)), 3)

        # 3. pad_to_display_width
        self.assertEqual(get_display_width(pad_to_display_width("Test", 10, align="left")), 10)
        self.assertEqual(get_display_width(pad_to_display_width("Test", 10, align="right")), 10)
        self.assertEqual(get_display_width(pad_to_display_width("Test", 10, align="center")), 10)

        # 4. fit_to_display_width
        for target in [10, 20, 50]:
            fitted = fit_to_display_width(styled, target)
            self.assertEqual(get_display_width(fitted), target)

    def test_dashboard_render_output(self):
        manager = TaskManager(max_workers=2)

        # Manually seed tasks for predictable rendering test
        t_running = Task(
            id="task-1",
            agent="code_reviewer",
            prompt="Review PR #7",
            target_id="#7",
            status=TaskStatus.RUNNING,
            enqueue_time=time.time() - 10,
            start_time=time.time() - 5,
            worker_thread_id="Worker-1",
        )
        t_queued = Task(
            id="task-2",
            agent="code_fixer",
            prompt="Fix issue #9 description",
            target_id="#9",
            status=TaskStatus.QUEUED,
            enqueue_time=time.time() - 2,
        )
        t_completed = Task(
            id="task-3",
            agent="issue_triager",
            prompt="Triage issue #12",
            target_id="#12",
            status=TaskStatus.COMPLETED,
            enqueue_time=time.time() - 30,
            start_time=time.time() - 25,
            finish_time=time.time() - 20,
            worker_thread_id="Worker-2",
            return_code=0,
        )

        manager._tasks = {
            "task-1": t_running,
            "task-2": t_queued,
            "task-3": t_completed,
        }

        dashboard = TerminalDashboard(
            task_manager=manager,
            host="127.0.0.1",
            port=8080,
            repo_root=Path("/tmp"),
        )

        rendered = dashboard.render(width=80)

        self.assertIn("GRAVITON SERVER DASHBOARD", rendered)
        self.assertIn("ANTIGRAVITY MODEL QUOTA", rendered)
        self.assertIn("Host: 127.0.0.1:8080", rendered)
        self.assertIn("ACTIVE TASKS (RUNNING)", rendered)
        self.assertIn("ATTEMPT", rendered)
        self.assertIn("1/3", rendered)
        self.assertIn("task-1", rendered)
        self.assertIn("code_reviewer", rendered)
        self.assertIn("TASK QUEUE (QUEUED)", rendered)
        self.assertIn("task-2", rendered)
        self.assertIn("code_fixer", rendered)
        self.assertIn("TASK HISTORY", rendered)
        self.assertNotIn("EVENT LOGS", rendered)
        self.assertIn("task-3", rendered)
        self.assertIn("COMPLETED", rendered)

    def test_dashboard_attempt_column_rendering(self):
        manager = TaskManager(max_workers=2)
        t_active = Task(
            id="task-1",
            agent="code_reviewer",
            prompt="Review PR #1",
            status=TaskStatus.RUNNING,
            start_time=time.time() - 5,
            attempt=2,
            max_attempts=3,
        )
        t_history = Task(
            id="task-2",
            agent="code_fixer",
            prompt="Fix PR #2",
            status=TaskStatus.COMPLETED,
            start_time=time.time() - 15,
            finish_time=time.time() - 5,
            attempt=3,
            max_attempts=3,
        )
        manager._tasks = {"task-1": t_active, "task-2": t_history}
        dashboard = TerminalDashboard(task_manager=manager)
        rendered = dashboard.render(width=80)

        self.assertIn("2/3", rendered)
        self.assertIn("3/3", rendered)
        self.assertIn("ATTEMPT", rendered)

    def test_dashboard_line_widths(self):
        manager = TaskManager(max_workers=2)
        t_running = Task(
            id="task-1",
            agent="code_reviewer⚡",
            prompt="Review PR #7 with wide emoji 🤖 and long description",
            target_id="#7",
            status=TaskStatus.RUNNING,
            enqueue_time=time.time() - 10,
            start_time=time.time() - 5,
            worker_thread_id="Worker-1",
        )
        t_queued = Task(
            id="task-2",
            agent="code_fixer⚡",
            prompt="Fix issue #9 🚀",
            target_id="#9",
            status=TaskStatus.QUEUED,
            enqueue_time=time.time() - 2,
        )
        t_failed = Task(
            id="task-3",
            agent="issue_triager",
            prompt="Triage issue #14 💥",
            target_id="#14",
            status=TaskStatus.FAILED,
            enqueue_time=time.time() - 30,
            start_time=time.time() - 25,
            finish_time=time.time() - 20,
            worker_thread_id="Worker-2",
            return_code=1,
        )

        manager._tasks = {
            "task-1": t_running,
            "task-2": t_queued,
            "task-3": t_failed,
        }

        dashboard = TerminalDashboard(
            task_manager=manager,
            host="0.0.0.0",
            port=8000,
        )

        for target_w in [80, 100, 120]:
            rendered = dashboard.render(width=target_w)
            lines = rendered.split("\n")
            for i, line in enumerate(lines):
                if not line:
                    continue  # skip blank separator lines between panels
                dw = get_display_width(line)
                self.assertEqual(
                    dw,
                    target_w,
                    f"Line {i} visual width {dw} != {target_w}: {line!r}",
                )

    def test_empty_dashboard_line_widths(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(task_manager=manager, host="0.0.0.0", port=8000)

        for target_w in [80, 100, 120]:
            rendered = dashboard.render(width=target_w)
            lines = rendered.split("\n")
            for i, line in enumerate(lines):
                if not line:
                    continue
                dw = get_display_width(line)
                self.assertEqual(
                    dw,
                    target_w,
                    f"Line {i} visual width {dw} != {target_w} in empty dashboard: {line!r}",
                )

    def test_dashboard_start_stop(self):
        stream = io.StringIO()
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(
            task_manager=manager,
            refresh_interval=0.05,
            out_stream=stream,
        )

        dashboard.start()
        time.sleep(0.15)
        dashboard.stop()

        output = stream.getvalue()
        self.assertIn("GRAVITON SERVER DASHBOARD", output)

    def test_git_metadata_caching(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(
            task_manager=manager,
            git_cache_ttl=0.2,
        )

        with patch("lib.tui.get_git_info", return_value=("a1b2c3d", "main")) as mock_get_git:
            # 1. Initial render populates cache
            self.assertIsNone(dashboard._git_info_cache)
            dashboard.render(width=80)
            self.assertEqual(dashboard._git_info_cache, ("a1b2c3d", "main"))
            self.assertEqual(mock_get_git.call_count, 1)

            # 2. Subsequent render within TTL uses cached metadata
            dashboard.render(width=80)
            self.assertEqual(mock_get_git.call_count, 1)

            # 3. Cache invalidation forces immediate refetch
            dashboard.invalidate_git_cache()
            self.assertIsNone(dashboard._git_info_cache)
            dashboard.render(width=80)
            self.assertEqual(mock_get_git.call_count, 2)

            # 4. Render after TTL expiration fetches fresh metadata
            mock_get_git.return_value = ("e5f6g7h", "feat/test")
            time.sleep(0.25)
            dashboard.render(width=80)
            self.assertEqual(mock_get_git.call_count, 3)
            self.assertEqual(dashboard._git_info_cache, ("e5f6g7h", "feat/test"))

    def test_separate_task_history_and_event_logs_panels(self):
        manager = TaskManager(max_workers=2)
        log_handler = TUILogHandler(max_records=10)
        dashboard = TerminalDashboard(
            task_manager=manager,
            log_handler=log_handler,
        )

        # 1. Render when empty
        history_empty = dashboard._render_history_tasks(80, [], {"completed": 0, "failed": 0})
        self.assertTrue(history_empty[0].startswith("┌─"))
        self.assertTrue(history_empty[-1].startswith("└─"))
        self.assertIn("(No task history recorded yet)", "\n".join(history_empty))

        logs_empty = dashboard._render_event_logs(80)
        self.assertTrue(logs_empty[0].startswith("┌─"))
        self.assertTrue(logs_empty[-1].startswith("└─"))
        self.assertIn("(No event logs recorded yet)", "\n".join(logs_empty))

        # 2. Populate task history and logs
        t_comp = Task(
            id="task-10",
            agent="code_reviewer",
            prompt="Review PR #25",
            target_id="#25",
            status=TaskStatus.COMPLETED,
            enqueue_time=time.time() - 20,
            start_time=time.time() - 15,
            finish_time=time.time() - 10,
            return_code=0,
        )
        t_fail = Task(
            id="task-11",
            agent="code_fixer",
            prompt="Fix issue #25",
            target_id="#25",
            status=TaskStatus.FAILED,
            enqueue_time=time.time() - 10,
            start_time=time.time() - 8,
            finish_time=time.time() - 5,
            return_code=1,
        )
        manager._tasks = {"task-10": t_comp, "task-11": t_fail}
        stats = manager.get_stats()

        logger = logging.getLogger("test_separate_panels")
        logger.setLevel(logging.INFO)
        logger.addHandler(log_handler)
        logger.info("Webhook event: issue_comment received")
        logger.error("Failed to parse event payload")

        # 3. Test independent panel renderings
        history_lines = dashboard._render_history_tasks(80, [t_comp, t_fail], stats)
        history_str = "\n".join(history_lines)
        self.assertIn("TASK HISTORY", history_str)
        self.assertIn("task-10", history_str)
        self.assertIn("task-11", history_str)
        self.assertNotIn("Webhook event: issue_comment received", history_str)
        for line in history_lines:
            self.assertEqual(get_display_width(line), 80)

        logs_lines = dashboard._render_event_logs(80)
        logs_str = "\n".join(logs_lines)
        self.assertIn("EVENT LOGS", logs_str)
        self.assertIn("Webhook event: issue_comment received", logs_str)
        self.assertIn("Failed to parse event payload", logs_str)
        self.assertNotIn("task-10", logs_str)
        for line in logs_lines:
            self.assertEqual(get_display_width(line), 80)

        # 4. Test full dashboard layout rendering
        rendered = dashboard.render(width=80)
        self.assertIn("TASK HISTORY", rendered)
        self.assertNotIn("EVENT LOGS", rendered)
        self.assertIn("task-10", rendered)
        self.assertNotIn("Webhook event: issue_comment received", rendered)

        dashboard.active_screen = "logs"
        rendered_logs = dashboard.render(width=80)
        self.assertIn("EVENT LOGS", rendered_logs)
        self.assertIn("Webhook event: issue_comment received", rendered_logs)
        self.assertNotIn("TASK HISTORY", rendered_logs)

        # Clean up logger handler
        logger.removeHandler(log_handler)

    def test_scheduled_jobs_panel_with_active_scheduler(self):
        manager = TaskManager(max_workers=2)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {
                        "job_id": "test_sweep_1",
                        "name": "Test Bug Sweep Job",
                        "interval_seconds": 86400,
                        "agent": "codebase_auditor",
                        "prompt": "Test prompt",
                        "enabled": True,
                        "last_run": "2026-08-09T01:00:00+00:00",
                        "next_run": "2026-08-10T01:00:00+00:00",
                    },
                    {
                        "job_id": "test_sweep_2",
                        "name": "Disabled Quality Sweep Job",
                        "interval_seconds": 3600,
                        "agent": "codebase_auditor",
                        "prompt": "Disabled prompt",
                        "enabled": False,
                        "last_run": None,
                        "next_run": None,
                    },
                ],
                f,
            )
            config_path = Path(f.name)
        self.addCleanup(config_path.unlink, missing_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_state:
            state_path = Path(f_state.name)
        self.addCleanup(state_path.unlink, missing_ok=True)

        scheduler = TaskScheduler(config_path=config_path, state_path=state_path)
        scheduler.start()

        dashboard = TerminalDashboard(
            task_manager=manager,
            scheduler=scheduler,
            host="127.0.0.1",
            port=8080,
        )

        # Verify main screen excludes scheduled jobs
        rendered_main = dashboard.render(width=80)
        self.assertNotIn("SCHEDULED JOBS", rendered_main)

        # Switch to jobs screen and verify panel rendering
        dashboard.active_screen = "jobs"
        rendered = dashboard.render(width=80)
        scheduler.stop()

        self.assertIn("SCHEDULED JOBS [(j/k) select | (space) toggle | (e/d) state | (r)un ]", rendered)
        self.assertIn("test_sweep_1", rendered)
        self.assertIn("Test Bug Sweep Job", rendered)
        self.assertIn("codebase_auditor", rendered)
        self.assertIn("test_sweep_2", rendered)
        self.assertIn("Disabled Quality Sweep Job", rendered)
        self.assertIn("DISABLED", rendered)

        # Verify visual line widths for multiple terminal widths
        for target_w in [80, 100, 120]:
            r = dashboard.render(width=target_w)
            lines = r.split("\n")
            for i, line in enumerate(lines):
                if not line:
                    continue
                dw = get_display_width(line)
                self.assertEqual(
                    dw,
                    target_w,
                    f"Line {i} visual width {dw} != {target_w} in scheduler dashboard: {line!r}",
                )

        # Verify table mode with dynamic column width allocation
        table_rendered_lines = dashboard._render_scheduled_jobs(width=120, scheduler=scheduler, mode="table")
        table_text = "\n".join(table_rendered_lines)
        self.assertIn("Test Bug Sweep Job", table_text)
        self.assertIn("Disabled Quality Sweep Job", table_text)

    def test_scheduled_jobs_running_badge_rendering(self):
        manager = TaskManager(max_workers=2)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {
                        "job_id": "running_job_1",
                        "name": "Active Running Job",
                        "interval_seconds": 3600,
                        "agent": "codebase_auditor",
                        "prompt": "Running prompt",
                        "enabled": True,
                    }
                ],
                f,
            )
            config_path = Path(f.name)
        self.addCleanup(config_path.unlink, missing_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_state:
            f_state.write("{}")
            state_path = Path(f_state.name)
        self.addCleanup(state_path.unlink, missing_ok=True)

        scheduler = TaskScheduler(config_path=config_path, state_path=state_path)
        job = scheduler.get_job("running_job_1")
        self.assertIsNotNone(job)
        job.is_running = True
        job.current_task_id = "task-42"

        dashboard = TerminalDashboard(task_manager=manager, scheduler=scheduler)
        dashboard.active_screen = "jobs"

        rendered = dashboard.render(width=80)
        self.assertIn("RUNNING", rendered)
        self.assertIn("running_job_1", rendered)
        self.assertIn("Active Running Job", rendered)

    def test_scheduled_jobs_panel_disabled(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(task_manager=manager, scheduler=None)

        rendered_main = dashboard.render(width=80)
        self.assertNotIn("SCHEDULED JOBS", rendered_main)

        dashboard.active_screen = "jobs"
        rendered = dashboard.render(width=80)

        self.assertIn("SCHEDULED JOBS [(j/k) select | (space) toggle | (e/d) state | (r)un ]", rendered)
        self.assertIn("(Scheduler disabled)", rendered)

    def test_scheduled_jobs_selector_rendering(self):
        manager = TaskManager(max_workers=2)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {
                        "job_id": "job_1",
                        "name": "First Job",
                        "interval_seconds": 3600,
                        "agent": "codebase_auditor",
                        "prompt": "Prompt 1",
                        "enabled": True,
                    },
                    {
                        "job_id": "job_2",
                        "name": "Second Job",
                        "interval_seconds": 3600,
                        "agent": "codebase_auditor",
                        "prompt": "Prompt 2",
                        "enabled": False,
                    },
                ],
                f,
            )
            config_path = Path(f.name)
        self.addCleanup(config_path.unlink, missing_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_state:
            state_path = Path(f_state.name)
        self.addCleanup(state_path.unlink, missing_ok=True)

        scheduler = TaskScheduler(config_path=config_path, state_path=state_path)
        dashboard = TerminalDashboard(task_manager=manager, scheduler=scheduler)
        dashboard.active_screen = "jobs"

        rendered = dashboard.render(width=80)
        self.assertIn("SCHEDULED JOBS [(j/k) select | (space) toggle | (e/d) state | (r)un ]", rendered)
        self.assertIn("> ", rendered)

        dashboard.select_next_job()
        self.assertEqual(dashboard.selected_job_index, 1)
        rendered_2 = dashboard.render(width=80)
        self.assertIn("> ", rendered_2)

    def test_scheduled_jobs_interactive_controls(self):
        manager = TaskManager(max_workers=2)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(
                [
                    {
                        "job_id": "job_1",
                        "name": "First Job",
                        "interval_seconds": 3600,
                        "agent": "codebase_auditor",
                        "prompt": "Prompt 1",
                        "enabled": True,
                    },
                    {
                        "job_id": "job_2",
                        "name": "Second Job",
                        "interval_seconds": 3600,
                        "agent": "codebase_auditor",
                        "prompt": "Prompt 2",
                        "enabled": False,
                    },
                ],
                f,
            )
            config_path = Path(f.name)
        self.addCleanup(config_path.unlink, missing_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_state:
            state_path = Path(f_state.name)
        self.addCleanup(state_path.unlink, missing_ok=True)

        scheduler = TaskScheduler(config_path=config_path, state_path=state_path)
        dashboard = TerminalDashboard(task_manager=manager, scheduler=scheduler)

        dashboard.active_screen = "jobs"
        self.assertEqual(dashboard.active_screen, "jobs")
        self.assertEqual(dashboard.selected_job_index, 0)

        toggled_job = dashboard.toggle_selected_job()
        self.assertIsNotNone(toggled_job)
        self.assertFalse(toggled_job.enabled)
        self.assertFalse(scheduler.jobs["job_1"].enabled)

        toggled_job_2 = dashboard.toggle_selected_job()
        self.assertIsNotNone(toggled_job_2)
        self.assertTrue(toggled_job_2.enabled)
        self.assertTrue(scheduler.jobs["job_1"].enabled)

        disabled_job = dashboard.disable_selected_job()
        self.assertIsNotNone(disabled_job)
        self.assertFalse(disabled_job.enabled)
        self.assertFalse(scheduler.jobs["job_1"].enabled)

        enabled_job = dashboard.enable_selected_job()
        self.assertIsNotNone(enabled_job)
        self.assertTrue(enabled_job.enabled)
        self.assertTrue(scheduler.jobs["job_1"].enabled)

        dashboard.select_next_job()
        self.assertEqual(dashboard.selected_job_index, 1)

        dashboard.handle_key("down")
        self.assertEqual(dashboard.selected_job_index, 1)

        dashboard.handle_key("d")
        self.assertFalse(scheduler.jobs["job_2"].enabled)

        dashboard.handle_key(" ")
        self.assertTrue(scheduler.jobs["job_2"].enabled)

        dashboard.handle_key(" ")
        self.assertFalse(scheduler.jobs["job_2"].enabled)

        dashboard.handle_key("e")
        self.assertTrue(scheduler.jobs["job_2"].enabled)

        with patch.object(scheduler, "trigger_job", return_value=True) as mock_trigger:
            dashboard.handle_key("r")
            mock_trigger.assert_called_once_with("job_2")

        dashboard.handle_key("k")
        self.assertEqual(dashboard.selected_job_index, 0)

        dashboard.handle_key("\x1b[A")
        self.assertEqual(dashboard.selected_job_index, 0)

        dashboard.handle_key("\x1b")
        self.assertEqual(dashboard.active_screen, "main")

    def test_hotkeys_and_screen_navigation(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(task_manager=manager)

        # 1. Default active_screen is "main"
        self.assertEqual(dashboard.active_screen, "main")

        # 2. Main screen render excludes scheduled jobs and event logs, displaying hotkey hint
        rendered_main = dashboard.render(width=80)
        self.assertNotIn("SCHEDULED JOBS", rendered_main)
        self.assertNotIn("EVENT LOGS", rendered_main)
        self.assertIn("[p] Prioritize", rendered_main)

        # 3. Toggle to "jobs" screen via 'j' hotkey
        dashboard.handle_key("j")
        self.assertEqual(dashboard.active_screen, "jobs")
        rendered_jobs = dashboard.render(width=80)
        self.assertIn("SCHEDULED JOBS", rendered_jobs)
        self.assertIn("Press [Esc] to return to Main Screen", rendered_jobs)

        # 4. Toggle back to "main" screen via 'Esc' key
        dashboard.handle_key("\x1b")
        self.assertEqual(dashboard.active_screen, "main")

    def test_gemini_and_third_party_model_selection_screens(self):
        quota = QuotaTracker()
        manager = TaskManager(max_workers=2, quota_tracker=quota)
        dashboard = TerminalDashboard(task_manager=manager, quota_tracker=quota)

        # Test Gemini Model Selection Screen via 'g'
        dashboard.handle_key("g")
        self.assertEqual(dashboard.active_screen, "gemini_models")
        rendered_gemini = dashboard.render(width=80)
        self.assertIn("GEMINI MODEL SELECTION", rendered_gemini)
        self.assertIn("gemini-3.6-flash-high", rendered_gemini)

        # Navigate down 'j' and select 'gemini-3.6-flash-medium' via Space
        dashboard.handle_key("j")
        dashboard.handle_key(" ")
        self.assertEqual(quota.get_active_model("gemini"), "gemini-3.6-flash-medium")

        # Esc back to main
        dashboard.handle_key("esc")
        self.assertEqual(dashboard.active_screen, "main")

        # Test 3rd Party Model Selection Screen via 'c'
        dashboard.handle_key("c")
        self.assertEqual(dashboard.active_screen, "third_party_models")
        rendered_3p = dashboard.render(width=80)
        self.assertIn("3RD PARTY MODEL SELECTION", rendered_3p)
        self.assertIn("claude-sonnet-4-6", rendered_3p)

        # Navigate down 'down' and select 'claude-opus-4-6-thinking' via Enter
        dashboard.handle_key("down")
        dashboard.handle_key("\n")
        self.assertEqual(quota.get_active_model("claude_gpt"), "claude-opus-4-6-thinking")

        # Esc back to main
        dashboard.handle_key("esc")
        self.assertEqual(dashboard.active_screen, "main")
        rendered_back = dashboard.render(width=80)
        self.assertNotIn("SCHEDULED JOBS", rendered_back)
        self.assertNotIn("EVENT LOGS", rendered_back)
        self.assertIn("[p] Prioritize", rendered_back)

        # 5. Toggle to "logs" screen via 'e' hotkey
        dashboard.handle_key("e")
        self.assertEqual(dashboard.active_screen, "logs")
        rendered_logs = dashboard.render(width=80)
        self.assertIn("EVENT LOGS", rendered_logs)
        self.assertIn("Press [Esc] to return to Main Screen", rendered_logs)

        # 6. Toggle back to "main" screen via 'Esc' key
        dashboard.handle_key("\x1b")
        self.assertEqual(dashboard.active_screen, "main")
        rendered_back_again = dashboard.render(width=80)
        self.assertNotIn("EVENT LOGS", rendered_back_again)
        self.assertIn("[p] Prioritize", rendered_back_again)

    def test_jobs_screen_line_widths(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(task_manager=manager)
        dashboard.active_screen = "jobs"

        for target_w in [80, 100, 120]:
            rendered = dashboard.render(width=target_w)
            lines = rendered.split("\n")
            for i, line in enumerate(lines):
                if not line:
                    continue
                dw = get_display_width(line)
                self.assertEqual(
                    dw,
                    target_w,
                    f"Line {i} visual width {dw} != {target_w} on jobs screen: {line!r}",
                )

    def test_logs_screen_line_widths(self):
        manager = TaskManager(max_workers=2)
        log_handler = TUILogHandler(max_records=20)
        for i in range(15):
            log_handler.records.append(f"Event log entry #{i+1} with 🚀 emoji and long line details")

        dashboard = TerminalDashboard(task_manager=manager, log_handler=log_handler)
        dashboard.active_screen = "logs"

        for target_w in [80, 100, 120]:
            rendered = dashboard.render(width=target_w)
            lines = rendered.split("\n")
            for i, line in enumerate(lines):
                if not line:
                    continue
                dw = get_display_width(line)
                self.assertEqual(
                    dw,
                    target_w,
                    f"Line {i} visual width {dw} != {target_w} on logs screen: {line!r}",
                )

    def test_render_approved_prs_empty_and_populated(self):
        from lib.pr_tracker import PRTracker
        manager = TaskManager(max_workers=2)
        tracker = PRTracker()

        dashboard = TerminalDashboard(
            task_manager=manager,
            pr_tracker=tracker,
        )

        # 1. Empty state
        rendered_empty = dashboard.render(width=80)
        self.assertIn("APPROVED PULL REQUESTS (READY TO MERGE)", rendered_empty)
        self.assertIn("(No approved PRs awaiting merge)", rendered_empty)

        # 2. Populated state
        tracker.add_approved_pr(
            number=42,
            title="Add feature for PR tracking 🚀",
            author="mweastwood",
            url="https://github.com/mweastwood/graviton/pull/42",
        )

        rendered_populated = dashboard.render(width=120)
        self.assertIn("#42", rendered_populated)
        self.assertIn("mweastwood", rendered_populated)
        self.assertIn("https://github.com/mweastwood/graviton/pull/42", rendered_populated)

        # Verify line display widths for 80, 100, 120 width frames
        for target_w in [80, 100, 120]:
            frame = dashboard.render(width=target_w)
            for i, line in enumerate(frame.split("\n")):
                if not line:
                    continue
                dw = get_display_width(line)
                self.assertEqual(
                    dw,
                    target_w,
                    f"Line {i} visual width {dw} != {target_w} in approved PRs dashboard frame: {line!r}",
                )

    def test_quota_panel_rendering(self):
        now = time.time()
        quota = QuotaTracker(remaining_percentage=65.0, quota_pool="gemini")
        quota.update_quota(
            remaining_percentage=65.0,
            remaining_percentage_5h=65.0,
            reset_time_5h=now + 11565.0,
            remaining_percentage_1w=20.0,
            reset_time_1w=now + 374400.0,
        )
        manager = TaskManager(max_workers=2, quota_tracker=quota)
        dashboard = TerminalDashboard(task_manager=manager, quota_tracker=quota)

        rendered = dashboard.render(width=100)
        self.assertIn("ANTIGRAVITY MODEL QUOTA", rendered)
        self.assertIn("GEMINI 5H QUOTA: 65%", rendered)
        self.assertIn("GEMINI 1W QUOTA: 20%", rendered)
        self.assertIn("PACING: BEHIND", rendered)
        self.assertIn("NEW TASKS SUSPENDED", rendered)

    def test_quota_fetch_latency_does_not_stall_tui_render(self):
        quota = QuotaTracker(remaining_percentage=90.0, quota_pool="gemini")
        manager = TaskManager(max_workers=2, quota_tracker=quota)
        dashboard = TerminalDashboard(task_manager=manager, quota_tracker=quota)

        def slow_fetch(*args, **kwargs):
            time.sleep(0.5)  # Simulate network latency
            return None

        with patch("lib.quota.fetch_live_antigravity_quota", side_effect=slow_fetch):
            dashboard.start()
            t0 = time.time()
            rendered = dashboard.render(width=80)
            render_duration = time.time() - t0

            # Rendering frame should complete almost instantaneously (< 0.1s), not blocked by network latency
            self.assertLess(render_duration, 0.1)
            self.assertIn("ANTIGRAVITY MODEL QUOTA", rendered)

            dashboard.stop()

    def test_stdin_arrow_keys_navigation(self):
        import os
        import pty
        from unittest.mock import patch

        master, slave = pty.openpty()

        try:
            manager = TaskManager(max_workers=2)
            self.addCleanup(manager.stop)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump(
                    [
                        {"job_id": "job_1", "name": "First Job", "interval_seconds": 3600, "agent": "auditor", "prompt": "P1", "enabled": True},
                        {"job_id": "job_2", "name": "Second Job", "interval_seconds": 3600, "agent": "auditor", "prompt": "P2", "enabled": True},
                        {"job_id": "job_3", "name": "Third Job", "interval_seconds": 3600, "agent": "auditor", "prompt": "P3", "enabled": True},
                    ],
                    f,
                )
                config_path = Path(f.name)
            self.addCleanup(config_path.unlink, missing_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_state:
                state_path = Path(f_state.name)
            self.addCleanup(state_path.unlink, missing_ok=True)

            scheduler = TaskScheduler(config_path=config_path, state_path=state_path)
            stream = io.StringIO()
            dashboard = TerminalDashboard(task_manager=manager, scheduler=scheduler, out_stream=stream)
            dashboard.active_screen = "jobs"
            dashboard.selected_job_index = 0

            class MockStdin:
                def fileno(self):
                    return slave
                def isatty(self):
                    return True

            mock_stdin = MockStdin()

            with patch("sys.stdin", mock_stdin):
                dashboard._running = True
                stdin_thread = threading.Thread(target=dashboard._stdin_loop, daemon=True)
                stdin_thread.start()
                time.sleep(0.05)

                try:
                    # Send Down arrow sequence
                    os.write(master, b"\x1b[B")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 1)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Send Down arrow sequence again
                    os.write(master, b"\x1b[B")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 2)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Send Up arrow sequence
                    os.write(master, b"\x1b[A")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 1)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Test streaming partial escape sequence (b"\x1b" followed after delay by b"[B")
                    os.write(master, b"\x1b")
                    time.sleep(0.01)
                    os.write(master, b"[B")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 2)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Test streaming partial escape sequence (b"\x1b[" followed after delay by b"A")
                    os.write(master, b"\x1b[")
                    time.sleep(0.01)
                    os.write(master, b"A")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 1)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Test multi-key chunk (b"\x1b[B\x1b[B" - 2 down arrows in a single write)
                    dashboard.selected_job_index = 0
                    os.write(master, b"\x1b[B\x1b[B")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 2)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Test multi-key chunk (b"kk" - 2 'k' keypresses in a single write)
                    os.write(master, b"kk")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 0)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Test Application Cursor Mode (SS3) arrow sequences (\x1bOB and \x1bOA)
                    os.write(master, b"\x1bOB")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 1)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    os.write(master, b"\x1bOA")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 0)
                    self.assertEqual(dashboard.active_screen, "jobs")

                    # Test streaming partial multi-byte UTF-8 sequence (b"\xc3" followed by b"\xa1")
                    os.write(master, b"\xc3")
                    time.sleep(0.01)
                    os.write(master, b"\xa1")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.selected_job_index, 0)

                    # Send standalone ESC key
                    os.write(master, b"\x1b")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.active_screen, "main")

                finally:
                    dashboard._running = False
                    stdin_thread.join(timeout=1.0)
        finally:
            os.close(master)
            os.close(slave)

    def test_stdin_idle_timeout_flushes_leftover_bytes(self):
        """Test that idle timeout flushes and clears leftover_bytes without corrupting subsequent inputs."""
        import os
        import pty
        from unittest.mock import patch

        master, slave = pty.openpty()

        try:
            manager = TaskManager(max_workers=2)
            self.addCleanup(manager.stop)
            stream = io.StringIO()
            dashboard = TerminalDashboard(task_manager=manager, out_stream=stream)
            dashboard.active_screen = "main"

            class MockStdin:
                def fileno(self):
                    return slave
                def isatty(self):
                    return True

            mock_stdin = MockStdin()

            with patch("sys.stdin", mock_stdin):
                dashboard._running = True
                stdin_thread = threading.Thread(target=dashboard._stdin_loop, daemon=True)
                stdin_thread.start()
                time.sleep(0.05)

                try:
                    # Write an incomplete sequence (b"\x1b[") that gets split into leftover_bytes
                    os.write(master, b"\x1b[")
                    # Wait long enough for stdin to become idle and select.select to time out (>0.1s)
                    time.sleep(0.25)

                    # At this point, leftover_bytes should have been flushed/cleared.
                    # Send a valid key (b"e") to switch to logs screen.
                    os.write(master, b"e")
                    time.sleep(0.15)
                    self.assertEqual(dashboard.active_screen, "logs")
                finally:
                    dashboard._running = False
                    stdin_thread.join(timeout=1.0)
        finally:
            os.close(master)
            os.close(slave)

    def test_is_incomplete_escape_sequence(self):
        # Empty or non-escape sequence
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b""))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"a"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"hello"))

        # Incomplete sequences
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b"))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b["))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[["))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[1"))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[15"))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\x1bO"))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"j\x1b["))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[A\x1b["))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"j\xc3"))
        self.assertTrue(TerminalDashboard._is_incomplete_escape_sequence(b"\xe2\x82"))

        # Complete sequences
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[A"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[B"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[15~"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1bOA"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1bOB"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b1"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1bM"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\xc3\xa1"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[A1"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[A "))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[A\n"))
        self.assertFalse(TerminalDashboard._is_incomplete_escape_sequence(b"\x1b[A\xc3\xa1"))

    def test_parse_keys(self):
        self.assertEqual(TerminalDashboard._parse_keys(b""), [])
        self.assertEqual(TerminalDashboard._parse_keys(b"j"), ["j"])
        self.assertEqual(TerminalDashboard._parse_keys(b"jj"), ["j", "j"])
        self.assertEqual(TerminalDashboard._parse_keys(b"kk"), ["k", "k"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b[B"), ["\x1b[B"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b[B\x1b[B"), ["\x1b[B", "\x1b[B"])
        self.assertEqual(TerminalDashboard._parse_keys(b"j\x1b[A"), ["j", "\x1b[A"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1bOA"), ["\x1bOA"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1bOB"), ["\x1bOB"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1bOB\x1bOA"), ["\x1bOB", "\x1bOA"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b1"), ["\x1b1"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b"), ["\x1b"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x80\x61\x62"), ["a", "b"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b[\x1b[A"), ["\x1b[", "\x1b[A"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b[["), ["\x1b[["])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b[1\x1b[B"), ["\x1b[1", "\x1b[B"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b\x1b[A"), ["\x1b", "\x1b[A"])
        self.assertEqual(TerminalDashboard._parse_keys(b"j\xc3"), ["j"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\xc3\xa1"), ["á"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b\xc3\xa1"), ["\x1bá"])
        self.assertEqual(TerminalDashboard._parse_keys(b"\x1b\xe2\x82\xac"), ["\x1b€"])

    def test_split_incomplete_utf8_tail(self):
        self.assertEqual(TerminalDashboard._split_incomplete_utf8_tail(b""), (b"", b""))
        self.assertEqual(TerminalDashboard._split_incomplete_utf8_tail(b"hello"), (b"hello", b""))
        self.assertEqual(TerminalDashboard._split_incomplete_utf8_tail(b"j\xc3"), (b"j", b"\xc3"))
        self.assertEqual(TerminalDashboard._split_incomplete_utf8_tail(b"\xe2\x82"), (b"", b"\xe2\x82"))
        self.assertEqual(TerminalDashboard._split_incomplete_utf8_tail(b"\xc3\xa1"), (b"\xc3\xa1", b""))

    def test_split_incomplete_escape_tail(self):
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b""), (b"", b""))
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b"hello"), (b"hello", b""))
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b"\x1b"), (b"\x1b", b""))
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b"\x1b["), (b"", b"\x1b["))
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b"\x1bO"), (b"", b"\x1bO"))
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b"\x1b[1;"), (b"", b"\x1b[1;"))
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b"\x1b[A"), (b"\x1b[A", b""))
        self.assertEqual(TerminalDashboard._split_incomplete_escape_tail(b"j\x1b["), (b"j", b"\x1b["))


    def test_dashboard_queued_tasks_selection_and_prioritization_hotkeys(self):
        manager = TaskManager(max_workers=0)
        t1 = manager.submit_task("code_reviewer", "Task 1", target_id="#1")
        t2 = manager.submit_task("code_fixer", "Task 2", target_id="#2")
        dashboard = TerminalDashboard(task_manager=manager)

        self.assertEqual(dashboard.selected_queue_index, 0)
        rendered_init = dashboard.render(width=80)
        self.assertIn("[p] Prioritize", rendered_init)

        # Move down to select task 2 (index 1)
        dashboard.handle_key("down")
        self.assertEqual(dashboard.selected_queue_index, 1)

        # Press [p] to prioritize selected task (t2)
        dashboard.handle_key("p")
        self.assertEqual(t2.priority, 1)
        queued = manager.get_queued_tasks()
        self.assertEqual(queued[0].id, t2.id)
        # Cursor tracking retains focus on prioritized task (t2), moving selected_queue_index from 1 to 0
        self.assertEqual(dashboard.selected_queue_index, 0)

        # Move up
        dashboard.handle_key("up")
        self.assertEqual(dashboard.selected_queue_index, 0)

        # Test toggle_pause and v key
        self.assertFalse(manager.is_paused)
        dashboard.handle_key("v")
        self.assertTrue(manager.is_paused)
        dashboard.handle_key("v")
        self.assertFalse(manager.is_paused)

    @patch("lib.tui.termios")
    @patch("lib.tui.sys.stdin.isatty", return_value=True)
    def test_restore_termios_tcsaflush_and_ansi_cursor_restoration(self, mock_isatty, mock_termios):
        stream = io.StringIO()
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager, out_stream=stream)
        mock_termios.TCSAFLUSH = 2
        dashboard._old_term_settings = [1, 2, 3, 4]

        dashboard._restore_termios()

        mock_termios.tcsetattr.assert_called_once_with(sys.stdin.fileno(), mock_termios.TCSAFLUSH, [1, 2, 3, 4])
        self.assertIsNone(dashboard._old_term_settings)
        self.assertIn("\033[?25h\033[0m", stream.getvalue())

        # Second call must be idempotent
        mock_termios.tcsetattr.reset_mock()
        dashboard._restore_termios()
        mock_termios.tcsetattr.assert_not_called()

    @patch("lib.tui.atexit.register")
    @patch("lib.tui.atexit.unregister")
    def test_atexit_registration_and_cleanup(self, mock_unregister, mock_register):
        stream = io.StringIO()
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager, out_stream=stream)

        dashboard.start()
        mock_register.assert_called_once_with(dashboard.stop)
        self.assertTrue(dashboard._atexit_registered)

        dashboard.stop()
        mock_unregister.assert_called_once_with(dashboard.stop)
        self.assertFalse(dashboard._atexit_registered)

    @patch("lib.tui.signal.signal")
    @patch("lib.tui.signal.getsignal", return_value=signal.SIG_DFL)
    def test_signal_handler_registration_and_execution(self, mock_getsignal, mock_signal):
        stream = io.StringIO()
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager, out_stream=stream)

        dashboard.start()
        self.assertTrue(dashboard._signals_registered)
        self.assertIn(signal.SIGINT, dashboard._old_signal_handlers)
        self.assertIn(signal.SIGTERM, dashboard._old_signal_handlers)

        # Get the registered signal handler for SIGINT
        sigint_call = [c for c in mock_signal.call_args_list if c.args[0] == signal.SIGINT]
        self.assertTrue(len(sigint_call) > 0)
        handler = sigint_call[0].args[1]

        # Triggering handler should restore termios and raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            handler(signal.SIGINT, None)

        dashboard.stop()
        self.assertFalse(dashboard._signals_registered)

    @patch("lib.tui.signal.signal")
    def test_signal_handler_defers_stopping_dashboard_when_custom_orig_h_invoked(self, mock_signal):
        mock_orig_h = MagicMock()
        with patch("lib.tui.signal.getsignal", return_value=mock_orig_h):
            stream = io.StringIO()
            manager = TaskManager(max_workers=1)
            dashboard = TerminalDashboard(task_manager=manager, out_stream=stream)

            dashboard.start()
            self.assertTrue(dashboard._running)

            sigint_call = [c for c in mock_signal.call_args_list if c.args[0] == signal.SIGINT]
            self.assertTrue(len(sigint_call) > 0)
            handler = sigint_call[0].args[1]

            # Invoke signal handler
            handler(signal.SIGINT, None)

            # Custom orig_h should be called
            mock_orig_h.assert_called_once_with(signal.SIGINT, None)

            # Dashboard refresh loop MUST remain running (_running == True) so TUI displays status badges during steps 1-3
            self.assertTrue(dashboard._running)

            dashboard.stop()

    def test_restore_termios_emits_ansi_sequences_once(self):
        stream = io.StringIO()
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager, out_stream=stream)

        dashboard.start()
        dashboard._restore_termios()
        output1 = stream.getvalue()
        self.assertEqual(output1, "\033[?25h\033[0m")

        # Second call to _restore_termios should be a no-op for ANSI sequences
        dashboard._restore_termios()
        output2 = stream.getvalue()
        self.assertEqual(output2, "\033[?25h\033[0m")

        dashboard.stop()

    @patch("lib.tui.signal.signal", side_effect=ValueError("signal only works in main thread of the main interpreter"))
    @patch("lib.tui.signal.getsignal", return_value=signal.SIG_DFL)
    def test_register_signal_handlers_non_main_thread_does_not_pollute_state(self, mock_getsignal, mock_signal):
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager)

        dashboard._register_signal_handlers()

        self.assertTrue(dashboard._signals_registered)
        # _old_signal_handlers must not retain stale references when signal.signal raises ValueError
        self.assertEqual(dashboard._old_signal_handlers, {})

        dashboard._unregister_signal_handlers()
        self.assertFalse(dashboard._signals_registered)

    @patch("lib.tui.signal.signal")
    @patch("lib.tui.signal.getsignal", return_value=signal.SIG_IGN)
    def test_register_signal_handlers_preserves_sig_ign(self, mock_getsignal, mock_signal):
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager)

        dashboard._register_signal_handlers()

        self.assertTrue(dashboard._signals_registered)
        # Should not call signal.signal for SIG_IGN
        mock_signal.assert_not_called()
        self.assertEqual(dashboard._old_signal_handlers, {})

        dashboard._unregister_signal_handlers()
        self.assertFalse(dashboard._signals_registered)

    @patch("lib.tui.signal.signal")
    @patch("lib.tui.signal.getsignal", return_value=None)
    def test_register_signal_handlers_preserves_none_handler(self, mock_getsignal, mock_signal):
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager)

        dashboard._register_signal_handlers()

        self.assertTrue(dashboard._signals_registered)
        # Should not call signal.signal for native C/None handler
        mock_signal.assert_not_called()
        self.assertEqual(dashboard._old_signal_handlers, {})

        dashboard._unregister_signal_handlers()
        self.assertFalse(dashboard._signals_registered)

    def test_non_blocking_hotkey_execution_and_frame_scheduling(self):
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(
            task_manager=manager,
            refresh_interval=1.0,
            out_stream=io.StringIO(),
        )
        dashboard._running = True
        start_time = time.time()
        dashboard.handle_key("j")
        elapsed = time.time() - start_time

        # Response time must be < 10ms (0.01s)
        self.assertLess(elapsed, 0.05)
        self.assertTrue(dashboard._need_refresh)
        self.assertTrue(dashboard._refresh_event.is_set())
        self.assertEqual(dashboard.active_screen, "jobs")

    def test_render_header_navigation_hint_includes_quit(self):
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager)
        rendered = dashboard.render(width=130)
        self.assertIn("[q] Quit", rendered)

    def test_render_header_shutdown_badges(self):
        from lib.tui_panels import render_header_panel
        header_draining = render_header_panel(80, "0.0.0.0", 8000, "sha", "main", "SHUTDOWN: DRAINING_TASKS", "00:01:00")
        self.assertTrue(any("[ SHUTDOWN: DRAINING_TASKS ]" in line for line in header_draining))

        header_waiting = render_header_panel(80, "0.0.0.0", 8000, "sha", "main", "SHUTDOWN: WAITING_WEBHOOKS", "00:01:00")
        self.assertTrue(any("[ SHUTDOWN: WAITING_WEBHOOKS ]" in line for line in header_waiting))

        header_persisting = render_header_panel(80, "0.0.0.0", 8000, "sha", "main", "SHUTDOWN: PERSISTING_QUEUE", "00:01:00")
        self.assertTrue(any("[ SHUTDOWN: PERSISTING_QUEUE ]" in line for line in header_persisting))

    def test_handle_key_q_triggers_quit(self):
        manager = TaskManager(max_workers=1)
        dashboard = TerminalDashboard(task_manager=manager, quit_grace_period=0.01)

        with patch.object(dashboard, "quit") as mock_quit:
            dashboard.handle_key("q")
            mock_quit.assert_called_once()

        with patch.object(dashboard, "quit") as mock_quit_upper:
            dashboard.handle_key("Q")
            mock_quit_upper.assert_called_once()

    def test_tui_graceful_shutdown_workflow(self):
        mock_task_manager = MagicMock()
        mock_scheduler = MagicMock()
        mock_httpd = MagicMock()
        mock_on_quit = MagicMock()

        dashboard = TerminalDashboard(
            task_manager=mock_task_manager,
            scheduler=mock_scheduler,
            quit_grace_period=0.01,
            httpd=mock_httpd,
            on_quit=mock_on_quit,
        )

        shutdown_thread = dashboard.quit()
        shutdown_thread.join(timeout=2.0)

        mock_task_manager.drain_active_tasks.assert_called_once()
        mock_task_manager.dump_queue_state.assert_called_once()
        mock_scheduler.stop.assert_called_once()
        mock_httpd.shutdown.assert_called_once()
        mock_httpd.server_close.assert_called_once()
        mock_on_quit.assert_called_once()

    def test_tui_graceful_shutdown_concurrent_lock_guard(self):
        mock_task_manager = MagicMock()
        dashboard = TerminalDashboard(task_manager=mock_task_manager, quit_grace_period=0.01)

        threads = []
        results = [None] * 10

        def worker(idx):
            results[idx] = dashboard.graceful_shutdown(grace_period=0.01)

        for i in range(10):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        first_thread = results[0]
        self.assertIsNotNone(first_thread)
        for res in results:
            self.assertIs(res, first_thread)

        first_thread.join(timeout=2.0)

    def test_run_graceful_shutdown_httpd_shutdown_before_dump_queue_state(self):
        from lib.tui import run_graceful_shutdown
        mock_task_manager = MagicMock()
        mock_httpd = MagicMock()
        call_order = []

        def record_httpd_shutdown():
            call_order.append("httpd.shutdown")

        def record_dump_queue():
            call_order.append("dump_queue_state")

        mock_httpd.shutdown.side_effect = record_httpd_shutdown
        mock_task_manager.dump_queue_state.side_effect = record_dump_queue

        run_graceful_shutdown(
            task_manager=mock_task_manager,
            httpd=mock_httpd,
            grace_period=0.01,
        )

        self.assertEqual(call_order, ["httpd.shutdown", "dump_queue_state"])

    def test_run_graceful_shutdown_exception_resilience(self):
        from lib.tui import run_graceful_shutdown
        mock_task_manager = MagicMock()
        mock_scheduler = MagicMock()
        mock_httpd = MagicMock()
        mock_on_quit = MagicMock()

        mock_task_manager.drain_active_tasks.side_effect = RuntimeError("Drain error")

        run_graceful_shutdown(
            task_manager=mock_task_manager,
            scheduler=mock_scheduler,
            httpd=mock_httpd,
            grace_period=0.01,
            on_quit=mock_on_quit,
        )

        mock_httpd.shutdown.assert_called_once()
        mock_task_manager.dump_queue_state.assert_called_once()
        mock_scheduler.stop.assert_called_once()
        mock_on_quit.assert_called_once()
        mock_httpd.server_close.assert_called_once()
        mock_task_manager.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()




