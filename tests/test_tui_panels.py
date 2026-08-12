"""
Unit tests for lib/tui_panels.py (Modular panel rendering components).
"""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.quota import QuotaTracker
from lib.scheduler import ScheduledJob, TaskScheduler
from lib.tasks import Task, TaskManager, TaskStatus
from lib.tui_panels import (
    ColumnSpec,
    TableLayoutSpec,
    allocate_approved_pr_columns,
    allocate_declarative_columns,
    allocate_scheduled_job_columns,
    fit_to_display_width,
    format_interval,
    format_remaining,
    format_table_row,
    format_target_for_display,
    format_timestamp,
    get_display_width,
    pad_to_display_width,
    render_active_tasks_panel,
    render_approved_prs_panel,
    render_event_logs_panel,
    render_header_panel,
    render_history_tasks_panel,
    render_panel_frame,
    render_panel_header,
    render_queued_tasks_panel,
    render_quota_panel,
    render_scheduled_jobs_panel,
    split_flex_columns,
    truncate_to_display_width,
    truncate_with_ellipsis,
)


class TestTUIPanels(unittest.TestCase):

    def test_extracted_layout_helpers(self):
        # Truncate with ellipsis
        self.assertEqual(truncate_with_ellipsis("hello world", 15), "hello world")
        self.assertEqual(truncate_with_ellipsis("hello world long", 10), "hello wo..")
        self.assertEqual(truncate_with_ellipsis("a", 1), "a")

        # Test ANSI escape code preservation and reset in truncate_with_ellipsis
        styled_s = "\033[92mHello World Styled String\033[0m"
        trunc_styled = truncate_with_ellipsis(styled_s, 15)
        self.assertTrue(trunc_styled.endswith("\033[0m"))
        self.assertIn("..", trunc_styled)
        self.assertEqual(get_display_width(trunc_styled), 15)

        # Format table row
        row_str = format_table_row([("ID", 5), ("STATUS", 8)])
        self.assertEqual(row_str, "ID    STATUS  ")

        # Render panel header
        hdr = render_panel_header(30, " TITLE ")
        self.assertTrue(hdr.startswith("┌─"))
        self.assertTrue(hdr.endswith("┐"))

        # Render panel header with title truncation when title exceeds width - 3
        hdr_trunc = render_panel_header(15, "Very Long Panel Title Exceeding Width")
        self.assertTrue(hdr_trunc.startswith("┌─"))
        self.assertTrue(hdr_trunc.endswith("┐"))
        self.assertIn("Very Long P", hdr_trunc)

        # Render panel frame
        frame = render_panel_frame(hdr, ["line 1", "line 2"], 30)
        self.assertEqual(len(frame), 4)
        self.assertTrue(frame[0].startswith("┌"))
        self.assertTrue(frame[3].startswith("└"))

        # Render panel frame with tuple (Sequence[str]) and narrow width (width < 4)
        frame_tuple = render_panel_frame(hdr, ("line 1", "line 2"), 2)
        self.assertEqual(len(frame_tuple), 4)
        self.assertTrue(frame_tuple[0].startswith("┌"))
        self.assertTrue(frame_tuple[3].startswith("└"))
        self.assertEqual(get_display_width(frame_tuple[3]), get_display_width(frame_tuple[1]))

        frame_zero = render_panel_frame(hdr, ("line 1",), 0)
        self.assertEqual(len(frame_zero), 3)

        # Split flex columns
        t_w, u_w = split_flex_columns(20)
        self.assertEqual(t_w + u_w, 20)
        self.assertEqual(t_w, 8)
        self.assertEqual(u_w, 12)
        self.assertEqual(split_flex_columns(0), (0, 0))
        self.assertEqual(split_flex_columns(-5), (0, 0))
        # Allocate approved PR columns (default parameter has_repo=False)
        cols_default = allocate_approved_pr_columns(76)
        self.assertNotIn("repo", cols_default)
        self.assertEqual(cols_default["pr"], 8)
        self.assertEqual(cols_default["author"], 15)

        cols_repo = allocate_approved_pr_columns(76, has_repo=True)
        self.assertIn("repo", cols_repo)
        self.assertEqual(cols_repo["pr"], 8)
        self.assertEqual(cols_repo["repo"], 16)
        self.assertEqual(cols_repo["author"], 14)

        cols_no_repo = allocate_approved_pr_columns(76, has_repo=False)
        self.assertNotIn("repo", cols_no_repo)
        self.assertEqual(cols_no_repo["pr"], 8)
        self.assertEqual(cols_no_repo["author"], 15)

        # Test narrow container widths (including inner_w < spacing, e.g. inner_w = 3 with has_repo=True where spacing = 4)
        cols_tiny_repo = allocate_approved_pr_columns(3, has_repo=True)
        self.assertIn("pr", cols_tiny_repo)
        self.assertIn("repo", cols_tiny_repo)
        self.assertEqual(sum(cols_tiny_repo.values()), 0)

        cols_tiny_no_repo = allocate_approved_pr_columns(2, has_repo=False)
        self.assertIn("pr", cols_tiny_no_repo)
        self.assertEqual(sum(cols_tiny_no_repo.values()), 0)

        for inner_w in range(0, 24):
            cols_t_narrow = allocate_approved_pr_columns(inner_w, has_repo=True)
            self.assertIn("pr", cols_t_narrow)
            self.assertIn("repo", cols_t_narrow)
            if inner_w >= 4:
                self.assertLessEqual(sum(cols_t_narrow.values()) + 4, inner_w)
            else:
                self.assertEqual(sum(cols_t_narrow.values()), 0)

        for inner_w in range(0, 24):
            cols_f_narrow = allocate_approved_pr_columns(inner_w, has_repo=False)
            self.assertIn("pr", cols_f_narrow)
            if inner_w >= 3:
                self.assertLessEqual(sum(cols_f_narrow.values()) + 3, inner_w)
            else:
                self.assertEqual(sum(cols_f_narrow.values()), 0)

        # Test intermediate container widths (e.g. inner_w = 40 for approved PRs with has_repo=True)
        cols_40 = allocate_approved_pr_columns(40, has_repo=True)
        self.assertLessEqual(sum(cols_40.values()) + 4, 40)

        for inner_w in range(24, 100):
            cols_t = allocate_approved_pr_columns(inner_w, has_repo=True)
            self.assertLessEqual(sum(cols_t.values()) + 4, inner_w)

        for inner_w in range(15, 100):
            cols_f = allocate_approved_pr_columns(inner_w, has_repo=False)
            self.assertLessEqual(sum(cols_f.values()) + 3, inner_w)

        # Allocate scheduled job columns
        id_w, name_w, agent_w = allocate_scheduled_job_columns(76)
        self.assertGreater(id_w, 0)
        self.assertGreater(name_w, 0)
        self.assertGreater(agent_w, 0)

        # Test narrow container widths for scheduled job columns (inner_w < 45)
        id_narrow, name_narrow, agent_narrow = allocate_scheduled_job_columns(30)
        self.assertEqual((id_narrow, name_narrow, agent_narrow), (0, 0, 0))

        # Test non-string and None inputs for string layout helpers
        self.assertEqual(truncate_with_ellipsis(None, 10), "")
        self.assertEqual(truncate_with_ellipsis(12345, 10), "12345")
        self.assertEqual(get_display_width(None), 0)
        self.assertEqual(get_display_width(12345), 5)

    def test_declarative_column_allocation_custom_specs(self):
        # Custom TableLayoutSpec with flex columns using non-standard column names
        custom_flex_spec = TableLayoutSpec(
            columns=[
                ColumnSpec("job_id", ratio=0.2, fixed_w=10, min_w=4, max_w=10, min_avail_threshold=3),
                ColumnSpec("description", ratio=0.5, fixed_w=None, min_w=0, max_w=None, min_avail_threshold=2, is_flex=True),
                ColumnSpec("endpoint_url", ratio=0.3, fixed_w=None, min_w=0, max_w=None, min_avail_threshold=0, is_flex=True),
            ],
            spacing=3,
            wide_threshold=40,
            narrow_threshold=15,
        )

        # Wide mode (inner_w >= wide_threshold)
        cols_wide = allocate_declarative_columns(custom_flex_spec, 50)
        self.assertEqual(cols_wide["job_id"], 10)
        self.assertGreater(cols_wide["description"], 0)
        self.assertGreater(cols_wide["endpoint_url"], 0)
        self.assertLessEqual(sum(cols_wide.values()) + 3, 50)

        # Fallback / intermediate mode (narrow_threshold <= avail < wide_threshold)
        cols_mid = allocate_declarative_columns(custom_flex_spec, 30)
        self.assertGreater(cols_mid["job_id"], 0)
        self.assertGreater(cols_mid["description"], 0)
        self.assertGreater(cols_mid["endpoint_url"], 0)
        self.assertLessEqual(sum(cols_mid.values()) + 3, 30)

        # Narrow mode (avail < narrow_threshold)
        cols_narrow = allocate_declarative_columns(custom_flex_spec, 10)
        self.assertIn("job_id", cols_narrow)
        self.assertIn("description", cols_narrow)
        self.assertIn("endpoint_url", cols_narrow)
        self.assertLessEqual(sum(cols_narrow.values()) + 3, 10)

        # Single flex column layout spec
        single_flex_spec = TableLayoutSpec(
            columns=[
                ColumnSpec("code", ratio=0.3, fixed_w=8, min_w=4),
                ColumnSpec("details", ratio=0.7, is_flex=True),
            ],
            spacing=2,
            wide_threshold=30,
            narrow_threshold=10,
        )
        cols_single_flex = allocate_declarative_columns(single_flex_spec, 40)
        self.assertEqual(cols_single_flex["code"], 8)
        self.assertEqual(cols_single_flex["details"], 40 - 8 - 2)

        # Deficit reduction test on custom layout spec
        over_spec = TableLayoutSpec(
            columns=[
                ColumnSpec("col1", ratio=0.4, fixed_w=20, min_w=10),
                ColumnSpec("col2", ratio=0.6, fixed_w=20, min_w=10, is_flex=True),
            ],
            spacing=5,
            wide_threshold=30,
            narrow_threshold=10,
        )
        cols_over = allocate_declarative_columns(over_spec, 25)
        self.assertLessEqual(sum(cols_over.values()) + 5, 25)

    def test_declarative_flex_columns_distinct_ratios(self):
        """Verify two flex columns with distinct ratios allocate proportionally according to ColumnSpec.ratio."""
        spec = TableLayoutSpec(
            columns=[
                ColumnSpec("colA", ratio=0.8, is_flex=True),
                ColumnSpec("colB", ratio=0.2, is_flex=True),
            ],
            spacing=0,
            wide_threshold=10,
            narrow_threshold=5,
        )
        res = allocate_declarative_columns(spec, 100)
        self.assertEqual(res["colA"], 80)
        self.assertEqual(res["colB"], 20)

    def test_declarative_intermediate_fallback_non_flex_bounds(self):
        """Verify non-flex column min_w/max_w constraints are preserved in intermediate mode when rem < 2."""
        spec = TableLayoutSpec(
            columns=[
                ColumnSpec("non_flex", ratio=0.5, min_w=10, max_w=15, is_flex=False),
                ColumnSpec("flex1", ratio=0.5, is_flex=True),
            ],
            spacing=0,
            wide_threshold=50,
            narrow_threshold=5,
        )
        # avail = 11, non_flex gets max(10, min(15, int(11*0.5))) = 10.
        # rem = 11 - 10 = 1 (< 2).
        res = allocate_declarative_columns(spec, 11)
        self.assertEqual(res["non_flex"], 10)
        self.assertEqual(res["flex1"], 1)

    def test_declarative_narrow_mode_last_column_min_avail_threshold(self):
        """Verify last column in narrow mode respects min_avail_threshold."""
        spec = TableLayoutSpec(
            columns=[
                ColumnSpec("col1", ratio=0.5, min_avail_threshold=1),
                ColumnSpec("col2", ratio=0.5, min_avail_threshold=10),
            ],
            spacing=0,
            wide_threshold=50,
            narrow_threshold=20,
        )
        # avail = 5 < narrow_threshold (20).
        # col2 has min_avail_threshold=10 > avail(5), so col2 should be 0.
        res = allocate_declarative_columns(spec, 5)
        self.assertEqual(res["col2"], 0)

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

        # Test naive datetime subtraction (no Z or tz info)
        job_naive = ScheduledJob(
            job_id="j5_naive", name="J5 Naive", agent="test", prompt="p", interval_seconds=3600, enabled=True,
            next_run="2026-08-09T23:00:00"
        )
        self.assertEqual(format_remaining(job_naive, ref_dt), "in 1h 0m")

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
            is_paused=False,
        )
        self.assertEqual(len(lines), 5)
        self.assertTrue(lines[0].startswith("┌"))
        self.assertIn("127.0.0.1:8000", lines[2])
        self.assertIn("[p] Prioritize", lines[3])

    def test_render_quota_panel(self):
        tracker = QuotaTracker()
        lines = render_quota_panel(width=80, quota_tracker=tracker)
        self.assertEqual(len(lines), 6)
        self.assertIn("ANTIGRAVITY MODEL QUOTA", lines[0])

        class DummyTrackerNoRemaining:
            quota_pool = "gemini"

        dummy_tracker = DummyTrackerNoRemaining()
        lines_dummy = render_quota_panel(width=80, quota_tracker=dummy_tracker)  # type: ignore
        self.assertEqual(len(lines_dummy), 6)

        # Test tracker with window_5h and window_1w attributes set to None
        class DummyTrackerNoneWindows:
            quota_pool = "gemini"
            remaining_percentage = 80.0
            window_5h = None
            window_1w = None

        lines_none = render_quota_panel(width=80, quota_tracker=DummyTrackerNoneWindows())  # type: ignore
        self.assertEqual(len(lines_none), 6)

        # Test tracker with BEHIND_PACING window and pacing recovery countdown display in panel
        from datetime import timedelta
        from lib.quota import QuotaWindow
        now_dt = datetime.now(timezone.utc)
        reset_time_str = (now_dt + timedelta(seconds=9000)).isoformat()
        w_behind = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=40.0, reset_time=reset_time_str)
        w_ok = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)
        tracker_behind = QuotaTracker()
        tracker_behind.update_windows(w_behind, w_ok)
        lines_behind = render_quota_panel(width=110, quota_tracker=tracker_behind, now_dt=now_dt)
        self.assertEqual(len(lines_behind), 6)
        self.assertTrue(any("RESUME IN 00:30:00" in line for line in lines_behind))

        # Verify now_dt propagation to get_pacing_status
        mock_w5h = QuotaWindow(name="5H", remaining_percentage=80.0)
        mock_w5h.get_pacing_status = MagicMock(return_value=("OK", 0.0))
        mock_w1w = QuotaWindow(name="1W", remaining_percentage=100.0)
        mock_w1w.get_pacing_status = MagicMock(return_value=("OK", 0.0))
        tracker_mock = QuotaTracker()
        tracker_mock.gemini_window_5h = mock_w5h
        tracker_mock.gemini_window_1w = mock_w1w
        tracker_mock.claude_window_5h = mock_w5h
        tracker_mock.claude_window_1w = mock_w1w
        custom_now = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        render_quota_panel(width=80, quota_tracker=tracker_mock, now_dt=custom_now)
        mock_w5h.get_pacing_status.assert_called_with(custom_now)
        mock_w1w.get_pacing_status.assert_called_with(custom_now)

    def test_format_target_for_display(self):
        # Username removal when space is sufficient
        self.assertEqual(format_target_for_display("mweastwood/graviton#148", 20), "graviton#148")
        self.assertEqual(format_target_for_display("octocat/Hello-World#42", 15), "Hello-World#42")

        # PR number preservation on truncation
        self.assertEqual(format_target_for_display("graviton#148", 6), "..#148")
        self.assertEqual(format_target_for_display("mweastwood/graviton#148", 6), "..#148")
        self.assertEqual(format_target_for_display("mweastwood/graviton#148", 8), "gr..#148")
        self.assertEqual(format_target_for_display("mweastwood/graviton#148", 10), "grav..#148")
        self.assertEqual(format_target_for_display("octocat/Hello-World#42", 10), "Hello..#42")
        self.assertEqual(format_target_for_display("octocat/Hello-World#42", 8), "Hel..#42")

        # Targets without PR/issue numbers
        self.assertEqual(format_target_for_display("mweastwood/graviton", 10), "graviton")
        self.assertEqual(format_target_for_display("mweastwood/graviton-server-repo", 8), "gravit..")

        # Trailing slash target inputs
        self.assertEqual(format_target_for_display("mweastwood/graviton/", 8), "graviton")
        self.assertEqual(format_target_for_display("mweastwood/graviton#148/", 10), "grav..#148")
        self.assertEqual(format_target_for_display("/", 8), "-")
        self.assertEqual(format_target_for_display("///", 8), "-")

        # Non-string integer target inputs
        self.assertEqual(format_target_for_display(148, 6), "148")
        self.assertEqual(format_target_for_display(1234567, 6), "1234..")

        # Empty / None / fallback targets
        self.assertEqual(format_target_for_display(None, 8), "-")
        self.assertEqual(format_target_for_display("", 8), "-")
        self.assertEqual(format_target_for_display("-", 8), "-")
        self.assertEqual(format_target_for_display("#148", 8), "#148")

    def test_render_active_tasks_panel_empty_and_populated(self):
        empty_lines = render_active_tasks_panel(width=80, tasks=[], max_workers=2)
        self.assertTrue(any("No active tasks" in l for l in empty_lines))

        task = Task(
            id="task-1",
            agent="code_reviewer",
            target_id="mweastwood/graviton#148",
            prompt="Review PR",
            status=TaskStatus.RUNNING,
            worker_thread_id="Worker-1",
        )
        pop_lines = render_active_tasks_panel(width=80, tasks=[task], max_workers=2)
        header_line = pop_lines[1]
        self.assertIn("ID", header_line)
        self.assertIn("AGENT", header_line)
        self.assertIn("TARGET", header_line)
        self.assertIn("ATTEMPT", header_line)
        self.assertIn("ELAPSED", header_line)
        self.assertNotIn("WORKER", header_line)
        self.assertNotIn("PROMPT", header_line)

        row_line = pop_lines[2]
        self.assertIn("task-1", row_line)
        self.assertIn("graviton#148", row_line)
        self.assertNotIn("mweastwood/", row_line)

        # Narrow width test (width=45 -> inner_w=41 -> target_w=8 -> gr..#148)
        narrow_lines = render_active_tasks_panel(width=45, tasks=[task], max_workers=2)
        narrow_row = narrow_lines[2]
        self.assertIn("gr..#148", narrow_row)


    def test_render_queued_tasks_panel_empty_and_populated(self):
        empty_lines = render_queued_tasks_panel(width=80, tasks=[])
        self.assertTrue(any("Task queue is empty" in l for l in empty_lines))
        self.assertNotIn("[PAUSED]", empty_lines[0])

        paused_lines = render_queued_tasks_panel(width=80, tasks=[], is_paused=True)
        self.assertIn("[PAUSED]", paused_lines[0])

        task = Task(
            id="task-2",
            agent="code_fixer",
            target_id="mweastwood/graviton#148",
            prompt="Fix bug",
            status=TaskStatus.QUEUED,
        )
        pop_lines = render_queued_tasks_panel(width=80, tasks=[task], selected_queue_index=0)
        header_line = pop_lines[1]
        self.assertIn("ID", header_line)
        self.assertIn("PRIO", header_line)
        self.assertIn("AGENT", header_line)
        self.assertIn("TARGET", header_line)
        self.assertIn("ATTEMPT", header_line)
        self.assertIn("WAIT", header_line)
        self.assertNotIn("PROMPT", header_line)

        row_line = pop_lines[2]
        self.assertIn(">", row_line)
        self.assertIn("task-2", row_line)
        self.assertIn("graviton#148", row_line)
        self.assertNotIn("mweastwood/", row_line)
        self.assertIn("1/3", row_line)

        # Narrow width test
        narrow_lines = render_queued_tasks_panel(width=45, tasks=[task])
        narrow_row = narrow_lines[2]
        self.assertIn("gr..#14", narrow_row)

        # Test cached task attempt formatting without truncation
        task_cached = Task(
            id="task-2c",
            agent="code_fixer",
            target_id="mweastwood/graviton#148",
            prompt="Fix bug",
            status=TaskStatus.QUEUED,
            attempt=3,
            max_attempts=6,
            requeue_count=1,
        )
        cached_lines = render_queued_tasks_panel(width=80, tasks=[task_cached])
        self.assertIn("3/6 (cached)", cached_lines[2])

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
                "number": None,
                "title": None,
                "author": None,
                "url": None,
            },
        ]
        lines_repo = render_approved_prs_panel(width=80, approved_prs=prs_none)
        self.assertTrue(any("#123" in l for l in lines_repo))
        self.assertFalse(any("#None" in l for l in lines_repo))

        lines_no_repo = render_approved_prs_panel(width=80, approved_prs=[prs_none[1]])
        self.assertFalse(any("#None" in l for l in lines_no_repo))

    def test_render_history_tasks_panel(self):
        empty_lines = render_history_tasks_panel(width=80, tasks=[], stats={"completed": 0, "failed": 0})
        self.assertTrue(any("No task history" in l for l in empty_lines))

        task = Task(
            id="task-3",
            agent="issue_triager",
            target_id="mweastwood/graviton#148",
            prompt="Triage issue",
            status=TaskStatus.COMPLETED,
            return_code=0,
        )
        pop_lines = render_history_tasks_panel(width=80, tasks=[task], stats={"completed": 1, "failed": 0})
        header_line = pop_lines[1]
        self.assertIn("ID", header_line)
        self.assertIn("STATUS", header_line)
        self.assertIn("AGENT", header_line)
        self.assertIn("TARGET", header_line)
        self.assertIn("ATTEMPT", header_line)
        self.assertIn("RETURN", header_line)
        self.assertIn("DURATION", header_line)

        # Ensure TARGET column is positioned after AGENT and before ATTEMPT
        agent_idx = header_line.index("AGENT")
        target_idx = header_line.index("TARGET")
        attempt_idx = header_line.index("ATTEMPT")
        self.assertLess(agent_idx, target_idx)
        self.assertLess(target_idx, attempt_idx)

        row_line = pop_lines[2]
        self.assertIn("task-3", row_line)
        self.assertIn("graviton#148", row_line)
        self.assertNotIn("mweastwood/", row_line)
        self.assertIn("1/3", row_line)

        # Test cached task attempt formatting in history panel without truncation
        task_cached = Task(
            id="task-3c",
            agent="issue_triager",
            target_id="mweastwood/graviton#148",
            prompt="Triage issue",
            status=TaskStatus.COMPLETED,
            return_code=0,
            attempt=3,
            max_attempts=6,
            requeue_count=1,
        )
        cached_lines = render_history_tasks_panel(width=80, tasks=[task_cached], stats={"completed": 2, "failed": 0})
        self.assertIn("3/6 (cached)", cached_lines[2])

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

    class MockLogHandlerWithMixedTypes:
        def get_logs(self, limit=15):
            return [None, 12345, "Log entry 3"]

    def test_render_event_logs_panel_compact_widths(self):
        handler = self.MockLogHandler()
        for w in (40, 60):
            lines = render_event_logs_panel(width=w, log_handler=handler)
            for line in lines:
                self.assertEqual(get_display_width(line), w)

    def test_render_event_logs_panel_non_string_entries(self):
        handler = self.MockLogHandlerWithMixedTypes()
        lines = render_event_logs_panel(width=80, log_handler=handler)
        self.assertEqual(len(lines), 5)
        self.assertTrue(any("12345" in l for l in lines))

    def test_render_gemini_and_third_party_models_panels(self):
        from lib.tui_panels import render_gemini_models_panel, render_third_party_models_panel

        gemini_models = ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro"]
        lines_g = render_gemini_models_panel(width=80, models=gemini_models, selected_index=0, active_model="gemini-2.5-flash")
        self.assertIn("GEMINI MODEL SELECTION", lines_g[0])
        self.assertTrue(any("gemini-2.5-flash" in l for l in lines_g))
        self.assertTrue(any("[ACTIVE]" in l for l in lines_g))

        tp_models = ["claude-3-5-sonnet", "claude-3-opus", "claude-3-5-haiku"]
        lines_c = render_third_party_models_panel(width=80, models=tp_models, selected_index=1, active_model="claude-3-opus")
        self.assertIn("3RD PARTY MODEL SELECTION", lines_c[0])
        self.assertTrue(any("claude-3-opus" in l for l in lines_c))
        self.assertTrue(any("[ACTIVE]" in l for l in lines_c))

    def test_declarative_column_allocation(self):
        col_spec = ColumnSpec(name="pr", ratio=0.15, fixed_w=8, min_w=4, max_w=8, min_avail_threshold=5)
        self.assertEqual(col_spec.name, "pr")
        self.assertEqual(col_spec.ratio, 0.15)
        self.assertEqual(col_spec.fixed_w, 8)
        self.assertEqual(col_spec.min_w, 4)

        custom_layout = TableLayoutSpec(
            columns=[
                ColumnSpec("col1", ratio=0.5, fixed_w=10, min_w=2, max_w=10, min_avail_threshold=2),
                ColumnSpec("col2", ratio=0.5, fixed_w=10, min_w=2, max_w=10, min_avail_threshold=2),
            ],
            spacing=2,
            wide_threshold=22,
            narrow_threshold=6,
        )

        res_wide = allocate_declarative_columns(custom_layout, 30)
        self.assertEqual(res_wide["col1"], 10)
        self.assertEqual(res_wide["col2"], 10)

        res_narrow = allocate_declarative_columns(custom_layout, 4)
        self.assertLessEqual(sum(res_narrow.values()) + 2, 4)

        # Assert intermediate container width allocations (e.g., inner_w=30)
        cols_mid = allocate_approved_pr_columns(30, has_repo=True)
        self.assertEqual(cols_mid["pr"], 4)      # Respects min_w=4 floor
        self.assertEqual(cols_mid["repo"], 8)    # Respects min_w=8 floor
        self.assertEqual(cols_mid["author"], 6)  # Respects min_w=6 floor
        # Flex columns receive allocations via split_flex_columns(rem=8)
        exp_title, exp_url = split_flex_columns(8)
        self.assertEqual(cols_mid["title"], exp_title)
        self.assertEqual(cols_mid["url"], exp_url)
        self.assertEqual(sum(cols_mid.values()) + 4, 30)

        # Test leading flex columns deficit reduction
        spec_leading_flex = TableLayoutSpec(
            columns=[
                ColumnSpec("c1", ratio=0.1, is_flex=True),
                ColumnSpec("c2", ratio=0.1, is_flex=True),
                ColumnSpec("c3", ratio=0.8, fixed_w=20, is_flex=False),
            ],
            spacing=4,
            wide_threshold=10,
            narrow_threshold=5,
        )
        cols_leading_flex = allocate_declarative_columns(spec_leading_flex, 15)
        self.assertLessEqual(sum(cols_leading_flex.values()) + spec_leading_flex.spacing, 15)
        self.assertEqual(cols_leading_flex["c3"], 11)
        self.assertEqual(cols_leading_flex["c1"], 0)
        self.assertEqual(cols_leading_flex["c2"], 0)

        # Test no-flex column specs in intermediate width mode
        spec_no_flex = TableLayoutSpec(
            columns=[
                ColumnSpec("col1", ratio=0.5, min_w=2, max_w=10, is_flex=False),
                ColumnSpec("col2", ratio=0.5, min_w=2, max_w=10, is_flex=False),
            ],
            spacing=2,
            wide_threshold=40,
            narrow_threshold=5,
        )
        cols_no_flex = allocate_declarative_columns(spec_no_flex, 30)
        self.assertEqual(cols_no_flex["col1"], 10)
        self.assertEqual(cols_no_flex["col2"], 10)
        self.assertLessEqual(sum(cols_no_flex.values()) + spec_no_flex.spacing, 30)

        for inner_w in range(0, 101):
            cols_has_repo = allocate_approved_pr_columns(inner_w, has_repo=True)
            self.assertIsInstance(cols_has_repo, dict)
            self.assertIn("pr", cols_has_repo)
            self.assertIn("repo", cols_has_repo)
            if inner_w >= 4:
                self.assertLessEqual(sum(cols_has_repo.values()) + 4, inner_w)

            cols_no_repo = allocate_approved_pr_columns(inner_w, has_repo=False)
            self.assertIsInstance(cols_no_repo, dict)
            self.assertIn("pr", cols_no_repo)
            self.assertNotIn("repo", cols_no_repo)
            if inner_w >= 3:
                self.assertLessEqual(sum(cols_no_repo.values()) + 3, inner_w)

            id_w, name_w, agent_w = allocate_scheduled_job_columns(inner_w)
            self.assertGreaterEqual(id_w, 0)
            self.assertGreaterEqual(name_w, 0)
            self.assertGreaterEqual(agent_w, 0)
            if inner_w >= 45:
                self.assertLessEqual(id_w + name_w + agent_w + 45, inner_w)
            else:
                self.assertEqual((id_w, name_w, agent_w), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()

