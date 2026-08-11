"""
Base routing utilities, cache management, and repository helper functions.
"""

from pathlib import Path
import re
import subprocess
import threading
from typing import Any, Dict, Optional, Tuple

from lib.pr_tracker import is_bot_event


_pr_review_timestamps: Dict[Any, float] = {}
_pr_review_timestamps_lock = threading.Lock()


def clear_pr_review_cache():
    """Clear the cached PR review event timestamps (useful for unit testing)."""
    with _pr_review_timestamps_lock:
        _pr_review_timestamps.clear()


def is_pr_created_by_us(pr: Dict[str, Any]) -> bool:
    """
    Check whether a pull request was created by Graviton / Antigravity agent.

    :param pr: Dictionary representing the pull request payload object.
    :return: True if created by us or missing explicit non-bot author info, False otherwise.
    """
    if not isinstance(pr, dict) or not pr:
        return True

    # 1. Explicit boolean flag if provided
    if pr.get("created_by_us") is False:
        return False
    if pr.get("created_by_us") is True:
        return True

    # 2. Check for bot marker signature or bot author in PR body/user
    body = pr.get("body") or ""
    user = pr.get("user") or pr.get("author")
    if is_bot_event(body, user):
        return True

    # 3. Check user / author dict details or string author
    user_dict = pr.get("user") if isinstance(pr.get("user"), dict) else (pr.get("author") if isinstance(pr.get("author"), dict) else None)
    if is_bot_event("", user_dict or user):
        return True

    # 4. Check head branch name ref prefix (fallback if no explicit user/author info or human author with bot branch)
    head = pr.get("head")
    if isinstance(head, dict):
        head_ref = head.get("ref", "").lower()
        if head_ref.startswith(("fix/", "feat/", "antigravity/", "bot/")):
            return True

    if (isinstance(user_dict, dict) and user_dict) or (isinstance(user, str) and user.strip()):
        return False

    return True


def has_explicit_command(text: str) -> bool:
    """
    Check whether a comment or review body contains an explicit human command.

    :param text: Comment or review text body.
    :return: True if text contains explicit commands like '/fix', '/review', or '@antigravity', False otherwise.
    """
    if not text:
        return False
    text_lower = text.lower()
    return bool(re.search(r'(?<![\w/])/(?:fix|review)\b|@antigravity', text_lower))


def _extract_repo_info(payload: Dict[str, Any]):
    """
    Extract repo_full_name, repo_name, and clone_url from webhook payload.
    """
    if not isinstance(payload, dict):
        return None, None, None
    repo = payload.get("repository")
    if not isinstance(repo, dict):
        return None, None, None
    repo_full_name = repo.get("full_name") or None
    repo_name = repo.get("name") or (repo_full_name.split("/")[-1] if repo_full_name and "/" in repo_full_name else None)
    clone_url = repo.get("clone_url") or repo.get("html_url") or None
    if repo_full_name and not clone_url:
        clone_url = f"https://github.com/{repo_full_name}.git"
    return repo_full_name, repo_name, clone_url


def _build_accepted_response(
    action: str,
    agent: str,
    prompt: str,
    repo_full_name: Optional[str] = None,
    repo_name: Optional[str] = None,
    clone_url: Optional[str] = None,
    author_agent: Optional[str] = None,
    **extra_fields: Any,
) -> Dict[str, Any]:
    """
    Construct an 'accepted' webhook response dictionary with optional repository metadata.
    """
    res: Dict[str, Any] = {
        "status": "accepted",
        "action": action,
    }
    res.update(extra_fields)
    res["agent"] = agent
    res["prompt"] = prompt

    if author_agent:
        res["author_agent"] = author_agent
    if repo_full_name:
        res["repo_full_name"] = repo_full_name
    if repo_name:
        res["repo_name"] = repo_name
    if clone_url:
        res["clone_url"] = clone_url
    return res


def _get_git_remote_repo_names(repo_root: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract (repo_name, repo_full_name) from git remote origin of repo_root.
    Returns (None, None) if git command fails or url cannot be parsed.
    """
    if repo_root is None:
        try:
            repo_root = Path(__file__).resolve().parent.parent.parent
        except Exception:
            return None, None
    try:
        git_res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if git_res.returncode == 0 and git_res.stdout:
            origin_url = git_res.stdout.strip()
            clean_url = origin_url.rstrip("/").removesuffix(".git").rstrip("/")
            if ":" in clean_url or "/" in clean_url:
                parts = clean_url.replace(":", "/").split("/")
                if len(parts) >= 2 and parts[-2] and parts[-1]:
                    repo_full_name = f"{parts[-2]}/{parts[-1]}"
                    repo_name = parts[-1]
                    return repo_name, repo_full_name
                elif len(parts) >= 1 and parts[-1]:
                    return parts[-1], None
    except Exception:
        pass
    return None, None


def get_server_repo_name(repo_root: Optional[Path] = None) -> str:
    """Get the server repository name from git remote origin or fallback to directory name."""
    if repo_root is None:
        try:
            repo_root = Path(__file__).resolve().parent.parent.parent
        except Exception:
            return "graviton"
    remote_name, _ = _get_git_remote_repo_names(repo_root)
    if remote_name:
        return remote_name
    return repo_root.name
