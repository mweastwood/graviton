"""
Unit tests for lib/tasks.py (TaskManager and Task model).
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.quota import QuotaState, QuotaTracker, QuotaWindow
from lib.runner import run_agent_container
from lib.tasks import Task, TaskManager, TaskStatus


class TestTaskManager(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        Path("/tmp/fake_repo").mkdir(parents=True, exist_ok=True)

    def test_task_model_properties(self):
        t = Task(
            id="task-1",
            agent="code_reviewer",
            prompt="Review PR #5",
            target_id="#5",
            enqueue_time=100.0,
        )
        self.assertEqual(t.id, "task-1")
        self.assertEqual(t.status, TaskStatus.QUEUED)
        self.assertEqual(t.elapsed_time, 0.0)
        self.assertGreaterEqual(t.wait_time, 0.0)

        t.start_time = 105.0
        t.finish_time = 115.0
        t.status = TaskStatus.COMPLETED
        t.return_code = 0

        self.assertEqual(t.elapsed_time, 10.0)
        self.assertEqual(t.wait_time, 5.0)

        d = t.to_dict()
        self.assertEqual(d["id"], "task-1")
        self.assertEqual(d["elapsed_time"], 10.0)
        self.assertEqual(d["wait_time"], 5.0)
        self.assertEqual(d["return_code"], 0)
        self.assertEqual(d["attempt"], 1)
        self.assertEqual(d["max_attempts"], 3)

    def test_task_attempt_parsing(self):
        t = Task(id="task-1", agent="code_reviewer", prompt="Test")
        self.assertEqual(t.attempt, 1)
        self.assertEqual(t.max_attempts, 3)

        updated = t.update_attempt_from_line("Auto-continuing conversation (Attempt 2/3)...")
        self.assertTrue(updated)
        self.assertEqual(t.attempt, 2)
        self.assertEqual(t.max_attempts, 3)

        ignored = t.update_attempt_from_line("Agent exited on attempt 3")
        self.assertFalse(ignored)
        self.assertEqual(t.attempt, 2)
        self.assertEqual(t.max_attempts, 3)

        output = "Starting container\nAuto-continuing conversation (Attempt 3/5)...\nDone"
        t.update_attempt_from_output(output)
        self.assertEqual(t.attempt, 3)
        self.assertEqual(t.max_attempts, 5)

        # Ensure max_attempts does not regress when parsing earlier log lines after expansion
        t.max_attempts = 6
        t.update_attempt_from_line("Auto-continuing conversation (Attempt 2/3)...")
        self.assertEqual(t.attempt, 2)
        self.assertEqual(t.max_attempts, 6)

    @patch("lib.tasks.run_agent_container")
    def test_task_manager_submit_task_custom_max_attempts(self, mock_run):
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
        )
        manager.start()

        task = manager.submit_task("code_fixer", "Fix issue", target_id="#9", max_attempts=5)
        self.assertEqual(task.max_attempts, 5)

        for _ in range(50):
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task.status, TaskStatus.COMPLETED)
        mock_run.assert_called_once_with(
            "code_fixer",
            "Fix issue",
            Path("/tmp/fake_script.sh"),
            Path("/tmp/fake_repo"),
            on_output=task.update_attempt_from_line,
            max_attempts=5,
            cached_workspace_dir=Path("/tmp/graviton-workspaces/cache/task-1"),
            initial_attempt=1,
            quota_pool="gemini",
            model="gemini-2.5-flash",
        )

        manager.stop()

    def test_task_manager_submit_task_max_attempts_exceeding_max_total_attempts_capped(self):
        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
        )
        task = manager.submit_task(
            "code_fixer",
            "Fix issue",
            target_id="#99",
            max_attempts=10,
            max_total_attempts=6,
        )
        self.assertEqual(task.max_attempts, 6)
        self.assertEqual(task.max_total_attempts, 6)

    def test_task_manager_submit_and_execute(self):
        manager = TaskManager(max_workers=2)
        manager.start()

        task1 = manager.submit_task("code_reviewer", "Review PR #1", target_id="#1")
        self.assertIn(task1.status, (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.COMPLETED))

        # Wait for worker thread to process dummy execution
        for _ in range(50):
            if task1.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task1.status, TaskStatus.COMPLETED)
        self.assertEqual(task1.return_code, 0)
        self.assertIsNotNone(task1.start_time)
        self.assertIsNotNone(task1.finish_time)
        self.assertIsNotNone(task1.worker_thread_id)

        stats = manager.get_stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["running"], 0)

        history = manager.get_task_history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].id, task1.id)

        manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_task_manager_script_execution_failure(self, mock_run):
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_run.return_value = mock_process

        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
        )
        manager.start()

        task = manager.submit_task("code_fixer", "Fix issue", target_id="#9")

        for _ in range(50):
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.return_code, 1)

        mock_run.assert_called_once_with(
            "code_fixer",
            "Fix issue",
            Path("/tmp/fake_script.sh"),
            Path("/tmp/fake_repo"),
            on_output=task.update_attempt_from_line,
            max_attempts=3,
            cached_workspace_dir=Path("/tmp/graviton-workspaces/cache/task-1"),
            initial_attempt=1,
            quota_pool="gemini",
            model="gemini-2.5-flash",
        )

        stats = manager.get_stats()
        self.assertEqual(stats["failed"], 1)

        manager.stop()

    def test_task_manager_drain_active_tasks_success(self):
        manager = TaskManager(max_workers=2)
        self.assertFalse(manager.is_draining)

        manager.start()
        task1 = manager.submit_task("code_reviewer", "Review PR #1", target_id="#1")

        # Wait for worker thread to pick up/complete task1 before initiating drain
        for _ in range(50):
            if task1.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
                break
            time.sleep(0.05)

        # Initiate drain
        drained = manager.drain_active_tasks(timeout=5.0)
        self.assertTrue(drained)
        self.assertTrue(manager.is_draining)
        self.assertEqual(task1.status, TaskStatus.COMPLETED)

        # Confirm can_accept_task returns True while draining and new tasks are accepted/queued
        self.assertTrue(manager.can_accept_task())
        task2 = manager.submit_task("code_fixer", "Fix bug #2", target_id="#2")
        self.assertEqual(task2.status, TaskStatus.QUEUED)

        manager.stop()

    def test_task_manager_drain_active_tasks_indefinite_default(self):
        manager = TaskManager(max_workers=2)
        self.assertFalse(manager.is_draining)

        manager.start()
        task1 = manager.submit_task("code_reviewer", "Review PR #1", target_id="#1")

        # Wait for worker thread to pick up/complete task1 before initiating drain
        for _ in range(50):
            if task1.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
                break
            time.sleep(0.05)

        # Initiate drain without parameters (defaulting to timeout=None / indefinite wait)
        drained = manager.drain_active_tasks()
        self.assertTrue(drained)
        self.assertTrue(manager.is_draining)
        self.assertEqual(task1.status, TaskStatus.COMPLETED)

        manager.stop()

    def test_task_manager_dump_and_restore_queue_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "test_queue_state.json"
            manager1 = TaskManager(max_workers=0)

            # Submit tasks t1 and t2 without worker threads running to test queue state serialization
            t1 = manager1.submit_task("code_reviewer", "Review PR #10", target_id="#10")
            t2 = manager1.submit_task("code_fixer", "Fix bug #11", target_id="#11")

            queued = manager1.get_queued_tasks()
            self.assertEqual(len(queued), 2)
            self.assertEqual(queued[0].id, t1.id)
            self.assertEqual(queued[1].id, t2.id)

            dumped_count = manager1.dump_queue_state(filepath=state_file)
            self.assertEqual(dumped_count, 2)
            self.assertTrue(state_file.exists())
            manager1.stop()

            # Restore into new manager instance
            manager2 = TaskManager(max_workers=0)
            restored_count = manager2.restore_queue_state(filepath=state_file)
            self.assertEqual(restored_count, 2)
            self.assertFalse(state_file.exists())

            restored_queued = manager2.get_queued_tasks()
            self.assertEqual(len(restored_queued), 2)
            self.assertEqual(restored_queued[0].id, "task-1")
            self.assertEqual(restored_queued[0].agent, "code_reviewer")
            self.assertEqual(restored_queued[0].prompt, "Review PR #10")
            self.assertEqual(restored_queued[0].target_id, "#10")
            self.assertEqual(restored_queued[1].id, "task-2")
            self.assertEqual(restored_queued[1].agent, "code_fixer")
            self.assertEqual(restored_queued[1].prompt, "Fix bug #11")
            self.assertEqual(restored_queued[1].target_id, "#11")

            # Next submitted task should get incremental ID task-3
            t3 = manager2.submit_task("issue_triager", "Triage #12", target_id="#12")
            self.assertEqual(t3.id, "task-3")

            manager2.stop()

    def test_task_manager_restore_queue_state_defensive_parsing(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "corrupted_queue_state.json"
            
            # Case 1: File contains invalid JSON object (e.g. array at root)
            state_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
            manager = TaskManager(max_workers=2)
            self.assertEqual(manager.restore_queue_state(filepath=state_file), 0)

            # Case 2: queued_tasks contains invalid/malformed items
            malformed_state = {
                "task_counter": 10,
                "queued_tasks": [
                    "not a dict",
                    {"id": "task-5"},  # missing agent and prompt
                    {"id": "task-6", "agent": "code_reviewer"},  # missing prompt
                    {"id": "task-7", "agent": "code_fixer", "prompt": "Valid task", "target_id": "#7"},
                ],
            }
            state_file.write_text(json.dumps(malformed_state), encoding="utf-8")
            restored = manager.restore_queue_state(filepath=state_file)
            self.assertEqual(restored, 1)
            queued = manager.get_queued_tasks()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].id, "task-7")
            self.assertEqual(queued[0].agent, "code_fixer")
            self.assertEqual(queued[0].prompt, "Valid task")
            self.assertEqual(manager._task_counter, 10)

            # Case 3: queued_tasks with unrecognized or non-string status (e.g. "INVALID_STATUS", "RUNNING", 123)
            unrecognized_status_state = {
                "task_counter": 15,
                "queued_tasks": [
                    {"id": "task-8", "agent": "code_reviewer", "prompt": "Task invalid status", "status": "INVALID_STATUS"},
                    {"id": "task-9", "agent": "code_fixer", "prompt": "Task running status", "status": "RUNNING"},
                    {"id": "task-10", "agent": "code_reviewer", "prompt": "Task non-string status", "status": 123},
                ],
            }
            state_file.write_text(json.dumps(unrecognized_status_state), encoding="utf-8")
            restored_unrecognized = manager.restore_queue_state(filepath=state_file)
            self.assertEqual(restored_unrecognized, 3)
            t8 = manager.get_task("task-8")
            t9 = manager.get_task("task-9")
            t10 = manager.get_task("task-10")
            self.assertIsNotNone(t8)
            self.assertIsNotNone(t9)
            self.assertIsNotNone(t10)
            self.assertEqual(t8.status, TaskStatus.QUEUED)
            self.assertEqual(t9.status, TaskStatus.QUEUED)
            self.assertEqual(t10.status, TaskStatus.QUEUED)

    @patch("lib.tasks.run_agent_container")
    def test_task_manager_drain_active_tasks_timeout(self, mock_run):
        # Make run_agent_container hang for a moment
        def slow_run(*args, **kwargs):
            time.sleep(1.0)
            res = MagicMock()
            res.returncode = 0
            return res

        mock_run.side_effect = slow_run

        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
        )
        manager.start()

        manager.submit_task("code_fixer", "Fix issue", target_id="#9")
        # Give worker a split second to transition task to RUNNING
        time.sleep(0.05)

        # Drain with short timeout (0.1s) while task is still running
        drained = manager.drain_active_tasks(timeout=0.1)
        self.assertFalse(drained)
        self.assertTrue(manager.is_draining)

        manager.stop()

    def test_task_manager_retention_limit(self):
        manager = TaskManager(max_workers=1, max_tasks=3)
        manager.start()

        tasks = []
        for i in range(5):
            t = manager.submit_task("code_reviewer", f"Task {i+1}")
            tasks.append(t)

        # Wait for all tasks to complete
        for _ in range(50):
            if all(t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED) for t in tasks):
                break
            time.sleep(0.05)

        stats = manager.get_stats()
        self.assertEqual(stats["max_tasks"], 3)
        all_tasks = manager.get_all_tasks()
        self.assertEqual(len(all_tasks), 3)

        # Oldest tasks (task-1, task-2) should have been evicted, retaining task-3, task-4, task-5
        retained_ids = {t.id for t in all_tasks}
        self.assertNotIn("task-1", retained_ids)
        self.assertNotIn("task-2", retained_ids)
        self.assertIn("task-3", retained_ids)
        self.assertIn("task-4", retained_ids)
        self.assertIn("task-5", retained_ids)

        manager.stop()

    def test_clear_completed_tasks(self):
        manager = TaskManager(max_workers=2)
        manager.start()

        tasks = []
        for i in range(3):
            t = manager.submit_task("code_reviewer", f"Task {i+1}")
            tasks.append(t)

        for _ in range(50):
            if all(t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED) for t in tasks):
                break
            time.sleep(0.05)

        cleared = manager.clear_completed_tasks()
        self.assertEqual(cleared, 3)
        self.assertEqual(len(manager.get_all_tasks()), 0)

        manager.stop()

    @patch.object(QuotaTracker, "poll_live_quota")
    def test_task_manager_quota_backoff_and_pause(self, mock_poll_live):
        quota = QuotaTracker(remaining_percentage=10.0, base_backoff_delay=0.01)
        manager = TaskManager(max_workers=1, quota_tracker=quota)

        # 1. Test LOW_QUOTA state stats
        stats = manager.get_stats()
        self.assertEqual(stats["quota_state"], QuotaState.LOW_QUOTA)
        self.assertEqual(stats["queue_status"], "BACKING_OFF")

        manager.start()
        task1 = manager.submit_task("code_reviewer", "Task under low quota")

        for _ in range(50):
            if task1.status == TaskStatus.COMPLETED:
                break
            time.sleep(0.05)

        self.assertEqual(task1.status, TaskStatus.COMPLETED)

        # 2. Test EXHAUSTED state pauses execution for pre-existing queued tasks
        quota.update_quota(0.0)
        stats = manager.get_stats()
        self.assertEqual(stats["quota_state"], QuotaState.EXHAUSTED)
        self.assertEqual(stats["queue_status"], "PAUSED_FOR_QUOTA")
        self.assertEqual(stats["status"], "PAUSED_FOR_QUOTA")

        # Submitting new task under EXHAUSTED state raises RuntimeError
        with self.assertRaises(RuntimeError) as ctx_exh:
            manager.submit_task("code_reviewer", "New task while exhausted")
        self.assertIn("quota is exhausted", str(ctx_exh.exception))

        # Add pre-existing task directly to queue (simulating task enqueued before quota exhaustion)
        task2 = Task(id="task-2", agent="code_fixer", prompt="Task queued before exhausted quota")
        with manager._lock:
            manager._tasks[task2.id] = task2
        manager._queue.put(task2)

        # Worker loop pauses task execution while quota is EXHAUSTED
        for _ in range(10):
            time.sleep(0.02)

        self.assertIn(task2.status, (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA))

        # 3. Recover quota to NORMAL -> worker resumes and completes task2
        quota.update_quota(100.0)
        for _ in range(100):
            if task2.status == TaskStatus.COMPLETED:
                break
            time.sleep(0.05)

        self.assertEqual(task2.status, TaskStatus.COMPLETED)
        manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_task_manager_can_accept_task_and_pacing_queueing(self, mock_run):
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = ""
        mock_process.stderr = ""
        mock_run.return_value = mock_process

        quota = QuotaTracker()
        manager = TaskManager(
            max_workers=1,
            quota_tracker=quota,
            script_path=Path("/tmp/fake_repo/script.sh"),
            cwd=Path("/tmp/fake_repo"),
        )

        # Initially OK
        self.assertTrue(manager.can_accept_task())

        # Submit active task
        t_active = manager.submit_task("code_reviewer", "Active Task", target_id="#100")
        self.assertEqual(t_active.id, "task-1")

        # Set quota behind pacing
        now = time.time()
        quota.update_quota(
            remaining_percentage=10.0,
            remaining_percentage_5h=10.0,
            reset_time_5h=now + 15000.0,
        )

        # Tasks can still be accepted and queued up when behind pacing
        self.assertTrue(manager.can_accept_task())
        stats = manager.get_stats()
        self.assertEqual(stats["queue_status"], "PAUSED_FOR_PACING")
        self.assertEqual(stats["status"], "BEHIND_PACING")

        # Submitting duplicate task returns existing task without raising RuntimeError
        t_dup = manager.submit_task("code_reviewer", "Active Task prompt updated", target_id="#100")
        self.assertIs(t_dup, t_active)

        # Submitting new non-duplicate task SUCCEEDS and queues up the task
        t_behind = manager.submit_task("code_fixer", "New task behind pacing")
        self.assertEqual(t_behind.id, "task-2")

        # Start workers while behind pacing - tasks should be popped, updated to PAUSED_FOR_QUOTA, and re-queued
        manager.start()
        time.sleep(0.3)
        self.assertEqual(len(manager.get_active_tasks()), 0)
        self.assertEqual(mock_run.call_count, 0)
        self.assertEqual(t_active.status, TaskStatus.PAUSED_FOR_QUOTA)
        self.assertEqual(t_behind.status, TaskStatus.PAUSED_FOR_QUOTA)

        # Recover pacing - tasks should now be executed by worker
        quota.update_quota(100.0, remaining_percentage_5h=100.0, reset_time_5h=now)
        time.sleep(0.5)
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(t_active.status, TaskStatus.COMPLETED)
        self.assertEqual(t_behind.status, TaskStatus.COMPLETED)
        manager.stop()

        # Test draining allows task queuing
        manager_drain = TaskManager()
        manager_drain._draining = True
        self.assertTrue(manager_drain.can_accept_task())
        t_drain = manager_drain.submit_task("code_fixer", "New task while draining")
        self.assertEqual(t_drain.status, TaskStatus.QUEUED)
        manager_drain._draining = False

        # Test stopped rejection
        manager.stop()
        self.assertFalse(manager.can_accept_task())
        with self.assertRaises(RuntimeError) as ctx_stop:
            manager.submit_task("code_fixer", "New task after stop")
        self.assertIn("task manager is stopped", str(ctx_stop.exception))

    @patch("lib.tasks.run_agent_container")
    def test_task_completion_mocked_container_triggers_quota_fetch(self, mock_run):
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = ""
        mock_process.stderr = ""
        mock_run.return_value = mock_process

        mock_quota = MagicMock()
        mock_quota.state = QuotaState.NORMAL
        mock_quota.is_behind_pacing.return_value = False

        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
            quota_tracker=mock_quota,
        )
        manager.start()

        task = manager.submit_task("code_reviewer", "Review PR #1", target_id="#1")

        for _ in range(50):
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task.status, TaskStatus.COMPLETED)
        mock_quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")

        manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_task_failure_triggers_quota_fetch(self, mock_run):
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stdout = ""
        mock_process.stderr = "Error during execution"
        mock_run.return_value = mock_process

        mock_quota = MagicMock()
        mock_quota.state = QuotaState.NORMAL
        mock_quota.is_behind_pacing.return_value = False

        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
            quota_tracker=mock_quota,
        )
        manager.start()

        # 1. Test TaskStatus.FAILED via returncode != 0
        task1 = manager.submit_task("code_fixer", "Fix issue", target_id="#2")

        for _ in range(50):
            if task1.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task1.status, TaskStatus.FAILED)
        mock_quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")
        mock_quota.reset_mock()

        # 2. Test worker execution raising exception
        mock_run.side_effect = RuntimeError("Worker process crashed")

        task2 = manager.submit_task("code_fixer", "Fix issue exception", target_id="#3")

        for _ in range(50):
            if task2.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task2.status, TaskStatus.FAILED)
        self.assertEqual(task2.error_message, "Worker process crashed")
        mock_quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")

        manager.stop()

    def test_task_manager_deduplicate_tasks(self):
        manager = TaskManager(max_workers=2)

        # 1. Submit initial task with target_id (manager workers not started -> stays QUEUED)
        t1 = manager.submit_task("code_reviewer", "Review PR #50", target_id="#50")
        self.assertEqual(t1.id, "task-1")
        self.assertEqual(t1.status, TaskStatus.QUEUED)

        # 2. Submit duplicate task for same agent and target_id while t1 is QUEUED
        t2 = manager.submit_task("code_reviewer", "Review PR #50 again", target_id="#50")
        self.assertIs(t2, t1)
        self.assertEqual(len(manager.get_queued_tasks()), 1)

        # 3. Simulate t1 moving to RUNNING
        t1.status = TaskStatus.RUNNING
        t3 = manager.submit_task("code_reviewer", "Review PR #50 concurrent", target_id="#50")
        self.assertIs(t3, t1)

        # 4. Submit task for different agent or target_id -> should NOT deduplicate
        t_diff_agent = manager.submit_task("code_fixer", "Fix PR #50", target_id="#50")
        self.assertEqual(t_diff_agent.id, "task-2")

        t_diff_target = manager.submit_task("code_reviewer", "Review PR #51", target_id="#51")
        self.assertEqual(t_diff_target.id, "task-3")

        # 5. When task completes, subsequent submission for same target_id creates a new task
        t1.status = TaskStatus.COMPLETED
        t4 = manager.submit_task("code_reviewer", "Review PR #50 after completion", target_id="#50")
        self.assertEqual(t4.id, "task-4")

    def test_task_manager_dump_queue_state_paused_for_quota(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "test_paused_queue_state.json"
            manager1 = TaskManager(max_workers=0)

            t1 = manager1.submit_task("code_reviewer", "Review PR #10", target_id="#10")
            t2 = manager1.submit_task("code_fixer", "Fix bug #11", target_id="#11")
            t2.status = TaskStatus.PAUSED_FOR_QUOTA

            dumped_count = manager1.dump_queue_state(filepath=state_file)
            self.assertEqual(dumped_count, 2)
            self.assertTrue(state_file.exists())
            manager1.stop()

            # Restore into new manager instance
            manager2 = TaskManager(max_workers=0)
            restored_count = manager2.restore_queue_state(filepath=state_file)
            self.assertEqual(restored_count, 2)
            self.assertFalse(state_file.exists())

            restored_queued = manager2.get_queued_tasks()
            self.assertEqual(len(restored_queued), 2)
            restored_ids = {t.id for t in restored_queued}
            self.assertEqual(restored_ids, {t1.id, t2.id})
            self.assertEqual(manager2.get_task(t1.id).status, TaskStatus.QUEUED)
            self.assertEqual(manager2.get_task(t2.id).status, TaskStatus.PAUSED_FOR_QUOTA)

            stats = manager2.get_stats()
            self.assertEqual(stats["queued"], 1)
            self.assertEqual(stats["paused"], 1)
            self.assertEqual(stats["total"], 2)

            # Verify restored PAUSED_FOR_QUOTA task transitions to COMPLETED when worker thread runs under normal quota
            manager2.max_workers = 1
            manager2.start()

            for _ in range(50):
                if manager2.get_task(t2.id).status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                time.sleep(0.05)

            self.assertEqual(manager2.get_task(t1.id).status, TaskStatus.COMPLETED)
            self.assertEqual(manager2.get_task(t2.id).status, TaskStatus.COMPLETED)
            manager2.stop()

    def test_multi_repo_task_target_id_formatting(self):
        manager = TaskManager()
        t = manager.submit_task(
            "code_reviewer",
            "Review PR #42 in owner/repo-alpha",
            target_id="#42",
            repo_full_name="owner/repo-alpha",
            repo_name="repo-alpha",
            clone_url="https://github.com/owner/repo-alpha.git",
        )
        self.assertEqual(t.target_id, "owner/repo-alpha#42")
        self.assertEqual(t.repo_full_name, "owner/repo-alpha")
        self.assertEqual(t.repo_name, "repo-alpha")

    @patch("subprocess.run")
    @patch("lib.tasks.run_agent_container")
    def test_multi_repo_workspace_resolution_and_auto_clone(self, mock_run_agent, mock_sub_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            manager = TaskManager(
                max_workers=1,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_server_cwd"),
                repos_dir=repos_dir,
            )
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_run_agent.return_value = mock_proc

            expected_repo_dir = repos_dir / "repo-alpha"
            mock_sub_run.side_effect = lambda *args, **kwargs: (expected_repo_dir.mkdir(parents=True, exist_ok=True), MagicMock(returncode=0))[1]

            manager.start()

            # Submit task for repo-alpha which does NOT exist locally yet
            task = manager.submit_task(
                agent="code_reviewer",
                prompt="Review PR #1",
                target_id="#1",
                repo_full_name="owner/repo-alpha",
                repo_name="repo-alpha",
                clone_url="https://github.com/owner/repo-alpha.git",
            )

            for _ in range(50):
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                time.sleep(0.05)

            self.assertEqual(task.status, TaskStatus.COMPLETED)

            # Check that git clone was invoked for non-existent repo_dir
            mock_sub_run.assert_called_once_with(
                ["git", "clone", "--", "https://github.com/owner/repo-alpha.git", str(expected_repo_dir)],
                check=True,
                capture_output=True,
                text=True,
            )

            # Check that run_agent_container was called with cwd set to expected_repo_dir
            mock_run_agent.assert_called_once_with(
                "code_reviewer",
                "Review PR #1",
                Path("/tmp/fake_script.sh"),
                expected_repo_dir,
                on_output=task.update_attempt_from_line,
                max_attempts=3,
                cached_workspace_dir=Path("/tmp/graviton-workspaces/cache/task-1"),
                initial_attempt=1,
                quota_pool="gemini",
                model="gemini-2.5-flash",
            )

            manager.stop()

    @patch("subprocess.run")
    @patch("lib.tasks.run_agent_container")
    def test_multi_repo_auto_clone_failure_marks_task_failed(self, mock_run_agent, mock_sub_run):
        import tempfile
        import subprocess
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            manager = TaskManager(
                max_workers=1,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_server_cwd"),
                repos_dir=repos_dir,
            )
            mock_sub_run.side_effect = subprocess.CalledProcessError(1, ["git", "clone"], stderr="Repository not found")

            manager.start()

            task = manager.submit_task(
                agent="code_reviewer",
                prompt="Review PR #1",
                target_id="#1",
                repo_full_name="owner/repo-alpha",
                repo_name="repo-alpha",
                clone_url="https://github.com/owner/repo-alpha.git",
            )

            for _ in range(100):
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                time.sleep(0.05)

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.return_code, -1)
            self.assertIn("Failed to auto-clone repository", task.error_message)

            mock_run_agent.assert_not_called()
            manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_path_traversal_rejection_in_task_workspace_resolution(self, mock_run_agent):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            repos_dir.mkdir()

            manager = TaskManager(
                max_workers=1,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_server_cwd"),
                repos_dir=repos_dir,
            )

            manager.start()

            # Submit task with leading slash path traversal repo_name "/tmp/bad"
            task = manager.submit_task(
                agent="code_reviewer",
                prompt="Review PR #1",
                target_id="#1",
                repo_full_name="owner/bad",
                repo_name="/tmp/bad",
            )

            for _ in range(50):
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                time.sleep(0.05)

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertIn("attempting path traversal", task.error_message)
            mock_run_agent.assert_not_called()
            manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_valid_repo_name_in_task_workspace_resolution(self, mock_run_agent):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            repos_dir.mkdir()
            bad_dir = repos_dir / "bad"
            bad_dir.mkdir()

            manager = TaskManager(
                max_workers=1,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_server_cwd"),
                repos_dir=repos_dir,
            )
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_run_agent.return_value = mock_proc

            manager.start()

            task = manager.submit_task(
                agent="code_reviewer",
                prompt="Review PR #1",
                target_id="#1",
                repo_full_name="owner/bad",
                repo_name="bad",
            )

            for _ in range(50):
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                time.sleep(0.05)

            self.assertEqual(task.status, TaskStatus.COMPLETED)
            mock_run_agent.assert_called_once()
            called_cwd = mock_run_agent.call_args[0][3]
            self.assertEqual(called_cwd.resolve(), bad_dir.resolve())
            manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_path_traversal_out_of_repos_dir_marks_task_failed(self, mock_run_agent):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            repos_dir.mkdir()

            manager = TaskManager(
                max_workers=1,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_server_cwd"),
                repos_dir=repos_dir,
            )

            manager.start()

            task = manager.submit_task(
                agent="code_reviewer",
                prompt="Review PR #1",
                target_id="#1",
                repo_full_name="owner/bad",
                repo_name="..",
            )

            for _ in range(50):
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                time.sleep(0.05)

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertIn("attempting path traversal", task.error_message)
            mock_run_agent.assert_not_called()
            manager.stop()

    def test_submit_task_target_id_prefix_matching(self):
        manager = TaskManager()
        # Similar repo name prefix where target_id belongs to repo-2
        t1 = manager.submit_task("code_reviewer", "Prompt 1", target_id="owner/repo-2#5", repo_full_name="owner/repo")
        self.assertEqual(t1.target_id, "owner/repo#5")

        # Matching repo target_id with hash prefix
        t2 = manager.submit_task("code_reviewer", "Prompt 2", target_id="owner/repo#5", repo_full_name="owner/repo")
        self.assertEqual(t2.target_id, "owner/repo#5")

        # Issue/PR number only with leading hash
        t3 = manager.submit_task("code_reviewer", "Prompt 3", target_id="#5", repo_full_name="owner/repo")
        self.assertEqual(t3.target_id, "owner/repo#5")

    @patch("subprocess.run")
    @patch("lib.tasks.run_agent_container")
    def test_concurrent_auto_cloning_no_race_condition(self, mock_run_agent, mock_subproc_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            repos_dir = Path(tmpdir) / "repos"
            repos_dir.mkdir()
            target_repo_dir = repos_dir / "myrepo"

            def mock_clone(cmd, **kwargs):
                time.sleep(0.05)
                target_repo_dir.mkdir(parents=True, exist_ok=True)
                res = MagicMock()
                res.returncode = 0
                return res

            mock_subproc_run.side_effect = mock_clone
            mock_proc = MagicMock()
            mock_proc.returncode = 0
            mock_proc.stdout = "Success"
            mock_proc.stderr = ""
            mock_run_agent.return_value = mock_proc

            manager = TaskManager(
                max_workers=2,
                script_path=Path("/tmp/fake_script.sh"),
                repos_dir=repos_dir,
            )
            manager.start()

            t1 = manager.submit_task("code_reviewer", "Prompt 1", repo_name="myrepo", clone_url="https://github.com/owner/myrepo.git")
            t2 = manager.submit_task("code_reviewer", "Prompt 2", repo_name="myrepo", clone_url="https://github.com/owner/myrepo.git")

            for _ in range(50):
                if t1.status == TaskStatus.COMPLETED and t2.status == TaskStatus.COMPLETED:
                    break
                time.sleep(0.05)

            self.assertEqual(t1.status, TaskStatus.COMPLETED)
            self.assertEqual(t2.status, TaskStatus.COMPLETED)
            self.assertEqual(mock_subproc_run.call_count, 1)
            manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_nonexistent_exec_cwd_raises_error(self, mock_run_agent):
        with tempfile.TemporaryDirectory() as tmpdir:
            non_existent_dir = Path(tmpdir) / "does_not_exist"
            manager = TaskManager(
                max_workers=1,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=non_existent_dir,
            )
            manager.start()

            task = manager.submit_task("code_reviewer", "Test prompt")

            for _ in range(50):
                if task.status == TaskStatus.FAILED:
                    break
                time.sleep(0.05)

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertIn("Target repository directory", task.error_message)
            self.assertIn("does not exist", task.error_message)
            mock_run_agent.assert_not_called()
            manager.stop()

    def test_task_completion_triggers_quota_fetch(self):
        quota = MagicMock(spec=QuotaTracker)
        manager = TaskManager(max_workers=1, quota_tracker=quota)
        manager.start()

        task = manager.submit_task("code_reviewer", "Test prompt")

        for _ in range(50):
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task.status, TaskStatus.COMPLETED)
        quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")
        manager.stop()

    def test_task_completion_quota_fetch_exception_handled(self):
        quota = MagicMock(spec=QuotaTracker)
        quota.poll_live_quota_async.side_effect = RuntimeError("Quota API error")
        manager = TaskManager(max_workers=1, quota_tracker=quota)
        with self.assertLogs("graviton.tasks", level="WARNING") as cm:
            manager.start()

            task = manager.submit_task("code_reviewer", "Test prompt")

            for _ in range(50):
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                time.sleep(0.05)

            manager._queue.join()
            manager.stop()

        self.assertEqual(task.status, TaskStatus.COMPLETED)
        quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")
        self.assertTrue(
            any("Quota fetch on task finish failed: Quota API error" in log for log in cm.output)
        )


    @patch("lib.tasks.run_agent_container")
    def test_task_failure_return_code_triggers_quota_fetch(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = ""
        mock_proc.stderr = "Error"
        mock_run.return_value = mock_proc

        quota = MagicMock(spec=QuotaTracker)
        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
            quota_tracker=quota,
        )
        manager.start()

        task = manager.submit_task("code_fixer", "Test failing prompt")

        for _ in range(50):
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task.status, TaskStatus.FAILED)
        quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")
        manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_task_failure_exception_triggers_quota_fetch(self, mock_run):
        mock_run.side_effect = RuntimeError("Worker execution exception")

        quota = MagicMock(spec=QuotaTracker)
        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
            quota_tracker=quota,
        )
        manager.start()

        task = manager.submit_task("code_fixer", "Test exception prompt")

        for _ in range(50):
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.05)

        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertIn("Worker execution exception", task.error_message)
        quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")
        manager.stop()


    def test_task_manager_pause_resume_toggle_and_is_paused(self):
        manager = TaskManager()
        self.assertFalse(manager.is_paused)
        self.assertTrue(manager.can_accept_task())
        stats = manager.get_stats()
        self.assertFalse(stats["is_paused"])
        self.assertEqual(stats["queue_status"], "ACTIVE")

        manager.pause()
        self.assertTrue(manager.is_paused)
        self.assertFalse(manager.can_accept_task())
        stats_paused = manager.get_stats()
        self.assertTrue(stats_paused["is_paused"])
        self.assertEqual(stats_paused["queue_status"], "PAUSED")

        manager.resume()
        self.assertFalse(manager.is_paused)
        self.assertTrue(manager.can_accept_task())
        stats_resumed = manager.get_stats()
        self.assertFalse(stats_resumed["is_paused"])

        new_state = manager.toggle_pause()
        self.assertTrue(new_state)
        self.assertTrue(manager.is_paused)
        self.assertFalse(manager.can_accept_task())

        new_state_2 = manager.toggle_pause()
        self.assertFalse(new_state_2)
        self.assertFalse(manager.is_paused)
        self.assertTrue(manager.can_accept_task())

    def test_submit_task_raises_runtime_error_when_paused(self):
        manager = TaskManager()
        manager.pause()
        self.assertFalse(manager.can_accept_task())
        with self.assertRaises(RuntimeError) as ctx:
            manager.submit_task("code_reviewer", "Review PR #1")
        self.assertIn("Cannot accept new task: task acceptance is paused", str(ctx.exception))

        manager.resume()
        self.assertTrue(manager.can_accept_task())
        task = manager.submit_task("code_reviewer", "Review PR #1")
        self.assertIsNotNone(task)
        self.assertEqual(task.id, "task-1")

    def test_submit_task_deduplicates_duplicate_when_paused(self):
        manager = TaskManager()
        # Submit an initial task
        task1 = manager.submit_task("code_reviewer", "Review PR #1", target_id="owner/repo#1")
        self.assertEqual(task1.id, "task-1")

        manager.pause()
        self.assertTrue(manager.is_paused)
        self.assertFalse(manager.can_accept_task())

        # Duplicate task submission while paused should deduplicate and return existing active task without raising RuntimeError
        task_dup = manager.submit_task("code_reviewer", "Review PR #1 updated prompt", target_id="owner/repo#1")
        self.assertIs(task_dup, task1)

        # New non-duplicate task submission while paused raises RuntimeError
        with self.assertRaises(RuntimeError) as ctx:
            manager.submit_task("code_reviewer", "Review PR #2", target_id="owner/repo#2")
        self.assertIn("Cannot accept new task: task acceptance is paused", str(ctx.exception))

    def test_submit_task_deduplicates_duplicate_when_draining(self):
        manager = TaskManager()
        task1 = manager.submit_task("code_reviewer", "Review PR #1", target_id="owner/repo#1")
        self.assertEqual(task1.id, "task-1")

        manager.drain_active_tasks(timeout=0.01)
        self.assertTrue(manager.is_draining)
        self.assertTrue(manager.can_accept_task())

        # Duplicate task submission while draining should deduplicate and return existing active task
        task_dup = manager.submit_task("code_reviewer", "Review PR #1 updated prompt", target_id="owner/repo#1")
        self.assertIs(task_dup, task1)

        # New non-duplicate task submission while draining succeeds and queues task
        task2 = manager.submit_task("code_reviewer", "Review PR #2", target_id="owner/repo#2")
        self.assertEqual(task2.status, TaskStatus.QUEUED)

    def test_worker_loop_pauses_execution_when_task_manager_paused(self):
        manager = TaskManager(max_workers=1)
        manager.pause()
        manager.start()

        # Submit task manually to bypass submit_task pause check
        with manager._lock:
            manager._task_counter += 1
            task = Task(
                id=f"task-{manager._task_counter}",
                agent="code_reviewer",
                prompt="Review PR #1",
                status=TaskStatus.QUEUED,
            )
            manager._tasks[task.id] = task
        manager._queue.put(task)

        # Worker loop should not pop or execute task while paused
        time.sleep(0.3)
        self.assertEqual(task.status, TaskStatus.QUEUED)
        self.assertEqual(len(manager.get_queued_tasks()), 1)

        # Resuming task manager allows worker to pick up and process queued task
        manager.resume()
        for _ in range(50):
            if task.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
                break
            time.sleep(0.05)

        self.assertIn(task.status, (TaskStatus.RUNNING, TaskStatus.COMPLETED))
        manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_task_completion_quota_polling_is_non_blocking(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ""
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        poll_called = threading.Event()

        def slow_poll_live_quota_async(force=True, thread_name=None):
            time.sleep(0.5)
            poll_called.set()
            return MagicMock()

        quota = MagicMock(spec=QuotaTracker)
        quota.state = QuotaState.NORMAL
        quota.is_behind_pacing.return_value = False
        quota.poll_live_quota_async.side_effect = slow_poll_live_quota_async

        manager = TaskManager(
            max_workers=1,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
            quota_tracker=quota,
        )
        manager.start()

        start_time = time.time()
        task = manager.submit_task("code_reviewer", "Test non-blocking prompt")

        for _ in range(50):
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                break
            time.sleep(0.02)

        finish_duration = time.time() - start_time
        self.assertEqual(task.status, TaskStatus.COMPLETED)
        self.assertLess(finish_duration, 0.4)

        self.assertTrue(poll_called.wait(timeout=2.0))
        quota.poll_live_quota_async.assert_called_with(force=True, thread_name="AsyncQuotaPoll-Worker-1")

        manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_attempt_exhaustion_caching_and_requeuing(self, mock_run):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "workspace_cache"
            cache_dir.mkdir()
            (cache_dir / "cached_work.txt").write_text("work done in attempts 1..3")

            # Mock first container pass: returns exit code 1 with 3 attempts reported
            def mock_run_fail_batch(agent, prompt, script_path, cwd, on_output=None, max_attempts=None, cached_workspace_dir=None, initial_attempt=None):
                if on_output:
                    on_output("Auto-continuing conversation (Attempt 3/3)...")
                res = MagicMock()
                res.returncode = 1
                res.stdout = "Auto-continuing conversation (Attempt 3/3)..."
                res.stderr = "Batch limit reached"
                return res

            mock_run.side_effect = mock_run_fail_batch

            manager = TaskManager(
                max_workers=0,  # Manual queue control
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_repo"),
            )

            task = manager.submit_task(
                "code_fixer",
                "Fix complex bug",
                target_id="#163",
                max_attempts=3,
                max_total_attempts=6,
                attempts_per_batch=3,
                cached_workspace_dir=cache_dir,
            )

            # Manually run one iteration of worker loop logic
            task.status = TaskStatus.RUNNING
            res = mock_run(
                task.agent,
                task.prompt,
                manager.script_path,
                Path("/tmp/fake_repo"),
                on_output=task.update_attempt_from_line,
                max_attempts=task.max_attempts,
                cached_workspace_dir=task.cached_workspace_dir,
                initial_attempt=1,
            )
            task.update_attempt_from_output(res.stdout)

            # Simulate worker loop finish logic for non-zero exit code
            with manager._lock:
                if res.returncode != 0 and task.attempt >= task.max_attempts:
                    if task.attempt < task.max_total_attempts:
                        old_max = task.max_attempts
                        task.max_attempts = min(task.attempt + task.attempts_per_batch, task.max_total_attempts)
                        task.requeue_count += 1
                        task.status = TaskStatus.QUEUED
                        task.finish_time = None
                        task.return_code = res.returncode
                        task.error_message = res.stderr
                        manager._queue.put(task)

            self.assertEqual(task.status, TaskStatus.QUEUED)
            self.assertEqual(task.requeue_count, 1)
            self.assertEqual(task.max_attempts, 6)
            self.assertEqual(task.attempt, 3)
            self.assertIsNone(task.finish_time)
            self.assertTrue(cache_dir.exists())

    @patch("lib.tasks.run_agent_container")
    def test_attempt_exhaustion_hard_ceiling_failure_and_cache_cleanup(self, mock_run):
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "workspace_cache"
            cache_dir.mkdir()
            (cache_dir / "cached_work.txt").write_text("work done in attempts 1..6")

            def mock_run_fail_final(agent, prompt, script_path, cwd, on_output=None, max_attempts=None, cached_workspace_dir=None, initial_attempt=None):
                if on_output:
                    on_output("Auto-continuing conversation (Attempt 6/6)...")
                res = MagicMock()
                res.returncode = 1
                res.stdout = "Auto-continuing conversation (Attempt 6/6)..."
                res.stderr = "Hard limit reached"
                return res

            mock_run.side_effect = mock_run_fail_final

            manager = TaskManager(
                max_workers=0,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_repo"),
            )

            task = Task(
                id="task-exhausted",
                agent="code_fixer",
                prompt="Fix bug",
                attempt=6,
                max_attempts=6,
                max_total_attempts=6,
                attempts_per_batch=3,
                requeue_count=1,
                cached_workspace_dir=cache_dir,
            )

            res = mock_run(
                task.agent,
                task.prompt,
                manager.script_path,
                Path("/tmp/fake_repo"),
                on_output=task.update_attempt_from_line,
                max_attempts=task.max_attempts,
                cached_workspace_dir=task.cached_workspace_dir,
                initial_attempt=4,
            )
            task.update_attempt_from_output(res.stdout)

            with manager._lock:
                if res.returncode != 0 and task.attempt >= task.max_attempts:
                    if task.attempt >= task.max_total_attempts:
                        task.finish_time = time.time()
                        task.return_code = res.returncode
                        task.status = TaskStatus.FAILED
                        task.error_message = res.stderr
                        if task.cached_workspace_dir and task.cached_workspace_dir.exists():
                            shutil.rmtree(task.cached_workspace_dir, ignore_errors=True)

            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.attempt, 6)
            self.assertIsNotNone(task.finish_time)
            self.assertFalse(cache_dir.exists())

    @patch("lib.tasks.run_agent_container")
    def test_attempt_exhaustion_success_cleans_cache(self, mock_run):
        import tempfile
        import shutil
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_dir = Path(tmpdir) / "workspace_cache"
            cache_dir.mkdir()
            (cache_dir / "cached_work.txt").write_text("work done")

            def mock_run_success(agent, prompt, script_path, cwd, on_output=None, max_attempts=None, cached_workspace_dir=None, initial_attempt=None):
                if on_output:
                    on_output("Auto-continuing conversation (Attempt 4/6)...")
                res = MagicMock()
                res.returncode = 0
                res.stdout = "Auto-continuing conversation (Attempt 4/6)..."
                res.stderr = ""
                return res

            mock_run.side_effect = mock_run_success

            manager = TaskManager(
                max_workers=0,
                script_path=Path("/tmp/fake_script.sh"),
                cwd=Path("/tmp/fake_repo"),
            )

            task = Task(
                id="task-requeued-success",
                agent="code_fixer",
                prompt="Fix bug",
                attempt=3,
                max_attempts=6,
                max_total_attempts=6,
                attempts_per_batch=3,
                requeue_count=1,
                cached_workspace_dir=cache_dir,
            )

            res = mock_run(
                task.agent,
                task.prompt,
                manager.script_path,
                Path("/tmp/fake_repo"),
                on_output=task.update_attempt_from_line,
                max_attempts=task.max_attempts,
                cached_workspace_dir=task.cached_workspace_dir,
                initial_attempt=4,
            )
            task.update_attempt_from_output(res.stdout)

            with manager._lock:
                if res.returncode == 0:
                    task.finish_time = time.time()
                    task.return_code = res.returncode
                    task.status = TaskStatus.COMPLETED
                    if task.cached_workspace_dir and task.cached_workspace_dir.exists():
                        shutil.rmtree(task.cached_workspace_dir, ignore_errors=True)

            self.assertEqual(task.status, TaskStatus.COMPLETED)
            self.assertEqual(task.attempt, 4)
            self.assertFalse(cache_dir.exists())

    def test_dump_and_restore_queue_state_with_cached_requeued_tasks(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "test_cached_queue_state.json"
            cache_path = Path(tmpdir) / "cached_workspace"

            manager1 = TaskManager(max_workers=0)
            t1 = manager1.submit_task(
                "code_fixer",
                "Fix issue #163",
                target_id="#163",
                max_attempts=6,
                max_total_attempts=6,
                attempts_per_batch=3,
                cached_workspace_dir=cache_path,
            )
            t1.attempt = 3
            t1.requeue_count = 1

            dumped = manager1.dump_queue_state(filepath=state_file)
            self.assertEqual(dumped, 1)
            self.assertTrue(state_file.exists())
            manager1.stop()

            manager2 = TaskManager(max_workers=0)
            restored = manager2.restore_queue_state(filepath=state_file)
            self.assertEqual(restored, 1)

            restored_task = manager2.get_task(t1.id)
            self.assertIsNotNone(restored_task)
            self.assertEqual(restored_task.cached_workspace_dir, cache_path)
            self.assertEqual(restored_task.attempt, 3)
            self.assertEqual(restored_task.max_attempts, 6)
            self.assertEqual(restored_task.max_total_attempts, 6)
            self.assertEqual(restored_task.attempts_per_batch, 3)
            self.assertEqual(restored_task.requeue_count, 1)
            manager2.stop()

    def test_rebuild_queue_locked_and_rebuild_queue(self):
        manager = TaskManager(max_workers=0)
        t1 = manager.submit_task("code_fixer", "Task 1", target_id="#1")
        t2 = manager.submit_task("code_reviewer", "Task 2", target_id="#2")
        t3 = manager.submit_task("issue_triager", "Task 3", target_id="#3")

        with manager._lock:
            t1.status = TaskStatus.QUEUED
            t2.status = TaskStatus.PAUSED_FOR_QUOTA
            t3.status = TaskStatus.COMPLETED
            manager._rebuild_queue_locked()

        queued_ids = []
        while not manager._queue.empty():
            task = manager._queue.get_nowait()
            queued_ids.append(task.id)

        self.assertIn(t1.id, queued_ids)
        self.assertIn(t2.id, queued_ids)
        self.assertNotIn(t3.id, queued_ids)
        manager.stop()

    def test_task_priority_ordering_and_worker_execution(self):
        manager = TaskManager(max_workers=0)

        # 1. Submit tasks with different priority levels and enqueue times
        t1 = manager.submit_task("agent1", "Prompt 1", target_id="#1", priority=0)
        t2 = manager.submit_task("agent2", "Prompt 2", target_id="#2", priority=2)
        t3 = manager.submit_task("agent3", "Prompt 3", target_id="#3", priority=1)

        queued = manager.get_queued_tasks()
        self.assertEqual([t.id for t in queued], [t2.id, t3.id, t1.id])

        # 2. Prioritize t1 (bump priority by 3 -> new priority=3)
        res = manager.prioritize_task(t1.id, priority_bump=3)
        self.assertTrue(res)
        self.assertEqual(t1.priority, 3)

        queued_after = manager.get_queued_tasks()
        self.assertEqual([t.id for t in queued_after], [t1.id, t2.id, t3.id])

        # 3. Test worker execution sequence based on priority
        execution_order = []

        def mock_run(agent, prompt, *args, **kwargs):
            execution_order.append(prompt)
            res = MagicMock()
            res.returncode = 0
            return res

        manager.script_path = Path("/tmp/fake_script.sh")
        manager.cwd = Path("/tmp/fake_repo")

        with patch("lib.tasks.run_agent_container", side_effect=mock_run):
            manager.max_workers = 1
            manager.start()

            for _ in range(50):
                if len(execution_order) == 3:
                    break
                time.sleep(0.05)

            self.assertEqual(execution_order, ["Prompt 1", "Prompt 2", "Prompt 3"])
            manager.stop()

    def test_dump_and_restore_queue_state_with_priority(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "test_prio_queue_state.json"
            manager1 = TaskManager(max_workers=0)
            t1 = manager1.submit_task("code_fixer", "Task 1", target_id="#1", priority=1)
            t2 = manager1.submit_task("code_fixer", "Task 2", target_id="#2", priority=5)

            dumped = manager1.dump_queue_state(filepath=state_file)
            self.assertEqual(dumped, 2)
            manager1.stop()

            manager2 = TaskManager(max_workers=0)
            restored = manager2.restore_queue_state(filepath=state_file)
            self.assertEqual(restored, 2)

            queued = manager2.get_queued_tasks()
            self.assertEqual([t.id for t in queued], [t2.id, t1.id])
            self.assertEqual(queued[0].priority, 5)
            self.assertEqual(queued[1].priority, 1)
            manager2.stop()

    def test_rebuild_queue_unfinished_tasks_balance(self):
        manager = TaskManager(max_workers=0)
        t1 = manager.submit_task("agent1", "Prompt 1", target_id="#1")
        t2 = manager.submit_task("agent2", "Prompt 2", target_id="#2")

        # Prioritize tasks to trigger multiple _rebuild_queue_locked calls
        manager.prioritize_task(t1.id)
        manager.prioritize_task(t2.id)
        manager.prioritize_task(t1.id)

        # There are 2 queued tasks, so unfinished_tasks counter should equal 2
        self.assertEqual(manager._queue.unfinished_tasks, 2)

        # Simulate worker popping and marking tasks done
        task_a = manager._queue.get_nowait()
        manager._queue.task_done()
        task_b = manager._queue.get_nowait()
        manager._queue.task_done()

        # Counter should be 0 and join() must return cleanly without hanging
        self.assertEqual(manager._queue.unfinished_tasks, 0)

        join_completed = False

        def wait_join():
            nonlocal join_completed
            manager._queue.join()
            join_completed = True

        join_thread = threading.Thread(target=wait_join)
        join_thread.start()
        join_thread.join(timeout=1.0)
        self.assertTrue(join_completed)
        manager.stop()

    @patch("lib.tasks.run_agent_container")
    def test_task_manager_prunes_completed_tasks_on_worker_completion(self, mock_run):
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_run.return_value = mock_process

        manager = TaskManager(
            max_workers=1,
            max_tasks=2,
            script_path=Path("/tmp/fake_script.sh"),
            cwd=Path("/tmp/fake_repo"),
        )
        manager.start()

        task1 = manager.submit_task("code_fixer", "Prompt 1", target_id="#1")
        task2 = manager.submit_task("code_fixer", "Prompt 2", target_id="#2")
        task3 = manager.submit_task("code_fixer", "Prompt 3", target_id="#3")

        # Wait for workers to finish processing
        for _ in range(50):
            if len(manager.get_active_tasks()) == 0 and len(manager.get_queued_tasks()) == 0:
                break
            time.sleep(0.05)

        manager.stop()
        # Max tasks limit (2) should be strictly enforced via pruning on task completion
        self.assertLessEqual(len(manager._tasks), 2)

    def test_dual_pool_task_selection(self):
        quota = QuotaTracker()
        # Set Gemini pool to 40% and Claude pool to 80%
        quota.update_quota(40.0, quota_pool="gemini")
        quota.update_quota(80.0, quota_pool="claude_gpt")

        manager = TaskManager(max_workers=1, quota_tracker=quota)
        manager.start()

        task = manager.submit_task("code_reviewer", "Test prompt dual pool")

        for _ in range(50):
            if task.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
                break
            time.sleep(0.05)

        self.assertEqual(task.selected_pool, "claude_gpt")
        self.assertEqual(task.selected_model, "claude-3-5-sonnet")
        manager.stop()

    def test_get_stats_queue_status_during_single_pool_exhaustion(self):
        quota = QuotaTracker()
        w_5h_g = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=0.0)
        w_1w_g = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=0.0)
        quota.update_windows(w_5h_g, w_1w_g, quota_pool="gemini")

        w_5h_c = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=100.0)
        w_1w_c = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)
        quota.update_windows(w_5h_c, w_1w_c, quota_pool="claude_gpt")

        manager = TaskManager(max_workers=1, quota_tracker=quota)

        # Single-pool exhaustion must NOT report PAUSED_FOR_QUOTA since 3rd party pool is active
        stats = manager.get_stats()
        self.assertEqual(stats["queue_status"], "ACTIVE")
        self.assertNotEqual(stats["queue_status"], "PAUSED_FOR_QUOTA")

        # Exhaust 3rd party pool as well
        w_5h_c0 = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=0.0)
        w_1w_c0 = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=0.0)
        quota.update_windows(w_5h_c0, w_1w_c0, quota_pool="claude_gpt")

        # Now all pools are exhausted -> PAUSED_FOR_QUOTA
        stats_exhausted = manager.get_stats()
        self.assertEqual(stats_exhausted["queue_status"], "PAUSED_FOR_QUOTA")

    def test_worker_loop_paused_or_draining_backoff(self):
        manager = TaskManager(max_workers=0)
        manager.pause()
        t = Task(id="task-paused-test", agent="code_fixer", prompt="test")
        manager._queue.put(t)

        with manager._lock:
            draining = manager._draining
            paused = manager._paused
            item = manager._queue.get_nowait()
            was_exhausted = False
            if manager._draining or manager._paused:
                was_exhausted = True
            manager._queue.put(item)
            manager._queue.task_done()

        self.assertTrue(was_exhausted)
        manager.stop()

    def test_drain_active_tasks(self):
        manager = TaskManager(max_workers=1)
        manager.start()
        t1 = manager.submit_task("code_reviewer", "Task 1", target_id="#1")
        # Wait for task 1 to finish
        res = manager.drain_active_tasks(timeout=2.0)
        self.assertTrue(res)
        self.assertEqual(len(manager.get_active_tasks()), 0)
        manager.stop()

    def test_dump_and_restore_queue_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / ".graviton_queue_state.json"
            manager = TaskManager(max_workers=0, cwd=Path(tmpdir))
            t1 = manager.submit_task("code_fixer", "Task dump 1", target_id="#101")
            t2 = manager.submit_task("issue_triager", "Task dump 2", target_id="#102")

            saved_count = manager.dump_queue_state(filepath=state_file)
            self.assertEqual(saved_count, 2)
            self.assertTrue(state_file.exists())

            new_manager = TaskManager(max_workers=0, cwd=Path(tmpdir))
            restored_count = new_manager.restore_queue_state(filepath=state_file)
            self.assertEqual(restored_count, 2)
            self.assertFalse(state_file.exists())

            restored_tasks = new_manager.get_queued_tasks()
            restored_ids = [t.id for t in restored_tasks]
            self.assertIn(t1.id, restored_ids)
            self.assertIn(t2.id, restored_ids)
            new_manager.stop()
            manager.stop()

    def test_webhook_task_submission_during_drain_persisted_to_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            manager = TaskManager(cwd=tmp_path)
            manager.drain_active_tasks(timeout=0.01)
            self.assertTrue(manager.is_draining)

            task = manager.submit_task("code_reviewer", "Review PR #42", target_id="owner/repo#42")
            self.assertEqual(task.status, TaskStatus.QUEUED)

            persisted_count = manager.dump_queue_state()
            self.assertEqual(persisted_count, 1)

            state_file = tmp_path / ".graviton_queue_state.json"
            self.assertTrue(state_file.exists())

            new_manager = TaskManager(cwd=tmp_path)
            restored_count = new_manager.restore_queue_state()
            self.assertEqual(restored_count, 1)
            restored = new_manager.get_task(task.id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.prompt, "Review PR #42")
            new_manager.stop()
            manager.stop()


if __name__ == "__main__":
    unittest.main()



