"""
Agent container execution runner for Graviton.
"""

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Union

logger = logging.getLogger("graviton.runner")


def is_transcript_incomplete(
    transcript_path: Union[str, Path],
    agent_name: Optional[str] = None,
) -> bool:
    """
    Check if an agy agent session transcript ended prematurely.

    Returns True if:
    1. The last step is a PLANNER_RESPONSE with unexecuted non-empty tool_calls.
    2. Any background command task was launched and has not finished or been canceled.
    3. The final response indicates the agent ended the turn waiting for background commands/tests.
    4. The agent is 'pr_drafter' and no 'gh pr create' invocation occurred anywhere in the transcript.

    :param transcript_path: Path to transcript.jsonl file.
    :param agent_name: Optional name of the running agent (e.g. 'pr_drafter').
    :return: True if session is incomplete and should be resumed/continued, False otherwise.
    """
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return False

        steps = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    try:
                        data = json.loads(stripped)
                        if isinstance(data, dict):
                            steps.append(data)
                    except Exception:
                        continue

        if not steps:
            return False

        # 1. Check for unexecuted tool calls at the end of the session
        last_step = steps[-1]
        if last_step.get("type") == "PLANNER_RESPONSE":
            tool_calls = last_step.get("tool_calls", [])
            if isinstance(tool_calls, list) and tool_calls:
                return True

        # 2. Check for uncompleted background commands
        bg_launch_pattern = re.compile(r"Tool is running as a background task with task id:\s*([^\s\r\n]+)")
        bg_finish_pattern = re.compile(r'Task id\s+"([^"]+)"\s+(?:finished|was canceled)\s+with result:')
        active_bg_commands = set()
        for step in steps:
            content = step.get("content") or ""
            if isinstance(content, str):
                m_launch = bg_launch_pattern.search(content)
                if m_launch:
                    tid = m_launch.group(1)
                    m_desc = re.search(r"Task Description:\s*(.*)", content)
                    if not (m_desc and m_desc.group(1).startswith("Timer:")):
                        active_bg_commands.add(tid)
                for tid in bg_finish_pattern.findall(content):
                    active_bg_commands.discard(tid)

        if active_bg_commands:
            return True

        # 3. Check if final response indicates waiting for background task / test completion
        waiting_pattern = re.compile(
            r"(?i)\b(?:waiting for (?:them|it|the tests?|the command|the task|completion)|launched the .* and am waiting|waiting on the (?:tests?|command|task)|will wait for the (?:tests?|command|task))\b"
        )
        if last_step.get("type") == "PLANNER_RESPONSE":
            last_content = last_step.get("content") or ""
            if isinstance(last_content, str) and waiting_pattern.search(last_content):
                return True

        # 4. Agent-specific deliverables: pr_drafter must have executed gh pr create
        if agent_name == "pr_drafter":
            pr_created = False
            for step in steps:
                for call in step.get("tool_calls") or []:
                    if isinstance(call, dict):
                        args = call.get("args") or {}
                        if isinstance(args, dict):
                            cmd = args.get("CommandLine") or ""
                            if isinstance(cmd, str) and re.search(r"\bgh\s+pr\s+create\b", cmd):
                                pr_created = True
                                break
                if pr_created:
                    break
            if not pr_created:
                return True

    except Exception as e:
        logger.debug(f"Error checking transcript completeness for '{transcript_path}': {e}")
    return False



def run_agent_container(
    agent_name: str,
    prompt: str,
    script_path: Path,
    cwd: Path,
    on_output: Optional[Callable[[str], None]] = None,
    max_attempts: Optional[int] = None,
    cached_workspace_dir: Optional[Path] = None,
    initial_attempt: Optional[int] = None,
    quota_pool: Optional[str] = None,
    model: Optional[str] = None,
    on_process_created: Optional[Callable[[subprocess.Popen], None]] = None,
) -> subprocess.CompletedProcess:
    """
    Execute the agent container script synchronously.

    :param agent_name: Name of agent specification (e.g. 'code_reviewer').
    :param prompt: Prompt instruction string for agent.
    :param script_path: Path to run_agent_container.sh.
    :param cwd: Working directory (repository root).
    :param on_output: Optional callback function invoked for each line of stdout/stderr output.
    :param max_attempts: Optional maximum agent retry attempt limit (sets MAX_AGENT_RETRIES env var).
    :param cached_workspace_dir: Optional path to workspace cache directory (sets GRAVITON_WORKSPACE_CACHE_DIR env var).
    :param initial_attempt: Optional initial attempt number to resume execution pass (sets GRAVITON_INITIAL_ATTEMPT env var).
    :param quota_pool: Optional quota pool selected for task execution (sets ANTIGRAVITY_QUOTA_POOL env var).
    :param model: Optional model selected for task execution (sets ANTIGRAVITY_MODEL & MODEL_NAME env vars).
    :param on_process_created: Optional callback function invoked immediately upon subprocess launch.
    :return: subprocess.CompletedProcess instance.
    """
    cmd = [str(script_path), agent_name, prompt]
    logger.info(f"Triggering agent '{agent_name}' with prompt: '{prompt}'")

    env = os.environ.copy()
    if max_attempts is not None:
        env["MAX_AGENT_RETRIES"] = str(max_attempts)
    if cached_workspace_dir is not None:
        env["GRAVITON_WORKSPACE_CACHE_DIR"] = str(cached_workspace_dir)
    if initial_attempt is not None:
        env["GRAVITON_INITIAL_ATTEMPT"] = str(initial_attempt)
    if quota_pool is not None:
        env["ANTIGRAVITY_QUOTA_POOL"] = str(quota_pool)
    if model is not None:
        env["ANTIGRAVITY_MODEL"] = str(model)
        env["MODEL_NAME"] = str(model)

    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    if on_process_created:
        try:
            on_process_created(process)
        except Exception as e:
            logger.debug(f"Error in on_process_created callback: {e}")

    stdout_lines = []
    stderr_lines = []

    def read_stream(stream, lines_list):
        if not stream:
            return
        for line in stream:
            lines_list.append(line)
            if on_output:
                try:
                    on_output(line)
                except Exception as e:
                    logger.debug(f"Error in on_output callback: {e}")

    t_out = threading.Thread(target=read_stream, args=(process.stdout, stdout_lines), daemon=True)
    t_err = threading.Thread(target=read_stream, args=(process.stderr, stderr_lines), daemon=True)
    t_out.start()
    t_err.start()

    process.wait()
    t_out.join()
    t_err.join()

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=process.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
    )


def run_agent_async(
    agent_name: str,
    prompt: str,
    script_path: Path,
    cwd: Path,
    max_attempts: Optional[int] = None,
    cached_workspace_dir: Optional[Path] = None,
    initial_attempt: Optional[int] = None,
    quota_pool: Optional[str] = None,
    model: Optional[str] = None,
    on_process_created: Optional[Callable[[subprocess.Popen], None]] = None,
) -> threading.Thread:
    """
    Execute the agent container asynchronously in a background daemon thread.

    :param agent_name: Name of agent specification.
    :param prompt: Prompt instruction string for agent.
    :param script_path: Path to run_agent_container.sh.
    :param cwd: Working directory.
    :param max_attempts: Optional maximum agent retry attempt limit.
    :param cached_workspace_dir: Optional path to workspace cache directory.
    :param initial_attempt: Optional initial attempt number to resume execution pass.
    :param quota_pool: Optional quota pool selected for task execution.
    :param model: Optional model selected for task execution.
    :param on_process_created: Optional callback function invoked immediately upon subprocess launch.
    :return: Started daemon Thread instance.
    """
    def worker():
        try:
            kwargs = {
                "max_attempts": max_attempts,
                "cached_workspace_dir": cached_workspace_dir,
                "initial_attempt": initial_attempt,
                "quota_pool": quota_pool,
                "model": model,
            }
            if on_process_created is not None:
                kwargs["on_process_created"] = on_process_created
            result = run_agent_container(
                agent_name,
                prompt,
                script_path,
                cwd,
                **kwargs,
            )
            if result.returncode == 0:
                logger.info(f"Agent '{agent_name}' finished successfully for prompt: '{prompt}'")
                if result.stdout:
                    logger.info(f"Agent stdout:\n{result.stdout.strip()}")
            else:
                logger.error(f"Agent '{agent_name}' failed with exit code {result.returncode}")
                if result.stderr:
                    logger.error(f"Agent stderr:\n{result.stderr.strip()}")
        except Exception as e:
            logger.exception(f"Error executing agent container script: {e}")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        target_file = sys.argv[1]
        agent = sys.argv[2] if len(sys.argv) > 2 else None
        if is_transcript_incomplete(target_file, agent_name=agent):
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        sys.exit(2)

