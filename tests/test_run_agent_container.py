"""
Unit tests for bin/run_agent_container.sh
"""

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_AGENT_CONTAINER_PATH = REPO_ROOT / "bin" / "run_agent_container.sh"

MOCK_DOCKER_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

log_file = os.environ.get("MOCK_DOCKER_LOG")
counter_file = os.environ.get("MOCK_DOCKER_COUNTER_FILE")
workspace_file = os.environ.get("MOCK_DOCKER_WORKSPACE_PATH_FILE")

call_count = 0
if counter_file and Path(counter_file).exists():
    try:
        call_count = int(Path(counter_file).read_text().strip())
    except Exception:
        call_count = 0

args = sys.argv[1:]
subcmd = args[0] if args else ""

# Extract ephemeral workspace mount path from docker run
if subcmd == "run":
    for i, arg in enumerate(args):
        target = ""
        if arg == "-v" and i + 1 < len(args):
            target = args[i + 1]
        elif arg.startswith("-v") and len(arg) > 2:
            target = arg[2:]
        if ":/workspace" in target and not target.endswith(":/workspace:ro"):
            host_ws = target.split(":/workspace")[0]
            if workspace_file:
                Path(workspace_file).write_text(host_ws)

# Record call
if log_file:
    record = {
        "call_index": call_count + 1,
        "args": args,
        "cwd": os.getcwd(),
    }
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\\n")

# Execute agent logic shared between docker exec and docker run --rm fallback
def handle_agent_execution():
    global call_count
    call_count += 1
    if counter_file:
        Path(counter_file).write_text(str(call_count))

    create_rel = os.environ.get("MOCK_WORKSPACE_FILE_TO_CREATE")
    if create_rel and workspace_file and Path(workspace_file).exists():
        ws_path = Path(Path(workspace_file).read_text().strip())
        target_path = ws_path / create_rel
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("created_by_mock_agent")

    # If configured, append a completed planner step to transcript on specified attempt
    complete_attempt = os.environ.get("MOCK_DOCKER_COMPLETE_TRANSCRIPT_ON_ATTEMPT")
    transcript_path = os.environ.get("MOCK_DOCKER_TRANSCRIPT_PATH")
    if complete_attempt and str(call_count) == complete_attempt and transcript_path and Path(transcript_path).exists():
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write('{"step_index": 1, "type": "PLANNER_RESPONSE", "tool_calls": []}\\n')

    if os.environ.get("MOCK_CHECK_HOOK_FILE") and workspace_file and Path(workspace_file).exists():
        ws_path = Path(Path(workspace_file).read_text().strip())
        hook_path = ws_path / ".githooks" / "pre-commit"
        if hook_path.exists() and os.access(hook_path, os.X_OK):
            print("HOOK_IS_EXECUTABLE")

    out_key = f"MOCK_DOCKER_OUTPUT_ATTEMPT_{call_count}"
    out_val = os.environ.get(out_key, os.environ.get("MOCK_DOCKER_EXEC_OUTPUT", ""))
    if out_val:
        print(out_val)

    exit_key = f"MOCK_DOCKER_EXIT_CODE_ATTEMPT_{call_count}"
    exit_code = int(os.environ.get(exit_key, os.environ.get("MOCK_DOCKER_EXEC_EXIT_CODE", "0")))
    sys.exit(exit_code)

if subcmd == "run":
    if "-d" in args:
        if os.environ.get("MOCK_DOCKER_RUN_FAIL") == "1":
            sys.exit(1)
        print("graviton-mock-container-12345")
        sys.exit(0)
    elif "--rm" in args and any("alpine" in a for a in args):
        sys.exit(0)
    else:
        # Direct docker run fallback execution (when USE_CONTAINER_EXEC=false)
        handle_agent_execution()

elif subcmd == "exec":
    if any(a in args for a in ["chmod", "rm"]):
        sys.exit(0)

    handle_agent_execution()

elif subcmd == "rm":
    sys.exit(0)

sys.exit(0)
"""

MOCK_GH_SCRIPT = """#!/usr/bin/env bash
if [ "$1" = "auth" ] && [ "$2" = "token" ]; then
    echo "${MOCK_GH_TOKEN:-gho_mock_token_12345}"
    exit 0
fi
exit 0
"""


class TestRunAgentContainer(unittest.TestCase):

    def setUp(self):
        self.assertTrue(
            RUN_AGENT_CONTAINER_PATH.exists(),
            f"{RUN_AGENT_CONTAINER_PATH} does not exist",
        )
        self.assertTrue(
            os.access(RUN_AGENT_CONTAINER_PATH, os.X_OK),
            f"{RUN_AGENT_CONTAINER_PATH} is not executable",
        )

    def _create_mock_binary(
        self,
        bin_dir: Path,
        name: str,
        body: str,
    ) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        binary_path = bin_dir / name
        binary_path.write_text(body)
        binary_path.chmod(binary_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return binary_path

    def _setup_test_env(self, tmp_path: Path):
        mock_bin = tmp_path / "mock_bin"
        fake_home = tmp_path / "fake_home"
        fake_cwd = tmp_path / "fake_cwd"
        fake_home.mkdir(parents=True, exist_ok=True)
        fake_cwd.mkdir(parents=True, exist_ok=True)

        docker_log = tmp_path / "docker_calls.jsonl"
        counter_file = tmp_path / "counter.txt"
        workspace_file = tmp_path / "workspace_path.txt"

        self._create_mock_binary(mock_bin, "docker", MOCK_DOCKER_SCRIPT)
        self._create_mock_binary(mock_bin, "gh", MOCK_GH_SCRIPT)

        env = os.environ.copy()
        env["HOME"] = str(fake_home)
        env["PATH"] = f"{mock_bin}:{os.environ.get('PATH', '')}"
        env["INCOMPLETE_SETTLE_SECS"] = "0"
        env["MOCK_DOCKER_LOG"] = str(docker_log)
        env["MOCK_DOCKER_COUNTER_FILE"] = str(counter_file)
        env["MOCK_DOCKER_WORKSPACE_PATH_FILE"] = str(workspace_file)

        # Clear potentially interfering environment variables
        for key in [
            "GRAVITON_WORKSPACE_CACHE_DIR",
            "DEFAULT_AGENT",
            "ANTIGRAVITY_IMAGE",
            "ANTIGRAVITY_MODEL",
            "MODEL_NAME",
            "ANTIGRAVITY_QUOTA_POOL",
            "MAX_AGENT_RETRIES",
            "GRAVITON_INITIAL_ATTEMPT",
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "MOCK_DOCKER_RUN_FAIL",
            "MOCK_DOCKER_EXEC_FAIL",
            "MOCK_DOCKER_EXEC_OUTPUT",
            "MOCK_DOCKER_EXEC_EXIT_CODE",
            "MOCK_WORKSPACE_FILE_TO_CREATE",
            "MOCK_GH_TOKEN",
            "MOCK_DOCKER_TRANSCRIPT_PATH",
            "MOCK_DOCKER_COMPLETE_TRANSCRIPT_ON_ATTEMPT",
            "MOCK_CHECK_HOOK_FILE",
        ]:
            env.pop(key, None)

        return {
            "mock_bin": mock_bin,
            "fake_home": fake_home,
            "fake_cwd": fake_cwd,
            "docker_log": docker_log,
            "workspace_file": workspace_file,
            "env": env,
        }

    def _get_docker_calls(self, log_path: Path):
        if not log_path.exists():
            return []
        calls = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                calls.append(json.loads(line))
        return calls

    # -------------------------------------------------------------------------
    # A. CLI Argument Handling & Agent Resolution
    # -------------------------------------------------------------------------

    def test_no_arguments_prints_usage_and_exits_1(self):
        """Running with no arguments exits with code 1 and outputs usage instructions."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH)],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("Usage:", res.stdout)
            self.assertIn("[AGENT_NAME] <PROMPT>", res.stdout)
            self.assertEqual(len(self._get_docker_calls(ctx["docker_log"])), 0)

    def test_single_argument_prompt_with_default_agent(self):
        """Supplying a single argument routes it as PROMPT with default agent code_reviewer."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Implement feature X"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("Agent 'code_reviewer' completed successfully.", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            exec_calls = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]]
            self.assertEqual(len(exec_calls), 1)
            args = exec_calls[0]["args"]
            self.assertIn("--agent", args)
            self.assertEqual(args[args.index("--agent") + 1], "code_reviewer")
            self.assertIn("--prompt", args)
            self.assertEqual(args[args.index("--prompt") + 1], "Implement feature X")

    def test_multiple_arguments_agent_name_and_prompt(self):
        """Supplying two or more arguments routes the first as AGENT_NAME and remaining as PROMPT."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "code_fixer", "Fix typo in lib/runner.py"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("Agent 'code_fixer' completed successfully.", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            exec_calls = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]]
            self.assertEqual(len(exec_calls), 1)
            args = exec_calls[0]["args"]
            self.assertIn("--agent", args)
            self.assertEqual(args[args.index("--agent") + 1], "code_fixer")
            self.assertIn("--prompt", args)
            self.assertEqual(args[args.index("--prompt") + 1], "Fix typo in lib/runner.py")

    # -------------------------------------------------------------------------
    # B. Environment Variable Overrides
    # -------------------------------------------------------------------------

    def test_default_agent_env_override(self):
        """Verify DEFAULT_AGENT overrides default agent when single argument prompt is provided."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["DEFAULT_AGENT"] = "codebase_auditor"
            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Audit the codebase"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("Agent 'codebase_auditor' completed successfully.", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            exec_calls = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]]
            self.assertEqual(len(exec_calls), 1)
            args = exec_calls[0]["args"]
            self.assertIn("--agent", args)
            self.assertEqual(args[args.index("--agent") + 1], "codebase_auditor")

    def test_antigravity_image_override(self):
        """Verify ANTIGRAVITY_IMAGE overrides the docker image tag."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            custom_image = "custom-repo/agent:v2.0"
            ctx["env"]["ANTIGRAVITY_IMAGE"] = custom_image
            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Perform review"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_calls = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]]
            self.assertEqual(len(run_calls), 1)
            self.assertIn(custom_image, run_calls[0]["args"])

    def test_model_and_quota_pool_env_propagation(self):
        """Verify ANTIGRAVITY_MODEL and ANTIGRAVITY_QUOTA_POOL propagate to docker env and CLI flags."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["ANTIGRAVITY_MODEL"] = "gemini-3.8-flash"
            ctx["env"]["ANTIGRAVITY_QUOTA_POOL"] = "premium"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Run with custom model"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            run_args_str = " ".join(run_call["args"])
            self.assertIn("-e ANTIGRAVITY_MODEL=gemini-3.8-flash", run_args_str)
            self.assertIn("-e MODEL_NAME=gemini-3.8-flash", run_args_str)
            self.assertIn("-e ANTIGRAVITY_QUOTA_POOL=premium", run_args_str)

            exec_call = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]][0]
            exec_args = exec_call["args"]
            exec_args_str = " ".join(exec_args)
            self.assertIn("-e ANTIGRAVITY_MODEL=gemini-3.8-flash", exec_args_str)
            self.assertIn("-e MODEL_NAME=gemini-3.8-flash", exec_args_str)
            self.assertIn("-e ANTIGRAVITY_QUOTA_POOL=premium", exec_args_str)
            self.assertIn("--model", exec_args)
            self.assertEqual(exec_args[exec_args.index("--model") + 1], "gemini-3.8-flash")

    def test_max_agent_retries_override(self):
        """Verify MAX_AGENT_RETRIES stops execution after the configured number of failing attempts."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["MAX_AGENT_RETRIES"] = "2"
            ctx["env"]["MOCK_DOCKER_EXEC_EXIT_CODE"] = "42"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Task that fails"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 42)
            self.assertIn("Agent 'code_reviewer' failed after 2 attempts with exit code 42.", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            exec_calls = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]]
            self.assertEqual(len(exec_calls), 2)

    # -------------------------------------------------------------------------
    # C. Workspace Cache Restoration & Persistence
    # -------------------------------------------------------------------------

    def test_cache_restoration_when_cache_dir_exists(self):
        """Verify workspace is restored from GRAVITON_WORKSPACE_CACHE_DIR and runs with --continue."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ctx = self._setup_test_env(tmp_path)
            cache_dir = tmp_path / "workspace_cache"
            cache_dir.mkdir()
            (cache_dir / "persisted_artifact.txt").write_text("cache content")

            ctx["env"]["GRAVITON_WORKSPACE_CACHE_DIR"] = str(cache_dir)

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Resume cached work"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn(f"Restoring workspace from cache: {cache_dir}", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            exec_calls = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]]
            self.assertEqual(len(exec_calls), 1)
            args = exec_calls[0]["args"]
            self.assertIn("--continue", args)
            self.assertIn("Resume from your existing work in /workspace and complete your goal", args)

    def test_cache_sync_on_failure(self):
        """Verify that on failure, the workspace state is synced back into GRAVITON_WORKSPACE_CACHE_DIR."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ctx = self._setup_test_env(tmp_path)
            cache_dir = tmp_path / "workspace_cache"
            cache_dir.mkdir()

            ctx["env"]["GRAVITON_WORKSPACE_CACHE_DIR"] = str(cache_dir)
            ctx["env"]["MAX_AGENT_RETRIES"] = "1"
            ctx["env"]["MOCK_DOCKER_EXEC_EXIT_CODE"] = "1"
            ctx["env"]["MOCK_WORKSPACE_FILE_TO_CREATE"] = "work_in_progress.txt"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Failing agent with changes"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn(f"Syncing workspace to cache: {cache_dir}", res.stdout)
            self.assertTrue(cache_dir.exists())
            self.assertTrue((cache_dir / "work_in_progress.txt").exists())
            self.assertEqual(
                (cache_dir / "work_in_progress.txt").read_text(),
                "created_by_mock_agent",
            )

    def test_cache_cleanup_on_success(self):
        """Verify that on success, GRAVITON_WORKSPACE_CACHE_DIR is removed/cleaned up."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            ctx = self._setup_test_env(tmp_path)
            cache_dir = tmp_path / "workspace_cache"
            cache_dir.mkdir()
            (cache_dir / "old_checkpoint.txt").write_text("checkpoint")

            ctx["env"]["GRAVITON_WORKSPACE_CACHE_DIR"] = str(cache_dir)

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Successful agent run"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn(f"Cleaning up workspace cache on success: {cache_dir}", res.stdout)
            self.assertFalse(cache_dir.exists())

    # -------------------------------------------------------------------------
    # D. Volume Mounts & Security Flags
    # -------------------------------------------------------------------------

    def test_security_opt_flag(self):
        """Verify --security-opt=no-new-privileges is present in docker run arguments."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Security flag test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            self.assertIn("--security-opt=no-new-privileges", run_call["args"])

    def test_conditional_directory_mounts(self):
        """Verify mounts for .ssh, .config/gh, antigravity-cli, and skills when present."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            fake_home = ctx["fake_home"]
            (fake_home / ".ssh").mkdir(parents=True, exist_ok=True)
            (fake_home / ".config" / "gh").mkdir(parents=True, exist_ok=True)
            (fake_home / ".gemini" / "antigravity-cli").mkdir(parents=True, exist_ok=True)

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Directory mounts test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            run_args_str = " ".join(run_call["args"])

            self.assertIn(f"{fake_home}/.ssh:/root/.ssh:ro", run_args_str)
            self.assertIn(f"{fake_home}/.config/gh:/root/.config/gh:ro", run_args_str)
            self.assertIn(f"{fake_home}/.gemini/antigravity-cli:/root/.gemini/antigravity-cli", run_args_str)
            if (REPO_ROOT / "plugin" / "skills").is_dir():
                self.assertIn(f"{REPO_ROOT}/plugin/skills:/root/.gemini/config/skills:ro", run_args_str)
            elif (REPO_ROOT / "skills").is_dir():
                self.assertIn(f"{REPO_ROOT}/skills:/root/.gemini/config/skills:ro", run_args_str)
            if (REPO_ROOT / "plugin" / "agents").is_dir():
                self.assertIn(f"{REPO_ROOT}/plugin/agents:/root/.gemini/config/agents:ro", run_args_str)
            elif (REPO_ROOT / "agents").is_dir():
                self.assertIn(f"{REPO_ROOT}/agents:/root/.gemini/config/agents:ro", run_args_str)

    def test_conditional_mounts_omitted_when_absent(self):
        """Verify .ssh and .config/gh mounts are omitted when the directories do not exist."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            # fake_home has no .ssh or .config/gh

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "No mounts test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            run_args_str = " ".join(run_call["args"])

            self.assertNotIn(":/root/.ssh:ro", run_args_str)
            self.assertNotIn(":/root/.config/gh:ro", run_args_str)
            self.assertNotIn(":/root/.gemini/antigravity-cli", run_args_str)

    # -------------------------------------------------------------------------
    # E. Retry Loop & Transcript Completion Evaluation
    # -------------------------------------------------------------------------

    def test_step_limit_retry_with_continue_flag(self):
        """Simulate attempt 1 hitting a step limit with incomplete transcript; verify attempt 2 uses --continue."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            conv_id = "11111111-2222-3333-4444-555555555555"

            # Create an incomplete transcript for attempt 1
            transcript_dir = (
                ctx["fake_home"]
                / ".gemini"
                / "antigravity-cli"
                / "brain"
                / conv_id
                / ".system_generated"
                / "logs"
            )
            transcript_dir.mkdir(parents=True, exist_ok=True)
            incomplete_transcript = transcript_dir / "transcript.jsonl"
            incomplete_transcript.write_text(
                '{"step_index": 0, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]}\n'
            )

            # Output conversation ID on attempt 1, and complete the transcript on attempt 2
            ctx["env"]["MOCK_DOCKER_OUTPUT_ATTEMPT_1"] = f"Log header\nConversation ID: {conv_id}\nWorking..."
            ctx["env"]["MOCK_DOCKER_OUTPUT_ATTEMPT_2"] = "Finished all tasks successfully without new calls."
            ctx["env"]["MOCK_DOCKER_TRANSCRIPT_PATH"] = str(incomplete_transcript)
            ctx["env"]["MOCK_DOCKER_COMPLETE_TRANSCRIPT_ON_ATTEMPT"] = "2"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Multi-step task"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("Auto-continuing conversation (Attempt 2/3)...", res.stdout)
            self.assertIn("Agent 'code_reviewer' completed successfully.", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            exec_calls = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]]
            self.assertEqual(len(exec_calls), 2)

            # Attempt 1 has initial prompt and no --continue
            self.assertNotIn("--continue", exec_calls[0]["args"])
            self.assertIn("Multi-step task", exec_calls[0]["args"])

            # Attempt 2 has --continue flag
            self.assertIn("--continue", exec_calls[1]["args"])
            self.assertIn("Resume from your existing work in /workspace and complete your goal", exec_calls[1]["args"])

    def test_exhausted_retries_propagation(self):
        """Simulate persistent incomplete transcripts through MAX_ATTEMPTS; verify failure exit code."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            conv_id = "22222222-3333-4444-5555-666666666666"
            ctx["env"]["MAX_AGENT_RETRIES"] = "2"

            transcript_dir = (
                ctx["fake_home"]
                / ".gemini"
                / "antigravity-cli"
                / "brain"
                / conv_id
                / ".system_generated"
                / "logs"
            )
            transcript_dir.mkdir(parents=True, exist_ok=True)
            (transcript_dir / "transcript.jsonl").write_text(
                '{"step_index": 0, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command"}]}\n'
            )

            ctx["env"]["MOCK_DOCKER_EXEC_OUTPUT"] = f"Session ID: {conv_id}"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Never-ending task"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 1)
            self.assertIn("Agent 'code_reviewer' failed after 2 attempts with exit code 1.", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            exec_calls = [c for c in calls if c["args"] and c["args"][0] == "exec" and "agy" in c["args"]]
            self.assertEqual(len(exec_calls), 2)

    # -------------------------------------------------------------------------
    # F. Exit Trap Execution & Cleanup
    # -------------------------------------------------------------------------

    def test_cleanup_trap_removes_container_and_temp_workspace(self):
        """Verify cleanup trap removes container instance and ephemeral workspace on exit."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Cleanup test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            # Ephemeral workspace was recorded during docker run and must be cleaned up
            ws_file = ctx["workspace_file"]
            self.assertTrue(ws_file.exists())
            ephemeral_ws = Path(ws_file.read_text().strip())
            self.assertTrue(str(ephemeral_ws).startswith("/tmp/graviton-workspaces/run-"))
            self.assertFalse(ephemeral_ws.exists())

            # docker rm -f was called on the container
            calls = self._get_docker_calls(ctx["docker_log"])
            rm_calls = [c for c in calls if c["args"] and c["args"][0] == "rm" and "-f" in c["args"]]
            self.assertEqual(len(rm_calls), 1)
            container_arg = rm_calls[0]["args"][-1]
            self.assertTrue(container_arg.startswith("graviton-agent-run-"))

    def test_cleanup_trap_on_failure(self):
        """Verify cleanup trap removes container and workspace even when execution fails."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["MAX_AGENT_RETRIES"] = "1"
            ctx["env"]["MOCK_DOCKER_EXEC_EXIT_CODE"] = "99"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Failing cleanup test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 99)

            ws_file = ctx["workspace_file"]
            self.assertTrue(ws_file.exists())
            ephemeral_ws = Path(ws_file.read_text().strip())
            self.assertFalse(ephemeral_ws.exists())

            calls = self._get_docker_calls(ctx["docker_log"])
            rm_calls = [c for c in calls if c["args"] and c["args"][0] == "rm" and "-f" in c["args"]]
            self.assertEqual(len(rm_calls), 1)

    def test_cleanup_trap_early_exit_removes_temp_workspace(self):
        """Verify cleanup trap removes ephemeral workspace when script aborts prior to container execution."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            mock_dirname_script = """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

workspace_file = os.environ.get("MOCK_DOCKER_WORKSPACE_PATH_FILE")
workspaces_dir = Path("/tmp/graviton-workspaces")
if workspaces_dir.exists():
    run_dirs = sorted(workspaces_dir.glob("run-*"), key=lambda p: p.stat().st_mtime, reverse=True)
    if run_dirs and workspace_file:
        Path(workspace_file).write_text(str(run_dirs[0]))

print("/nonexistent/directory/for/testing/failure")
sys.exit(0)
"""
            self._create_mock_binary(ctx["mock_bin"], "dirname", mock_dirname_script)

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Early exit test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertNotEqual(res.returncode, 0)
            self.assertNotIn("USE_CONTAINER_EXEC: unbound variable", res.stderr)

            ws_file = ctx["workspace_file"]
            self.assertTrue(ws_file.exists())
            ephemeral_ws = Path(ws_file.read_text().strip())
            self.assertTrue(str(ephemeral_ws).startswith("/tmp/graviton-workspaces/run-"))
            self.assertFalse(ephemeral_ws.exists())

            # Verify no container was created since exit occurred before container launch
            calls = self._get_docker_calls(ctx["docker_log"])
            run_calls = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]]
            self.assertEqual(len(run_calls), 0)

    # -------------------------------------------------------------------------
    # G. Additional Fallbacks, Environment Forwarding, and Tool Mounts
    # -------------------------------------------------------------------------

    def test_docker_run_fallback_when_daemon_fails_container_exec(self):
        """Verify script falls back to docker run --rm per attempt when background container fails."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["MOCK_DOCKER_RUN_FAIL"] = "1"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Run fallback test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("Agent 'code_reviewer' completed successfully.", res.stdout)

            calls = self._get_docker_calls(ctx["docker_log"])
            # Background run -d failed
            self.assertTrue(any(c["args"][:2] == ["run", "-d"] for c in calls))
            # Executed via docker run --rm
            run_rm_calls = [c for c in calls if c["args"] and c["args"][:2] == ["run", "--rm"] and "agy" in c["args"]]
            self.assertEqual(len(run_rm_calls), 1)
            self.assertIn("--agent", run_rm_calls[0]["args"])
            self.assertIn("code_reviewer", run_rm_calls[0]["args"])

    def test_docker_run_fallback_mounts_config_and_instance_name(self):
        """Verify fallback docker run --rm includes config.json mount and ANTIGRAVITY_INSTANCE_NAME."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["MOCK_DOCKER_RUN_FAIL"] = "1"
            ctx["env"]["ANTIGRAVITY_INSTANCE_NAME"] = "Fallback-Instance-01"

            config_dir = Path(ctx["env"]["HOME"]) / ".gemini" / "config"
            config_dir.mkdir(parents=True)
            config_file = config_dir / "config.json"
            config_file.write_text('{"cliRemoteControlHostname": "fallback-remote-host"}')

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Fallback mount test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_rm_calls = [c for c in calls if c["args"] and c["args"][:2] == ["run", "--rm"] and "agy" in c["args"]]
            self.assertEqual(len(run_rm_calls), 1)
            expected_mount = f"{config_file.resolve()}:/root/.gemini/config/config.json:ro"
            self.assertIn(expected_mount, run_rm_calls[0]["args"])
            self.assertIn("ANTIGRAVITY_INSTANCE_NAME=Fallback-Instance-01", run_rm_calls[0]["args"])

    def test_git_identity_and_token_forwarding(self):
        """Verify GIT_AUTHOR_NAME, GIT_AUTHOR_EMAIL, and GITHUB_TOKEN forward into the container."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["GIT_AUTHOR_NAME"] = "Custom Contributor"
            ctx["env"]["GIT_AUTHOR_EMAIL"] = "contrib@example.com"
            ctx["env"]["MOCK_GH_TOKEN"] = "gho_test_secret_token_123"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Identity forwarding test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            run_args_str = " ".join(run_call["args"])

            self.assertIn("GIT_AUTHOR_NAME=Custom Contributor", run_args_str)
            self.assertIn("GIT_AUTHOR_EMAIL=contrib@example.com", run_args_str)
            self.assertIn("GIT_COMMITTER_NAME=Custom Contributor", run_args_str)
            self.assertIn("GIT_COMMITTER_EMAIL=contrib@example.com", run_args_str)
            self.assertIn("GITHUB_TOKEN=gho_test_secret_token_123", run_args_str)

    def test_agy_binary_mount_when_available(self):
        """Verify that when agy binary is available on PATH, it is mounted into /usr/local/bin/agy:ro."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            mock_agy = self._create_mock_binary(ctx["mock_bin"], "agy", "#!/usr/bin/env bash\nexit 0\n")

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Agy mount test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            run_args_str = " ".join(run_call["args"])
            self.assertIn(f"{mock_agy}:/usr/local/bin/agy:ro", run_args_str)

    def test_pre_commit_hook_configured_when_present(self):
        """Verify .githooks/pre-commit in the workspace is marked executable and configured in git."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            hook_dir = ctx["fake_cwd"] / ".githooks"
            hook_dir.mkdir(parents=True, exist_ok=True)
            hook_file = hook_dir / "pre-commit"
            hook_file.write_text("#!/bin/sh\nexit 0\n")
            hook_file.chmod(0o644)  # Non-executable initially

            ctx["env"]["MOCK_CHECK_HOOK_FILE"] = "1"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Hook test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("HOOK_IS_EXECUTABLE", res.stdout)

    def test_workspace_initialization_from_git_repo(self):
        """Verify workspace cloning when current working directory is an initialized git repository."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            fake_cwd = ctx["fake_cwd"]
            # Initialize a git repository with a committed file
            subprocess.run(["git", "init", "-q"], cwd=str(fake_cwd), check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(fake_cwd), check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(fake_cwd), check=True)
            (fake_cwd / "sample.py").write_text("print('hello world')\n")
            subprocess.run(["git", "add", "sample.py"], cwd=str(fake_cwd), check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial commit"], cwd=str(fake_cwd), check=True)

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Git repo clone test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(fake_cwd),
            )
            self.assertEqual(res.returncode, 0)
            self.assertIn("Agent 'code_reviewer' completed successfully.", res.stdout)

    def test_antigravity_instance_name_with_spaces(self):
        """Verify that ANTIGRAVITY_INSTANCE_NAME with whitespace is preserved without word-splitting."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            ctx["env"]["ANTIGRAVITY_INSTANCE_NAME"] = "Workstation 1 With Spaces"

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Instance name test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            self.assertIn("-e", run_call["args"])
            self.assertIn("ANTIGRAVITY_INSTANCE_NAME=Workstation 1 With Spaces", run_call["args"])

    def test_config_json_mount(self):
        """Verify that ~/.gemini/config/config.json is mounted into /root/.gemini/config/config.json:ro."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ctx = self._setup_test_env(Path(tmp_dir))
            config_dir = Path(ctx["env"]["HOME"]) / ".gemini" / "config"
            config_dir.mkdir(parents=True)
            config_file = config_dir / "config.json"
            config_file.write_text('{"cliRemoteControlHostname": "test-remote-host"}')

            res = subprocess.run(
                [str(RUN_AGENT_CONTAINER_PATH), "Config mount test"],
                capture_output=True,
                text=True,
                env=ctx["env"],
                cwd=str(ctx["fake_cwd"]),
            )
            self.assertEqual(res.returncode, 0)

            calls = self._get_docker_calls(ctx["docker_log"])
            run_call = [c for c in calls if c["args"] and c["args"][0] == "run" and "-d" in c["args"]][0]
            self.assertIn("-v", run_call["args"])
            expected_mount = f"{config_file.resolve()}:/root/.gemini/config/config.json:ro"
            self.assertIn(expected_mount, run_call["args"])


if __name__ == "__main__":
    unittest.main()
