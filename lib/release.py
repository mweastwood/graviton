"""
Release Management and Tagging Module for Graviton.

Handles per-repository release configurations (.graviton.json), dedicated release
issue detection, authorization, and background execution of release tagging scripts
(e.g., bin/tag.sh patch/minor/major).
"""

import json
import logging
import os
import re
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from lib.security import BOT_MARKER, is_valid_repo_name

logger = logging.getLogger("graviton.release")

RELEASE_BOT_TAG = f"{BOT_MARKER}\n<!-- graviton:release -->"

CONFIG_FILENAMES = [".graviton.json", "graviton.json"]
DEFAULT_RELEASE_ISSUE_PATTERN = r"(?i)^🚀?\s*release(?:\s+(?:controller|tracker))?\s*$"
DEFAULT_BRANCH = "main"
DEFAULT_COMMANDS = {
    "patch": "bin/tag.sh patch",
    "minor": "bin/tag.sh minor",
    "major": "bin/tag.sh major",
}
_HELP_COMMANDS = {"help", "/help", "?", "info", "/info"}

_release_locks: Dict[str, threading.Lock] = {}
_release_locks_guard = threading.Lock()


def clear_release_locks() -> None:
    """Clear cached repository release locks (useful for unit testing)."""
    with _release_locks_guard:
        _release_locks.clear()


def get_repo_release_lock(repo_identifier: str) -> threading.Lock:
    """
    Get or create a thread lock for a repository to prevent concurrent releases.

    :param repo_identifier: Unique string identifier for repository (e.g. repo path or full_name).
    :return: threading.Lock instance.
    """
    with _release_locks_guard:
        if repo_identifier not in _release_locks:
            _release_locks[repo_identifier] = threading.Lock()
        return _release_locks[repo_identifier]


def resolve_repo_dir(
    repo_name: Optional[str],
    repo_root: Optional[Path] = None,
    repos_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Resolve the local filesystem directory for a repository.

    :param repo_name: Base repository name (e.g. 'myapp').
    :param repo_root: Path to Graviton's server repository root.
    :param repos_dir: Path to directory containing managed repositories.
    :return: Resolved Path if it exists and is safe, else None.
    """
    if repos_dir and repo_name:
        if is_valid_repo_name(repo_name):
            repos_dir_path = Path(repos_dir).expanduser().resolve()
            candidate = (repos_dir_path / repo_name).resolve()
            if candidate != repos_dir_path and repos_dir_path in candidate.parents and candidate.is_dir():
                return candidate

    if repo_root:
        root_path = Path(repo_root).expanduser().resolve()
        if repo_name:
            if root_path.name == repo_name and root_path.is_dir():
                return root_path
        elif root_path.is_dir():
            return root_path

    return None


def load_release_config(repo_dir: Optional[Union[str, Path]]) -> Optional[Dict[str, Any]]:
    """
    Load the 'release' section from .graviton.json or graviton.json in repo_dir.

    :param repo_dir: Path to repository root directory.
    :return: Dictionary of release configuration or None if not configured.
    """
    if not repo_dir:
        return None

    path = Path(repo_dir)
    if not path.is_dir():
        return None

    for filename in CONFIG_FILENAMES:
        config_file = path / filename
        if config_file.is_file():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    release_cfg = data.get("release")
                    if isinstance(release_cfg, dict):
                        return release_cfg
            except Exception as e:
                logger.warning(f"Failed to read release config from '{config_file}': {e}")

    return None


def is_release_issue(
    issue_title: str,
    release_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Check if an issue title matches the release issue pattern.

    :param issue_title: The title of the GitHub issue.
    :param release_config: Optional release configuration dictionary from .graviton.json.
    :return: True if issue title matches pattern, False otherwise.
    """
    if not issue_title:
        return False

    pattern_str = DEFAULT_RELEASE_ISSUE_PATTERN
    if release_config and isinstance(release_config.get("issue_pattern"), str):
        pattern_str = release_config["issue_pattern"]

    try:
        return bool(re.search(pattern_str, issue_title.strip()))
    except re.error as e:
        logger.warning(f"Invalid regex in release issue_pattern '{pattern_str}': {e}")
        return bool(re.search(DEFAULT_RELEASE_ISSUE_PATTERN, issue_title.strip()))


def parse_release_command(
    comment_body: str,
    release_config: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a release command from comment body.

    Supported formats:
    - 'patch', 'minor', 'major'
    - '/release patch', '/release minor', '/release major'
    - '/tag patch', '/tag minor', '/tag major'
    - 'release patch', 'tag minor'
    - 'help', '/help', '?'

    :param comment_body: The body text of the issue comment.
    :param release_config: Optional release configuration dictionary from .graviton.json.
    :return: Tuple of (command_name, shell_command_string).
             If help was requested, returns ("help", None).
             If command is unrecognized, returns (None, None).
    """
    if not comment_body:
        return None, None

    text = comment_body.strip()
    if text.startswith("`") and text.endswith("`"):
        text = text.strip("`").strip()

    text_lower = text.lower()

    if text_lower in _HELP_COMMANDS:
        return "help", None

    commands = DEFAULT_COMMANDS.copy()
    if release_config and isinstance(release_config.get("commands"), dict):
        commands = release_config["commands"]

    # 1. Exact match against command key
    if text_lower in commands:
        return text_lower, commands[text_lower]

    # 2. Check prefix forms: '/release <key>', '/tag <key>', 'release <key>', 'tag <key>'
    m = re.match(r"^/?(?:release|tag)\s+([a-zA-Z0-9_\-\.]+)\s*$", text_lower)
    if m:
        key = m.group(1)
        if key in commands:
            return key, commands[key]

    return None, None


def is_user_authorized_for_release(
    user_login: Optional[str],
    payload: Dict[str, Any],
    release_config: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Check if the user is authorized to trigger releases.

    :param user_login: GitHub login username.
    :param payload: Webhook payload dictionary.
    :param release_config: Optional release configuration dictionary from .graviton.json.
    :return: True if authorized, False otherwise.
    """
    if not user_login:
        return False

    login_lower = user_login.strip().lower().lstrip("@")

    # 1. Check explicit allowed_users in release_config
    if release_config and "allowed_users" in release_config:
        allowed = release_config["allowed_users"]
        if isinstance(allowed, str):
            allowed = [allowed]
        if isinstance(allowed, list):
            allowed_lower = {str(u).strip().lower().lstrip("@") for u in allowed if u}
            return login_lower in allowed_lower

    # 2. Fallback check: author_association from comment or issue
    comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    author_association = comment.get("author_association") or issue.get("author_association", "")
    if isinstance(author_association, str) and author_association.upper() in ("OWNER", "MEMBER", "COLLABORATOR"):
        return True

    # 3. Fallback check: repository owner
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    owner = repo.get("owner")
    if isinstance(owner, dict):
        owner_login = owner.get("login") or owner.get("name")
        if owner_login and str(owner_login).strip().lower() == login_lower:
            return True

    return False


def post_issue_comment(
    repo_full_name: str,
    issue_number: int,
    body: str,
    timeout: float = 10.0,
) -> bool:
    """
    Post a comment to a GitHub issue.
    Tries `gh` CLI first, falling back to urllib with GITHUB_TOKEN or GH_TOKEN.

    :param repo_full_name: Full repository name ('owner/repo').
    :param issue_number: Issue number.
    :param body: Markdown body text for comment.
    :param timeout: Request timeout in seconds.
    :return: True if comment was posted successfully, False otherwise.
    """
    if not repo_full_name or not issue_number or not body:
        return False

    if BOT_MARKER not in body:
        body = f"{body.rstrip()}\n\n{RELEASE_BOT_TAG}"

    # 1. Try gh CLI
    try:
        cmd = [
            "gh",
            "issue",
            "comment",
            str(issue_number),
            "--repo",
            repo_full_name,
            "--body",
            body,
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode == 0:
            logger.info(f"Successfully posted comment to {repo_full_name}#{issue_number} via gh CLI")
            return True
        else:
            logger.warning(
                f"gh CLI returned non-zero ({res.returncode}) commenting on {repo_full_name}#{issue_number}: {res.stderr.strip()}"
            )
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        logger.debug(f"gh CLI comment failed for {repo_full_name}#{issue_number}: {e}")

    # 2. Fallback to urllib.request
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        logger.warning(f"No GITHUB_TOKEN or GH_TOKEN set. Cannot fallback to urllib for {repo_full_name}#{issue_number}.")
        return False

    url = f"https://api.github.com/repos/{repo_full_name}/issues/{issue_number}/comments"
    data = json.dumps({"body": body}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Graviton-Webhook-Server",
        "Authorization": f"Bearer {token}",
    }

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                logger.info(f"Successfully posted comment via urllib to {repo_full_name}#{issue_number}")
                return True
            else:
                logger.warning(f"urllib returned HTTP status {resp.status} for {repo_full_name}#{issue_number}")
                return False
    except Exception as e:
        logger.warning(f"urllib comment request failed for {repo_full_name}#{issue_number}: {e}")
        return False


def format_release_help(
    release_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Format markdown instructions for available release commands."""
    commands = DEFAULT_COMMANDS.copy()
    branch = DEFAULT_BRANCH
    allowed = []
    if release_config:
        if isinstance(release_config.get("commands"), dict):
            commands = release_config["commands"]
        branch = release_config.get("branch", DEFAULT_BRANCH)
        if isinstance(release_config.get("allowed_users"), list):
            allowed = release_config["allowed_users"]

    cmd_lines = "\n".join(f"- `{cmd}`: `{sh_cmd}`" for cmd, sh_cmd in commands.items())
    allowed_str = ", ".join(f"`@{u.lstrip('@')}`" for u in allowed) if allowed else "Repository maintainers"

    return (
        f"### 🚀 Graviton Release Controller\n\n"
        f"**Available release commands:**\n{cmd_lines}\n\n"
        f"- **Target branch**: `{branch}`\n"
        f"- **Authorized users**: {allowed_str}\n\n"
        f"*Comment any command above (e.g. `patch` or `/tag patch`) to trigger a release.*\n\n"
        f"{RELEASE_BOT_TAG}"
    )


def format_release_init(
    release_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Format welcome comment when a release issue is opened."""
    help_body = format_release_help(release_config)
    return f"🚀 **Graviton Release Controller Initialized**\n\n{help_body}"


def post_release_help_async(
    repo_full_name: str,
    issue_number: int,
    release_config: Optional[Dict[str, Any]] = None,
) -> threading.Thread:
    """Post release help asynchronously."""
    msg = format_release_help(release_config)
    t = threading.Thread(
        target=post_issue_comment,
        args=(repo_full_name, issue_number, msg),
        daemon=True,
        name="ReleaseHelpThread",
    )
    t.start()
    return t


def post_release_init_async(
    repo_full_name: str,
    issue_number: int,
    release_config: Optional[Dict[str, Any]] = None,
) -> threading.Thread:
    """Post release initialization welcome message asynchronously."""
    msg = format_release_init(release_config)
    t = threading.Thread(
        target=post_issue_comment,
        args=(repo_full_name, issue_number, msg),
        daemon=True,
        name="ReleaseInitThread",
    )
    t.start()
    return t


def post_release_unrecognized_async(
    repo_full_name: str,
    issue_number: int,
    comment_body: str,
    release_config: Optional[Dict[str, Any]] = None,
) -> threading.Thread:
    """Post unrecognized release command feedback asynchronously."""
    first_line = comment_body.splitlines()[0] if comment_body.splitlines() else comment_body
    sanitized = first_line.replace("`", "'").strip()
    if len(sanitized) > 80:
        sanitized = sanitized[:77] + "..."
    help_msg = format_release_help(release_config)
    body = (
        f"⚠️ Unrecognized release command: `{sanitized}`\n\n"
        f"{help_msg}"
    )
    t = threading.Thread(
        target=post_issue_comment,
        args=(repo_full_name, issue_number, body),
        daemon=True,
        name="ReleaseUnrecognizedThread",
    )
    t.start()
    return t


def execute_release(
    repo_dir: Path,
    repo_full_name: str,
    issue_number: int,
    release_type: str,
    command: str,
    target_branch: str = DEFAULT_BRANCH,
    pre_flight_checks: Optional[Union[List[str], str]] = None,
    timeout: float = 600.0,
) -> bool:
    """
    Execute release script in repo_dir with pre-flight git checks, locking, and status commenting.

    :param repo_dir: Local path to repository.
    :param repo_full_name: Full repository name ('owner/repo').
    :param issue_number: Issue number to comment on.
    :param release_type: Release bump name ('patch', 'minor', 'major', etc.).
    :param command: Shell command to execute.
    :param target_branch: Branch to checkout and pull (default: 'main').
    :param pre_flight_checks: Optional list of pre-flight shell commands.
    :param timeout: Command timeout in seconds.
    :return: True if command executed with exit code 0, False otherwise.
    """
    target_branch = target_branch or DEFAULT_BRANCH
    if isinstance(pre_flight_checks, str):
        pre_flight_checks = [pre_flight_checks]

    repo_key = str(repo_dir.resolve())
    lock = get_repo_release_lock(repo_key)

    if not lock.acquire(blocking=False):
        logger.warning(f"Release already running for {repo_key}. Skipping concurrent release request.")
        post_issue_comment(
            repo_full_name,
            issue_number,
            "⚠️ **Release In Progress**: Another release is currently running for this repository. Please wait for it to complete.",
        )
        return False

    try:
        logger.info(f"Starting {release_type} release for '{repo_full_name}' on branch '{target_branch}'...")

        # 1. Pre-flight: Git status check
        try:
            status_res = subprocess.run(
                ["git", "status", "--porcelain", "-uno"],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if status_res.returncode != 0:
                post_issue_comment(
                    repo_full_name,
                    issue_number,
                    f"❌ **Release Failed (Pre-flight Git Error)**\n\nCould not check git status:\n```text\n{status_res.stderr.strip()}\n```",
                )
                return False
            if status_res.stdout.strip():
                post_issue_comment(
                    repo_full_name,
                    issue_number,
                    f"❌ **Release Failed (Dirty Working Tree)**\n\nWorking tree has uncommitted tracked changes:\n```text\n{status_res.stdout.strip()}\n```",
                )
                return False
        except Exception as e:
            post_issue_comment(
                repo_full_name,
                issue_number,
                f"❌ **Release Failed (Pre-flight Error)**\n\n{str(e)}",
            )
            return False

        # 2. Checkout target branch and pull latest
        try:
            checkout_res = subprocess.run(
                ["git", "checkout", target_branch],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            if checkout_res.returncode != 0:
                post_issue_comment(
                    repo_full_name,
                    issue_number,
                    f"❌ **Release Failed (Git Checkout)**\n\nFailed to checkout `{target_branch}`:\n```text\n{checkout_res.stderr.strip()}\n```",
                )
                return False

            pull_res = subprocess.run(
                ["git", "pull", "--ff-only", "origin", target_branch],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if pull_res.returncode != 0:
                post_issue_comment(
                    repo_full_name,
                    issue_number,
                    f"❌ **Release Failed (Git Pull)**\n\nFailed to fast-forward `origin/{target_branch}`:\n```text\n{pull_res.stderr.strip()}\n```",
                )
                return False
        except Exception as e:
            post_issue_comment(
                repo_full_name,
                issue_number,
                f"❌ **Release Failed (Git Sync)**\n\n{str(e)}",
            )
            return False

        # 3. Run optional custom pre-flight commands if specified in config
        if pre_flight_checks:
            for pf_cmd in pre_flight_checks:
                try:
                    pf_res = subprocess.run(
                        pf_cmd,
                        shell=True,
                        cwd=str(repo_dir),
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    if pf_res.returncode != 0:
                        post_issue_comment(
                            repo_full_name,
                            issue_number,
                            f"❌ **Release Failed (Pre-flight Check Failed)**\n\nCommand `{pf_cmd}` failed with exit code {pf_res.returncode}:\n```text\n{(pf_res.stderr or pf_res.stdout).strip()}\n```",
                        )
                        return False
                except Exception as e:
                    post_issue_comment(
                        repo_full_name,
                        issue_number,
                        f"❌ **Release Failed (Pre-flight Command Error)**\n\n`{pf_cmd}`: {str(e)}",
                    )
                    return False

        # 4. Execute release command
        logger.info(f"Executing release command `{command}` in {repo_dir}")
        try:
            exec_res = subprocess.run(
                command,
                shell=True,
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            post_issue_comment(
                repo_full_name,
                issue_number,
                f"❌ **Release Timed Out**\n\nCommand `{command}` exceeded the {timeout}s timeout limit.",
            )
            return False
        except Exception as e:
            post_issue_comment(
                repo_full_name,
                issue_number,
                f"❌ **Release Execution Error**\n\n{str(e)}",
            )
            return False

        # Output formatting
        stdout = (exec_res.stdout or "").strip()
        stderr = (exec_res.stderr or "").strip()
        combined_output = f"{stdout}\n{stderr}".strip()
        tail = "\n".join(combined_output.splitlines()[-25:]) if combined_output else "(No output)"

        if exec_res.returncode == 0:
            logger.info(f"{release_type.capitalize()} release command succeeded for {repo_full_name}")
            msg = (
                f"✅ **{release_type.capitalize()} Release Succeeded!**\n\n"
                f"- **Command**: `{command}`\n"
                f"- **Branch**: `{target_branch}`\n\n"
                f"<details>\n<summary>Output log</summary>\n\n```text\n{tail}\n```\n</details>\n\n"
                f"{RELEASE_BOT_TAG}"
            )
            post_issue_comment(repo_full_name, issue_number, msg)
            return True
        else:
            logger.warning(f"{release_type.capitalize()} release command failed (exit code {exec_res.returncode})")
            msg = (
                f"❌ **{release_type.capitalize()} Release Failed!**\n\n"
                f"- **Command**: `{command}` (exit code {exec_res.returncode})\n"
                f"- **Branch**: `{target_branch}`\n\n"
                f"```text\n{tail}\n```\n\n"
                f"{RELEASE_BOT_TAG}"
            )
            post_issue_comment(repo_full_name, issue_number, msg)
            return False

    finally:
        lock.release()


def execute_release_async(
    repo_dir: Path,
    repo_full_name: str,
    issue_number: int,
    release_type: str,
    command: str,
    target_branch: str = DEFAULT_BRANCH,
    pre_flight_checks: Optional[Union[List[str], str]] = None,
    timeout: float = 600.0,
) -> threading.Thread:
    """
    Execute release in a background daemon thread.
    """
    target_branch = target_branch or DEFAULT_BRANCH
    if isinstance(pre_flight_checks, str):
        pre_flight_checks = [pre_flight_checks]

    t = threading.Thread(
        target=execute_release,
        kwargs={
            "repo_dir": repo_dir,
            "repo_full_name": repo_full_name,
            "issue_number": issue_number,
            "release_type": release_type,
            "command": command,
            "target_branch": target_branch,
            "pre_flight_checks": pre_flight_checks,
            "timeout": timeout,
        },
        daemon=True,
        name=f"ReleaseThread-{release_type}",
    )
    t.start()
    return t


__all__ = [
    "CONFIG_FILENAMES",
    "DEFAULT_RELEASE_ISSUE_PATTERN",
    "DEFAULT_BRANCH",
    "DEFAULT_COMMANDS",
    "RELEASE_BOT_TAG",
    "clear_release_locks",
    "get_repo_release_lock",
    "resolve_repo_dir",
    "load_release_config",
    "is_release_issue",
    "parse_release_command",
    "is_user_authorized_for_release",
    "post_issue_comment",
    "format_release_help",
    "format_release_init",
    "post_release_help_async",
    "post_release_init_async",
    "post_release_unrecognized_async",
    "execute_release",
    "execute_release_async",
]
