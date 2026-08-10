"""
GitHub Webhook Event Routing Logic for Graviton.
"""

from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Any, Dict, Optional, Tuple
from lib.security import contains_bot_marker
from lib.pr_tracker import has_approval_marker, has_change_request_marker, is_bot_event

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
    if isinstance(user_dict, dict) and user_dict:
        login = user_dict.get("login", "").lower()
        user_type = user_dict.get("type", "")
        if user_type == "Bot" or "bot" in login or "antigravity" in login:
            return True
        return False
    elif isinstance(user, str) and user.strip():
        login = user.strip().lower()
        if login.endswith("[bot]") or "bot" in login or "antigravity" in login:
            return True
        return False

    # 4. Check head branch name ref prefix (fallback if no explicit user/author info)
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

    if repo_full_name:
        res["repo_full_name"] = repo_full_name
    if repo_name:
        res["repo_name"] = repo_name
    if clone_url:
        res["clone_url"] = clone_url
    return res


def handle_ping_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Handle GitHub 'ping' webhook event."""
    return {
        "status": "accepted",
        "action": "ping",
        "zen": payload.get("zen", ""),
    }


def _get_git_remote_repo_names(repo_root: Optional[Path] = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Extract (repo_name, repo_full_name) from git remote origin of repo_root.
    Returns (None, None) if git command fails or url cannot be parsed.
    """
    if repo_root is None:
        try:
            repo_root = Path(__file__).resolve().parent.parent
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
            repo_root = Path(__file__).resolve().parent.parent
        except Exception:
            return "graviton"
    remote_name, _ = _get_git_remote_repo_names(repo_root)
    if remote_name:
        return remote_name
    return repo_root.name


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

        target_root = repo_root if repo_root is not None else Path(__file__).resolve().parent.parent
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

    if action in ("opened", "synchronize", "reopened"):
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

    if is_bot_event(review_body, review_author) and review_state != "CHANGES_REQUESTED":
        return {
            "status": "ignored",
            "reason": "Bot self-review event dropped",
        }

    if pr_tracker and pr_number is not None:
        if action == "submitted":
            if review_state == "APPROVED" or (review_state == "COMMENTED" and has_approval_marker(review_body) and not has_change_request_marker(review_body)):
                pr_tracker.add_approved_pr(pr_number, pr_title, pr_author, pr_url, repo_full_name=repo_full_name)
            elif review_state == "CHANGES_REQUESTED" or has_change_request_marker(review_body):
                pr_tracker.remove_approved_pr(pr_number, repo_full_name=repo_full_name)
        elif action == "dismissed":
            pr_tracker.remove_approved_pr(pr_number, repo_full_name=repo_full_name)

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
    pr = payload.get("pull_request", {})
    pr_url = pr.get("html_url", "")
    pr_number = pr.get("number") or (pr_url.rstrip("/").split("/")[-1] if pr_url else "")
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    if is_bot_event(comment_body, comment_author):
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
            pr_number=pr_number,
            file=file_path,
            line=line,
        )
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
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    if action in ("opened", "reopened", "edited"):
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
            issue_number=issue_number,
        )
    elif action == "labeled":
        label = payload.get("label", {})
        label_name = label.get("name", "")
        if label_name in ("ready-for-pr", "ready-for-implementation"):
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
    comment_body = comment.get("body", "")
    comment_author = comment.get("user") or comment.get("author")
    issue = payload.get("issue", {})
    issue_number = issue.get("number")
    pr = issue.get("pull_request")
    repo_full_name, repo_name, clone_url = _extract_repo_info(payload)

    if is_bot_event(comment_body, comment_author) and not has_explicit_command(comment_body):
        return {
            "status": "ignored",
            "reason": "Bot comment dropped",
        }

    if pr and pr_tracker and issue_number is not None and action in ("created", "edited"):
        if has_change_request_marker(comment_body):
            pr_tracker.remove_approved_pr(issue_number, repo_full_name=repo_full_name)
        elif has_approval_marker(comment_body) and not is_bot_event(comment_body, comment_author):
            pr_title = issue.get("title", "")
            pr_url = issue.get("html_url", "") or issue.get("url", "")
            issue_user = issue.get("user", {})
            pr_author = issue_user.get("login", "") if isinstance(issue_user, dict) else str(issue_user or "")
            pr_tracker.add_approved_pr(issue_number, pr_title, pr_author, pr_url, repo_full_name=repo_full_name)

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
                pr_number=issue_number,
            )

        # 2. Comment on a pure Issue (Triage vs PR Drafting)
        else:
            labels_raw = issue.get("labels", [])
            labels = [
                l.get("name", "") if isinstance(l, dict) else str(l) for l in labels_raw
            ]
            body_lower = comment_body.lower()
            if "ready-for-pr" in labels or "ready-for-implementation" in labels or "/draft-pr" in body_lower:
                if repo_full_name:
                    prompt = f"Draft initial PR for Issue #{issue_number} in {repo_full_name} based on comment: '{comment_body}'"
                else:
                    prompt = f"Draft initial PR for Issue #{issue_number} based on comment: '{comment_body}'"
                return _build_accepted_response(
                    action=action,
                    agent=default_drafter,
                    prompt=prompt,
                    repo_full_name=repo_full_name,
                    repo_name=repo_name,
                    clone_url=clone_url,
                    issue_number=issue_number,
                )
            else:
                if repo_full_name:
                    prompt = f"Continue triage on Issue #{issue_number} in {repo_full_name} based on comment: '{comment_body}'"
                else:
                    prompt = f"Continue triage on Issue #{issue_number} based on comment: '{comment_body}'"
                return _build_accepted_response(
                    action=action,
                    agent=default_triager,
                    prompt=prompt,
                    repo_full_name=repo_full_name,
                    repo_name=repo_name,
                    clone_url=clone_url,
                    issue_number=issue_number,
                )

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
