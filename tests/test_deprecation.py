"""
Unit tests verifying deprecation warnings for legacy runner components in Graviton.
"""

import os
import subprocess
import unittest
import warnings
from pathlib import Path
from unittest.mock import MagicMock, patch

from lib.runner import run_agent_async, run_agent_container

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDeprecationWarnings(unittest.TestCase):
    @patch("subprocess.Popen")
    def test_run_agent_container_emits_deprecation_warning(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = []
        mock_proc.stderr = []
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            run_agent_container(
                agent_name="code_reviewer",
                prompt="test prompt",
                script_path=Path("/tmp/run_agent_container.sh"),
                cwd=Path("/tmp"),
            )
            dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertTrue(len(dep_warnings) >= 1)
            self.assertIn("run_agent_container is deprecated", str(dep_warnings[0].message))

    @patch("subprocess.Popen")
    def test_run_agent_async_emits_deprecation_warning(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = []
        mock_proc.stderr = []
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            thread = run_agent_async(
                agent_name="code_reviewer",
                prompt="test prompt",
                script_path=Path("/tmp/run_agent_container.sh"),
                cwd=Path("/tmp"),
            )
            thread.join(timeout=2.0)
            dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
            self.assertTrue(len(dep_warnings) >= 1)
            self.assertIn("run_agent_async is deprecated", str(dep_warnings[0].message))

    def test_run_agent_container_sh_outputs_deprecation_banner(self):
        script_path = REPO_ROOT / "bin" / "run_agent_container.sh"
        # Run with nonexistent image or dry command to inspect stderr banner
        res = subprocess.run(
            [str(script_path), "code_reviewer", "help"],
            capture_output=True,
            text=True,
            env={**dict(os.environ), "ANTIGRAVITY_IMAGE": "nonexistent:test"},
        )
        self.assertIn("[DEPRECATED] bin/run_agent_container.sh is deprecated", res.stderr)


if __name__ == "__main__":
    unittest.main()
