"""
Unit tests for lib/runner.py
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from lib.runner import run_agent_container, run_agent_async, is_transcript_incomplete


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
            env=unittest.mock.ANY,
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

    @patch("subprocess.Popen")
    def test_run_agent_container_with_max_attempts(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = []
        mock_proc.stderr = []
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")

        res = run_agent_container(
            "code_reviewer",
            "Review PR",
            script_path,
            cwd,
            max_attempts=5,
        )

        self.assertEqual(mock_popen.call_count, 1)
        _, kwargs = mock_popen.call_args
        self.assertEqual(kwargs["env"].get("MAX_AGENT_RETRIES"), "5")

    @patch("subprocess.Popen")
    def test_run_agent_container_with_cached_workspace_dir_and_initial_attempt(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = []
        mock_proc.stderr = []
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")
        cache_dir = Path("/tmp/graviton-workspaces/cache/task-1")

        res = run_agent_container(
            "code_reviewer",
            "Review PR",
            script_path,
            cwd,
            max_attempts=6,
            cached_workspace_dir=cache_dir,
            initial_attempt=4,
        )

        self.assertEqual(mock_popen.call_count, 1)
        _, kwargs = mock_popen.call_args
        self.assertEqual(kwargs["env"].get("MAX_AGENT_RETRIES"), "6")
        self.assertEqual(kwargs["env"].get("GRAVITON_WORKSPACE_CACHE_DIR"), str(cache_dir))
        self.assertEqual(kwargs["env"].get("GRAVITON_INITIAL_ATTEMPT"), "4")

    @patch("subprocess.Popen")
    def test_run_agent_container_with_quota_pool_and_model(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = []
        mock_proc.stderr = []
        mock_proc.wait.return_value = 0
        mock_popen.return_value = mock_proc

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")

        res = run_agent_container(
            "code_reviewer",
            "Review PR",
            script_path,
            cwd,
            quota_pool="claude_gpt",
            model="claude-sonnet-4-6",
        )

        self.assertEqual(mock_popen.call_count, 1)
        _, kwargs = mock_popen.call_args
        self.assertEqual(kwargs["env"].get("ANTIGRAVITY_QUOTA_POOL"), "claude_gpt")
        self.assertEqual(kwargs["env"].get("ANTIGRAVITY_MODEL"), "claude-sonnet-4-6")
        self.assertEqual(kwargs["env"].get("MODEL_NAME"), "claude-sonnet-4-6")

    @patch("lib.runner.run_agent_container")
    def test_run_agent_async(self, mock_run_container):
        mock_run_container.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr=""
        )

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")
        thread = run_agent_async("code_fixer", "Fix code", script_path, cwd, max_attempts=4)
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        mock_run_container.assert_called_once_with(
            "code_fixer",
            "Fix code",
            script_path,
            cwd,
            max_attempts=4,
            cached_workspace_dir=None,
            initial_attempt=None,
            quota_pool=None,
            model=None,
        )

    @patch("lib.runner.run_agent_container")
    def test_run_agent_async_default_max_attempts(self, mock_run_container):
        mock_run_container.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr=""
        )

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")
        thread = run_agent_async("code_fixer", "Fix code", script_path, cwd)
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        mock_run_container.assert_called_once_with(
            "code_fixer",
            "Fix code",
            script_path,
            cwd,
            max_attempts=None,
            cached_workspace_dir=None,
            initial_attempt=None,
            quota_pool=None,
            model=None,
        )

    @patch("lib.runner.run_agent_container")
    def test_run_agent_async_with_quota_pool_and_model(self, mock_run_container):
        mock_run_container.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Done", stderr=""
        )

        script_path = Path("/tmp/run_agent_container.sh")
        cwd = Path("/workspace")
        thread = run_agent_async(
            "code_fixer",
            "Fix code",
            script_path,
            cwd,
            max_attempts=4,
            quota_pool="claude_gpt",
            model="claude-sonnet-4-6",
        )
        thread.join(timeout=2.0)

        self.assertFalse(thread.is_alive())
        mock_run_container.assert_called_once_with(
            "code_fixer",
            "Fix code",
            script_path,
            cwd,
            max_attempts=4,
            cached_workspace_dir=None,
            initial_attempt=None,
            quota_pool="claude_gpt",
            model="claude-sonnet-4-6",
        )


class TestAgentContainerScript(unittest.TestCase):

    def setUp(self):
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
        self.assertIn("exec -w /workspace", log_content)
        self.assertIn("graviton-agent-run-", log_content)
        self.assertIn("Resume from your existing work in /workspace and complete your goal", log_content)
        self.assertIn("rm -f graviton-agent-run-", log_content)

    def test_fallback_to_docker_run_when_exec_fails(self):
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

    def test_default_max_attempts_is_3(self):
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls.log"

        conv_id = "99999999-8888-7777-6666-555555555555"
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
    if [ ! -f "{self.test_dir}/default_attempt2.txt" ]; then
        echo '{{"type": "PLANNER_RESPONSE", "tool_calls": [{{"name": "write_file"}}]}}' > "{transcript_file}"
        echo "attempt 1 done" > "{self.test_dir}/default_attempt2.txt"
        exit 0
    else
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
        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue default retries"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertIn("Auto-continuing conversation (Attempt 2/3)...", proc.stdout)

    def test_workspace_caching_and_restoration_in_container_script(self):
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls.log"
        cache_dir = self.test_dir / "test_workspace_cache"

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
    echo "$HOST_WS" > "{self.test_dir}/last_ws_cache_test.txt"
fi
LAST_WS="$(cat "{self.test_dir}/last_ws_cache_test.txt" 2>/dev/null || echo "")"

if [ "$1" = "run" ] && [ "$2" = "-d" ]; then
    exit 0
elif [ "$1" = "exec" ]; then
    if [ ! -f "$LAST_WS/work_attempt_1.txt" ]; then
        # First pass: edit workspace and fail to trigger exhaustion sync
        echo "attempt 1 work" > "$LAST_WS/work_attempt_1.txt"
        exit 1
    else
        # Second pass (restored from cache): verify work_attempt_1.txt exists!
        echo "attempt 4 continuation" > "$LAST_WS/work_attempt_4.txt"
        exit 0
    fi
else
    exit 0
fi
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        # Pass 1: run container with MAX_AGENT_RETRIES=1 and GRAVITON_WORKSPACE_CACHE_DIR
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["MAX_AGENT_RETRIES"] = "1"
        env["GRAVITON_WORKSPACE_CACHE_DIR"] = str(cache_dir)
        env["GRAVITON_INITIAL_ATTEMPT"] = "1"

        proc1 = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue #163"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(proc1.returncode, 0)
        self.assertIn("Syncing workspace to cache:", proc1.stdout)
        self.assertTrue(cache_dir.exists())
        self.assertTrue((cache_dir / "work_attempt_1.txt").exists())

        # Pass 2: run container with restored cache, initial attempt 4, MAX_AGENT_RETRIES=6
        env["MAX_AGENT_RETRIES"] = "6"
        env["GRAVITON_INITIAL_ATTEMPT"] = "4"

        proc2 = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue #163"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc2.returncode, 0)
        self.assertIn("Restoring workspace from cache:", proc2.stdout)
        self.assertIn("Cleaning up workspace cache on success:", proc2.stdout)
        self.assertFalse(cache_dir.exists())

    def test_workspace_cache_sync_purges_deleted_files(self):
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls_del.log"
        cache_dir = self.test_dir / "test_workspace_cache_del"

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
    echo "$HOST_WS" > "{self.test_dir}/last_ws_cache_del.txt"
fi
LAST_WS="$(cat "{self.test_dir}/last_ws_cache_del.txt" 2>/dev/null || echo "")"

if [ "$1" = "run" ] && [ "$2" = "-d" ]; then
    exit 0
elif [ "$1" = "exec" ]; then
    if [ ! -f "$LAST_WS/work_attempt_1.txt" ]; then
        # First pass: create file1 and file_to_delete, then fail to populate cache
        echo "keep" > "$LAST_WS/work_attempt_1.txt"
        echo "delete me" > "$LAST_WS/file_to_delete.txt"
        exit 1
    else
        # Second pass: delete file_to_delete.txt and create work_attempt_2.txt, then fail again
        rm -f "$LAST_WS/file_to_delete.txt"
        echo "attempt 2 work" > "$LAST_WS/work_attempt_2.txt"
        exit 1
    fi
else
    exit 0
fi
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["MAX_AGENT_RETRIES"] = "1"
        env["GRAVITON_WORKSPACE_CACHE_DIR"] = str(cache_dir)
        env["GRAVITON_INITIAL_ATTEMPT"] = "1"

        # Pass 1: creates file_to_delete.txt and work_attempt_1.txt, fails -> syncs to cache
        proc1 = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue #163"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc1.returncode, 0)
        self.assertTrue((cache_dir / "work_attempt_1.txt").exists())
        self.assertTrue((cache_dir / "file_to_delete.txt").exists())

        # Pass 2: restored from cache, deletes file_to_delete.txt, fails -> syncs to cache again
        env["MAX_AGENT_RETRIES"] = "2"
        env["GRAVITON_INITIAL_ATTEMPT"] = "2"
        proc2 = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue #163"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc2.returncode, 0)
        self.assertTrue((cache_dir / "work_attempt_1.txt").exists())
        self.assertTrue((cache_dir / "work_attempt_2.txt").exists())
        # Assert deleted file is NOT resurrected / retained in cache!
        self.assertFalse((cache_dir / "file_to_delete.txt").exists())

    def test_githooks_pre_commit_configuration(self):
        githooks_dir = self.repo_dir / ".githooks"
        githooks_dir.mkdir()
        aux_dir = githooks_dir / "scripts"
        aux_dir.mkdir()
        aux_script = aux_dir / "check-fmt.sh"
        aux_script.write_text("#!/bin/sh\necho 'auxiliary script executed' >> hook_output.txt\nexit 0\n")
        aux_script.chmod(0o755)
        pre_commit_hook = githooks_dir / "pre-commit"
        pre_commit_hook.write_text("#!/bin/sh\n\"$(dirname \"$0\")/scripts/check-fmt.sh\"\necho 'pre-commit hook executed' >> hook_output.txt\nexit 0\n")
        pre_commit_hook.chmod(0o644)
        gitkeep = githooks_dir / ".gitkeep"
        gitkeep.write_text("")
        gitkeep.chmod(0o644)
        subprocess.run(["git", "add", ".githooks"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add pre-commit hook"], cwd=str(self.repo_dir), check=True, capture_output=True)

        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        hooks_verified_file = self.test_dir / "hooks_verified.txt"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
HOST_WS=""
for arg in "$@"; do
    if [[ "$arg" == *":/workspace"* ]]; then
        HOST_WS="${{arg%%:/workspace*}}"
    fi
done

if [ -n "$HOST_WS" ]; then
    HOOK_PATH="$(git -C "$HOST_WS" config core.hooksPath 2>/dev/null || echo "")"
    PRE_COMMIT_X="$(python3 -c "import os; print(os.access('$HOST_WS/.githooks/pre-commit', os.X_OK))" 2>/dev/null || echo "False")"
    GITKEEP_X="$(python3 -c "import os; print(os.access('$HOST_WS/.githooks/.gitkeep', os.X_OK))" 2>/dev/null || echo "False")"
    if [ "$HOOK_PATH" = ".githooks" ] && [ "$PRE_COMMIT_X" = "True" ] && [ "$GITKEEP_X" = "False" ]; then
        mkdir -p "$HOST_WS/subfolder"
        echo "change in subfolder" >> "$HOST_WS/subfolder/file.txt"
        git -C "$HOST_WS/subfolder" add file.txt
        if git -C "$HOST_WS/subfolder" -c user.name="Test" -c user.email="test@example.com" commit -m "Test commit with hook" &>/dev/null; then
            if [ -f "$HOST_WS/subfolder/hook_output.txt" ] || [ -f "$HOST_WS/hook_output.txt" ]; then
                echo "configured_and_executed" > "{hooks_verified_file}"
            fi
        fi
    fi
fi
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Test githooks"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(hooks_verified_file.exists())
        self.assertEqual(hooks_verified_file.read_text().strip(), "configured_and_executed")

    def test_githooks_pre_commit_failing_hook(self):
        githooks_dir = self.repo_dir / ".githooks"
        githooks_dir.mkdir()
        pre_commit_hook = githooks_dir / "pre-commit"
        pre_commit_hook.write_text("#!/bin/sh\necho 'pre-commit failed' >&2\nexit 1\n")
        pre_commit_hook.chmod(0o644)
        subprocess.run(["git", "add", ".githooks"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add failing pre-commit hook"], cwd=str(self.repo_dir), check=True, capture_output=True)

        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        hook_failed_verified = self.test_dir / "hook_failed_verified.txt"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
HOST_WS=""
for arg in "$@"; do
    if [[ "$arg" == *":/workspace"* ]]; then
        HOST_WS="${{arg%%:/workspace*}}"
    fi
done

if [ -n "$HOST_WS" ]; then
    HOOK_PATH="$(git -C "$HOST_WS" config core.hooksPath 2>/dev/null || echo "")"
    PRE_COMMIT_X="$(python3 -c "import os; print(os.access('$HOST_WS/.githooks/pre-commit', os.X_OK))" 2>/dev/null || echo "False")"
    if [ "$HOOK_PATH" = ".githooks" ] && [ "$PRE_COMMIT_X" = "True" ]; then
        echo "change" >> "$HOST_WS/README.md"
        git -C "$HOST_WS" add README.md
        if ! git -C "$HOST_WS" -c user.name="Test" -c user.email="test@example.com" commit -m "Failing commit" &>/dev/null; then
            echo "hook_failed_as_expected" > "{hook_failed_verified}"
        fi
    fi
fi
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Test failing githooks"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(hook_failed_verified.exists())
        self.assertEqual(hook_failed_verified.read_text().strip(), "hook_failed_as_expected")

    def test_githooks_directory_only_configuration(self):
        githooks_dir = self.repo_dir / ".githooks"
        githooks_dir.mkdir()
        gitkeep = githooks_dir / ".gitkeep"
        gitkeep.write_text("")
        gitkeep.chmod(0o644)
        subprocess.run(["git", "add", ".githooks"], cwd=str(self.repo_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Add empty .githooks directory"], cwd=str(self.repo_dir), check=True, capture_output=True)

        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        hooks_config_verified = self.test_dir / "hooks_config_verified.txt"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
HOST_WS=""
for arg in "$@"; do
    if [[ "$arg" == *":/workspace"* ]]; then
        HOST_WS="${{arg%%:/workspace*}}"
    fi
done

if [ -n "$HOST_WS" ]; then
    HOOK_PATH="$(git -C "$HOST_WS" config core.hooksPath 2>/dev/null || echo "")"
    GITKEEP_X="$(python3 -c "import os; print(os.access('$HOST_WS/.githooks/.gitkeep', os.X_OK))" 2>/dev/null || echo "False")"
    if [ -z "$HOOK_PATH" ] && [ "$GITKEEP_X" = "False" ]; then
        echo "hooks_not_configured" > "{hooks_config_verified}"
    fi
fi
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Test empty githooks dir"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        self.assertTrue(hooks_config_verified.exists())
        self.assertEqual(hooks_config_verified.read_text().strip(), "hooks_not_configured")

    def test_container_script_model_and_quota_pool_propagation(self):
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls_model.log"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
echo "$@" >> "{docker_log}"
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["ANTIGRAVITY_MODEL"] = "claude-sonnet-4-6"
        env["MODEL_NAME"] = "claude-sonnet-4-6"
        env["ANTIGRAVITY_QUOTA_POOL"] = "claude_gpt"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        log_content = docker_log.read_text()
        self.assertIn("-e ANTIGRAVITY_MODEL=claude-sonnet-4-6", log_content)
        self.assertIn("-e MODEL_NAME=claude-sonnet-4-6", log_content)
        self.assertIn("-e ANTIGRAVITY_QUOTA_POOL=claude_gpt", log_content)
        self.assertIn("--model claude-sonnet-4-6", log_content)

    def test_container_script_model_fallback_only_model_name(self):
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls_model_name_only.log"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
echo "$@" >> "{docker_log}"
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env.pop("ANTIGRAVITY_MODEL", None)
        env["MODEL_NAME"] = "gemini-3.6-flash"

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        log_content = docker_log.read_text()
        self.assertIn("-e ANTIGRAVITY_MODEL=gemini-3.6-flash", log_content)
        self.assertIn("-e MODEL_NAME=gemini-3.6-flash", log_content)
        self.assertIn("--model gemini-3.6-flash", log_content)

    def test_container_script_model_fallback_only_antigravity_model(self):
        bin_dir = self.test_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        docker_log = self.test_dir / "docker_calls_antigravity_model_only.log"

        mock_docker = bin_dir / "docker"
        mock_docker_content = f"""#!/usr/bin/env bash
echo "$@" >> "{docker_log}"
exit 0
"""
        mock_docker.write_text(mock_docker_content)
        mock_docker.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["ANTIGRAVITY_MODEL"] = "gpt-4o"
        env.pop("MODEL_NAME", None)

        proc = subprocess.run(
            [str(self.script_path), "code_fixer", "Fix issue"],
            cwd=str(self.repo_dir),
            env=env,
            capture_output=True,
            text=True,
        )

        self.assertEqual(proc.returncode, 0)
        log_content = docker_log.read_text()
        self.assertIn("-e ANTIGRAVITY_MODEL=gpt-4o", log_content)
        self.assertIn("-e MODEL_NAME=gpt-4o", log_content)
        self.assertIn("--model gpt-4o", log_content)


class TestTranscriptInspector(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_is_transcript_incomplete_true(self):
        transcript_file = self.test_dir / "transcript.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {}}], "content": "Run tool"}'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")

        self.assertTrue(is_transcript_incomplete(transcript_file))
        self.assertTrue(is_transcript_incomplete(str(transcript_file)))

    def test_is_transcript_incomplete_false_completed(self):
        transcript_file = self.test_dir / "transcript.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {}}], "content": "Run tool"}',
            '{"step_index": 3, "type": "TOOL_RESULT", "status": "DONE"}'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")

        self.assertFalse(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_false_empty_tool_calls(self):
        transcript_file = self.test_dir / "transcript.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [], "content": "Finished"}'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")

        self.assertFalse(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_missing_file_and_empty(self):
        non_existent = self.test_dir / "missing.jsonl"
        self.assertFalse(is_transcript_incomplete(non_existent))

        empty_file = self.test_dir / "empty.jsonl"
        empty_file.write_text("", encoding="utf-8")
        self.assertFalse(is_transcript_incomplete(empty_file))

    def test_is_transcript_incomplete_trailing_whitespace(self):
        transcript_file = self.test_dir / "trailing.jsonl"
        content = (
            '{"type": "USER_INPUT", "content": "hello"}\n'
            '{"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "cmd"}]}\n'
            '\n'
            '   \n'
        )
        transcript_file.write_text(content, encoding="utf-8")
        self.assertTrue(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_interspersed_blank_lines(self):
        transcript_file = self.test_dir / "interspersed.jsonl"
        content = (
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}\n'
            '\n'
            '   \n'
            '{"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]}\n'
            '\n'
            '\t\n'
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

    def test_is_transcript_incomplete_malformed_json(self):
        transcript_file = self.test_dir / "malformed.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            'this is invalid json {{{'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")
        self.assertFalse(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_non_dict_json(self):
        transcript_file = self.test_dir / "non_dict.jsonl"
        lines = [
            '{"step_index": 1, "type": "USER_INPUT", "content": "Hello"}',
            '[1, 2, 3]'
        ]
        transcript_file.write_text("\n".join(lines), encoding="utf-8")
        self.assertFalse(is_transcript_incomplete(transcript_file))

    def test_is_transcript_incomplete_non_list_tool_calls(self):
        cases = [
            '{"type": "PLANNER_RESPONSE", "tool_calls": null}',
            '{"type": "PLANNER_RESPONSE", "tool_calls": "not-a-list"}',
            '{"type": "PLANNER_RESPONSE", "tool_calls": true}',
            '{"type": "PLANNER_RESPONSE", "tool_calls": 123}',
            '{"type": "PLANNER_RESPONSE", "tool_calls": {"key": "val"}}',
        ]
        for i, case in enumerate(cases):
            tf = self.test_dir / f"non_list_tool_calls_{i}.jsonl"
            tf.write_text(case, encoding="utf-8")
            self.assertFalse(is_transcript_incomplete(tf))


if __name__ == "__main__":
    unittest.main()

