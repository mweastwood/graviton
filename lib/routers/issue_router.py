"""
Webhook router handler for GitHub Issue events:
'issues' and 'issue_comment'.
"""

import re
from typing import Any, Dict, Optional

from lib.pr_tracker import has_approval_marker, has_change_request_marker, is_bot_event
from lib.routers.base import (
    _build_accepted_response,
    _extract_repo_info,
    has_explicit_command,
    is_pr_created_by_us,
)
from lib.security import extract_agent_marker


def handle_issues_event(
    payload: Dict[str, Any],
    default_triager: str = "issue_triager",
    default_fixer: str = "code_fixer",
    default_drafter: str = "pr_drafter",
) -> Dict[str, Any]:
    """Handle GitHub 'issues' webhook event (Opened, Edited, Labeled)."""
    action = payload.get("action")
    issue = payload.get("issue", {}) if isinstance(payload.get("issue"), dict) else {}
    issue_number = issue.get("number")
    issue_title = issue.get("title", "") or ""
    issue_body = issue.get("body", "") or ""
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    author_agent = extract_agent_marker(issue_body)

    if action in ("opened", "reopened", "edited"):
        if author_agent and author_agent == default_triager:
            return {
                "status": "ignored",
                "reason": "Bot issue event dropped",
            }
        if repo_full_name:
            prompt = f"Triage Issue #{issue_number} in {repo_full_name}: '{issue_title}' - {issue_body}"
        else:
            prompt = f"Triage Issue #{issue_number}: '{issue_title}' - {issue_body}"
        return _build_accepted_response(
            action=action,
            agent=default_triager,
            prompt=prompt,
            repo_full_name=repo_full_name,
            repo_name=repo_name,
            clone_url=clone_url,
            author_agent=author_agent,
            issue_number=issue_number,
        )
    elif action == "labeled":
        label = payload.get("label", {}) if isinstance(payload.get("label"), dict) else {}
        label_name = label.get("name", "") or ""
        if label_name in ("ready-for-pr", "ready-for-implementation"):
            if author_agent and author_agent == default_drafter:
                return {
                    "status": "ignored",
                    "reason": "Bot issue event dropped",
                }
            if repo_full_name:
                prompt = f"Draft initial PR to implement ready Issue #{issue_number} in {repo_full_name}: '{issue_title}' - {issue_body}"
            else:
                prompt = f"Draft initial PR to implement ready Issue #{issue_number}: '{issue_title}' - {issue_body}"
            return _build_accepted_response(
                action=action,
                agent=default_drafter,
                prompt=prompt,
                repo_full_name=repo_full_name,
                repo_name=repo_name,
                clone_url=clone_url,
                author_agent=author_agent,
                label=label_name,
                issue_number=issue_number,
            )
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
    pr_tracker: Optional[Any] = None,
) -> Dict[str, Any]:
    """Handle GitHub 'issue_comment' webhook event."""
    action = payload.get("action")
    comment = payload.get("comment", {}) if isinstance(payload.get("comment"), dict) else {}
    comment_body = comment.get("body", "") or ""
    comment_author = comment.get("user") or comment.get("author")
    issue = payload.get("issue", {}) if isinstance(payload.get("issue"), dict) else {}
    issue_number = issue.get("number")
    pr = issue.get("pull_request")
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    author_agent = extract_agent_marker(comment_body)

    if pr and pr_tracker and issue_number is not None and action in ("created", "edited"):
        if has_change_request_marker(comment_body):
            pr_tracker.remove_approved_pr(issue_number, repo_full_name=repo_full_name)
        elif has_approval_marker(comment_body):
            pr_title = issue.get("title", "") or ""
            pr_url = issue.get("html_url", "") or issue.get("url", "") or ""
            issue_user = issue.get("user", {}) if isinstance(issue.get("user"), dict) else {}
            pr_author = issue_user.get("login", "") if issue_user else str(issue.get("user", "") or "")
            pr_tracker.add_approved_pr(issue_number, pr_title, pr_author, pr_url, repo_full_name=repo_full_name)

    if (author_agent or is_bot_event(comment_body, comment_author)) and not has_explicit_command(comment_body):
        labels_raw = issue.get("labels", []) if isinstance(issue.get("labels"), list) else []
        labels = [l.get("name", "") if isinstance(l, dict) else str(l) for l in labels_raw]
        is_triager_transition = bool(
            not pr
            and author_agent
            and author_agent == default_triager
            and ("ready-for-pr" in labels or "ready-for-implementation" in labels)
        )
        if not is_triager_transition:
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

            if author_agent and author_agent == agent:
                return {
                    "status": "ignored",
                    "reason": "Bot comment dropped",
                }

            if repo_full_name:
                prompt = f"Address comment on PR #{issue_number} in {repo_full_name}: '{comment_body}'"
            else:
                prompt = f"Address comment on PR #{issue_number}: '{comment_body}'"
            return _build_accepted_response(
                action=action,
                agent=agent,
                prompt=prompt,
                repo_full_name=repo_full_name,
                repo_name=repo_name,
                clone_url=clone_url,
                author_agent=author_agent,
                pr_number=issue_number,
            )

        # 2. Comment on a pure Issue (Triage vs PR Drafting)
        else:
            labels_raw = issue.get("labels", []) if isinstance(issue.get("labels"), list) else []
            labels = [
                l.get("name", "") if isinstance(l, dict) else str(l) for l in labels_raw
            ]
            body_lower = comment_body.lower()
            if "ready-for-pr" in labels or "ready-for-implementation" in labels or "/draft-pr" in body_lower:
                agent = default_drafter
                if author_agent and author_agent == agent:
                    return {
                        "status": "ignored",
                        "reason": "Bot comment dropped",
                    }
                if repo_full_name:
                    prompt = f"Draft initial PR for Issue #{issue_number} in {repo_full_name} based on comment: '{comment_body}'"
                else:
                    prompt = f"Draft initial PR for Issue #{issue_number} based on comment: '{comment_body}'"
                return _build_accepted_response(
                    action=action,
                    agent=agent,
                    prompt=prompt,
                    repo_full_name=repo_full_name,
                    repo_name=repo_name,
                    clone_url=clone_url,
                    author_agent=author_agent,
                    issue_number=issue_number,
                )
            else:
                agent = default_triager
                if author_agent and author_agent == agent:
                    return {
                        "status": "ignored",
                        "reason": "Bot comment dropped",
                    }
                if repo_full_name:
                    prompt = f"Continue triage on Issue #{issue_number} in {repo_full_name} based on comment: '{comment_body}'"
                else:
                    prompt = f"Continue triage on Issue #{issue_number} based on comment: '{comment_body}'"
                return _build_accepted_response(
                    action=action,
                    agent=agent,
                    prompt=prompt,
                    repo_full_name=repo_full_name,
                    repo_name=repo_name,
                    clone_url=clone_url,
                    author_agent=author_agent,
                    issue_number=issue_number,
                )

    return {
        "status": "ignored",
        "reason": f"Issue comment action '{action}' not handled",
    }
