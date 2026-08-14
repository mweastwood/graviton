"""
Unit tests for lib/updater.py
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from lib.updater import (
    perform_git_pull,
    check_if_dockerfile_changed,
    rebuild_agent_container,
    hot_reload_server,
    sync_repo_and_reload,
    stop_smee_listener,
    get_hot_reload_state,
    set_hot_reload_state,
)


class TestUpdater(unittest.TestCase):

    def setUp(self):
        set_hot_reload_state("IDLE")

    def tearDown(self):
        set_hot_reload_state("IDLE")

    def test_check_if_dockerfile_changed_true(self):
        output1 = "Updating 123..456\n Fast-forward\n Dockerfile | 2 +-\n 1 file changed"
        output2 = "Updating 123..456\n Fast-forward\n bin/build_agent_container.sh | 5 +++\n 1 file changed"

        self.assertTrue(check_if_dockerfile_changed(output1))
        self.assertTrue(check_if_dockerfile_changed(output2))

    def test_check_if_dockerfile_changed_false(self):
        output = "Updating 123..456\n Fast-forward\n lib/router.py | 10 +++---\n 1 file changed"
        self.assertFalse(check_if_dockerfile_changed(output))
        self.assertFalse(check_if_dockerfile_changed(""))

    @patch("subprocess.run")
    def test_perform_git_pull_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "pull"],
            returncode=0,
            stdout="Already up to date.",
            stderr="",
        )
        repo_root = Path("/tmp/fake_repo")
        success, output = perform_git_pull(repo_root, branch="master")

        mock_run.assert_called_once_with(
            ["git", "pull", "origin", "master"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertTrue(success)
        self.assertIn("Already up to date", output)

    @patch("subprocess.run")
    def test_rebuild_agent_container(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["build_agent_container.sh"],
            returncode=0,
            stdout="Build complete!",
            stderr="",
        )

        with patch("pathlib.Path.exists", return_value=True):
            repo_root = Path("/tmp/fake_repo")
            success = rebuild_agent_container(repo_root)
            self.assertTrue(success)

    @patch("os.execv")
    def test_hot_reload_server_drains_tasks(self, mock_execv):
        mock_tm = MagicMock()
        mock_httpd = MagicMock()
        mock_qt = MagicMock()

        hot_reload_server(httpd=mock_httpd, task_manager=mock_tm, quota_tracker=mock_qt)

        mock_tm.drain_active_tasks.assert_called_once()
        mock_tm.dump_queue_state.assert_called_once()
        mock_qt.dump_model_selection.assert_called_once()
        mock_httpd.server_close.assert_called_once()
        mock_execv.assert_called_once()

    @patch("os.execv")
    def test_hot_reload_server_extracts_quota_tracker_from_task_manager(self, mock_execv):
        mock_tm = MagicMock()
        mock_qt = MagicMock()
        mock_tm.quota_tracker = mock_qt

        hot_reload_server(task_manager=mock_tm)

        mock_tm.drain_active_tasks.assert_called_once()
        mock_tm.dump_queue_state.assert_called_once()
        mock_qt.dump_model_selection.assert_called_once()
        mock_execv.assert_called_once()

    @patch("os.execv")
    @patch("lib.updater.perform_git_pull")
    def test_sync_repo_and_reload_drains_tasks_before_reload(self, mock_git_pull, mock_execv):
        mock_git_pull.return_value = (True, "Already up to date.")
        mock_tm = MagicMock()
        mock_qt = MagicMock()

        states_during_drain = []

        def side_effect_drain(*args, **kwargs):
            states_during_drain.append(get_hot_reload_state())
            return True

        mock_tm.drain_active_tasks.side_effect = side_effect_drain

        sync_repo_and_reload(
            repo_root=Path("/tmp/fake_repo"),
            ref="refs/heads/main",
            task_manager=mock_tm,
            quota_tracker=mock_qt,
        )

        mock_git_pull.assert_called_once_with(Path("/tmp/fake_repo"), branch="main")
        mock_tm.drain_active_tasks.assert_called_once()
        mock_qt.dump_model_selection.assert_called_once()
        self.assertEqual(states_during_drain, ["DRAINING_TASKS"])
        mock_execv.assert_called_once()

    @patch("lib.updater.perform_git_pull")
    def test_sync_repo_and_reload_git_pull_failure_skips_drain(self, mock_git_pull):
        mock_git_pull.return_value = (False, "Git merge conflict")
        mock_tm = MagicMock()

        sync_repo_and_reload(
            repo_root=Path("/tmp/fake_repo"),
            ref="refs/heads/main",
            task_manager=mock_tm,
        )

        mock_git_pull.assert_called_once()
        mock_tm.drain_active_tasks.assert_not_called()
        self.assertEqual(get_hot_reload_state(), "IDLE")

    def test_stop_smee_listener_graceful_exit(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        stop_smee_listener(mock_proc)
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=2)
        mock_proc.kill.assert_not_called()

    def test_stop_smee_listener_timeout_triggers_kill(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="smee", timeout=2), None]
        stop_smee_listener(mock_proc)
        mock_proc.terminate.assert_called_once()
        mock_proc.kill.assert_called_once()
        self.assertEqual(mock_proc.wait.call_count, 2)

    @patch("os.execv")
    def test_hot_reload_server_stops_listener_proc(self, mock_execv):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        hot_reload_server(listener_proc=mock_proc)

        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called_once_with(timeout=2)
        mock_execv.assert_called_once()


if __name__ == "__main__":
    unittest.main()
