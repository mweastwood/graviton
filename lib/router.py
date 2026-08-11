"""
GitHub Webhook Event Routing Logic for Graviton.

This module acts as the top-level entry point and dispatcher for GitHub webhook event routing,
re-exporting event-specific sub-module handlers for backward compatibility.
"""

from pathlib import Path
from typing import Any, Dict, Optional

from lib.routers.base import (
    _build_accepted_response,
    _extract_repo_info,
    _get_git_remote_repo_names,
    _pr_review_timestamps,
    _pr_review_timestamps_lock,
    clear_pr_review_cache,
    get_server_repo_name,
    has_explicit_command,
    is_pr_created_by_us,
)
from lib.routers.issue_router import (
    handle_issue_comment_event,
    handle_issues_event,
)
from lib.routers.pr_router import (
    handle_pull_request_event,
    handle_pull_request_review_comment_event,
    handle_pull_request_review_event,
)
from lib.routers.push_router import (
    handle_ping_event,
    handle_push_event,
)


def format_event_summary(event_type: str, payload: Dict[str, Any]) -> str:
    """
    Format a concise target descriptor summary for a GitHub webhook event payload.

    :param event_type: Header X-GitHub-Event string (e.g. 'pull_request', 'issues', 'issue_comment', 'push', 'ping').
    :param payload: Parsed JSON dictionary of the webhook payload.
    :return: String target descriptor (e.g. "PR #12 (action: opened)", "Issue #62 (action: opened)", "Branch 'refs/heads/main'", "Ping").
    """
    if not isinstance(payload, dict):
        payload = {}

    action = payload.get("action")
    repo_full_name, _, _ = _extract_repo_info(payload)
    repo_prefix = f"{repo_full_name} " if repo_full_name else ""

    if event_type in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        pr = payload.get("pull_request")
        pr_dict = pr if isinstance(pr, dict) else {}
        pr_number = payload.get("number") or pr_dict.get("number")
        if pr_number is None and pr_dict.get("html_url"):
            url = str(pr_dict.get("html_url", ""))
            parts = url.rstrip("/").split("/")
            if parts and parts[-1].isdigit():
                pr_number = int(parts[-1])

        target = f"{repo_prefix}PR #{pr_number}" if pr_number is not None and pr_number != "" else f"{repo_prefix}PR"
        if action:
            return f"{target} (action: {action})"
        return target

    elif event_type == "issues":
        issue = payload.get("issue")
        issue_dict = issue if isinstance(issue, dict) else {}
        issue_number = issue_dict.get("number") or payload.get("number")

        target = f"{repo_prefix}Issue #{issue_number}" if issue_number is not None and issue_number != "" else f"{repo_prefix}Issue"
        if action:
            return f"{target} (action: {action})"
        return target

    elif event_type == "issue_comment":
        issue = payload.get("issue")
        issue_dict = issue if isinstance(issue, dict) else {}
        issue_number = issue_dict.get("number") or payload.get("number")
        is_pr = bool(issue_dict.get("pull_request"))
        prefix = f"{repo_prefix}PR" if is_pr else f"{repo_prefix}Issue"

        target = f"{prefix} #{issue_number}" if issue_number is not None and issue_number != "" else prefix
        if action:
            return f"{target} (action: {action})"
        return target

    elif event_type == "push":
        ref = payload.get("ref", "")
        if ref:
            return f"{repo_prefix}Branch '{ref}'".strip()
        return f"{repo_prefix}Push".strip()

    elif event_type == "ping":
        return "Ping"

    else:
        if action:
            return f"{repo_prefix}{event_type} (action: {action})".strip()
        return f"{repo_prefix}{event_type}".strip() or "Unknown"


def route_webhook_event(
    event_type: str,
    payload: Dict[str, Any],
    default_reviewer: str = "code_reviewer",
    default_fixer: str = "code_fixer",
    default_triager: str = "issue_triager",
    default_drafter: str = "pr_drafter",
    pr_tracker: Optional[Any] = None,
    debounce_window: float = 30.0,
    server_repo_name: Optional[str] = None,
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Route an incoming GitHub webhook event payload and return a decision dictionary.

    :param event_type: Header X-GitHub-Event string (e.g. 'pull_request', 'issues').
    :param payload: Parsed JSON dictionary of the webhook payload.
    :param default_reviewer: Name of the reviewer agent.
    :param default_fixer: Name of the fixer agent.
    :param default_triager: Name of the issue triage agent.
    :param default_drafter: Name of the PR drafter agent.
    :param pr_tracker: Optional PRTracker instance to track approved PRs ready for merge.
    :param debounce_window: Debounce window in seconds for rapid PR events (default 30s).
    :param server_repo_name: Optional repository name for graviton server to check on push events.
    :param repo_root: Optional Path to server repository root directory for inspecting git remote origin.
    :return: Dict containing status ('accepted' | 'ignored'), optional agent, prompt, and metadata.
    """
    handlers = {
        "ping": handle_ping_event,
        "push": lambda p: handle_push_event(p, server_repo_name=server_repo_name, repo_root=repo_root),
        "pull_request": lambda p: handle_pull_request_event(
            p, default_reviewer=default_reviewer, pr_tracker=pr_tracker, debounce_window=debounce_window
        ),
        "pull_request_review": lambda p: handle_pull_request_review_event(p, default_fixer=default_fixer, pr_tracker=pr_tracker),
        "pull_request_review_comment": lambda p: handle_pull_request_review_comment_event(p, default_fixer=default_fixer),
        "issues": lambda p: handle_issues_event(
            p, default_triager=default_triager, default_fixer=default_fixer, default_drafter=default_drafter
        ),
        "issue_comment": lambda p: handle_issue_comment_event(
            p,
            default_reviewer=default_reviewer,
            default_fixer=default_fixer,
            default_triager=default_triager,
            default_drafter=default_drafter,
            pr_tracker=pr_tracker,
        ),
    }

    handler = handlers.get(event_type)
    if handler:
        return handler(payload)

    return {
        "status": "ignored",
        "reason": f"Event type '{event_type}' not handled",
    }


__all__ = [
    "route_webhook_event",
    "format_event_summary",
    "clear_pr_review_cache",
    "is_pr_created_by_us",
    "has_explicit_command",
    "get_server_repo_name",
    "_extract_repo_info",
    "_build_accepted_response",
    "_get_git_remote_repo_names",
    "_pr_review_timestamps",
    "_pr_review_timestamps_lock",
    "handle_ping_event",
    "handle_push_event",
    "handle_pull_request_event",
    "handle_pull_request_review_event",
    "handle_pull_request_review_comment_event",
    "handle_issues_event",
    "handle_issue_comment_event",
]
