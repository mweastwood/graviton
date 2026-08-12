"""
Agent container execution runner for Graviton.
"""

import json
import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional, Union

logger = logging.getLogger("graviton.runner")


def is_transcript_incomplete(transcript_path: Union[str, Path]) -> bool:
    """
    Check if an agy agent session transcript ended mid-task with unexecuted tool calls.

    :param transcript_path: Path to transcript.jsonl file.
    :return: True if last step is a PLANNER_RESPONSE with non-empty tool_calls, False otherwise.
    """
    try:
        path = Path(transcript_path)
        if not path.is_file():
            return False
        last_line = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    last_line = stripped
        if not last_line:
            return False
        last_step = json.loads(last_line)
        if not isinstance(last_step, dict):
            return False
        if last_step.get("type") == "PLANNER_RESPONSE":
            tool_calls = last_step.get("tool_calls", [])
            if isinstance(tool_calls, list) and tool_calls:
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

    process = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

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
    :return: Started daemon Thread instance.
    """
    def worker():
        try:
            result = run_agent_container(
                agent_name,
                prompt,
                script_path,
                cwd,
                max_attempts=max_attempts,
                cached_workspace_dir=cached_workspace_dir,
                initial_attempt=initial_attempt,
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
        if is_transcript_incomplete(target_file):
            sys.exit(0)
        else:
            sys.exit(1)
    else:
        sys.exit(2)

