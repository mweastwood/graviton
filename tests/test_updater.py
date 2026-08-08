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
)


class TestUpdater(unittest.TestCase):

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
        success, output = perform_git_pull(repo_root)

        mock_run.assert_called_once_with(
            ["git", "pull", "origin", "main"],
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


if __name__ == "__main__":
    unittest.main()
