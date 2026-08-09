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
    Thread-safe tracker for GitHub Pull Requests awaiting human merge across single or multiple repositories.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._approved_prs: Dict[Any, Dict[str, Any]] = {}

    def add_approved_pr(
        self,
        number: int,
        title: str = "",
        author: Any = "",
        url: str = "",
        repo_full_name: str = "",
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

        repo_str = repo_full_name or ""
        key = (repo_str, num_int)

        with self._lock:
            self._approved_prs[key] = {
                "number": num_int,
                "repo_full_name": repo_str,
                "title": title or "",
                "author": author_str,
                "url": url or "",
            }

    def remove_approved_pr(self, number: int, repo_full_name: str = "") -> None:
        """Remove a PR from the approved list."""
        try:
            num_int = int(number)
        except (ValueError, TypeError):
            return
        repo_str = repo_full_name or ""
        with self._lock:
            if repo_str:
                self._approved_prs.pop((repo_str, num_int), None)
                self._approved_prs.pop(("", num_int), None)
                self._approved_prs.pop(num_int, None)
            else:
                to_remove = [k for k in self._approved_prs if k == num_int or (isinstance(k, tuple) and k[1] == num_int)]
                for k in to_remove:
                    self._approved_prs.pop(k, None)

    # Aliases
    mark_approved = add_approved_pr
    remove_pr = remove_approved_pr

    def get_approved_prs(self) -> List[Dict[str, Any]]:
        """Return a sorted list of approved PRs."""
        with self._lock:
            return sorted(
                self._approved_prs.values(),
                key=lambda x: (x.get("repo_full_name", ""), x.get("number", 0)),
            )

    def _sync_single_repo(self, cwd: Optional[str] = None) -> List[Dict[str, Any]]:
        cmd = [
            "gh",
            "pr",
            "list",
            "--json",
            "number,title,url,author,reviewDecision,isDraft,latestReviews",
        ]
        res = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(res.stdout)
        results = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or item.get("isDraft") is True:
                    continue
                review_decision = str(item.get("reviewDecision") or "").upper()
                is_approved = review_decision == "APPROVED"
                if not is_approved and review_decision != "CHANGES_REQUESTED":
                    latest_reviews = item.get("latestReviews")
                    if isinstance(latest_reviews, list):
                        states = [
                            str(r.get("state") or "").upper()
                            for r in latest_reviews
                            if isinstance(r, dict)
                        ]
                        if "CHANGES_REQUESTED" not in states and "APPROVED" in states:
                            is_approved = True
                if is_approved:
                    try:
                        num = int(item.get("number"))
                    except (ValueError, TypeError):
                        continue
                    title = item.get("title") or ""
                    url = item.get("url") or ""
                    author_raw = item.get("author")
                    author = author_raw.get("login") if isinstance(author_raw, dict) else str(author_raw or "")
                    results.append({
                        "number": num,
                        "title": title,
                        "author": author,
                        "url": url,
                    })
        return results

    def sync_github_prs(
        self, repo_root: Optional[Path] = None, repos_dir: Optional[Path] = None
    ) -> None:
        """
        Synchronize approved PRs state using `gh pr list`.
        Fetch open PRs across repo_root and managed repositories inside repos_dir.
        """
        try:
            new_approved = {}
            target_dirs = []
            if repo_root and Path(repo_root).exists():
                target_dirs.append(Path(repo_root))
            if repos_dir and Path(repos_dir).exists():
                for item in Path(repos_dir).iterdir():
                    if item.is_dir() and (item / ".git").exists() and item not in target_dirs:
                        target_dirs.append(item)

            if not target_dirs:
                target_dirs.append(Path(repo_root) if repo_root else Path.cwd())

            for d in target_dirs:
                repo_full_name = ""
                git_res = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=str(d),
                    capture_output=True,
                    text=True,
                )
                if git_res.returncode == 0 and git_res.stdout:
                    origin_url = git_res.stdout.strip()
                    if "github.com" in origin_url:
                        parts = origin_url.removesuffix(".git").split("github.com")[-1].lstrip(":/").split("/")
                        if len(parts) >= 2:
                            repo_full_name = f"{parts[-2]}/{parts[-1]}"

                prs = self._sync_single_repo(cwd=str(d))
                for item in prs:
                    num = item["number"]
                    key = (repo_full_name, num)
                    new_approved[key] = {
                        "number": num,
                        "repo_full_name": repo_full_name,
                        "title": item["title"],
                        "author": item["author"],
                        "url": item["url"],
                    }

            with self._lock:
                self._approved_prs = new_approved
            logger.info(f"PRTracker synced {len(new_approved)} approved PR(s) via gh CLI.")
        except Exception as e:
            logger.warning(f"PRTracker initial background sync skipped/failed: {e}")

    def sync_in_background(
        self, repo_root: Optional[Path] = None, repos_dir: Optional[Path] = None
    ) -> threading.Thread:
        """Start background thread to sync PRs via gh CLI."""
        t = threading.Thread(
            target=self.sync_github_prs,
            args=(repo_root, repos_dir),
            daemon=True,
            name="PRTrackerSync",
        )
        t.start()
        return t
