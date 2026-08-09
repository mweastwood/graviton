"""
Unit tests for lib/tui.py (TerminalDashboard).
"""

import io
import time
import unittest
from pathlib import Path

from lib.tasks import Task, TaskManager, TaskStatus
from lib.tui import (
    TerminalDashboard,
    fit_to_display_width,
    get_display_width,
    pad_to_display_width,
    truncate_to_display_width,
)


class TestTerminalDashboard(unittest.TestCase):

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
        self.assertIn("Host: 127.0.0.1:8080", rendered)
        self.assertIn("ACTIVE TASKS (RUNNING)", rendered)
        self.assertIn("task-1", rendered)
        self.assertIn("code_reviewer", rendered)
        self.assertIn("TASK QUEUE (QUEUED)", rendered)
        self.assertIn("task-2", rendered)
        self.assertIn("code_fixer", rendered)
        self.assertIn("TASK HISTORY & EVENT LOG", rendered)
        self.assertIn("task-3", rendered)
        self.assertIn("COMPLETED", rendered)

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


if __name__ == "__main__":
    unittest.main()
