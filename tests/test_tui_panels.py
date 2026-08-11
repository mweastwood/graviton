"""
Unit tests for lib/tui_panels.py (Modular panel rendering components).
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lib.quota import QuotaTracker
from lib.scheduler import ScheduledJob, TaskScheduler
from lib.tasks import Task, TaskManager, TaskStatus
from lib.tui_panels import (
    allocate_approved_pr_columns,
    allocate_scheduled_job_columns,
    fit_to_display_width,
    format_interval,
    format_panel_header,
    format_remaining,
    format_row_columns,
    format_timestamp,
    get_display_width,
    pad_to_display_width,
    render_active_tasks_panel,
    render_approved_prs_panel,
    render_event_logs_panel,
    render_header_panel,
    render_history_tasks_panel,
    render_panel_header,
    render_queued_tasks_panel,
    render_quota_panel,
    render_scheduled_jobs_panel,
    truncate_to_display_width,
    truncate_with_ellipsis,
)


class TestTUIPanels(unittest.TestCase):

    def test_formatting_helpers(self):
        self.assertEqual(format_interval(86400), "1d")
        self.assertEqual(format_interval(3600), "1h")
        self.assertEqual(format_interval(60), "1m")
        self.assertEqual(format_interval(45), "45s")

        self.assertEqual(format_timestamp(None), "-")
        self.assertEqual(format_timestamp("2026-08-09T22:00:00Z"), "22:00:00")
        self.assertEqual(format_timestamp("invalid"), "invalid")

        job_disabled = ScheduledJob(job_id="j1", name="J1", agent="test", prompt="p", interval_seconds=60, enabled=False)
        now_dt = datetime.now(timezone.utc)
        self.assertEqual(format_remaining(job_disabled, now_dt), "DISABLED")

        job_disabled_running = ScheduledJob(job_id="j1_run", name="J1 Run", agent="test", prompt="p", interval_seconds=60, enabled=False, is_running=True)
        self.assertEqual(format_remaining(job_disabled_running, now_dt), "RUNNING")

        job_due = ScheduledJob(job_id="j2", name="J2", agent="test", prompt="p", interval_seconds=60, enabled=True)
        self.assertEqual(format_remaining(job_due, now_dt), "DUE")

        ref_dt = datetime(2026, 8, 9, 22, 0, 0, tzinfo=timezone.utc)
        job_next_z = ScheduledJob(
            job_id="j3", name="J3", agent="test", prompt="p", interval_seconds=60, enabled=True,
            next_run="2026-08-09T23:00:00Z"
        )
        self.assertEqual(format_remaining(job_next_z, ref_dt), "in 1h 0m")

        job_last_z = ScheduledJob(
            job_id="j4", name="J4", agent="test", prompt="p", interval_seconds=3600, enabled=True,
            last_run="2026-08-09T22:00:00Z"
        )
        self.assertEqual(format_remaining(job_last_z, ref_dt), "in 1h 0m")

    def test_truncate_with_ellipsis(self):
        self.assertEqual(truncate_with_ellipsis(None, 10), "")
        self.assertEqual(truncate_with_ellipsis("short", 10), "short")

        trunc = truncate_with_ellipsis("very long text string", 10)
        self.assertEqual(get_display_width(trunc), 10)
        self.assertTrue(trunc.endswith(".."))

        self.assertEqual(truncate_with_ellipsis("text", 1), "t")

        ansi_str = "\033[91mHello World\033[0m"
        trunc_ansi = truncate_with_ellipsis(ansi_str, 7)
        self.assertEqual(get_display_width(trunc_ansi), 7)
        self.assertTrue(trunc_ansi.endswith("\033[0m"))

    def test_format_row_columns(self):
        vals = ["PR #1", "Title"]
        widths = [8, 12]
        formatted = format_row_columns(vals, widths)
        self.assertEqual(formatted, "PR #1    Title       ")

        formatted_sep = format_row_columns(vals, widths, sep=" | ")
        self.assertEqual(formatted_sep, "PR #1    | Title       ")

    def test_allocate_scheduled_job_columns(self):
        headers, widths = allocate_scheduled_job_columns(80)
        self.assertEqual(len(headers), 8)
        self.assertEqual(len(widths), 8)
        self.assertLessEqual(sum(widths) + 7, 80)

        for w in range(10, 90):
            hdrs, col_w = allocate_scheduled_job_columns(w)
            self.assertEqual(len(hdrs), 8)
            self.assertEqual(len(col_w), 8)
            if w >= 7:
                self.assertLessEqual(sum(col_w) + 7, w)

    def test_render_header_panel(self):
        lines = render_header_panel(
            width=80,
            host="127.0.0.1",
            port=8000,
            commit="a1b2c3d",
            branch="main",
            reload_state="IDLE",
            uptime="01:23:45",
            active_screen="main",
        )
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("┌"))
        self.assertIn("127.0.0.1:8000", lines[2])
        self.assertIn("Periodic Jobs", lines[3])

    def test_render_quota_panel(self):
        tracker = QuotaTracker()
        lines = render_quota_panel(width=80, quota_tracker=tracker)
        self.assertEqual(len(lines), 4)
        self.assertIn("ANTIGRAVITY MODEL QUOTA", lines[0])

        class DummyTrackerNoRemaining:
            quota_pool = "gemini"

        dummy_tracker = DummyTrackerNoRemaining()
        lines_dummy = render_quota_panel(width=80, quota_tracker=dummy_tracker)  # type: ignore
        self.assertEqual(len(lines_dummy), 4)

    def test_render_active_tasks_panel_empty_and_populated(self):
        empty_lines = render_active_tasks_panel(width=80, tasks=[], max_workers=2)
        self.assertTrue(any("No active tasks" in l for l in empty_lines))

        task = Task(
            id="task-1",
            agent="code_reviewer",
            prompt="Review PR",
            status=TaskStatus.RUNNING,
            worker_thread_id="Worker-1",
        )
        pop_lines = render_active_tasks_panel(width=80, tasks=[task], max_workers=2)
        self.assertTrue(any("task-1" in l for l in pop_lines))

    def test_render_queued_tasks_panel_empty_and_populated(self):
        empty_lines = render_queued_tasks_panel(width=80, tasks=[])
        self.assertTrue(any("Task queue is empty" in l for l in empty_lines))

        task = Task(id="task-2", agent="code_fixer", prompt="Fix bug", status=TaskStatus.QUEUED)
        pop_lines = render_queued_tasks_panel(width=80, tasks=[task])
        self.assertTrue(any("task-2" in l for l in pop_lines))

    def test_render_scheduled_jobs_panel(self):
        empty_lines = render_scheduled_jobs_panel(width=80, scheduler=None)
        self.assertTrue(any("Scheduler disabled" in l for l in empty_lines))

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_cfg, tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_state:
            config_path = Path(f_cfg.name)
            state_path = Path(f_state.name)
        self.addCleanup(config_path.unlink, missing_ok=True)
        self.addCleanup(state_path.unlink, missing_ok=True)

        scheduler = TaskScheduler(config_path=config_path, state_path=state_path)
        scheduler.jobs["job-1"] = ScheduledJob(
            job_id="job-1", name="Health Check", agent="code_reviewer", prompt="Check status", interval_seconds=300
        )
        card_lines = render_scheduled_jobs_panel(width=80, scheduler=scheduler, mode="card")
        self.assertTrue(any("job-1" in l for l in card_lines))

        table_lines = render_scheduled_jobs_panel(width=80, scheduler=scheduler, mode="table")
        self.assertTrue(any("job-1" in l for l in table_lines))

    def test_render_scheduled_jobs_panel_compact_widths(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_cfg, tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f_state:
            config_path = Path(f_cfg.name)
            state_path = Path(f_state.name)
        self.addCleanup(config_path.unlink, missing_ok=True)
        self.addCleanup(state_path.unlink, missing_ok=True)

        scheduler = TaskScheduler(config_path=config_path, state_path=state_path)
        scheduler.jobs["job-1"] = ScheduledJob(
            job_id="job-1", name="Health Check", agent="code_reviewer", prompt="Check status", interval_seconds=300
        )
        for w in (60, 78):
            lines = render_scheduled_jobs_panel(width=w, scheduler=scheduler, mode="table")
            for line in lines:
                self.assertEqual(get_display_width(line), w)
            hdr_line = lines[1]
            self.assertIn("REMAIN", hdr_line)
            self.assertIn("NEXT RUN", hdr_line)

    def test_render_approved_prs_panel(self):
        empty_lines = render_approved_prs_panel(width=80, approved_prs=[])
        self.assertTrue(any("No approved PRs" in l for l in empty_lines))

        prs = [{"number": 42, "title": "Fix issue", "author": "dev", "url": "http://github.com/pr/42"}]
        pop_lines = render_approved_prs_panel(width=80, approved_prs=prs)
        self.assertTrue(any("#42" in l for l in pop_lines))

    def test_render_approved_prs_panel_compact_widths(self):
        prs = [{"number": 42, "title": "Fix issue with a very long description that might overflow", "author": "developer-name", "url": "http://github.com/org/repo/pull/42"}]
        for w in (60, 78):
            lines = render_approved_prs_panel(width=w, approved_prs=prs)
            for line in lines:
                self.assertEqual(get_display_width(line), w)
            hdr_line = lines[1]
            self.assertIn("URL", hdr_line)
            self.assertIn("AUTHOR", hdr_line)

    def test_render_history_tasks_panel(self):
        empty_lines = render_history_tasks_panel(width=80, tasks=[], stats={"completed": 0, "failed": 0})
        self.assertTrue(any("No task history" in l for l in empty_lines))

        task = Task(
            id="task-3",
            agent="issue_triager",
            prompt="Triage issue",
            status=TaskStatus.COMPLETED,
            return_code=0,
        )
        pop_lines = render_history_tasks_panel(width=80, tasks=[task], stats={"completed": 1, "failed": 0})
        self.assertTrue(any("task-3" in l for l in pop_lines))

    def test_render_event_logs_panel(self):
        empty_lines = render_event_logs_panel(width=80, log_handler=None)
        self.assertTrue(any("No event logs" in l for l in empty_lines))

    def test_render_quota_panel_compact_widths(self):
        tracker = QuotaTracker()
        for w in (40, 60):
            lines = render_quota_panel(width=w, quota_tracker=tracker)
            for line in lines:
                self.assertEqual(get_display_width(line), w)

    def test_render_active_tasks_panel_compact_widths(self):
        task = Task(
            id="task-100",
            agent="code_reviewer",
            prompt="Review pull request with very long prompt description",
            status=TaskStatus.RUNNING,
            worker_thread_id="Worker-1",
        )
        for w in (40, 60):
            lines = render_active_tasks_panel(width=w, tasks=[task], max_workers=10)
            for line in lines:
                self.assertEqual(get_display_width(line), w)

    def test_render_queued_tasks_panel_compact_widths(self):
        task = Task(id="task-200", agent="code_fixer", prompt="Fix bug", status=TaskStatus.QUEUED)
        for w in (40, 60):
            lines = render_queued_tasks_panel(width=w, tasks=[task])
            for line in lines:
                self.assertEqual(get_display_width(line), w)

    def test_render_history_tasks_panel_compact_widths(self):
        task = Task(
            id="task-300",
            agent="issue_triager",
            prompt="Triage issue",
            status=TaskStatus.COMPLETED,
            return_code=0,
        )
        for w in (40, 60):
            lines = render_history_tasks_panel(width=w, tasks=[task], stats={"completed": 1234, "failed": 5678})
            for line in lines:
                self.assertEqual(get_display_width(line), w)

    class MockLogHandler:
        def get_logs(self, limit=15):
            return ["Log entry 1 with long details", "Log entry 2"]

    def test_render_event_logs_panel_compact_widths(self):
        handler = self.MockLogHandler()
        for w in (40, 60):
            lines = render_event_logs_panel(width=w, log_handler=handler)
            for line in lines:
                self.assertEqual(get_display_width(line), w)

    def test_allocate_approved_pr_columns_narrow_container_widths(self):
        for inner_w in range(5, 51):
            # Test with repo column
            headers, widths = allocate_approved_pr_columns(inner_w, has_repo=True)
            if inner_w >= 9:
                self.assertLessEqual(sum(widths) + 4, inner_w)
            for w in widths:
                self.assertGreaterEqual(w, 1)

            # Test without repo column
            headers2, widths2 = allocate_approved_pr_columns(inner_w, has_repo=False)
            if inner_w >= 7:
                self.assertLessEqual(sum(widths2) + 3, inner_w)
            for w in widths2:
                self.assertGreaterEqual(w, 1)

        # Title vs URL width allocation check
        _, w_has_repo = allocate_approved_pr_columns(60, has_repo=True)
        # pr=8, repo=16, author=14 -> sum=38. avail=56. remaining=18. title=8, url=10.
        self.assertEqual(w_has_repo[2], 8)
        self.assertEqual(w_has_repo[4], 10)

        _, w_no_repo = allocate_approved_pr_columns(50, has_repo=False)
        # pr=8, author=15 -> sum=23. avail=47. remaining=24. title=11, url=13.
        self.assertEqual(w_no_repo[1], 11)
        self.assertEqual(w_no_repo[3], 13)

    def test_render_panel_header_truncation_when_exceeding_width(self):
        # Normal width where title fits comfortably
        hdr_normal = render_panel_header(width=80, title="EVENT LOGS")
        self.assertEqual(get_display_width(hdr_normal), 80)
        self.assertTrue(hdr_normal.startswith("┌─"))
        self.assertTrue(hdr_normal.endswith("┐"))
        self.assertIn("EVENT LOGS", hdr_normal)

        # Narrow width where title display width exceeds width - 3
        long_title = "APPROVED PULL REQUESTS (READY TO MERGE) [10 Ready]"
        for w in (20, 25, 30):
            hdr_trunc = render_panel_header(width=w, title=long_title)
            self.assertEqual(get_display_width(hdr_trunc), w)
            self.assertTrue(hdr_trunc.startswith("┌─"))
            self.assertTrue(hdr_trunc.endswith("┐"))

        # Very small widths (width <= 3)
        for w in (2, 3):
            hdr_small = render_panel_header(width=w, title="TITLE")
            self.assertEqual(get_display_width(hdr_small), max(3, w))

        # Parameter order consistency in format_panel_header(width, title, color_code)
        fmt_hdr = format_panel_header(80, "TITLE")
        rnd_hdr = render_panel_header(80, "TITLE")
        self.assertEqual(fmt_hdr, rnd_hdr)


if __name__ == "__main__":
    unittest.main()


