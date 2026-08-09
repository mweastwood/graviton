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
        self.assertIn("Resume from your existing work in /workspace and complete your goal", log_content)
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
        self.assertIn("Resume from your existing work in /workspace and complete your goal", log_content)

    def test_remote_origin_synchronization(self):
        import os
        # 1. Create a remote bare repository
        remote_dir = self.test_dir / "remote_repo.git"
        subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=str(remote_dir), check=True)
        # Ensure self.repo_dir is on main branch
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(self.repo_dir), check=True, capture_output=True)
        # Point self.repo_dir origin to remote_dir
        subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=str(self.repo_dir), check=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(self.repo_dir), check=True, capture_output=True)

        # 2. Add a new commit to origin via a secondary clone
        other_clone = self.test_dir / "other_clone"
        subprocess.run(["git", "clone", str(remote_dir), str(other_clone)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(other_clone), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(other_clone), check=True)
        (other_clone / "new_remote_file.txt").write_text("Remote changes")
        subprocess.run(["git", "add", "new_remote_file.txt"], cwd=str(other_clone), check=True)
        subprocess.run(["git", "commit", "-m", "Remote update commit"], cwd=str(other_clone), check=True)
        subprocess.run(["git", "push", "origin", "main"], cwd=str(other_clone), check=True, capture_output=True)

        # At this point self.repo_dir does NOT have new_remote_file.txt, but origin/main does.
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        mock_docker = bin_dir / "docker"
        sync_verified_file = self.test_dir / "sync_verified.txt"
        mock_docker_content = f"""#!/usr/bin/env bash
HOST_WS=""
for arg in "$@"; do
    if [[ "$arg" == *":/workspace"* ]]; then
        HOST_WS="${{arg%%:/workspace*}}"
    fi
done

if [ -n "$HOST_WS" ] && [ -f "$HOST_WS/new_remote_file.txt" ]; then
    echo "synced" > "{sync_verified_file}"
fi
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Test sync"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(sync_verified_file.exists())
        self.assertEqual(sync_verified_file.read_text().strip(), "synced")

    def test_transcript_incomplete_triggers_continuation_retry(self):
        import os
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls.log"

        conv_id = "11111111-2222-3333-4444-555555555555"
        mock_home = self.test_dir / "home"
        brain_log_dir = mock_home / ".gemini" / "antigravity-cli" / "brain" / conv_id / ".system_generated" / "logs"
        brain_log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = brain_log_dir / "transcript.jsonl"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
echo "$@" >> "{docker_log}"
echo "Conversation ID: {conv_id}"

if [ "$1" = "run" ] && [ "$2" = "-d" ]; then
    exit 0
elif [ "$1" = "exec" ]; then
    if [ ! -f "{self.test_dir}/attempt2_ran.txt" ]; then
        # Attempt 1: output incomplete transcript
        echo '{{"type": "PLANNER_RESPONSE", "tool_calls": [{{"name": "write_file"}}]}}' > "{transcript_file}"
        echo "ran_attempt_1" > "{self.test_dir}/attempt2_ran.txt"
        exit 0
    else
        # Attempt 2: complete transcript
        echo '{{"type": "PLANNER_RESPONSE", "tool_calls": []}}' > "{transcript_file}"
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
        env["HOME"] = str(mock_home)
        env["MAX_AGENT_RETRIES"] = "2"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("Agent session hit step limit with unexecuted tool calls. Auto-continuing conversation (Attempt 2/2)...", proc.stdout)

    def test_transcript_complete_exits_cleanly(self):
        import os
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls.log"

        conv_id = "66666666-7777-8888-9999-000000000000"
        mock_home = self.test_dir / "home"
        brain_log_dir = mock_home / ".gemini" / "antigravity-cli" / "brain" / conv_id / ".system_generated" / "logs"
        brain_log_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = brain_log_dir / "transcript.jsonl"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
echo "$@" >> "{docker_log}"
echo "Conversation ID: {conv_id}"
echo '{{"type": "PLANNER_RESPONSE", "tool_calls": []}}' > "{transcript_file}"
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["HOME"] = str(mock_home)
        env["MAX_AGENT_RETRIES"] = "2"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("Agent 'code_fixer' completed successfully.", proc.stdout)
        self.assertNotIn("Auto-continuing conversation", proc.stdout)


class TestTranscriptInspector(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_transcript_incomplete_true(self):
        from lib.runner import is_transcript_incomplete
        transcript_file = self.test_dir / "transcript.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {}}], "content": "Run tool"}'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")

        self.assertTrue(is_transcript_incomplete(transcript_file))
        self.assertTrue(is_transcript_incomplete(str(transcript_file)))

    def test_is_transcript_incomplete_false_completed(self):
        from lib.runner import is_transcript_incomplete
        transcript_file = self.test_dir / "transcript.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {}}], "content": "Run tool"}',
            '{"step_index": 3, "type": "TOOL_RESULT", "status": "DONE"}'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")

        self.assertFalse(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_false_empty_tool_calls(self):
        from lib.runner import is_transcript_incomplete
        transcript_file = self.test_dir / "transcript.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [], "content": "Finished"}'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")

        self.assertFalse(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_missing_file_and_empty(self):
        from lib.runner import is_transcript_incomplete
        non_existent = self.test_dir / "missing.jsonl"
        self.assertFalse(is_transcript_incomplete(non_existent))

        empty_file = self.test_dir / "empty.jsonl"
        empty_file.write_text("", encoding="utf-8")
        self.assertFalse(is_transcript_incomplete(empty_file))

    def test_is_transcript_incomplete_trailing_whitespace(self):
        from lib.runner import is_transcript_incomplete
        transcript_file = self.test_dir / "trailing.jsonl"
        content = (
            '{"type": "USER_INPUT", "content": "hello"}\n'
            '{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "cmd"}]}\n'
            '\n'
            '   \n'
        )
        transcript_file.write_text(content, encoding="utf-8")
        self.assertTrue(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_cli_execution(self):
        runner_py = Path(__file__).resolve().parent.parent / "lib" / "runner.py"

        incomplete_file = self.test_dir / "incomplete.jsonl"
        incomplete_file.write_text(
            '{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "test"}]}', encoding="utf-8"
        )
        res_inc = subprocess.run(["python3", str(runner_py), str(incomplete_file)])
        self.assertEqual(res_inc.returncode, 0)

        complete_file = self.test_dir / "complete.jsonl"
        complete_file.write_text(
            '{"type": "PLANNER_RESPONSE", "tool_calls": []}', encoding="utf-8"
        )
        res_comp = subprocess.run(["python3", str(runner_py), str(complete_file)])
        self.assertEqual(res_comp.returncode, 1)


if __name__ == "__main__":
    unittest.main()

