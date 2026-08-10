"""
GitHub Webhook Event Routing Logic for Graviton.
"""

import re
import threading
import time
from typing import Any, Dict, Optional
from lib.security import contains_bot_marker

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

    # 2. Check for bot marker signature in PR body
    body = pr.get("body") or ""
    if contains_bot_marker(body):
        return True

    # 3. Check user author details
    user = pr.get("user")
    if isinstance(user, dict) and user:
        login = user.get("login", "").lower()
        user_type = user.get("type", "")
        if user_type == "Bot" or "bot" in login or "antigravity" in login:
            return True
        return False

    # 4. Check head branch name ref prefix (when author user object is omitted)
    head = pr.get("head")
    if isinstance(head, dict):
        head_ref = head.get("ref", "").lower()
        if head_ref.startswith(("fix/", "feat/", "antigravity/", "bot/")):
            return True

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


def handle_ping_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle GitHub 'ping' webhook event."""
    return {
        "status": "accepted",
        "action": "ping",
        "zen": payload.get("zen", ""),
    }


def handle_push_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle GitHub 'push' webhook event (Self-Update & Hot Reload on main/master)."""
    ref = payload.get("ref", "")
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

    if pr_tracker and pr_number is not None and (action in ("closed", "merged") or payload.get("merged") or pr.get("merged")):
        pr_tracker.remove_approved_pr(pr_number)

    if action in ("opened", "synchronize", "reopened"):
        if pr_number is not None and debounce_window > 0:
            pr_key = str(pr_number)
            now = time.time()
            with _pr_review_timestamps_lock:
                last_time = _pr_review_timestamps.get(pr_key)
                if action == "synchronize" and last_time is not None and (now - last_time) < debounce_window:
                    return {
                        "status": "ignored",
                        "reason": f"PR #{pr_number} review event debounced (action '{action}')",
                    }
                _pr_review_timestamps[pr_key] = now

        prompt = f"Review PR #{pr_number}. Use --request-changes for any findings or code fixes."
        return {
            "status": "accepted",
            "action": action,
            "pr_number": pr_number,
            "agent": default_reviewer,
            "prompt": prompt,
        }
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
    pr = payload.get("pull_request", {}) if isinstance(payload.get("pull_request"), dict) else {}
    pr_number = pr.get("number") or payload.get("number")
    pr_title = pr.get("title", "")
    pr_url = pr.get("html_url", "") or pr.get("url", "")
    pr_user = pr.get("user", {}) if isinstance(pr.get("user"), dict) else {}
    pr_author = pr_user.get("login", "") if pr_user else str(pr.get("user", "") or "")

    if pr_tracker and pr_number is not None:
        if action == "submitted" and review_state == "APPROVED":
            pr_tracker.add_approved_pr(pr_number, pr_title, pr_author, pr_url)
        elif (action == "submitted" and review_state == "CHANGES_REQUESTED") or action == "dismissed":
            pr_tracker.remove_approved_pr(pr_number)

    if contains_bot_marker(review_body) and review_state != "CHANGES_REQUESTED":
        return {
            "status": "ignored",
            "reason": "Bot self-review event dropped",
        }

    if not is_pr_created_by_us(pr) and not has_explicit_command(review_body):
        return {
            "status": "ignored",
            "reason": "PR was not created by us",
        }

    if action == "submitted" and review_state == "CHANGES_REQUESTED":
        prompt = f"Resolve review feedback on PR #{pr_number}: '{review_body}'"
        return {
            "status": "accepted",
            "action": action,
            "review_state": review_state,
            "pr_number": pr_number,
            "agent": default_fixer,
            "prompt": prompt,
        }

    if action == "submitted" and review_state == "COMMENTED":
        prompt = f"Resolve review feedback on PR #{pr_number}: '{review_body}'"
        return {
            "status": "accepted",
            "action": action,
            "review_state": review_state,
            "pr_number": pr_number,
            "agent": default_fixer,
            "prompt": prompt,
        }
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
    comment = payload.get("comment", {})
    comment_body = comment.get("body", "")
    file_path = comment.get("path", "")
    line = comment.get("line") or comment.get("original_line")
    pr = payload.get("pull_request", {})
    pr_url = pr.get("html_url", "")
    pr_number = pr.get("number") or (pr_url.rstrip("/").split("/")[-1] if pr_url else "")

    if contains_bot_marker(comment_body):
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
        prompt = f"Resolve review comment on PR #{pr_number} in file '{file_path}' (line {line}): '{comment_body}'"
        return {
            "status": "accepted",
            "action": action,
            "pr_number": pr_number,
            "file": file_path,
            "line": line,
            "agent": default_fixer,
            "prompt": prompt,
        }
    return {
        "status": "ignored",
        "reason": f"Review comment action '{action}' does not trigger fixer",
    }


def handle_issues_event(
    payload: Dict[str, Any],
    default_triager: str = "issue_triager",
    default_fixer: str = "code_fixer",
    default_drafter: str = "pr_drafter",
) -> Dict[str, Any]:
    """Handle GitHub 'issues' webhook event (Opened, Edited, Labeled)."""
    action = payload.get("action")
    issue = payload.get("issue", {})
    issue_number = issue.get("number")
    issue_title = issue.get("title", "")
    issue_body = issue.get("body", "")

    if action in ("opened", "reopened", "edited"):
        prompt = f"Triage Issue #{issue_number}: '{issue_title}' - {issue_body}"
        return {
            "status": "accepted",
            "action": action,
            "issue_number": issue_number,
            "agent": default_triager,
            "prompt": prompt,
        }
    elif action == "labeled":
        label = payload.get("label", {})
        label_name = label.get("name", "")
        if label_name in ("ready-for-pr", "ready-for-implementation"):
            prompt = f"Draft initial PR to implement ready Issue #{issue_number}: '{issue_title}' - {issue_body}"
            return {
                "status": "accepted",
                "action": action,
                "label": label_name,
                "issue_number": issue_number,
                "agent": default_drafter,
                "prompt": prompt,
            }
        return {
            "status": "ignored",
            "reason": f"Issue label '{label_name}' does not trigger PR drafting",
        }

    return {
        "status": "ignored",
        "reason": f"Issue action '{action}' does not trigger triage or fix",
    }


def handle_issue_comment_event(
    payload: Dict[str, Any],
    default_reviewer: str = "code_reviewer",
    default_fixer: str = "code_fixer",
    default_triager: str = "issue_triager",
    default_drafter: str = "pr_drafter",
) -> Dict[str, Any]:
    """Handle GitHub 'issue_comment' webhook event."""
    action = payload.get("action")
    comment = payload.get("comment", {})
    comment_body = comment.get("body", "")
    issue = payload.get("issue", {})
    issue_number = issue.get("number")
    pr = issue.get("pull_request")

    if contains_bot_marker(comment_body) and not has_explicit_command(comment_body):
        return {
            "status": "ignored",
            "reason": "Bot comment dropped",
        }

    if action == "created":
        # 1. Comment on a Pull Request
        if pr:
            pr_data = {**issue, **(pr if isinstance(pr, dict) else {})}
            if not is_pr_created_by_us(pr_data) and not has_explicit_command(comment_body):
                return {
                    "status": "ignored",
                    "reason": "PR was not created by us",
                }

            body_lower = comment_body.lower()
            is_fix_cmd = bool(re.search(r'(?<![\w/])/fix\b', body_lower))
            is_review_cmd = bool(re.search(r'(?<![\w/])/review\b', body_lower))

            if is_fix_cmd:
                agent = default_fixer
            elif is_review_cmd:
                agent = default_reviewer
            else:
                agent = default_fixer
            prompt = f"Address comment on PR #{issue_number}: '{comment_body}'"
            return {
                "status": "accepted",
                "action": action,
                "pr_number": issue_number,
                "agent": agent,
                "prompt": prompt,
            }

        # 2. Comment on a pure Issue (Triage vs PR Drafting)
        else:
            labels_raw = issue.get("labels", [])
            labels = [
                l.get("name", "") if isinstance(l, dict) else str(l) for l in labels_raw
            ]
            body_lower = comment_body.lower()
            if "ready-for-pr" in labels or "ready-for-implementation" in labels or "/draft-pr" in body_lower:
                prompt = f"Draft initial PR for Issue #{issue_number} based on comment: '{comment_body}'"
                return {
                    "status": "accepted",
                    "action": action,
                    "issue_number": issue_number,
                    "agent": default_drafter,
                    "prompt": prompt,
                }
            else:
                prompt = f"Continue triage on Issue #{issue_number} based on comment: '{comment_body}'"
                return {
                    "status": "accepted",
                    "action": action,
                    "issue_number": issue_number,
                    "agent": default_triager,
                    "prompt": prompt,
                }

    return {
        "status": "ignored",
        "reason": f"Issue comment action '{action}' not handled",
    }


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

    if event_type in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        pr = payload.get("pull_request")
        pr_dict = pr if isinstance(pr, dict) else {}
        pr_number = payload.get("number") or pr_dict.get("number")
        if pr_number is None and pr_dict.get("html_url"):
            url = str(pr_dict.get("html_url", ""))
            parts = url.rstrip("/").split("/")
            if parts and parts[-1].isdigit():
                pr_number = int(parts[-1])

        target = f"PR #{pr_number}" if pr_number is not None and pr_number != "" else "PR"
        if action:
            return f"{target} (action: {action})"
        return target

    elif event_type == "issues":
        issue = payload.get("issue")
        issue_dict = issue if isinstance(issue, dict) else {}
        issue_number = issue_dict.get("number") or payload.get("number")

        target = f"Issue #{issue_number}" if issue_number is not None and issue_number != "" else "Issue"
        if action:
            return f"{target} (action: {action})"
        return target

    elif event_type == "issue_comment":
        issue = payload.get("issue")
        issue_dict = issue if isinstance(issue, dict) else {}
        issue_number = issue_dict.get("number") or payload.get("number")
        is_pr = bool(issue_dict.get("pull_request"))
        prefix = "PR" if is_pr else "Issue"

        target = f"{prefix} #{issue_number}" if issue_number is not None and issue_number != "" else prefix
        if action:
            return f"{target} (action: {action})"
        return target

    elif event_type == "push":
        ref = payload.get("ref", "")
        if ref:
            return f"Branch '{ref}'"
        return "Push"

    elif event_type == "ping":
        return "Ping"

    else:
        if action:
            return f"{event_type} (action: {action})"
        return event_type or "Unknown"


def route_webhook_event(
    event_type: str,
    payload: Dict[str, Any],
    default_reviewer: str = "code_reviewer",
    default_fixer: str = "code_fixer",
    default_triager: str = "issue_triager",
    default_drafter: str = "pr_drafter",
    pr_tracker: Optional[Any] = None,
    debounce_window: float = 30.0,
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
    :return: Dict containing status ('accepted' | 'ignored'), optional agent, prompt, and metadata.
    """
    handlers = {
        "ping": handle_ping_event,
        "push": handle_push_event,
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
        ),
    }

    handler = handlers.get(event_type)
    if handler:
        return handler(payload)

    return {
        "status": "ignored",
        "reason": f"Event type '{event_type}' not handled",
    }
