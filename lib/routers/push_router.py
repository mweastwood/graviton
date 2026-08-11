"""
Webhook router handler for GitHub 'ping' and 'push' events.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from lib.routers.base import (
    _extract_repo_info,
    _get_git_remote_repo_names,
)


def handle_ping_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle GitHub 'ping' webhook event."""
    return {
        "status": "accepted",
        "action": "ping",
        "zen": payload.get("zen", ""),
    }


def handle_push_event(
    payload: Dict[str, Any],
    server_repo_name: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Handle GitHub 'push' webhook event (Self-Update & Hot Reload on main/master)."""
    ref = payload.get("ref", "")
    repo_full_name, repo_name, _ = _extract_repo_info(payload)

    if server_repo_name or repo_root is not None:
        allowed_names = set()
        if server_repo_name:
            allowed_names.add(server_repo_name)

        target_root = repo_root if repo_root is not None else Path(__file__).resolve().parent.parent.parent
        remote_name, remote_full_name = _get_git_remote_repo_names(target_root)
        if remote_name:
            allowed_names.add(remote_name)
        if remote_full_name:
            allowed_names.add(remote_full_name)

        if allowed_names and repo_name not in allowed_names and repo_full_name not in allowed_names:
            return {
                "status": "ignored",
                "reason": f"Push event for repository '{repo_name or repo_full_name}' is not graviton server repository",
            }
    if ref in ("refs/heads/main", "refs/heads/master"):
        return {
            "status": "accepted",
            "action": "self_update",
            "ref": ref,
        }
    return {
        "status": "ignored",
        "reason": f"Push ref '{ref}' is not target main branch",
    }
