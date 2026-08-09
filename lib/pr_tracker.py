"""
PR Tracker module for Graviton.

Tracks GitHub Pull Requests that have received full review approval
and are ready for merge.
"""

import json
import logging
import subprocess
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger("graviton")


class PRTracker:
    """
    Thread-safe tracker for approved GitHub pull requests awaiting merge.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._prs: Dict[int, Dict[str, Any]] = {}

    def mark_approved(
        self,
        pr_number: int,
        title: str = "",
        author: str = "",
        url: str = "",
    ):
        """Mark a PR as approved and ready for merge."""
        try:
            num = int(pr_number)
        except (ValueError, TypeError):
            return

        with self._lock:
            existing = self._prs.get(num, {})
            self._prs[num] = {
                "number": num,
                "title": title or existing.get("title", ""),
                "author": author or existing.get("author", ""),
                "url": url or existing.get("url", ""),
                "is_approved": True,
            }

    def remove_pr(self, pr_number: int):
        """Remove a PR from the ready list."""
        try:
            num = int(pr_number)
        except (ValueError, TypeError):
            return

        with self._lock:
            self._prs.pop(num, None)

    def mark_changes_requested(self, pr_number: int):
        """Remove a PR from the ready list when changes are requested."""
        self.remove_pr(pr_number)

    def get_approved_prs(self) -> List[Dict[str, Any]]:
        """Return list of currently approved PRs sorted by PR number."""
        with self._lock:
            approved = [
                dict(pr)
                for pr in self._prs.values()
                if pr.get("is_approved")
            ]
            return sorted(approved, key=lambda x: x["number"])

    def clear(self):
        """Clear all tracked PRs."""
        with self._lock:
            self._prs.clear()

    def sync_from_gh(self, cwd: Optional[str] = None):
        """
        Sync open PR status via gh CLI.
        Runs `gh pr list --json number,title,url,author,reviewDecision,isDraft`.
        """
        try:
            cmd = [
                "gh",
                "pr",
                "list",
                "--json",
                "number,title,url,author,reviewDecision,isDraft",
            ]
            res = subprocess.run(
                cmd, capture_output=True, text=True, cwd=cwd, check=True
            )
            items = json.loads(res.stdout)
            if not isinstance(items, list):
                return

            new_approved = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                num = item.get("number")
                if not num:
                    continue

                review_dec = item.get("reviewDecision", "")
                is_draft = item.get("isDraft", False)

                if review_dec == "APPROVED" and not is_draft:
                    author_val = item.get("author")
                    author_str = ""
                    if isinstance(author_val, dict):
                        author_str = author_val.get("login", "")
                    elif isinstance(author_val, str):
                        author_str = author_val

                    new_approved[int(num)] = {
                        "number": int(num),
                        "title": item.get("title", ""),
                        "author": author_str,
                        "url": item.get("url", ""),
                        "is_approved": True,
                    }

            with self._lock:
                self._prs = new_approved
        except Exception as e:
            logger.warning(f"Failed to sync PRs from gh CLI: {e}")
