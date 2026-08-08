"""
Agent container execution runner for Graviton.
"""

import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("graviton.runner")


def run_agent_container(
    agent_name: str,
    prompt: str,
    script_path: Path,
    cwd: Path,
) -> subprocess.CompletedProcess:
    """
    Execute the agent container script synchronously.

    :param agent_name: Name of agent specification (e.g. 'code_reviewer').
    :param prompt: Prompt instruction string for agent.
    :param script_path: Path to run_agent_container.sh.
    :param cwd: Working directory (repository root).
    :return: subprocess.CompletedProcess instance.
    """
    cmd = [str(script_path), agent_name, prompt]
    logger.info(f"Triggering agent '{agent_name}' with prompt: '{prompt}'")
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def run_agent_async(
    agent_name: str,
    prompt: str,
    script_path: Path,
    cwd: Path,
) -> threading.Thread:
    """
    Execute the agent container asynchronously in a background daemon thread.

    :param agent_name: Name of agent specification.
    :param prompt: Prompt instruction string for agent.
    :param script_path: Path to run_agent_container.sh.
    :param cwd: Working directory.
    :return: Started daemon Thread instance.
    """
    def worker():
        try:
            result = run_agent_container(agent_name, prompt, script_path, cwd)
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
