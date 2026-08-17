"""
Webhook router handler for GitHub Pull Request events:
'pull_request', 'pull_request_review', and 'pull_request_review_comment'.
"""

import time
from typing import Any, Dict, Optional

from lib.pr_tracker import has_approval_marker, has_change_request_marker, is_bot_event
from lib.routers.base import (
    _build_accepted_response,
    _extract_repo_info,
    _pr_review_timestamps,
    _pr_review_timestamps_lock,
    has_explicit_command,
    is_pr_created_by_us,
)
from lib.security import extract_agent_marker


def handle_pull_request_event(
    payload: Dict[str, Any],
    default_reviewer: str = "code_reviewer",
    pr_tracker: Optional[Any] = None,
    debounce_window: float = 30.0,
) -> Dict[str, Any]:
    """Handle GitHub 'pull_request' webhook event."""
    action = payload.get("action")
    pr = payload.get("pull_request", {}) if isinstance(payload.get("pull_request"), dict) else {}
    pr_number = payload.get("number") or pr.get("number")
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    if pr_tracker and pr_number is not None and (action in ("closed", "merged") or payload.get("merged") or pr.get("merged")):
        pr_tracker.remove_approved_pr(pr_number, repo_full_name=repo_full_name)

    pr_body = pr.get("body", "") or ""
    author_agent = extract_agent_marker(pr_body)

    if action in ("opened", "synchronize", "reopened"):
        if author_agent and author_agent == default_reviewer:
            return {
                "status": "ignored",
                "reason": "Bot PR event dropped",
            }

        if pr_number is not None and debounce_window > 0:
            pr_key = f"{repo_full_name}#{pr_number}" if repo_full_name else str(pr_number)
            now = time.time()
            with _pr_review_timestamps_lock:
                last_time = _pr_review_timestamps.get(pr_key)
                if action == "synchronize" and last_time is not None and (now - last_time) < debounce_window:
                    return {
                        "status": "ignored",
                        "reason": f"PR #{pr_number} review event debounced (action '{action}')",
                    }
                _pr_review_timestamps[pr_key] = now

        if repo_full_name:
            prompt = f"Review PR #{pr_number} in {repo_full_name}. Use --request-changes for any findings or code fixes."
        else:
            prompt = f"Review PR #{pr_number}. Use --request-changes for any findings or code fixes."

        return _build_accepted_response(
            action=action,
            agent=default_reviewer,
            prompt=prompt,
            repo_full_name=repo_full_name,
            repo_name=repo_name,
            clone_url=clone_url,
            author_agent=author_agent,
            pr_number=pr_number,
        )

    return {
        "status": "ignored",
        "reason": f"Pull request action '{action}' does not trigger review",
    }


def handle_pull_request_review_event(
    payload: Dict[str, Any],
    default_fixer: str = "code_fixer",
    pr_tracker: Optional[Any] = None,
) -> Dict[str, Any]:
    """Handle GitHub 'pull_request_review' webhook event."""
    action = payload.get("action")
    review = payload.get("review", {}) if isinstance(payload.get("review"), dict) else {}
    review_state = review.get("state", "").upper()
    review_body = review.get("body", "")
    review_author = review.get("user") or review.get("author")
    pr = payload.get("pull_request", {}) if isinstance(payload.get("pull_request"), dict) else {}
    pr_number = pr.get("number") or payload.get("number")
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "") or pr.get("url", "")
    pr_user = pr.get("user", {}) if isinstance(pr.get("user"), dict) else {}
    pr_author = pr_user.get("login", "") if pr_user else str(pr.get("user", "") or "")
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    author_agent = extract_agent_marker(review_body)

    if pr_tracker and pr_number is not None:
        if action == "submitted":
            if review_state == "APPROVED" or (review_state == "COMMENTED" and has_approval_marker(review_body) and not has_change_request_marker(review_body)):
                pr_tracker.add_approved_pr(pr_number, pr_title, pr_author, pr_url, repo_full_name=repo_full_name)
            elif review_state == "CHANGES_REQUESTED" or has_change_request_marker(review_body):
                pr_tracker.remove_approved_pr(pr_number, repo_full_name=repo_full_name)
        elif action == "dismissed":
            pr_tracker.remove_approved_pr(pr_number, repo_full_name=repo_full_name)

    if author_agent and author_agent == default_fixer:
        return {
            "status": "ignored",
            "reason": "Bot self-review event dropped",
        }

    if (author_agent or is_bot_event(review_body, review_author)) and review_state != "CHANGES_REQUESTED" and not has_change_request_marker(review_body):
        return {
            "status": "ignored",
            "reason": "Bot self-review event dropped",
        }

    if not is_pr_created_by_us(pr) and not has_explicit_command(review_body):
        return {
            "status": "ignored",
            "reason": "PR was not created by us",
        }

    if action == "submitted" and review_state in ("CHANGES_REQUESTED", "COMMENTED"):
        if review_state == "COMMENTED" and has_approval_marker(review_body) and not has_change_request_marker(review_body):
            return {
                "status": "ignored",
                "reason": f"Review state '{review_state}' with approval marker does not trigger fixer",
            }
        if repo_full_name:
            prompt = f"Resolve review feedback on PR #{pr_number} in {repo_full_name}: '{review_body}'"
        else:
            prompt = f"Resolve review feedback on PR #{pr_number}: '{review_body}'"
        return _build_accepted_response(
            action=action,
            agent=default_fixer,
            prompt=prompt,
            repo_full_name=repo_full_name,
            repo_name=repo_name,
            clone_url=clone_url,
            author_agent=author_agent,
            review_state=review_state,
            pr_number=pr_number,
        )

    return {
        "status": "ignored",
        "reason": f"Review state '{review_state}' action '{action}' does not trigger fixer",
    }


def handle_pull_request_review_comment_event(
    payload: Dict[str, Any],
    default_fixer: str = "code_fixer",
) -> Dict[str, Any]:
    """Handle GitHub 'pull_request_review_comment' webhook event (inline comments)."""
    action = payload.get("action")
    comment = payload.get("comment", {}) if isinstance(payload.get("comment"), dict) else {}
    comment_body = comment.get("body", "")
    comment_author = comment.get("user") or comment.get("author")
    file_path = comment.get("path", "")
    line = comment.get("line") or comment.get("original_line")
    pr = payload.get("pull_request", {}) if isinstance(payload.get("pull_request"), dict) else {}
    pr_url = pr.get("html_url", "") or ""
    pr_number = pr.get("number") or (pr_url.rstrip("/").split("/")[-1] if pr_url else "")
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    author_agent = extract_agent_marker(comment_body)

    if author_agent and author_agent == default_fixer:
        return {
            "status": "ignored",
            "reason": "Bot comment dropped",
        }

    if (author_agent or is_bot_event(comment_body, comment_author)) and not has_explicit_command(comment_body):
        return {
            "status": "ignored",
            "reason": "Bot comment dropped",
        }

    if not is_pr_created_by_us(pr) and not has_explicit_command(comment_body):
        return {
            "status": "ignored",
            "reason": "PR was not created by us",
        }

    if action == "created":
        if repo_full_name:
            prompt = f"Resolve review comment on PR #{pr_number} in {repo_full_name} in file '{file_path}' (line {line}): '{comment_body}'"
        else:
            prompt = f"Resolve review comment on PR #{pr_number} in file '{file_path}' (line {line}): '{comment_body}'"
        return _build_accepted_response(
            action=action,
            agent=default_fixer,
            prompt=prompt,
            repo_full_name=repo_full_name,
            repo_name=repo_name,
            clone_url=clone_url,
            author_agent=author_agent,
            pr_number=pr_number,
            file=file_path,
            line=line,
        )
    return {
        "status": "ignored",
        "reason": f"Review comment action '{action}' does not trigger fixer",
    }
