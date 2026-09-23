"""
GitHub notification helpers for Graviton tasks and PR/issue comments.
"""

import logging
import re
from typing import Any, Optional

import lib.release

logger = logging.getLogger("graviton.notifications")

__all__ = [
    "post_task_completion_comment",
    "post_task_start_comment",
]


def post_task_completion_comment(
    task: Any,
    result: Optional[Any] = None,
    timeout: float = 10.0,
) -> bool:
    """
    Post a structured completion comment to the GitHub PR or issue.
    """
    if not task or not getattr(task, "repo_full_name", None) or not getattr(task, "target_id", None):
        return False
    m = re.search(r"#?(\d+)$", str(task.target_id).strip())
    if not m:
        return False
    try:
        issue_number = int(m.group(1))
    except (TypeError, ValueError):
        return False

    is_success = (result and getattr(result, "is_success", False)) or getattr(task, "status", None) == "COMPLETED"
    status_icon = "✅" if is_success else "❌"
    agent_name = getattr(task, "agent", None) or "agent"
    header = f"{status_icon} **Antigravity Agent `{agent_name}` Finished**"

    body_parts = [header]
    cid = getattr(task, "conversation_id", None) or (getattr(result, "conversation_id", None) if result else None)
    if cid:
        body_parts.append(f"- **Conversation ID**: `{cid}`")
    rc_url = getattr(task, "remote_control_url", None) or (getattr(result, "remote_control_url", None) if result else None)
    if rc_url:
        body_parts.append(f"- **Remote Control**: [{rc_url}]({rc_url})")
    elapsed = getattr(task, "elapsed_time", 0.0)
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        body_parts.append(f"- **Elapsed Time**: {elapsed:.1f}s")
    if result and getattr(result, "response", None):
        resp_preview = str(result.response).strip()
        resp_preview = resp_preview.replace("<!-- GOAL_COMPLETE -->", "").strip()
        if resp_preview:
            if len(resp_preview) > 3000:
                resp_preview = resp_preview[:3000] + "\n\n*(output truncated)*"
            body_parts.append(f"\n<details><summary>Agent Response Summary</summary>\n\n{resp_preview}\n\n</details>")

    body_parts.append("\n<!-- antigravity-auto-reply -->\n<!-- graviton:task_manager -->")
    body = "\n".join(body_parts)
    try:
        return lib.release.post_issue_comment(task.repo_full_name, issue_number, body, timeout=timeout)
    except Exception as e:
        logger.warning(f"Failed to post task completion comment: {e}")
        return False


def post_task_start_comment(
    task: Any,
    timeout: float = 10.0,
) -> bool:
    """
    Post an initial progress comment with live remote control link to GitHub PR or issue.
    """
    if not task or not getattr(task, "repo_full_name", None) or not getattr(task, "target_id", None):
        return False
    m = re.search(r"#?(\d+)$", str(task.target_id).strip())
    if not m:
        return False
    try:
        issue_number = int(m.group(1))
    except (TypeError, ValueError):
        return False

    agent_name = getattr(task, "agent", None) or "agent"
    body_parts = [f"🚀 **Antigravity Agent `{agent_name}` Started**"]
    cid = getattr(task, "conversation_id", None)
    if cid:
        body_parts.append(f"- **Conversation ID**: `{cid}`")
    rc_url = getattr(task, "remote_control_url", None)
    if rc_url:
        body_parts.append(f"- **Live Remote Control**: [{rc_url}]({rc_url})")
    body_parts.append("\n<!-- antigravity-auto-reply -->\n<!-- graviton:task_manager:start -->")
    body = "\n".join(body_parts)
    try:
        return lib.release.post_issue_comment(task.repo_full_name, issue_number, body, timeout=timeout)
    except Exception as e:
        logger.warning(f"Failed to post task start comment: {e}")
        return False
