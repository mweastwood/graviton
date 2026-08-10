"""
Unit tests for Periodic Background Task Scheduler Engine (lib/scheduler.py).
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.scheduler import (
    ScheduledJob,
    TaskScheduler,
    fetch_open_issues,
    is_duplicate_issue,
    parse_iso_timestamp,
)


class TestScheduledJob(unittest.TestCase):

    def test_job_serialization(self):
        job = ScheduledJob(
            job_id="test_job",
            name="Test Job",
            interval_seconds=3600,
            agent="codebase_auditor",
            prompt="Audit code",
            enabled=True,
        )
        data = job.to_dict()
        self.assertEqual(data["job_id"], "test_job")
        self.assertEqual(data["interval_seconds"], 3600)

        restored = ScheduledJob.from_dict(data)
        self.assertEqual(restored.job_id, "test_job")
        self.assertEqual(restored.name, "Test Job")
        self.assertEqual(restored.agent, "codebase_auditor")
        self.assertTrue(restored.enabled)

    def test_is_due_disabled(self):
        job = ScheduledJob(
            job_id="test",
            name="Test",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
            enabled=False,
        )
        self.assertFalse(job.is_due())

    def test_is_due_new_job(self):
        job = ScheduledJob(
            job_id="test",
            name="Test",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
            enabled=True,
        )
        self.assertTrue(job.is_due())

    def test_is_due_future_next_run(self):
        future_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        job = ScheduledJob(
            job_id="test",
            name="Test",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
            enabled=True,
            next_run=future_dt.isoformat(),
        )
        self.assertFalse(job.is_due())

    def test_is_due_past_next_run(self):
        past_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        job = ScheduledJob(
            job_id="test",
            name="Test",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
            enabled=True,
            next_run=past_dt.isoformat(),
        )
        self.assertTrue(job.is_due())

    def test_mark_executed(self):
        job = ScheduledJob(
            job_id="test",
            name="Test",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
        )
        now_dt = datetime.now(timezone.utc)
        job.mark_executed(now_dt)

        self.assertIsNotNone(job.last_run)
        self.assertIsNotNone(job.next_run)
        self.assertEqual(job.last_run, now_dt.isoformat())

    def test_is_due_invalid_next_run_logs_warning(self):
        job = ScheduledJob(
            job_id="test_invalid_next",
            name="Test",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
            enabled=True,
            next_run="bad-timestamp",
        )
        with self.assertLogs("graviton.scheduler", level="WARNING") as cm:
            self.assertTrue(job.is_due())
        self.assertTrue(any("job 'test_invalid_next' next_run" in log for log in cm.output))

    def test_is_due_invalid_last_run_logs_warning(self):
        job = ScheduledJob(
            job_id="test_invalid_last",
            name="Test",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
            enabled=True,
            last_run="bad-timestamp",
        )
        with self.assertLogs("graviton.scheduler", level="WARNING") as cm:
            self.assertTrue(job.is_due())
        self.assertTrue(any("job 'test_invalid_last' last_run" in log for log in cm.output))


class TestParseIsoTimestamp(unittest.TestCase):

    def test_parse_none_or_empty(self):
        self.assertIsNone(parse_iso_timestamp(None))
        self.assertIsNone(parse_iso_timestamp(""))

    def test_parse_valid_utc_aware(self):
        ts = "2026-08-09T02:00:00+00:00"
        dt = parse_iso_timestamp(ts)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_valid_naive(self):
        ts = "2026-08-09T02:00:00"
        dt = parse_iso_timestamp(ts)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_invalid_string_logs_warning(self):
        invalid_ts = "invalid-iso-date"
        with self.assertLogs("graviton.scheduler", level="WARNING") as cm:
            res = parse_iso_timestamp(invalid_ts, context="test_context")
            self.assertIsNone(res)
        self.assertTrue(any("Failed to parse ISO timestamp 'invalid-iso-date' for test_context" in log for log in cm.output))

    def test_parse_invalid_string_no_context_logs_warning(self):
        invalid_ts = "invalid-iso-date"
        with self.assertLogs("graviton.scheduler", level="WARNING") as cm:
            res = parse_iso_timestamp(invalid_ts)
            self.assertIsNone(res)
        self.assertTrue(any("Failed to parse ISO timestamp 'invalid-iso-date': " in log for log in cm.output))
        self.assertFalse(any(" for : " in log for log in cm.output))

    def test_parse_invalid_type_logs_warning(self):
        with self.assertLogs("graviton.scheduler", level="WARNING") as cm:
            res = parse_iso_timestamp(12345, context="type_context")  # type: ignore
            self.assertIsNone(res)
        self.assertTrue(any("Failed to parse ISO timestamp '12345' for type_context" in log for log in cm.output))

    def test_parse_z_suffix_native(self):
        dt_uppercase = parse_iso_timestamp("2026-08-09T02:00:00Z")
        self.assertIsNotNone(dt_uppercase)
        self.assertEqual(dt_uppercase.hour, 2)
        self.assertEqual(dt_uppercase.tzinfo, timezone.utc)

        dt_lowercase = parse_iso_timestamp("2026-08-09T02:00:00z")
        self.assertIsNotNone(dt_lowercase)
        self.assertEqual(dt_lowercase.hour, 2)
        self.assertEqual(dt_lowercase.tzinfo, timezone.utc)

        with self.assertLogs("graviton.scheduler", level="WARNING"):
            res = parse_iso_timestamp("2026-08-09T02:00:00Z_invalid")
            self.assertIsNone(res)

    def test_parse_single_z_edge_case(self):
        with self.assertLogs("graviton.scheduler", level="WARNING") as cm:
            res_upper = parse_iso_timestamp("Z")
            self.assertIsNone(res_upper)
            res_lower = parse_iso_timestamp("z")
            self.assertIsNone(res_lower)
        self.assertTrue(any("Failed to parse ISO timestamp 'Z':" in log for log in cm.output))
        self.assertTrue(any("Failed to parse ISO timestamp 'z':" in log for log in cm.output))


class TestTaskScheduler(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "schedules.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_load_default_jobs_if_file_missing(self):
        mock_runner = MagicMock()
        scheduler = TaskScheduler(config_path=self.config_path, runner=mock_runner)
        self.assertTrue(self.config_path.exists())
        self.assertIn("periodic_bug_sweep", scheduler.jobs)
        self.assertIn("periodic_quality_sweep", scheduler.jobs)

    def test_add_get_remove_job(self):
        scheduler = TaskScheduler(config_path=self.config_path)
        new_job = ScheduledJob(
            job_id="custom_sweep",
            name="Custom Sweep",
            interval_seconds=7200,
            agent="codebase_auditor",
            prompt="Custom prompt",
        )
        scheduler.add_job(new_job)

        retrieved = scheduler.get_job("custom_sweep")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.name, "Custom Sweep")

        removed = scheduler.remove_job("custom_sweep")
        self.assertTrue(removed)
        self.assertIsNone(scheduler.get_job("custom_sweep"))

    def test_trigger_job(self):
        mock_runner = MagicMock()
        scheduler = TaskScheduler(config_path=self.config_path, runner=mock_runner)
        success = scheduler.trigger_job("periodic_bug_sweep")
        self.assertTrue(success)
        mock_runner.assert_called_once()

    def test_start_and_stop_lifecycle(self):
        scheduler = TaskScheduler(config_path=self.config_path, check_interval_seconds=0.1)
        scheduler.start()
        self.assertTrue(scheduler.is_running())
        scheduler.stop()
        self.assertFalse(scheduler.is_running())

    def test_is_due_running_job(self):
        job = ScheduledJob(
            job_id="test_running",
            name="Test Running Job",
            interval_seconds=3600,
            agent="auditor",
            prompt="test",
            enabled=True,
            is_running=True,
        )
        self.assertFalse(job.is_due())

    def test_scheduler_loop_executes_due_job(self):
        mock_runner = MagicMock()
        # Set a short interval and job due in past
        past_dt = datetime.now(timezone.utc) - timedelta(days=1)
        job = ScheduledJob(
            job_id="due_job",
            name="Due Job",
            interval_seconds=1,
            agent="codebase_auditor",
            prompt="Audit now",
            enabled=True,
            next_run=past_dt.isoformat(),
        )

        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump([job.to_dict()], f)

        scheduler = TaskScheduler(
            config_path=self.config_path,
            runner=mock_runner,
            check_interval_seconds=0.05,
        )
        scheduler.start()
        # Wait briefly for thread to loop
        import time

        time.sleep(0.3)
        scheduler.stop()

        mock_runner.assert_called()

    def test_task_manager_routing_and_running_state(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(config_path=self.config_path, task_manager=tm)

        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)
        self.assertFalse(job.is_running)

        success = scheduler.trigger_job("periodic_bug_sweep")
        self.assertTrue(success)

        # Verify task was submitted with target_id sched:periodic_bug_sweep
        queued_or_all = tm.get_all_tasks()
        self.assertEqual(len(queued_or_all), 1)
        submitted_task = queued_or_all[0]
        self.assertEqual(submitted_task.target_id, "sched:periodic_bug_sweep")
        self.assertEqual(submitted_task.agent, job.agent)

        # Verify job is_running and current_task_id tracking
        self.assertTrue(job.is_running)
        self.assertEqual(job.current_task_id, submitted_task.id)

        # Simulate task completion and update running states
        submitted_task.status = "COMPLETED"
        scheduler.update_running_states()
        self.assertFalse(job.is_running)
        self.assertIsNone(job.current_task_id)

    def test_update_running_states_resets_is_running_when_current_task_id_none(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(config_path=self.config_path, task_manager=tm)

        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)
        job.is_running = True
        job.current_task_id = None

        scheduler.update_running_states()
        self.assertFalse(job.is_running)

    def test_update_running_states_persists_config_changes(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(config_path=self.config_path, task_manager=tm)

        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)

        scheduler.trigger_job("periodic_bug_sweep")
        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        saved_job = next(item for item in data if item["job_id"] == "periodic_bug_sweep")
        self.assertTrue(saved_job["is_running"])

        tasks = tm.get_all_tasks()
        tasks[0].status = "COMPLETED"
        scheduler.update_running_states()

        with open(self.config_path, "r", encoding="utf-8") as f:
            data_after = json.load(f)
        saved_job_after = next(item for item in data_after if item["job_id"] == "periodic_bug_sweep")
        self.assertFalse(saved_job_after["is_running"])



class TestIssueUtilities(unittest.TestCase):

    def test_is_duplicate_issue(self):
        existing = [
            {"number": 1, "title": "Unhandled null pointer exception in router.py", "body": "..."},
            {"number": 2, "title": "[Bug Sweep] Memory leak in runner thread", "body": "..."},
        ]
        self.assertTrue(is_duplicate_issue("Unhandled null pointer exception in router.py", existing))
        self.assertTrue(is_duplicate_issue("[Bug Sweep] Memory leak in runner thread", existing))
        self.assertFalse(is_duplicate_issue("Completely new bug report", existing))

    @patch("lib.scheduler.subprocess.run")
    def test_fetch_open_issues(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = json.dumps([{"number": 10, "title": "Periodic tasks"}])
        mock_run.return_value = mock_res

        issues = fetch_open_issues()
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["number"], 10)


if __name__ == "__main__":
    unittest.main()
