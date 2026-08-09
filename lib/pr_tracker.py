"""
PR Tracker Module for Graviton.

Tracks GitHub Pull Requests that are approved and ready for human merge.
"""

import json
import logging
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("graviton.pr_tracker")


class PRTracker:
    """
    Thread-safe tracker for GitHub Pull Requests awaiting human merge.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._approved_prs: Dict[int, Dict[str, Any]] = {}

    def add_approved_pr(
        self,
        number: int,
        title: str = "",
        author: Any = "",
        url: str = "",
    ) -> None:
        """Mark a PR as approved and ready for merge."""
        try:
            num_int = int(number)
        except (ValueError, TypeError):
            return

        if isinstance(author, dict):
            author_str = author.get("login") or ""
        else:
            author_str = str(author or "")

        with self._lock:
            self._approved_prs[num_int] = {
                "number": num_int,
                "title": title or "",
                "author": author_str,
                "url": url or "",
            }

    def remove_approved_pr(self, number: int) -> None:
        """Remove a PR from the approved list."""
        try:
            num_int = int(number)
        except (ValueError, TypeError):
            return
        with self._lock:
            self._approved_prs.pop(num_int, None)

    # Aliases
    mark_approved = add_approved_pr
    remove_pr = remove_approved_pr

    def get_approved_prs(self) -> List[Dict[str, Any]]:
        """Return a sorted list of approved PRs (sorted by PR number)."""
        with self._lock:
            return sorted(self._approved_prs.values(), key=lambda x: x["number"])

    def sync_github_prs(self, repo_root: Optional[Path] = None) -> None:
        """
        Synchronize approved PRs state using `gh pr list`.
        Fetch open PRs with json fields: number,title,url,author,reviewDecision,isDraft.
        """
        try:
            cmd = [
                "gh",
                "pr",
                "list",
                "--json",
                "number,title,url,author,reviewDecision,isDraft",
            ]
            cwd = str(repo_root) if repo_root else None
            res = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(res.stdout)

            new_approved = {}
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    if item.get("isDraft") is True:
                        continue
                    review_decision = str(item.get("reviewDecision") or "").upper()
                    if review_decision == "APPROVED":
                        try:
                            num = int(item.get("number"))
                        except (ValueError, TypeError):
                            continue
                        title = item.get("title") or ""
                        url = item.get("url") or ""
                        author_raw = item.get("author")
                        if isinstance(author_raw, dict):
                            author = author_raw.get("login") or ""
                        else:
                            author = str(author_raw or "")
                        new_approved[num] = {
                            "number": num,
                            "title": title,
                            "author": author,
                            "url": url,
                        }

            with self._lock:
                self._approved_prs = new_approved
            logger.info(f"PRTracker synced {len(new_approved)} approved PR(s) via gh CLI.")
        except Exception as e:
            logger.warning(f"PRTracker initial background sync skipped/failed: {e}")

    def sync_in_background(self, repo_root: Optional[Path] = None) -> threading.Thread:
        """Start background thread to sync PRs via gh CLI."""
        t = threading.Thread(
            target=self.sync_github_prs,
            args=(repo_root,),
            daemon=True,
            name="PRTrackerSync",
        )
        t.start()
        return t
