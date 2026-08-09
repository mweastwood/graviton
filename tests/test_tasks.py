"""
Unit tests for lib/tasks.py (TaskManager and Task model).
"""

import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.tasks import Task, TaskManager, TaskStatus


class TestTaskManager(unittest.TestCase):

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

        # Confirm new task submission succeeds while draining and stays QUEUED (workers paused)
        task2 = manager.submit_task("code_fixer", "Fix bug #2", target_id="#2")
        self.assertIsNotNone(task2)
        self.assertEqual(task2.status, TaskStatus.QUEUED)
        time.sleep(0.2)
        # Workers must refrain from pulling new tasks while draining
        self.assertEqual(task2.status, TaskStatus.QUEUED)
        self.assertEqual(len(manager.get_queued_tasks()), 1)

        manager.stop()

    def test_task_manager_dump_and_restore_queue_state(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "test_queue_state.json"
            manager1 = TaskManager(max_workers=2)
            manager1.start()

            # Submit task t1 and wait for worker to pick up/complete it before initiating drain
            t1 = manager1.submit_task("code_reviewer", "Review PR #10", target_id="#10")
            for _ in range(50):
                if t1.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED):
                    break
                time.sleep(0.05)

            manager1.drain_active_tasks(timeout=5.0)
            t2 = manager1.submit_task("code_fixer", "Fix bug #11", target_id="#11")

            queued = manager1.get_queued_tasks()
            self.assertEqual(len(queued), 1)
            self.assertEqual(queued[0].id, t2.id)

            dumped_count = manager1.dump_queue_state(filepath=state_file)
            self.assertEqual(dumped_count, 1)
            self.assertTrue(state_file.exists())
            manager1.stop()

            # Restore into new manager instance
            manager2 = TaskManager(max_workers=2)
            restored_count = manager2.restore_queue_state(filepath=state_file)
            self.assertEqual(restored_count, 1)
            self.assertFalse(state_file.exists())

            restored_queued = manager2.get_queued_tasks()
            self.assertEqual(len(restored_queued), 1)
            self.assertEqual(restored_queued[0].id, "task-2")
            self.assertEqual(restored_queued[0].agent, "code_fixer")
            self.assertEqual(restored_queued[0].prompt, "Fix bug #11")
            self.assertEqual(restored_queued[0].target_id, "#11")

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


if __name__ == "__main__":
    unittest.main()
