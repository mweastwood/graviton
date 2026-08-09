"""
GitHub Webhook Event Routing Logic for Graviton.
"""

from typing import Any, Dict
from lib.security import contains_bot_marker


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
        # Explicit user object given that is a human and not a bot / antigravity
        return False

    # 4. Check head branch name ref prefix
    head = pr.get("head")
    if isinstance(head, dict):
        head_ref = head.get("ref", "").lower()
        if head_ref.startswith(("fix/", "feat/", "antigravity/", "bot/")):
            return True

    return True


def route_webhook_event(
    event_type: str,
    payload: Dict[str, Any],
    default_reviewer: str = "code_reviewer",
    default_fixer: str = "code_fixer",
    default_triager: str = "issue_triager",
) -> Dict[str, Any]:
    """
    Route an incoming GitHub webhook event payload and return a decision dictionary.

    :param event_type: Header X-GitHub-Event string (e.g. 'pull_request', 'issues').
    :param payload: Parsed JSON dictionary of the webhook payload.
    :param default_reviewer: Name of the reviewer agent.
    :param default_fixer: Name of the fixer agent.
    :param default_triager: Name of the issue triage agent.
    :return: Dict containing status ('accepted' | 'ignored'), optional agent, prompt, and metadata.
    """
    if event_type == "ping":
        return {
            "status": "accepted",
            "action": "ping",
            "zen": payload.get("zen", ""),
        }

    # 0. GitHub Push Events (Self-Update & Hot Reload on main)
    elif event_type == "push":
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

    # 1. Pull Request Events
    elif event_type == "pull_request":
        action = payload.get("action")
        pr_number = payload.get("number") or payload.get("pull_request", {}).get("number")

        if action in ("opened", "synchronize", "reopened"):
            prompt = f"Review PR #{pr_number}"
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

    # 2. PR Submitted Review Events
    elif event_type == "pull_request_review":
        action = payload.get("action")
        review = payload.get("review", {})
        review_state = review.get("state", "").upper()
        review_body = review.get("body", "")
        pr = payload.get("pull_request", {})
        pr_number = pr.get("number") or payload.get("number")

        if not is_pr_created_by_us(pr):
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

        if contains_bot_marker(review_body):
            return {
                "status": "ignored",
                "reason": "Bot self-review event dropped",
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

    # 3. Inline Line-by-Line Review Comments
    elif event_type == "pull_request_review_comment":
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

        if not is_pr_created_by_us(pr):
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

    # 4. GitHub Issues Events (Opened, Edited, Labeled)
    elif event_type == "issues":
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
                    "agent": default_fixer,
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

    # 5. General Issue / PR Comments
    elif event_type == "issue_comment":
        action = payload.get("action")
        comment = payload.get("comment", {})
        comment_body = comment.get("body", "")
        issue = payload.get("issue", {})
        issue_number = issue.get("number")
        pr = issue.get("pull_request")

        if contains_bot_marker(comment_body):
            return {
                "status": "ignored",
                "reason": "Bot comment dropped",
            }

        if action == "created":
            # 5a. Comment on a Pull Request
            if pr:
                pr_data = {**issue, **(pr if isinstance(pr, dict) else {})}
                if not is_pr_created_by_us(pr_data):
                    return {
                        "status": "ignored",
                        "reason": "PR was not created by us",
                    }

                body_lower = comment_body.lower()
                agent = default_reviewer if "/review" in body_lower else default_fixer
                prompt = f"Address comment on PR #{issue_number}: '{comment_body}'"
                return {
                    "status": "accepted",
                    "action": action,
                    "pr_number": issue_number,
                    "agent": agent,
                    "prompt": prompt,
                }

            # 5b. Comment on a pure Issue (Triage vs PR Drafting)
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
                        "agent": default_fixer,
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

    else:
        return {
            "status": "ignored",
            "reason": f"Event type '{event_type}' not handled",
        }

