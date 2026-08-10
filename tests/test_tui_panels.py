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
    fit_to_display_width,
    format_interval,
    format_remaining,
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

    def test_render_approved_prs_panel_with_none_values(self):
        prs_none = [
            {
                "number": 123,
                "repo_full_name": "owner/repo",
                "title": None,
                "author": None,
                "url": None,
            },
            {
                "number": 124,
                "title": None,
                "author": None,
                "url": None,
            },
        ]
        lines_repo = render_approved_prs_panel(width=80, approved_prs=prs_none)
        self.assertTrue(any("#123" in l for l in lines_repo))

        lines_no_repo = render_approved_prs_panel(width=80, approved_prs=[prs_none[1]])
        self.assertTrue(any("#124" in l for l in lines_no_repo))

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
        for inner_w in (10, 15, 20, 23):
            # Test with repo column
            pr_w, repo_w, title_w, author_w, url_w = allocate_approved_pr_columns(inner_w, has_repo=True)
            self.assertGreaterEqual(pr_w, 4)
            self.assertGreaterEqual(repo_w, 8)
            self.assertGreaterEqual(author_w, 6)
            self.assertGreaterEqual(title_w, 1)
            self.assertGreaterEqual(url_w, 1)

            # Test without repo column
            pr_w2, repo_w2, title_w2, author_w2, url_w2 = allocate_approved_pr_columns(inner_w, has_repo=False)
            self.assertGreaterEqual(pr_w2, 4)
            self.assertEqual(repo_w2, 0)
            self.assertGreaterEqual(author_w2, 6)
            self.assertGreaterEqual(title_w2, 1)
            self.assertGreaterEqual(url_w2, 1)

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


if __name__ == "__main__":
    unittest.main()

