"""
Unit tests for ScheduledJob and TaskScheduler repository cycling (Issue #204).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from lib.scheduler import ScheduledJob, TaskScheduler


class TestSchedulerRepoCycling(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.temp_dir.name)
        self.config_path = self.root_path / "config.json"
        self.state_path = self.root_path / "state.json"
        self.repos_dir = self.root_path / "repos"
        self.repos_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scheduled_job_fields_default_and_serialization(self):
        """Test default values and serialization for repo_cycling and current_repo_index."""
        job = ScheduledJob(
            job_id="test_job",
            name="Test Job",
            interval_seconds=3600,
            agent="codebase_auditor",
            prompt="Audit code",
        )
        self.assertFalse(job.repo_cycling)
        self.assertEqual(job.current_repo_index, 0)

        # to_config_dict
        cfg = job.to_config_dict()
        self.assertIn("repo_cycling", cfg)
        self.assertFalse(cfg["repo_cycling"])

        # Enable repo_cycling
        job.repo_cycling = True
        job.current_repo_index = 2
        data = job.to_dict()
        self.assertTrue(data["repo_cycling"])
        self.assertEqual(data["current_repo_index"], 2)

        restored = ScheduledJob.from_dict(data)
        self.assertTrue(restored.repo_cycling)
        self.assertEqual(restored.current_repo_index, 2)

    def test_pick_next_repo_round_robin_cycling(self):
        """Test that _pick_next_repo cycles through available repos in alphabetical order and wraps around."""
        # Create repo directories
        (self.repos_dir / "repo_c").mkdir()
        (self.repos_dir / "repo_a").mkdir()
        (self.repos_dir / "repo_b").mkdir()
        # Create non-directory file and hidden directory to ensure they are ignored
        (self.repos_dir / "some_file.txt").write_text("hello")
        (self.repos_dir / ".git").mkdir()

        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=self.repos_dir,
        )

        job = ScheduledJob(
            job_id="sweep",
            name="Sweep",
            interval_seconds=86400,
            agent="codebase_auditor",
            prompt="Audit code",
            repo_cycling=True,
            current_repo_index=0,
        )

        # First pick -> repo_a (alphabetically first)
        repo1 = scheduler._pick_next_repo(job)
        self.assertIsNotNone(repo1)
        self.assertEqual(repo1.name, "repo_a")
        self.assertEqual(job.current_repo_index, 1)

        # Second pick -> repo_b
        repo2 = scheduler._pick_next_repo(job)
        self.assertIsNotNone(repo2)
        self.assertEqual(repo2.name, "repo_b")
        self.assertEqual(job.current_repo_index, 2)

        # Third pick -> repo_c
        repo3 = scheduler._pick_next_repo(job)
        self.assertIsNotNone(repo3)
        self.assertEqual(repo3.name, "repo_c")
        self.assertEqual(job.current_repo_index, 0)

        # Fourth pick -> wraps around to repo_a
        repo4 = scheduler._pick_next_repo(job)
        self.assertIsNotNone(repo4)
        self.assertEqual(repo4.name, "repo_a")
        self.assertEqual(job.current_repo_index, 1)

    def test_pick_next_repo_disabled_when_repo_cycling_false(self):
        """Test that _pick_next_repo returns None when repo_cycling is False."""
        (self.repos_dir / "repo_a").mkdir()

        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=self.repos_dir,
        )

        job = ScheduledJob(
            job_id="sweep",
            name="Sweep",
            interval_seconds=86400,
            agent="codebase_auditor",
            prompt="Audit code",
            repo_cycling=False,
            current_repo_index=0,
        )

        repo = scheduler._pick_next_repo(job)
        self.assertIsNone(repo)
        self.assertEqual(job.current_repo_index, 0)

    def test_pick_next_repo_edge_cases(self):
        """Test edge cases: missing repos_dir, empty repos_dir, single repo."""
        # 1. No repos_dir
        scheduler_no_dir = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=None,
        )
        job = ScheduledJob(
            job_id="sweep",
            name="Sweep",
            interval_seconds=86400,
            agent="codebase_auditor",
            prompt="Audit code",
            repo_cycling=True,
        )
        self.assertIsNone(scheduler_no_dir._pick_next_repo(job))

        # 2. Non-existent repos_dir
        missing_dir = self.root_path / "nonexistent"
        scheduler_missing_dir = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=missing_dir,
        )
        self.assertIsNone(scheduler_missing_dir._pick_next_repo(job))

        # 3. Empty repos_dir
        empty_dir = self.root_path / "empty"
        empty_dir.mkdir()
        scheduler_empty = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=empty_dir,
        )
        self.assertIsNone(scheduler_empty._pick_next_repo(job))

        # 4. Single repo in repos_dir
        single_dir = self.root_path / "single"
        single_dir.mkdir()
        (single_dir / "only_repo").mkdir()
        scheduler_single = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=single_dir,
        )
        r1 = scheduler_single._pick_next_repo(job)
        self.assertEqual(r1.name, "only_repo")
        self.assertEqual(job.current_repo_index, 0)
        r2 = scheduler_single._pick_next_repo(job)
        self.assertEqual(r2.name, "only_repo")
        self.assertEqual(job.current_repo_index, 0)

    def test_repo_index_persistence_across_state_save_and_load(self):
        """Test that current_repo_index is saved to state and restored upon reload."""
        (self.repos_dir / "repo_1").mkdir()
        (self.repos_dir / "repo_2").mkdir()

        scheduler1 = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=self.repos_dir,
        )
        job = ScheduledJob(
            job_id="sweep",
            name="Sweep",
            interval_seconds=86400,
            agent="codebase_auditor",
            prompt="Audit code",
            repo_cycling=True,
            current_repo_index=0,
        )
        scheduler1.add_job(job)

        # Trigger pick and save
        repo = scheduler1._pick_next_repo(job)
        self.assertEqual(repo.name, "repo_1")
        self.assertEqual(job.current_repo_index, 1)
        scheduler1.save_state()

        # Load with new scheduler instance
        scheduler2 = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            repos_dir=self.repos_dir,
        )
        loaded_job = scheduler2.get_job("sweep")
        self.assertIsNotNone(loaded_job)
        self.assertEqual(loaded_job.current_repo_index, 1)

        # Next pick from loaded state should be repo_2
        next_repo = scheduler2._pick_next_repo(loaded_job)
        self.assertEqual(next_repo.name, "repo_2")
        self.assertEqual(loaded_job.current_repo_index, 0)

    def test_execute_job_with_task_manager_repo_cycling(self):
        """Test that _execute_job passes repo_name, repo_dir, and formatted prompt to task_manager.submit_task."""
        (self.repos_dir / "my_app").mkdir()

        mock_tm = MagicMock()
        mock_task = MagicMock()
        mock_task.id = "task-101"
        mock_tm.submit_task.return_value = mock_task
        mock_tm.can_accept_task.return_value = True

        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            task_manager=mock_tm,
            repos_dir=self.repos_dir,
        )

        original_prompt = "Perform bug sweep across files."
        job = ScheduledJob(
            job_id="periodic_bug_sweep",
            name="Bug Sweep",
            interval_seconds=86400,
            agent="codebase_auditor",
            prompt=original_prompt,
            repo_cycling=True,
            current_repo_index=0,
        )
        scheduler.add_job(job)

        scheduler._execute_job(job)

        # Assert job.prompt was not mutated
        self.assertEqual(job.prompt, original_prompt)

        # Assert task_manager.submit_task was called with expected kwargs
        mock_tm.submit_task.assert_called_once()
        call_kwargs = mock_tm.submit_task.call_args[1]
        self.assertEqual(call_kwargs["agent"], "codebase_auditor")
        self.assertEqual(call_kwargs["target_id"], "my_app#sched:periodic_bug_sweep")
        self.assertEqual(call_kwargs["repo_name"], "my_app")
        self.assertEqual(call_kwargs["repo_dir"], self.repos_dir / "my_app")
        self.assertIn("Target repository: my_app", call_kwargs["prompt"])
        self.assertIn(original_prompt, call_kwargs["prompt"])

    def test_execute_job_with_runner_repo_cycling(self):
        """Test that _execute_job passes active_prompt and repo_dir as cwd to runner."""
        (self.repos_dir / "my_app").mkdir()

        mock_runner = MagicMock()
        script_path = Path("/bin/run_agent_container.sh")
        default_cwd = Path("/workspace")

        scheduler = TaskScheduler(
            config_path=self.config_path,
            state_path=self.state_path,
            runner=mock_runner,
            script_path=script_path,
            cwd=default_cwd,
            repos_dir=self.repos_dir,
        )

        original_prompt = "Perform quality sweep."
        job = ScheduledJob(
            job_id="periodic_quality_sweep",
            name="Quality Sweep",
            interval_seconds=86400,
            agent="codebase_auditor",
            prompt=original_prompt,
            repo_cycling=True,
            current_repo_index=0,
        )
        scheduler.add_job(job)

        scheduler._execute_job(job)

        # Assert runner called with repo_dir as cwd
        mock_runner.assert_called_once()
        args = mock_runner.call_args[0]
        self.assertEqual(args[0], "codebase_auditor")
        self.assertIn("Target repository: my_app", args[1])
        self.assertEqual(args[2], script_path)
        self.assertEqual(args[3], self.repos_dir / "my_app")


if __name__ == "__main__":
    unittest.main()
