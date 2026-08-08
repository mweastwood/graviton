"""
Unit tests for lib/runner.py
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from lib.runner import run_agent_container, run_agent_async


class TestRunner(unittest.TestCase):

    @patch("subprocess.run")
    def test_run_agent_container_success(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["run_agent_container.sh", "code_reviewer", "Review PR"],
            returncode=0,
            stdout="Agent finished successfully",
            stderr="",
        )

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")
        res = run_agent_container("code_reviewer", "Review PR", script_path, cwd)

        mock_run.assert_called_once_with(
            [str(script_path), "code_reviewer", "Review PR"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "Agent finished successfully")

    @patch("lib.runner.run_agent_container")
    def test_run_agent_async(self, mock_run_container):
        mock_run_container.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr=""
        )

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")
        thread = run_agent_async("code_fixer", "Fix code", script_path, cwd)
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        mock_run_container.assert_called_once_with("code_fixer", "Fix code", script_path, cwd)


if __name__ == "__main__":
    unittest.main()
