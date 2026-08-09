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


if __name__ == "__main__":
    unittest.main()
