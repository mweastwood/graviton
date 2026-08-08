"""
Self-update and hot reload utilities for Graviton server.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("graviton.updater")

SERVER_START_TIME = time.time()

_HOT_RELOAD_STATE = "IDLE"
_HOT_RELOAD_LOCK = threading.Lock()


def get_hot_reload_state() -> str:
    """Return the current hot reload status (IDLE, PULLING_GIT, REBUILDING_CONTAINER, RELOADING)."""
    with _HOT_RELOAD_LOCK:
        return _HOT_RELOAD_STATE


def set_hot_reload_state(state: str):
    """Update current hot reload status."""
    global _HOT_RELOAD_STATE
    with _HOT_RELOAD_LOCK:
        _HOT_RELOAD_STATE = state


def get_uptime_seconds() -> float:
    """Return total uptime of the server in seconds."""
    return time.time() - SERVER_START_TIME


def get_uptime_str() -> str:
    """Return formatted uptime string HH:MM:SS."""
    secs = int(get_uptime_seconds())
    hours, remainder = divmod(secs, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def get_git_info(repo_root: Optional[Path] = None) -> Tuple[str, str]:
    """
    Retrieve current git commit SHA and branch name.

    :param repo_root: Optional Path to repository root.
    :return: Tuple (commit_sha, branch_name).
    """
    cwd = str(repo_root) if repo_root else None
    commit = "unknown"
    branch = "unknown"
    try:
        res_sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res_sha.returncode == 0 and res_sha.stdout.strip():
            commit = res_sha.stdout.strip()

        res_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res_branch.returncode == 0 and res_branch.stdout.strip():
            branch = res_branch.stdout.strip()
    except Exception:
        pass

    return commit, branch


def perform_git_pull(repo_root: Path, branch: str = "main") -> Tuple[bool, str]:
    """
    Execute git pull origin <branch> in repo_root.

    :param repo_root: Path to repository root.
    :param branch: Branch name to pull (e.g. 'main' or 'master').
    :return: Tuple (success: bool, output: str).
    """
    try:
        res = subprocess.run(
            ["git", "pull", "origin", branch],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = (res.stdout or "") + (res.stderr or "")
        return (res.returncode == 0, output.strip())
    except Exception as e:
        logger.exception(f"Failed to execute git pull on branch '{branch}': {e}")
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


def hot_reload_server(httpd=None):
    """
    Hot reload the running Python server process by re-executing sys.executable.

    :param httpd: Optional HTTPServer instance to close sockets gracefully before execv.
    """
    logger.info("Hot reloading Graviton server process (os.execv)...")

    if httpd is not None:
        try:
            logger.info("Closing server listening socket before process re-execution...")
            httpd.server_close()
        except Exception as e:
            logger.warning(f"Error closing server socket: {e}")

    # Flush all logging handlers
    for handler in logging.root.handlers[:]:
        handler.flush()

    # Re-execute current Python binary with original command-line arguments
    os.execv(sys.executable, [sys.executable] + sys.argv)


def sync_repo_and_reload(repo_root: Path, ref: str = "refs/heads/main", httpd=None):
    """
    Pull latest git commits for target branch, rebuild Docker image if necessary, and hot-reload server.

    :param repo_root: Path to repository root.
    :param ref: Git ref from push webhook payload (e.g. 'refs/heads/main').
    :param httpd: Optional HTTPServer instance.
    """
    branch = ref.split("/")[-1] if "/" in ref else "main"
    logger.info(f"Self-update triggered: Pulling latest commits from branch '{branch}'...")
    set_hot_reload_state("PULLING_GIT")
    success, git_output = perform_git_pull(repo_root, branch=branch)

    if not success:
        logger.error(f"Git pull failed for branch '{branch}':\n{git_output}")
        set_hot_reload_state("IDLE")
        return

    logger.info(f"Git pull output:\n{git_output}")

    if check_if_dockerfile_changed(git_output):
        set_hot_reload_state("REBUILDING_CONTAINER")
        rebuild_agent_container(repo_root)

    set_hot_reload_state("RELOADING")
    hot_reload_server(httpd=httpd)

