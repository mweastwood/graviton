"""
Unit tests for Periodic Background Task Scheduler Engine (lib/scheduler.py).
"""

import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.scheduler import (
    ScheduledJob,
    TaskScheduler,
    _normalize_title,
    fetch_open_issues,
    is_duplicate_issue,
    parse_iso_timestamp,
    pretokenize_issues,
    tokenize_title,
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
        self.state_path = Path(self.temp_dir.name) / ".graviton_scheduler_state.json"

    def tearDown(self):
        TaskScheduler.wait_all_saves()
        self.temp_dir.cleanup()

    def test_load_default_jobs_if_file_missing(self):
        mock_runner = MagicMock()
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, runner=mock_runner)
        self.assertTrue(self.config_path.exists())
        self.assertTrue(self.state_path.exists())
        expected_jobs = [
            "periodic_bug_sweep",
            "periodic_quality_sweep",
            "periodic_security_sweep",
            "periodic_test_coverage_sweep",
            "periodic_typing_sweep",
            "periodic_dead_code_sweep",
            "periodic_docs_audit",
            "periodic_ready_pr_sweep",
            "periodic_pr_hygiene_sweep",
        ]
        for job_id in expected_jobs:
            self.assertIn(job_id, scheduler.jobs)
        self.assertTrue(scheduler.jobs["periodic_bug_sweep"].enabled)
        self.assertTrue(scheduler.jobs["periodic_quality_sweep"].enabled)
        self.assertFalse(scheduler.jobs["periodic_security_sweep"].enabled)
        self.assertFalse(scheduler.jobs["periodic_ready_pr_sweep"].enabled)
        self.assertEqual(scheduler.jobs["periodic_ready_pr_sweep"].agent, "pr_drafter")
        self.assertEqual(scheduler.jobs["periodic_pr_hygiene_sweep"].agent, "code_reviewer")

    def test_default_jobs_and_config_schedules_consistency(self):
        """Verify that DEFAULT_JOBS in lib/scheduler.py matches config/schedules.json."""
        default_config_path = Path(__file__).resolve().parent.parent / "config" / "schedules.json"
        with open(default_config_path, "r", encoding="utf-8") as f:
            config_jobs = json.load(f)

        from lib.scheduler import DEFAULT_JOBS

        config_jobs_by_id = {j["job_id"]: j for j in config_jobs}
        default_jobs_by_id = {j["job_id"]: j for j in DEFAULT_JOBS}

        self.assertEqual(set(config_jobs_by_id.keys()), set(default_jobs_by_id.keys()))

        for job_id, default_job in default_jobs_by_id.items():
            config_job = config_jobs_by_id[job_id]
            self.assertEqual(default_job["prompt"], config_job["prompt"], f"Prompt mismatch for job '{job_id}'")
            self.assertEqual(default_job["agent"], config_job["agent"])
            self.assertEqual(default_job["interval_seconds"], config_job["interval_seconds"])
            self.assertEqual(default_job["enabled"], config_job["enabled"])

        # Specifically verify periodic_test_coverage_sweep prompt content
        test_sweep_prompt = default_jobs_by_id["periodic_test_coverage_sweep"]["prompt"]
        self.assertIn("flaky tests", test_sweep_prompt.lower())
        self.assertIn("low-quality tests", test_sweep_prompt.lower())
        self.assertIn("suggested resolution", test_sweep_prompt.lower())

    def test_add_get_remove_job(self):
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
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

        # Verify configuration and state persistence after add_job
        with open(self.config_path, "r", encoding="utf-8") as f:
            config_jobs = json.load(f)
        self.assertTrue(any(j["job_id"] == "custom_sweep" for j in config_jobs))

        with open(self.state_path, "r", encoding="utf-8") as f:
            state_data = json.load(f)
        self.assertIn("custom_sweep", state_data)

        # Verify job persists across re-initialization
        reloaded_scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        self.assertIsNotNone(reloaded_scheduler.get_job("custom_sweep"))

        removed = scheduler.remove_job("custom_sweep")
        self.assertTrue(removed)
        self.assertIsNone(scheduler.get_job("custom_sweep"))

        # Verify configuration and state cleanup after remove_job
        with open(self.config_path, "r", encoding="utf-8") as f:
            config_jobs_after = json.load(f)
        self.assertFalse(any(j["job_id"] == "custom_sweep" for j in config_jobs_after))

        with open(self.state_path, "r", encoding="utf-8") as f:
            state_data_after = json.load(f)
        self.assertNotIn("custom_sweep", state_data_after)

        # Verify job remains removed across re-initialization
        reloaded_scheduler_2 = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        self.assertIsNone(reloaded_scheduler_2.get_job("custom_sweep"))

    def test_async_save_state(self):
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        job = scheduler.get_job("periodic_bug_sweep")
        job.enabled = False
        scheduler.save_state(async_save=True)
        # Wait for background save thread to finish writing
        scheduler.wait_for_saves()
        self.assertTrue(self.state_path.exists())

        with open(self.state_path, "r", encoding="utf-8") as f:
            state_data = json.load(f)
        self.assertFalse(state_data["periodic_bug_sweep"]["enabled"])

    def test_save_state_out_of_order_versioning(self):
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        job = scheduler.get_job("periodic_bug_sweep")
        job.enabled = False

        # First snapshot (version 1)
        scheduler.save_state(async_save=False)

        # Update state and create second snapshot (version 2)
        job.enabled = True
        scheduler.save_state(async_save=False)

        # Simulate older thread (version 1) running after newer thread (version 2)
        with scheduler._lock:
            old_version = 1
            data_old = {"periodic_bug_sweep": {"enabled": False}}

        with scheduler._save_lock:
            if old_version >= scheduler._latest_written_state_version:
                lib.scheduler._atomic_write_json(scheduler.state_path, data_old, indent=2)
                scheduler._latest_written_state_version = old_version

        # Verify disk file still contains version 2 state (enabled: True)
        with open(self.state_path, "r", encoding="utf-8") as f:
            disk_state = json.load(f)
        self.assertTrue(disk_state["periodic_bug_sweep"]["enabled"])

    def test_save_config_prevents_runtime_timestamps(self):
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)

        now_dt = datetime.now(timezone.utc)
        job.mark_executed(now_dt)
        scheduler.save_state()
        scheduler.save_config()

        # Check config file (schedules.json) - must have null timestamps
        with open(self.config_path, "r", encoding="utf-8") as f:
            config_jobs = json.load(f)
        config_job = next(j for j in config_jobs if j["job_id"] == "periodic_bug_sweep")
        self.assertNotIn("last_run", config_job)
        self.assertNotIn("next_run", config_job)

        # Check state file (.graviton_scheduler_state.json) - must preserve active ISO timestamps
        with open(self.state_path, "r", encoding="utf-8") as f:
            state_data = json.load(f)
        self.assertIsNotNone(state_data["periodic_bug_sweep"]["last_run"])
        self.assertIsNotNone(state_data["periodic_bug_sweep"]["next_run"])

    def test_trigger_job(self):
        mock_runner = MagicMock()
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, runner=mock_runner)
        success = scheduler.trigger_job("periodic_bug_sweep")
        self.assertTrue(success)
        mock_runner.assert_called_once()

    def test_static_config_untouched_on_job_execution(self):
        mock_runner = MagicMock()
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, runner=mock_runner)

        with open(self.config_path, "r", encoding="utf-8") as f:
            initial_config_content = f.read()

        scheduler.trigger_job("periodic_bug_sweep")

        with open(self.config_path, "r", encoding="utf-8") as f:
            after_config_content = f.read()

        self.assertEqual(initial_config_content, after_config_content)

        with open(self.state_path, "r", encoding="utf-8") as f:
            state_data = json.load(f)

        self.assertIn("periodic_bug_sweep", state_data)
        self.assertIsNotNone(state_data["periodic_bug_sweep"]["last_run"])
        self.assertIsNotNone(state_data["periodic_bug_sweep"]["next_run"])

    def test_state_loading_and_saving(self):
        job = ScheduledJob(
            job_id="test_job",
            name="Test Job",
            interval_seconds=3600,
            agent="codebase_auditor",
            prompt="Audit",
            enabled=True,
        )
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump([job.to_dict()], f)

        state_info = {
            "test_job": {
                "last_run": "2026-08-10T00:00:00+00:00",
                "next_run": "2026-08-10T01:00:00+00:00",
                "enabled": False,
            }
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(state_info, f)

        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        retrieved_job = scheduler.get_job("test_job")
        self.assertIsNotNone(retrieved_job)
        self.assertEqual(retrieved_job.last_run, "2026-08-10T00:00:00+00:00")
        self.assertEqual(retrieved_job.next_run, "2026-08-10T01:00:00+00:00")
        self.assertFalse(retrieved_job.enabled)

    def test_state_fallback_corrupted_file(self):
        job = ScheduledJob(
            job_id="test_job",
            name="Test Job",
            interval_seconds=3600,
            agent="codebase_auditor",
            prompt="Audit",
            enabled=True,
        )
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump([job.to_dict()], f)

        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{invalid_json_content...")

        with self.assertLogs("graviton.scheduler", level="ERROR") as cm:
            scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)

        self.assertTrue(any("Failed to load schedule state from" in log for log in cm.output))
        self.assertIn("test_job", scheduler.jobs)
        self.assertTrue(self.state_path.exists())
        with open(self.state_path, "r", encoding="utf-8") as f:
            recovered_state = json.load(f)
        self.assertIn("test_job", recovered_state)

    def test_start_and_stop_lifecycle(self):
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, check_interval_seconds=0.1)
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
            state_path=self.state_path,
            runner=mock_runner,
            check_interval_seconds=0.05,
        )
        scheduler.start()
        import time

        time.sleep(0.3)
        scheduler.stop()

        mock_runner.assert_called()

    def test_task_manager_routing_and_running_state(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, task_manager=tm)

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

    def test_task_manager_routing_with_cwd_repo_name(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            task_manager=tm,
            cwd=Path("/workspace/my_app"),
        )

        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)
        self.assertFalse(job.is_running)

        success = scheduler.trigger_job("periodic_bug_sweep")
        self.assertTrue(success)

        # Verify task was submitted with target_id my_app#sched:periodic_bug_sweep
        queued_or_all = tm.get_all_tasks()
        self.assertEqual(len(queued_or_all), 1)
        submitted_task = queued_or_all[0]
        self.assertEqual(submitted_task.target_id, "my_app#sched:periodic_bug_sweep")
        self.assertEqual(submitted_task.agent, job.agent)
        self.assertEqual(submitted_task.repo_name, "my_app")
        self.assertEqual(submitted_task.repo_dir, Path("/workspace/my_app"))

        # Verify job is_running and current_task_id tracking
        self.assertTrue(job.is_running)
        self.assertEqual(job.current_task_id, submitted_task.id)

        # Simulate task completion and update running states
        submitted_task.status = "COMPLETED"
        scheduler.update_running_states()
        self.assertFalse(job.is_running)
        self.assertIsNone(job.current_task_id)

    def test_update_running_states_resets_orphaned_is_running_when_current_task_id_none(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, task_manager=tm)

        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)
        job.is_running = True
        job.current_task_id = None

        scheduler.update_running_states()
        self.assertFalse(job.is_running)

    def test_custom_handler_is_running_lifecycle(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, task_manager=tm)
        executed = []

        def custom_handler(job):
            self.assertTrue(job.is_running)
            scheduler.update_running_states()
            self.assertTrue(job.is_running)
            executed.append(job.job_id)

        scheduler.register_handler("periodic_bug_sweep", custom_handler)
        scheduler.trigger_job("periodic_bug_sweep")

        self.assertEqual(executed, ["periodic_bug_sweep"])
        job = scheduler.get_job("periodic_bug_sweep")
        self.assertFalse(job.is_running)

    def test_thread_safety_lock(self):
        import threading
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        errors = []

        def worker():
            try:
                for _ in range(20):
                    job = ScheduledJob(
                        job_id="thread_job",
                        name="Thread Job",
                        interval_seconds=3600,
                        agent="auditor",
                        prompt="test",
                    )
                    scheduler.add_job(job)
                    scheduler.get_job("thread_job")
                    scheduler.save_config()
                    scheduler.save_state()
                    scheduler.load_state()
                    scheduler.remove_job("thread_job")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])

    def test_update_running_states_persists_state_changes(self):
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, task_manager=tm)

        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)

        scheduler.trigger_job("periodic_bug_sweep")
        with open(self.state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("periodic_bug_sweep", data)
        self.assertTrue(data["periodic_bug_sweep"]["is_running"])

        # Also verify config file does NOT contain is_running or current_task_id or timestamps
        with open(self.config_path, "r", encoding="utf-8") as f:
            config_data = json.load(f)
        saved_config_job = next(item for item in config_data if item["job_id"] == "periodic_bug_sweep")
        self.assertNotIn("is_running", saved_config_job)
        self.assertNotIn("current_task_id", saved_config_job)

    def test_load_state_resets_stale_is_running_and_current_task_id_on_startup(self):
        """Verify process crash recovery: load_state resets is_running=False on startup to prevent deadlock."""
        # 1. Simulate process crash by writing stale is_running=True state to disk
        stale_state = {
            "periodic_bug_sweep": {
                "last_run": None,
                "next_run": None,
                "enabled": True,
                "is_running": True,
                "current_task_id": "stale-task-999",
            }
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(stale_state, f)

        # 2. Instantiate TaskScheduler without task_manager (runner mode / restart)
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        job = scheduler.get_job("periodic_bug_sweep")
        self.assertIsNotNone(job)

        # 3. Verify stale is_running and current_task_id were reset to False/None on load_state
        self.assertFalse(job.is_running)
        self.assertIsNone(job.current_task_id)

        # 4. Verify job is_due() returns True instead of deadlocking
        self.assertTrue(job.is_due())

    def test_load_state_reconciles_is_running_with_active_task_manager(self):
        """Verify load_state reconciles is_running when active task manager has corresponding task."""
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        submitted_task = tm.submit_task(agent="codebase_auditor", prompt="test", target_id="sched:periodic_bug_sweep")

        # Create state file with stale task ID
        stale_state = {
            "periodic_bug_sweep": {
                "last_run": None,
                "next_run": None,
                "enabled": True,
                "is_running": True,
                "current_task_id": "stale-task-old",
            }
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(stale_state, f)

        # Instantiate scheduler with task_manager containing active task
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path, task_manager=tm)
        job = scheduler.get_job("periodic_bug_sweep")

        # Verify load_state + update_running_states reconciled active task ID and is_running=True
        self.assertTrue(job.is_running)
        self.assertEqual(job.current_task_id, submitted_task.id)

    def test_load_state_reconciles_is_running_with_repo_qualified_target_id(self):
        """Verify load_state reconciles is_running when active task has repo-qualified target_id."""
        from lib.tasks import TaskManager
        tm = TaskManager(max_workers=1)
        submitted_task = tm.submit_task(
            agent="codebase_auditor",
            prompt="test",
            target_id="my_repo#sched:periodic_bug_sweep",
            repo_name="my_repo",
        )

        stale_state = {
            "periodic_bug_sweep": {
                "last_run": None,
                "next_run": None,
                "enabled": True,
                "is_running": True,
                "current_task_id": "stale-task-old",
            }
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(stale_state, f)

        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            task_manager=tm,
        )
        job = scheduler.get_job("periodic_bug_sweep")

        self.assertTrue(job.is_running)
        self.assertEqual(job.current_task_id, submitted_task.id)

    def test_scheduler_defers_job_submission_when_task_manager_paused(self):
        from lib.tasks import TaskManager
        manager = TaskManager()
        manager.pause()

        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            task_manager=manager,
        )
        job = ScheduledJob(
            job_id="test_paused_job",
            name="Test Paused Job",
            agent="codebase_auditor",
            prompt="Run audit",
            enabled=True,
            interval_seconds=3600,
        )
        scheduler.jobs["test_paused_job"] = job

        with patch.object(scheduler, "save_state", wraps=scheduler.save_state) as mock_save:
            # Execute job while TaskManager is paused
            scheduler._execute_job(job)

            # Job should be deferred cleanly without raising exception or setting current_task_id, while marking execution time
            self.assertFalse(job.is_running)
            self.assertIsNone(job.current_task_id)
            self.assertIsNotNone(job.last_run)
            self.assertIsNotNone(job.next_run)
            self.assertFalse(job.is_due())
            self.assertEqual(len(manager.get_all_tasks()), 0)
            # Verify save_state was only called once when deferred (avoiding unnecessary disk I/O)
            self.assertEqual(mock_save.call_count, 1)

        # Force job due again by clearing last_run and next_run to simulate next scheduled interval
        job.last_run = None
        job.next_run = None
        self.assertTrue(job.is_due())

        # Resuming TaskManager allows deferred job to be submitted on next execution cycle and updates last_run
        manager.resume()
        scheduler._execute_job(job)
        self.assertTrue(job.is_running)
        self.assertIsNotNone(job.last_run)
        self.assertIsNotNone(job.current_task_id)
        self.assertEqual(len(manager.get_all_tasks()), 1)

    def test_scheduler_skips_task_submission_when_behind_pacing(self):
        mock_tm = MagicMock()
        mock_tm.can_accept_task.return_value = False

        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            task_manager=mock_tm,
        )
        job = ScheduledJob(
            job_id="test_pacing_job",
            name="Test Pacing Job",
            agent="codebase_auditor",
            prompt="Run audit",
            enabled=True,
            interval_seconds=3600,
        )
        scheduler.jobs["test_pacing_job"] = job

        with patch.object(scheduler, "save_state", wraps=scheduler.save_state) as mock_save:
            scheduler._execute_job(job)

            mock_tm.submit_task.assert_not_called()
            self.assertFalse(job.is_running)
            self.assertIsNotNone(job.last_run)
            self.assertIsNotNone(job.next_run)
            self.assertFalse(job.is_due())
            # Save state called exactly once when deferred
            self.assertEqual(mock_save.call_count, 1)

    def test_scheduler_loop_does_not_continuously_retrigger_deferred_jobs(self):
        """Verify deferred jobs mark execution time so _scheduler_loop does not re-trigger every 5s tick."""
        mock_tm = MagicMock()
        mock_tm.can_accept_task.return_value = False

        past_dt = datetime.now(timezone.utc) - timedelta(days=1)
        job = ScheduledJob(
            job_id="test_deferred_loop_job",
            name="Test Deferred Loop Job",
            agent="codebase_auditor",
            prompt="Run audit",
            enabled=True,
            interval_seconds=3600,
            next_run=past_dt.isoformat(),
        )
        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            task_manager=mock_tm,
            check_interval_seconds=0.05,
        )
        scheduler.jobs = {"test_deferred_loop_job": job}

        with patch.object(scheduler, "_execute_job", wraps=scheduler._execute_job) as mock_exec:
            scheduler.start()
            import time
            time.sleep(0.25)
            scheduler.stop()

            # _execute_job should be called exactly once, mark execution time, and not be continuously re-triggered
            self.assertEqual(mock_exec.call_count, 1)
            self.assertFalse(job.is_due())
            mock_tm.submit_task.assert_not_called()

    def test_atomic_file_writes_for_state_and_config(self):
        """Verify save_state and save_config use atomic file replacement without leaving leftover temp files."""
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        job = scheduler.get_job("periodic_bug_sweep")
        job.enabled = False

        scheduler.save_config()
        scheduler.save_state()

        # Check target files were correctly written
        self.assertTrue(self.config_path.exists())
        self.assertTrue(self.state_path.exists())

        with open(self.config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        saved_config_job = next(j for j in cfg if j["job_id"] == "periodic_bug_sweep")
        self.assertFalse(saved_config_job["enabled"])
        self.assertNotIn("last_run", saved_config_job)
        self.assertNotIn("next_run", saved_config_job)
        self.assertNotIn("is_running", saved_config_job)

        with open(self.state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        self.assertFalse(st["periodic_bug_sweep"]["enabled"])

        # Check no temporary files were left behind in parent directory
        tmp_files = list(self.state_path.parent.glob("*.tmp"))
        self.assertEqual(tmp_files, [])

    @patch("lib.scheduler._atomic_write_json")
    def test_save_state_and_config_release_lock_during_fsync(self, mock_write):
        """Verify save_state, save_config, and load_state release self._lock while retaining self._save_lock during file write."""
        import threading
        from lib.tasks import TaskManager
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        acquired_lock_states = []

        def fake_write(target_path, data, *args, **kwargs):
            # Verify self._lock is released so another thread can acquire it concurrently,
            # while self._save_lock remains held to serialize concurrent disk writes.
            def acquire_locks():
                got_main_lock = scheduler._lock.acquire(blocking=False)
                if got_main_lock:
                    scheduler._lock.release()

                got_save_lock = scheduler._save_lock.acquire(blocking=False)
                if got_save_lock:
                    scheduler._save_lock.release()

                acquired_lock_states.append((got_main_lock, got_save_lock))

            t = threading.Thread(target=acquire_locks)
            t.start()
            t.join()

        mock_write.side_effect = fake_write

        # Clear any fallback save results recorded during constructor initialization
        acquired_lock_states.clear()

        scheduler.save_state()
        self.assertEqual(len(acquired_lock_states), 1)
        main_lock_released, save_lock_acquired = acquired_lock_states[-1]
        self.assertTrue(main_lock_released, "Expected main scheduler._lock to be released during file write")
        self.assertFalse(save_lock_acquired, "Expected scheduler._save_lock to be retained during file write")

        acquired_lock_states.clear()

        scheduler.save_config()
        self.assertEqual(len(acquired_lock_states), 1)
        main_lock_released, save_lock_acquired = acquired_lock_states[-1]
        self.assertTrue(main_lock_released, "Expected main scheduler._lock to be released during file write")
        self.assertFalse(save_lock_acquired, "Expected scheduler._save_lock to be retained during file write")

        acquired_lock_states.clear()

        # Test load_state fallback save when state_path does not exist
        if self.state_path.exists():
            self.state_path.unlink()
        scheduler.load_state()
        self.assertEqual(len(acquired_lock_states), 1)
        main_lock_released, save_lock_acquired = acquired_lock_states[-1]
        self.assertTrue(main_lock_released, "Expected main scheduler._lock to be released during file write")
        self.assertFalse(save_lock_acquired, "Expected scheduler._save_lock to be retained during file write")

        acquired_lock_states.clear()

        # Test load_state with active TaskManager updating running states
        tm = TaskManager(max_workers=1)
        scheduler.task_manager = tm
        tm.submit_task(agent="codebase_auditor", prompt="test", target_id="sched:periodic_bug_sweep")
        scheduler.load_state()
        self.assertEqual(len(acquired_lock_states), 1)
        main_lock_released, save_lock_acquired = acquired_lock_states[-1]
        self.assertTrue(main_lock_released, "Expected main scheduler._lock to be released during file write")
        self.assertFalse(save_lock_acquired, "Expected scheduler._save_lock to be retained during file write")

    def test_save_lock_serializes_disk_writes(self):
        """Verify _save_lock serializes _atomic_write_json execution across concurrent save calls."""
        import threading
        import time
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        self.assertTrue(hasattr(scheduler, "_save_lock"))

        concurrent_writes = []
        max_concurrent_writes = 0
        counter_lock = threading.Lock()

        def slow_write(target_path, data, *args, **kwargs):
            nonlocal max_concurrent_writes
            with counter_lock:
                concurrent_writes.append(1)
                if len(concurrent_writes) > max_concurrent_writes:
                    max_concurrent_writes = len(concurrent_writes)
            time.sleep(0.01)
            with counter_lock:
                concurrent_writes.pop()

        with patch("lib.scheduler._atomic_write_json", side_effect=slow_write):
            threads = [
                threading.Thread(target=scheduler.save_state),
                threading.Thread(target=scheduler.save_config),
                threading.Thread(target=scheduler.save_state),
                threading.Thread(target=scheduler.save_config),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(max_concurrent_writes, 1)

    def test_high_concurrency_lock_decoupling(self):
        """Verify high concurrency operations on TaskScheduler do not block on disk fsync or deadlock."""
        import threading
        import time
        scheduler = TaskScheduler(config_path=self.config_path, state_path=self.state_path)
        errors = []

        def mock_slow_fsync(target_path, data, *args, **kwargs):
            time.sleep(0.005)

        with patch("lib.scheduler._atomic_write_json", side_effect=mock_slow_fsync):
            def writer():
                try:
                    for _ in range(10):
                        scheduler.save_state()
                        scheduler.save_config()
                except Exception as e:
                    errors.append(e)

            def reader():
                try:
                    for _ in range(20):
                        scheduler.get_job("periodic_bug_sweep")
                        with scheduler._lock:
                            pass
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=writer) for _ in range(3)] + [threading.Thread(target=reader) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(errors, [])



class TestIssueUtilities(unittest.TestCase):

    def test_normalize_title(self):
        self.assertEqual(_normalize_title("[Bug Sweep] [TUI] Decouple panel rendering"), "decouple panel rendering")
        self.assertEqual(_normalize_title("  [Tag1]   [Tag2]  [Tag3]  Fix memory leak  "), "fix memory leak")
        self.assertEqual(_normalize_title("[Bug Sweep] Memory leak"), "memory leak")
        self.assertEqual(_normalize_title("Simple title"), "simple title")
        self.assertEqual(_normalize_title(""), "")
        self.assertEqual(_normalize_title(None), "")
        self.assertEqual(_normalize_title(123), "")

    def test_tokenize_title(self):
        self.assertEqual(tokenize_title("[Bug Sweep] Memory leak in runner thread"), {"memory", "leak", "in", "runner", "thread"})
        self.assertEqual(tokenize_title("  [Tag1]   [Tag2]  Fix race condition  "), {"fix", "race", "condition"})
        self.assertEqual(tokenize_title(""), set())
        self.assertEqual(tokenize_title(None), set())
        self.assertEqual(tokenize_title(123), set())

    def test_pretokenize_issues(self):
        raw_issues = [
            {"number": 1, "title": "[Bug Sweep] Memory leak in runner thread"},
            {"number": 2, "title": "Unhandled exception"},
            None,
            "invalid element",
            {"number": 3, "title": None},
        ]
        pretokenized = pretokenize_issues(raw_issues)
        self.assertIn("_norm_title", pretokenized[0])
        self.assertIn("_clean_title", pretokenized[0])
        self.assertIn("_tokens", pretokenized[0])
        self.assertEqual(pretokenized[0]["_clean_title"], "memory leak in runner thread")
        self.assertEqual(pretokenized[0]["_tokens"], {"memory", "leak", "in", "runner", "thread"})
        self.assertEqual(pretokenized[1]["_tokens"], {"unhandled", "exception"})

        # Test pretokenization optimization converting list/tuple tokens to set
        list_tokens_issue = [{"title": "test issue", "tokens": ["test", "issue"]}]
        pretokenized_list = pretokenize_issues(list_tokens_issue)
        self.assertIsInstance(pretokenized_list[0]["_tokens"], set)
        self.assertEqual(pretokenized_list[0]["_tokens"], {"test", "issue"})

        # Test fallback for non-iterable tokens and non-string normalized titles
        non_iterable_tokens_issue = [{"title": "test issue", "tokens": 12345, "_norm_title": None, "_clean_title": 123}]
        pretokenized_non_iter = pretokenize_issues(non_iterable_tokens_issue)
        self.assertIsInstance(pretokenized_non_iter[0]["_tokens"], set)
        self.assertEqual(pretokenized_non_iter[0]["_tokens"], {"test", "issue"})
        self.assertEqual(pretokenized_non_iter[0]["_norm_title"], "test issue")
        self.assertEqual(pretokenized_non_iter[0]["_clean_title"], "test issue")

    def test_is_duplicate_issue(self):
        existing = [
            {"number": 1, "title": "Unhandled null pointer exception in router.py", "body": "..."},
            {"number": 2, "title": "[Bug Sweep] Memory leak in runner thread", "body": "..."},
            {"number": 3, "title": "bug", "body": "..."},
            {"number": 4, "title": "fix", "body": "..."},
            {"number": 5, "title": "tui", "body": "..."},
            {"number": 6, "title": "[Bug Sweep] [TUI] Decouple panel rendering into dedicated components", "body": "..."},
        ]
        # Exact and normalized title matching
        self.assertTrue(is_duplicate_issue("Unhandled null pointer exception in router.py", existing))
        self.assertTrue(is_duplicate_issue("[Bug Sweep] Memory leak in runner thread", existing))
        self.assertTrue(is_duplicate_issue("Memory leak in runner thread", existing))

        # Test with pre-tokenized existing issues list
        pretokenized_existing = pretokenize_issues(list(existing))
        self.assertTrue(is_duplicate_issue("Memory leak in runner thread", pretokenized_existing))
        self.assertTrue(is_duplicate_issue("[Quality Sweep] Decouple panel rendering into dedicated components", pretokenized_existing))
        self.assertFalse(is_duplicate_issue("Completely new bug report", pretokenized_existing))

        # Test with custom 'tokens' key pre-populated in issue dict (set, list, tuple)
        custom_tokens_set = [
            {"title": "Custom pretokenized issue", "tokens": {"custom", "pretokenized", "issue"}}
        ]
        self.assertTrue(is_duplicate_issue("Custom pretokenized issue", custom_tokens_set))

        custom_tokens_list = [
            {"title": "Custom pretokenized issue", "tokens": ["custom", "pretokenized", "issue"]}
        ]
        self.assertTrue(is_duplicate_issue("Custom pretokenized issue", custom_tokens_list))

        custom_tokens_tuple = [
            {"title": "Custom pretokenized issue", "_tokens": ("custom", "pretokenized", "issue")}
        ]
        self.assertTrue(is_duplicate_issue("Custom pretokenized issue", custom_tokens_tuple))

        # Test non-set tokens with non-matching title to force token similarity check without TypeError
        non_set_tokens_non_match = [
            {"title": "Custom pretokenized issue", "tokens": ["custom", "pretokenized", "issue"]}
        ]
        self.assertTrue(is_duplicate_issue("Custom pretokenized issue detailed", non_set_tokens_non_match))
        self.assertFalse(is_duplicate_issue("Unrelated issue title", non_set_tokens_non_match))

        # Test non-iterable tokens/tokens container evaluated against non-matching proposed title
        invalid_token_containers = [
            {"title": "test issue title", "tokens": 12345},
            {"title": "another test issue", "_tokens": 67890},
            {"title": "float token issue", "tokens": 12.34},
            {"title": "bool token issue", "_tokens": True},
            {"title": "none norm issue", "_norm_title": None, "_clean_title": 123},
        ]
        self.assertFalse(is_duplicate_issue("completely unrelated title", invalid_token_containers))
        self.assertTrue(is_duplicate_issue("test issue title", invalid_token_containers))

        # Test _norm_title fallback consistency
        custom_norm_issue = [
            {"title": "Raw Title", "_norm_title": "raw title"}
        ]
        self.assertTrue(is_duplicate_issue("raw title", custom_norm_issue))

        # Short-circuit test: Exact match on first item should return True immediately
        short_circuit_issues = [
            {"title": "exact match title"},
            {"title": "broken token container issue", "tokens": 12345}  # invalid token type
        ]
        self.assertTrue(is_duplicate_issue("exact match title", short_circuit_issues))

        # Multiple bracket prefix tag stripping matching
        self.assertTrue(is_duplicate_issue("Decouple panel rendering into dedicated components", existing))
        self.assertTrue(is_duplicate_issue("[Quality Sweep] Decouple panel rendering into dedicated components", existing))

        # Distinct issues
        self.assertFalse(is_duplicate_issue("Completely new bug report", existing))
        self.assertFalse(is_duplicate_issue("[Bug Sweep] Database connection timeout in query handler", existing))

        # Short open issue titles should not cause false positive matches for longer proposed titles (Issue #110)
        self.assertFalse(is_duplicate_issue("[Bug Sweep] tui: Refactor panel rendering pipeline", existing))
        self.assertFalse(is_duplicate_issue("Fix race condition in task runner loop", existing))
        self.assertFalse(is_duplicate_issue("Memory leak in background task scheduler worker", existing))

        # Jaccard token similarity boundary tests around default 0.7 threshold
        # 5 matching tokens out of 7 union tokens (5/7 ≈ 0.714 >= 0.70) -> True
        bound_ex_7 = [{"title": "word1 word2 word3 word4 word5 word6"}]
        self.assertTrue(is_duplicate_issue("word1 word2 word3 word4 word5 word7", bound_ex_7))

        # 5 matching tokens out of 8 union tokens (5/8 = 0.625 < 0.70) -> False
        self.assertFalse(is_duplicate_issue("word1 word2 word3 word4 word5 extraA extraB", bound_ex_7))

        # Exactly 70% boundary: 7 matching tokens out of 10 union tokens (7/10 = 0.70 >= 0.70) -> True
        bound_ex_10 = [{"title": "t1 t2 t3 t4 t5 t6 t7 ex1 ex2"}]
        self.assertTrue(is_duplicate_issue("t1 t2 t3 t4 t5 t6 t7 pr1", bound_ex_10))

        # Below 70% boundary: 6 matching tokens out of 11 union tokens (6/11 ≈ 0.545 < 0.70) -> False
        self.assertFalse(is_duplicate_issue("t1 t2 t3 t4 t5 t6 pr1 pr2", bound_ex_10))

        # Edge cases & defensive type handling
        self.assertFalse(is_duplicate_issue("", existing))
        self.assertFalse(is_duplicate_issue(None, existing))
        self.assertFalse(is_duplicate_issue(123, existing))
        self.assertFalse(is_duplicate_issue("Any issue title", []))
        self.assertFalse(is_duplicate_issue("Any issue title", None))
        self.assertFalse(is_duplicate_issue("Any issue title", 123))
        self.assertFalse(is_duplicate_issue("Any issue title", [{"number": 99, "body": "no title"}]))

        # Non-dict elements and invalid title values in existing_issues
        existing_with_invalid = [
            None,
            "invalid string element",
            123,
            [],
            {"title": None},
            {"title": 123},
            {"number": 1, "title": "Unhandled null pointer exception in router.py"},
        ]
        self.assertTrue(is_duplicate_issue("Unhandled null pointer exception in router.py", existing_with_invalid))
        self.assertFalse(is_duplicate_issue("Completely new bug report", existing_with_invalid))

    @patch("lib.scheduler.subprocess.run")
    def test_fetch_open_issues(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = json.dumps([{"number": 10, "title": "Periodic tasks"}])
        mock_run.return_value = mock_res

        issues = fetch_open_issues(force=True)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["number"], 10)

    @patch("lib.scheduler.subprocess.run")
    def test_fetch_open_issues_caching_and_timeout(self, mock_run):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = json.dumps([{"number": 1, "title": "Cached issue"}])
        mock_run.return_value = mock_res

        issues1 = fetch_open_issues(ttl=60.0, force=True)
        self.assertEqual(len(issues1), 1)
        self.assertEqual(mock_run.call_count, 1)

        # Subsequent call within TTL should return cached result without calling subprocess.run
        issues2 = fetch_open_issues(ttl=60.0, force=False)
        self.assertEqual(issues2, issues1)
        self.assertEqual(mock_run.call_count, 1)

    @patch("lib.scheduler.subprocess.run")
    def test_fetch_open_issues_non_zero_exit_preserves_cache(self, mock_run):
        mock_success = MagicMock(returncode=0, stdout=json.dumps([{"number": 1, "title": "Valid Issue"}]))
        mock_failure = MagicMock(returncode=1, stderr="CLI error", stdout="")
        mock_run.side_effect = [mock_success, mock_failure]

        # 1. Successful fetch populates cache
        issues1 = fetch_open_issues(force=True)
        self.assertEqual(len(issues1), 1)
        self.assertEqual(issues1[0]["title"], "Valid Issue")

        # 2. Transient failure should return cached issues instead of overwriting with empty list
        issues2 = fetch_open_issues(force=True)
        self.assertEqual(len(issues2), 1)
        self.assertEqual(issues2[0]["title"], "Valid Issue")


if __name__ == "__main__":
    unittest.main()
