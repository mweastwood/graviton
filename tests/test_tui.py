"""
Unit tests for lib/tui.py (TerminalDashboard).
"""

import io
import json
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

                rendered = dashboard.render(width=80)
                self.assertIn("EVENT LOGS", rendered)
                self.assertIn("Webhook event received", rendered)

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
        self.assertIn("EVENT LOGS", rendered)
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
        self.assertIn("EVENT LOGS", rendered)
        self.assertIn("task-10", rendered)
        self.assertIn("Webhook event: issue_comment received", rendered)

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

        scheduler = TaskScheduler(config_path=config_path)
        scheduler.start()

        dashboard = TerminalDashboard(
            task_manager=manager,
            scheduler=scheduler,
            host="127.0.0.1",
            port=8080,
        )

        rendered = dashboard.render(width=80)
        scheduler.stop()

        self.assertIn("SCHEDULED JOBS [ENABLED | RUNNING]", rendered)
        self.assertIn("test_sweep_1", rendered)
        self.assertIn("Test Bug Swe..", rendered)
        self.assertIn("codebase_auditor", rendered)
        self.assertIn("test_sweep_2", rendered)
        self.assertIn("Disabled Qua..", rendered)
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

    def test_scheduled_jobs_panel_disabled(self):
        manager = TaskManager(max_workers=2)
        dashboard = TerminalDashboard(task_manager=manager, scheduler=None)
        rendered = dashboard.render(width=80)

        self.assertIn("SCHEDULED JOBS [DISABLED | STOPPED]", rendered)
        self.assertIn("(Scheduler disabled)", rendered)

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
        quota = QuotaTracker(remaining_percentage=10.0, reset_time="14:30:00")
        manager = TaskManager(max_workers=2, quota_tracker=quota)
        dashboard = TerminalDashboard(task_manager=manager, quota_tracker=quota)

        # 1. LOW_QUOTA state rendering
        rendered_low = dashboard.render(width=80)
        self.assertIn("ANTIGRAVITY MODEL QUOTA", rendered_low)
        self.assertIn("[ LOW QUOTA: 10.0% ]", rendered_low)
        self.assertIn("LOW_QUOTA", rendered_low)
        self.assertIn("14:30:00", rendered_low)

        # 2. EXHAUSTED state rendering
        quota.update_quota(0.0)
        rendered_ex = dashboard.render(width=80)
        self.assertIn("[ EXHAUSTED: 0.0% ]", rendered_ex)
        self.assertIn("EXHAUSTED (PAUSED_FOR_QUOTA)", rendered_ex)

        # 3. NORMAL state rendering
        quota.update_quota(95.0)
        rendered_norm = dashboard.render(width=80)
        self.assertIn("[ QUOTA OK: 95.0% ]", rendered_norm)
        self.assertIn("NORMAL", rendered_norm)


if __name__ == "__main__":
    unittest.main()
