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


class TestAgentContainerScript(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)

        # Create dummy git repository structure
        self.repo_dir = self.test_dir / "repo"
        self.repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(self.repo_dir), check=True)
        (self.repo_dir / "README.md").write_text("Hello World")
        subprocess.run(["git", "add", "README.md"], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(self.repo_dir), check=True)

        self.script_path = Path(__file__).resolve().parent.parent / 'bin' / 'run_agent_container.sh'

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_continuation_retry_and_workspace_preservation(self):
        import os
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir()
        docker_log = self.test_dir / "docker_calls.log"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
echo "$@" >> "{docker_log}"

HOST_WS=""
for arg in "$@"; do
    if [[ "$arg" == *":/workspace"* ]]; then
        HOST_WS="${{arg%%:/workspace*}}"
    fi
done

if [ -n "$HOST_WS" ]; then
    echo "$HOST_WS" > "{self.test_dir}/last_ws.txt"
fi

LAST_WS="$(cat "{self.test_dir}/last_ws.txt" 2>/dev/null || echo "")"

if [ "$1" = "run" ] && [ "$2" = "-d" ]; then
    exit 0
elif [ "$1" = "exec" ]; then
    if [ ! -f "$LAST_WS/attempt_1_file.txt" ]; then
        # First attempt: simulate editing a file in the workspace
        echo "modified by step 1" > "$LAST_WS/attempt_1_file.txt"
        exit 1
    else
        # Second attempt: verify file from attempt 1 exists!
        echo "verified continuation" > "$LAST_WS/attempt_2_file.txt"
        exit 0
    fi
else
    exit 0
fi
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["MAX_AGENT_RETRIES"] = "2"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue #38"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("Auto-continuing conversation (Attempt 2/2)", proc.stdout)

        log_content = docker_log.read_text()
        self.assertIn("run -d --name graviton-agent-run-", log_content)
        self.assertIn("exec -w /workspace graviton-agent-run-", log_content)
        self.assertIn("Resume from your existing work in /workspace and complete the commit/PR drafting", log_content)
        self.assertIn("rm -f graviton-agent-run-", log_content)

    def test_fallback_to_docker_run_when_exec_fails(self):
        import os
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir()
        docker_log = self.test_dir / "docker_calls.log"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
echo "$@" >> "{docker_log}"

HOST_WS=""
for arg in "$@"; do
    if [[ "$arg" == *":/workspace"* ]]; then
        HOST_WS="${{arg%%:/workspace*}}"
    fi
done

if [ -n "$HOST_WS" ]; then
    echo "$HOST_WS" > "{self.test_dir}/last_ws_fallback.txt"
fi
LAST_WS="$(cat "{self.test_dir}/last_ws_fallback.txt" 2>/dev/null || echo "")"

if [ "$1" = "run" ] && [ "$2" = "-d" ]; then
    # Fail persistent container creation to test fallback
    exit 1
elif [ "$1" = "run" ] && [ "$2" = "--rm" ]; then
    if [ ! -f "$LAST_WS/attempt_1_fallback.txt" ]; then
        echo "modified in fallback attempt 1" > "$LAST_WS/attempt_1_fallback.txt"
        exit 1
    else
        echo "verified fallback continuation" > "$LAST_WS/attempt_2_fallback.txt"
        exit 0
    fi
else
    exit 0
fi
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["MAX_AGENT_RETRIES"] = "2"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue #38"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("Auto-continuing conversation (Attempt 2/2)", proc.stdout)

        log_content = docker_log.read_text()
        self.assertIn("run --rm", log_content)
        self.assertIn("Resume from your existing work in /workspace and complete the commit/PR drafting", log_content)


if __name__ == "__main__":
    unittest.main()
