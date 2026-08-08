"""
Self-update and hot reload utilities for Graviton server.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Tuple

logger = logging.getLogger("graviton.updater")


def perform_git_pull(repo_root: Path) -> Tuple[bool, str]:
    """
    Execute git pull origin main in repo_root.

    :param repo_root: Path to repository root.
    :return: Tuple (success: bool, output: str).
    """
    try:
        res = subprocess.run(
            ["git", "pull", "origin", "main"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (res.stdout or "") + (res.stderr or "")
        return (res.returncode == 0, output.strip())
    except Exception as e:
        logger.exception(f"Failed to execute git pull: {e}")
        return (False, str(e))


def check_if_dockerfile_changed(git_output: str) -> bool:
    """
    Check if Dockerfile or build script were modified in git pull output.

    :param git_output: Output from git pull.
    :return: True if Dockerfile/build script was updated, False otherwise.
    """
    if not git_output:
        return False
    targets = ("Dockerfile", "bin/build_agent_container.sh")
    return any(target in git_output for target in targets)


def rebuild_agent_container(repo_root: Path) -> bool:
    """
    Run ./bin/build_agent_container.sh script to update the Docker container image.

    :param repo_root: Path to repository root.
    :return: True if build succeeded, False otherwise.
    """
    build_script = repo_root / "bin" / "build_agent_container.sh"
    if not build_script.exists():
        logger.warning(f"Build script not found at: {build_script}")
        return False

    try:
        logger.info("Rebuilding agent Docker container image...")
        res = subprocess.run(
            [str(build_script)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if res.returncode == 0:
            logger.info("Agent Docker container rebuild completed successfully.")
            return True
        else:
            logger.error(f"Agent container rebuild failed (code {res.returncode}): {res.stderr}")
            return False
    except Exception as e:
        logger.exception(f"Error rebuilding agent container: {e}")
        return False


def hot_reload_server():
    """
    Hot reload the running Python server process by re-executing sys.executable.
    """
    logger.info("Hot reloading Graviton server process (os.execv)...")

    # Flush all logging handlers
    for handler in logging.root.handlers[:]:
        handler.flush()

    # Re-execute current Python binary with original command-line arguments
    os.execv(sys.executable, [sys.executable] + sys.argv)


def sync_repo_and_reload(repo_root: Path):
    """
    Pull latest git commits, rebuild Docker image if necessary, and hot-reload server.

    :param repo_root: Path to repository root.
    """
    logger.info("Self-update triggered: Pulling latest commits from main...")
    success, git_output = perform_git_pull(repo_root)

    if not success:
        logger.error(f"Git pull failed:\n{git_output}")
        return

    logger.info(f"Git pull output:\n{git_output}")

    if check_if_dockerfile_changed(git_output):
        rebuild_agent_container(repo_root)

    hot_reload_server()
