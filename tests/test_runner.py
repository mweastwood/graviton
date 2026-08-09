"""
Unit tests for lib/runner.py
"""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from lib.runner import run_agent_container, run_agent_async


class TestRunner(unittest.TestCase):

    @patch("subprocess.Popen")
    def test_run_agent_container_success(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = ["Agent finished successfully\n", "Auto-continuing conversation (Attempt 2/3)...\n"]
        mock_proc.stderr = []
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")
        received_lines = []

        res = run_agent_container(
            "code_reviewer",
            "Review PR",
            script_path,
            cwd,
            on_output=received_lines.append,
        )

        mock_popen.assert_called_once_with(
            [str(script_path), "code_reviewer", "Review PR"],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Agent finished successfully", res.stdout)
        self.assertEqual(len(received_lines), 2)
        self.assertEqual(received_lines[0], "Agent finished successfully\n")
        self.assertEqual(received_lines[1], "Auto-continuing conversation (Attempt 2/3)...\n")

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
