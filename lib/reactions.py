"""
GitHub Emoji Reactions Module for Graviton.

Provides helper functions to asynchronously post visual acknowledgement reactions
(e.g., eyes emoji) on GitHub issues, pull requests, and comments.
"""

import json
import logging
import os
import subprocess
import threading
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger("graviton.reactions")


def get_reaction_endpoint(event_type: str, payload: Dict[str, Any]) -> Optional[str]:
    """
    Determine the GitHub REST API endpoint for posting an emoji reaction.

    :param event_type: GitHub X-GitHub-Event header string.
    :param payload: Parsed JSON webhook payload dictionary.
    :return: Endpoint path string (e.g., "/repos/owner/repo/issues/1/reactions") or None if not supported/invalid.
    """
    if not isinstance(payload, dict):
        return None

    repo = payload.get("repository")
    if not isinstance(repo, dict):
        return None

    repo_full_name = repo.get("full_name")
    if not repo_full_name:
        owner = repo.get("owner")
        owner_name = owner.get("login") or owner.get("name") if isinstance(owner, dict) else None
        repo_name = repo.get("name")
        if owner_name and repo_name:
            repo_full_name = f"{owner_name}/{repo_name}"

    if not repo_full_name:
        return None

    if event_type == "issue_comment":
        comment = payload.get("comment")
        comment_id = comment.get("id") if isinstance(comment, dict) else None
        if comment_id is not None:
            return f"/repos/{repo_full_name}/issues/comments/{comment_id}/reactions"

    elif event_type == "pull_request_review_comment":
        comment = payload.get("comment")
        comment_id = comment.get("id") if isinstance(comment, dict) else None
        if comment_id is not None:
            return f"/repos/{repo_full_name}/pulls/comments/{comment_id}/reactions"

    elif event_type == "pull_request_review":
        review = payload.get("review")
        review_dict = review if isinstance(review, dict) else {}
        comment_id = review_dict.get("comment_id")
        if comment_id is not None:
            return f"/repos/{repo_full_name}/pulls/comments/{comment_id}/reactions"

        pr = payload.get("pull_request")
        pr_dict = pr if isinstance(pr, dict) else {}
        pr_number = pr_dict.get("number") or payload.get("number")
        if pr_number is not None:
            return f"/repos/{repo_full_name}/issues/{pr_number}/reactions"

    elif event_type == "issues":
        issue = payload.get("issue")
        issue_dict = issue if isinstance(issue, dict) else {}
        issue_number = issue_dict.get("number") or payload.get("number")
        if issue_number is not None:
            return f"/repos/{repo_full_name}/issues/{issue_number}/reactions"

    elif event_type == "pull_request":
        pr = payload.get("pull_request")
        pr_dict = pr if isinstance(pr, dict) else {}
        pr_number = pr_dict.get("number") or payload.get("number")
        if pr_number is not None:
            return f"/repos/{repo_full_name}/issues/{pr_number}/reactions"

    return None


def post_emoji_reaction(
    event_type: str,
    payload: Dict[str, Any],
    reaction: str = "eyes",
    timeout: float = 10.0,
) -> bool:
    """
    Post an emoji reaction to a GitHub issue, PR, or comment.

    :param event_type: GitHub event type string.
    :param payload: Webhook payload dictionary.
    :param reaction: Emoji reaction content (default: 'eyes').
    :param timeout: Request timeout in seconds.
    :return: True if reaction posted successfully, False otherwise.
    """
    endpoint = get_reaction_endpoint(event_type, payload)
    if not endpoint:
        logger.warning(f"Could not determine reaction endpoint for event '{event_type}'.")
        return False

    # 1. Try gh CLI if available
    try:
        cmd = [
            "gh",
            "api",
            "-X",
            "POST",
            endpoint,
            "-f",
            f"content={reaction}",
        ]
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if res.returncode == 0:
            logger.info(f"Successfully posted '{reaction}' reaction via gh CLI to {endpoint}")
            return True
        else:
            logger.warning(f"gh CLI returned non-zero ({res.returncode}) for reaction to {endpoint}: {res.stderr.strip()}")
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as e:
        logger.warning(f"gh CLI execution failed for reaction to {endpoint}: {e}")

    # 2. Fallback to urllib.request
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        logger.warning(f"No GITHUB_TOKEN or GH_TOKEN set. Cannot fallback to urllib for {endpoint}.")
        return False

    url = f"https://api.github.com{endpoint}"
    data = json.dumps({"content": reaction}).encode("utf-8")
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
                logger.info(f"Successfully posted '{reaction}' reaction via urllib to {endpoint}")
                return True
            else:
                logger.warning(f"urllib returned HTTP status {resp.status} for reaction to {endpoint}")
                return False
    except Exception as e:
        logger.warning(f"urllib request failed for reaction to {endpoint}: {e}")
        return False


def post_emoji_reaction_async(
    event_type: str,
    payload: Dict[str, Any],
    reaction: str = "eyes",
) -> threading.Thread:
    """
    Post an emoji reaction asynchronously in a daemon background thread.

    :param event_type: GitHub event type string.
    :param payload: Webhook payload dictionary.
    :param reaction: Emoji reaction content (default: 'eyes').
    :return: The started threading.Thread object.
    """
    t = threading.Thread(
        target=post_emoji_reaction,
        args=(event_type, payload, reaction),
        daemon=True,
        name="EmojiReactionThread",
    )
    t.start()
    return t
