"""
PR Tracker Module for Graviton.

Tracks GitHub Pull Requests that are approved and ready for human merge.
"""

import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from lib.security import contains_bot_marker

logger = logging.getLogger("graviton.pr_tracker")

_QUESTION_PATTERN = re.compile(r'\b(how|when|why|what|if|whether)\b(?:\s+\w+){0,4}\s+(approved|lgtm)\b')
_INVERTED_QUESTION_PATTERN = re.compile(r'(?:^|[\.\!\?\n;])\s*\b(is|was)\b(?:\s+\w+){0,4}\s+(approved|lgtm)\b')
_NOUN_PHRASE_PATTERN = re.compile(r'\bapproved\s+(pull\s+requests?|prs?|panel|box|list|section|screen|widget|table|feature)\b')
_PREPOSITION_PATTERN = re.compile(r'\b(in|of|from|on|about|view|show|display|fix|fixed|update|updated)\s+approved\b')
_MULTI_WORD_NEG_PATTERN = re.compile(r'\b(not|no|cannot|can\'t|won\'t|don\'t|doesn\'t|never|needs?|requires?|awaiting|pending)\b(?:\s+\w+){0,3}\s+(approved|lgtm)\b')
_PREFIX_NEG_PATTERN = re.compile(r'\b(un|non|dis)[-\s]?(approved|lgtm)\b')
_CR_PATTERN = re.compile(r'\bchanges[_\s]requested\b')
_NEG_CR_PATTERN = re.compile(r'\b(no|not|without|zero|never|don\'t|didn\'t|haven\'t)\b(?:\s+\w+){0,3}\s+changes[_\s]requested\b')
_BOT_LOGIN_PATTERN = re.compile(r'(?:^|[-_])bot(?:[-_]|$)')
_ORIGIN_URL_PATTERN = re.compile(r'[:/]([^/]+/[^/]+)$')

_NEGATIVE_APPROVAL_PHRASES: Tuple[str, ...] = (
    "not approved",
    "unapproved",
    "disapproved",
    "non-approved",
    "not yet approved",
    "not been approved",
    "not fully approved",
    "not completely approved",
    "not be approved",
    "cannot be approved",
    "can't be approved",
    "won't be approved",
    "will be approved",
    "to be approved",
    "should be approved",
    "must be approved",
    "may be approved",
    "would be approved",
    "could be approved",
    "pending approval",
    "awaiting approval",
    "needs approval",
    "requiring approval",
    "requires approval",
    "not lgtm",
    "no lgtm",
    "non-lgtm",
    "un-lgtm",
    "is this pr approved",
    "is this approved",
    "was this approved",
    "has this been approved",
    "has it been approved",
)

_EXPLICIT_BOT_LOGINS: Tuple[str, ...] = (
    "antigravity",
    "code_reviewer",
    "code_fixer",
    "issue_triager",
    "pr_drafter",
    "codebase_auditor",
)



def _parse_event_timestamp(ts: Any) -> float:
    """Parse an ISO timestamp string or numeric timestamp to float epoch seconds for chronological sorting."""
    if not ts:
        return 0.0
    if isinstance(ts, (int, float)) and not isinstance(ts, bool):
        return float(ts)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.timestamp()
    if isinstance(ts, str):
        ts_str = ts.strip()
        if not ts_str:
            return 0.0
        try:
            if ts_str.endswith("Z") or ts_str.endswith("z"):
                clean_ts = ts_str[:-1] + "+00:00"
            else:
                clean_ts = ts_str
            dt = datetime.fromisoformat(clean_ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def has_approval_marker(text: str) -> bool:
    """Check if text contains approval markers like LGTM or Approved."""
    if not text or not isinstance(text, str):
        return False
    text_lower = text.lower()
    if "lgtm" not in text_lower and "approved" not in text_lower:
        return False

    # Reject questions (trailing '?' or question words/phrasing like 'Is this PR approved?', 'Was this approved?', 'Has this been approved?')
    if "?" in text_lower:
        return False

    if _QUESTION_PATTERN.search(text_lower) or _INVERTED_QUESTION_PATTERN.search(text_lower):
        return False

    # Reject discussion / noun phrases (e.g. "approved pull requests panel", "in approved prs")
    if _NOUN_PHRASE_PATTERN.search(text_lower) or _PREPOSITION_PATTERN.search(text_lower):
        return False

    if _MULTI_WORD_NEG_PATTERN.search(text_lower) or _PREFIX_NEG_PATTERN.search(text_lower):
        return False

    if any(neg in text_lower for neg in _NEGATIVE_APPROVAL_PHRASES):
        return False

    return True


def has_change_request_marker(text: str) -> bool:
    """Check if text contains change request markers or commands."""
    if not text:
        return False
    text_lower = text.lower()
    if "/fix" in text_lower:
        return True

    matches = list(_CR_PATTERN.finditer(text_lower))
    if not matches:
        return False

    neg_matches = list(_NEG_CR_PATTERN.finditer(text_lower))

    if len(neg_matches) >= len(matches):
        return False

    return True


def is_bot_event(body: str, author_raw: Any = None) -> bool:
    """Check if a review or comment event originated from a bot."""
    if contains_bot_marker(body):
        return True
    if isinstance(author_raw, dict):
        if author_raw.get("isBot") is True or author_raw.get("type") == "Bot":
            return True
        login = str(author_raw.get("login") or "").lower()
    else:
        login = str(author_raw or "").lower()
    if not login:
        return False
    if login.endswith("[bot]"):
        return True
    if _BOT_LOGIN_PATTERN.search(login):
        return True
    if any(b in login for b in _EXPLICIT_BOT_LOGINS):
        return True
    return False


class PRTracker:
    """
    Thread-safe tracker for GitHub Pull Requests awaiting human merge across single or multiple repositories.
    """

    def __init__(self, cache_ttl: float = 60.0):
        self._lock = threading.RLock()
        self._approved_prs: Dict[Any, Dict[str, Any]] = {}
        self._open_prs: Dict[Any, Dict[str, Any]] = {}
        self._open_issues: Dict[Any, Dict[str, Any]] = {}
        self._last_sync_time: float = 0.0
        self._last_issues_sync_time: float = 0.0
        self._cache_ttl: float = cache_ttl
        self._polling_thread: Optional[threading.Thread] = None
        self._stop_polling_event = threading.Event()
        self._poll_interval: float = 30.0
        self._repo_root: Optional[Path] = None
        self._repos_dir: Optional[Path] = None
        self._on_sync: Optional[Callable[[], None]] = None

    def add_approved_pr(
        self,
        number: Union[int, Dict[str, Any]],
        title: str = "",
        author: Any = "",
        url: str = "",
        repo_full_name: str = "",
    ) -> None:
        """Mark a PR as approved and ready for merge."""
        if isinstance(number, dict):
            d = number
            number = d.get("number", 0)
            title = d.get("title", title)
            author = d.get("author", author)
            url = d.get("url", url)
            repo_full_name = d.get("repo_full_name", repo_full_name)

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

    def remove_approved_pr(self, number: Union[int, str], repo_full_name: str = "") -> None:
        """Remove a PR from the approved list."""
        if isinstance(number, str) and not number.isdigit() and (isinstance(repo_full_name, int) or (isinstance(repo_full_name, str) and repo_full_name.isdigit())):
            repo_full_name, number = number, int(repo_full_name)
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

    def get_approved_prs(self, repo_full_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a sorted list of approved PRs."""
        with self._lock:
            prs = list(self._approved_prs.values())
            if repo_full_name:
                prs = [p for p in prs if p.get("repo_full_name") == repo_full_name]
            return sorted(
                prs,
                key=lambda x: (x.get("repo_full_name", ""), x.get("number", 0)),
            )

    def add_open_pr(
        self,
        number: Union[int, Dict[str, Any]],
        title: str = "",
        author: Any = "",
        url: str = "",
        repo_full_name: str = "",
        is_draft: bool = False,
        status: str = "OPEN",
        updated_at: str = "",
        review_decision: str = "",
    ) -> None:
        """Add or update an open pull request."""
        if isinstance(number, dict):
            d = number
            number = d.get("number", 0)
            title = d.get("title", title)
            author = d.get("author", author)
            url = d.get("url", url)
            repo_full_name = d.get("repo_full_name", repo_full_name)
            is_draft = d.get("is_draft", is_draft)
            status = d.get("status", status)
            updated_at = d.get("updated_at", updated_at)
            review_decision = d.get("review_decision", review_decision)

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
            self._open_prs[key] = {
                "number": num_int,
                "repo_full_name": repo_str,
                "title": title or "",
                "author": author_str,
                "url": url or "",
                "is_draft": bool(is_draft),
                "status": status or "OPEN",
                "updated_at": updated_at or "",
                "review_decision": review_decision or "",
            }

    def remove_open_pr(self, number: Union[int, str], repo_full_name: str = "") -> None:
        """Remove a PR from the open PRs list."""
        if isinstance(number, str) and not number.isdigit() and (isinstance(repo_full_name, int) or (isinstance(repo_full_name, str) and repo_full_name.isdigit())):
            repo_full_name, number = number, int(repo_full_name)
        try:
            num_int = int(number)
        except (ValueError, TypeError):
            return
        repo_str = repo_full_name or ""
        with self._lock:
            if repo_str:
                self._open_prs.pop((repo_str, num_int), None)
                self._open_prs.pop(("", num_int), None)
                self._open_prs.pop(num_int, None)
            else:
                to_remove = [k for k in self._open_prs if k == num_int or (isinstance(k, tuple) and k[1] == num_int)]
                for k in to_remove:
                    self._open_prs.pop(k, None)

    def get_open_prs(self, repo_full_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a sorted list of open pull requests across tracked repositories."""
        with self._lock:
            prs = list(self._open_prs.values())
            if repo_full_name:
                prs = [p for p in prs if p.get("repo_full_name") == repo_full_name]
            return sorted(
                prs,
                key=lambda x: (x.get("repo_full_name", ""), x.get("number", 0)),
            )

    def add_open_issue(
        self,
        number: Union[int, Dict[str, Any]],
        title: str = "",
        author: Any = "",
        url: str = "",
        repo_full_name: str = "",
        labels: Optional[List[str]] = None,
        updated_at: str = "",
        state: str = "OPEN",
    ) -> None:
        """Add or update an open issue."""
        if isinstance(number, dict):
            d = number
            number = d.get("number", 0)
            title = d.get("title", title)
            author = d.get("author", author)
            url = d.get("url", url)
            repo_full_name = d.get("repo_full_name", repo_full_name)
            labels = d.get("labels", labels)
            updated_at = d.get("updated_at", updated_at)
            state = d.get("state", state)

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

        labels_list = list(labels) if labels is not None else []

        with self._lock:
            self._open_issues[key] = {
                "number": num_int,
                "repo_full_name": repo_str,
                "title": title or "",
                "author": author_str,
                "url": url or "",
                "labels": labels_list,
                "updated_at": updated_at or "",
                "state": state or "OPEN",
            }

    def remove_open_issue(self, number: Union[int, str], repo_full_name: str = "") -> None:
        """Remove an issue from the open issues list."""
        if isinstance(number, str) and not number.isdigit() and (isinstance(repo_full_name, int) or (isinstance(repo_full_name, str) and repo_full_name.isdigit())):
            repo_full_name, number = number, int(repo_full_name)
        try:
            num_int = int(number)
        except (ValueError, TypeError):
            return
        repo_str = repo_full_name or ""
        with self._lock:
            if repo_str:
                self._open_issues.pop((repo_str, num_int), None)
                self._open_issues.pop(("", num_int), None)
                self._open_issues.pop(num_int, None)
            else:
                to_remove = [k for k in self._open_issues if k == num_int or (isinstance(k, tuple) and k[1] == num_int)]
                for k in to_remove:
                    self._open_issues.pop(k, None)

    def get_open_issues(self, repo_full_name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return a sorted list of open issues across tracked repositories."""
        with self._lock:
            issues = list(self._open_issues.values())
            if repo_full_name:
                issues = [i for i in issues if i.get("repo_full_name") == repo_full_name]
            return sorted(
                issues,
                key=lambda x: (x.get("repo_full_name", ""), x.get("number", 0)),
            )

    def get_repo_status(self) -> Dict[str, Any]:
        """Return aggregated repository status dictionary containing open PRs, approved PRs, and issues."""
        with self._lock:
            repos: Set[str] = set()
            for k in self._open_prs:
                if isinstance(k, tuple) and k[0]:
                    repos.add(k[0])
            for k in self._open_issues:
                if isinstance(k, tuple) and k[0]:
                    repos.add(k[0])
            for k in self._approved_prs:
                if isinstance(k, tuple) and k[0]:
                    repos.add(k[0])

            status_by_repo: Dict[str, Dict[str, Any]] = {}
            for repo in sorted(repos):
                repo_prs = [p for p in self._open_prs.values() if p.get("repo_full_name") == repo]
                repo_approved = [p for p in self._approved_prs.values() if p.get("repo_full_name") == repo]
                repo_issues = [i for i in self._open_issues.values() if i.get("repo_full_name") == repo]
                status_by_repo[repo] = {
                    "open_prs": repo_prs,
                    "approved_prs": repo_approved,
                    "open_issues": repo_issues,
                    "open_prs_count": len(repo_prs),
                    "open_issues_count": len(repo_issues),
                    "approved_prs_count": len(repo_approved),
                }

            return {
                "repositories": status_by_repo,
                "repos": status_by_repo,
                "summary": {
                    "total_repos": len(repos),
                    "total_open_prs": len(self._open_prs),
                    "total_open_issues": len(self._open_issues),
                    "total_approved_prs": len(self._approved_prs),
                },
                "total_open_prs": len(self._open_prs),
                "total_open_issues": len(self._open_issues),
                "total_approved_prs": len(self._approved_prs),
                "last_sync_time": self._last_sync_time,
            }

    def _get_repo_full_name_for_dir(self, d: Path) -> str:
        """Resolve origin repository full name (owner/repo) for a directory."""
        repo_full_name = ""
        try:
            git_res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=str(d),
                capture_output=True,
                text=True,
            )
            if git_res.returncode == 0 and git_res.stdout:
                origin_url = git_res.stdout.strip()
                clean_url = origin_url.strip().rstrip("/").removesuffix(".git").rstrip("/")
                match = _ORIGIN_URL_PATTERN.search(clean_url)
                if match:
                    repo_full_name = match.group(1)
        except Exception as e:
            logger.debug(f"Failed resolving origin url for directory '{d}': {e}")
        return repo_full_name

    def _sync_single_repo_details(
        self, cwd: Optional[str] = None, repo_full_name: str = ""
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Fetch pull requests using gh CLI, returning (approved_prs, open_prs)."""
        cmd = [
            "gh",
            "pr",
            "list",
            "--limit",
            "300",
            "--json",
            "number,title,url,author,reviewDecision,isDraft,latestReviews,comments,reviews",
        ]
        res = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        data = json.loads(res.stdout)
        approved_results = []
        open_pr_results = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue

                try:
                    num = int(item.get("number"))
                except (ValueError, TypeError):
                    continue

                title = item.get("title") or ""
                url = item.get("url") or ""
                author_raw = item.get("author")
                author = (author_raw.get("login") or "") if isinstance(author_raw, dict) else str(author_raw or "")
                is_draft = item.get("isDraft") is True
                review_decision = str(item.get("reviewDecision") or "").upper()
                updated_at = str(item.get("updatedAt") or "")

                reviews = item.get("reviews")
                latest_reviews = item.get("latestReviews")
                comments = item.get("comments")

                events = []
                seen_review_ids = set()
                for rev_list in (latest_reviews, reviews):
                    if isinstance(rev_list, list):
                        for r in rev_list:
                            if isinstance(r, dict):
                                raw_id = r.get("id")
                                r_id = str(raw_id) if raw_id is not None and raw_id != "" else (
                                    r.get("submittedAt"),
                                    r.get("createdAt"),
                                    r.get("body"),
                                    r.get("state"),
                                    str(r.get("author")),
                                )
                                if r_id in seen_review_ids:
                                    continue
                                seen_review_ids.add(r_id)
                                events.append({
                                    "state": str(r.get("state") or "").upper(),
                                    "body": r.get("body") or "",
                                    "author": r.get("author"),
                                    "created_at": r.get("submittedAt") or r.get("createdAt") or "",
                                })

                if isinstance(comments, list):
                    for c in comments:
                        if isinstance(c, dict):
                            events.append({
                                "state": "",
                                "body": c.get("body") or "",
                                "author": c.get("author") or c.get("user"),
                                "created_at": c.get("createdAt") or c.get("submittedAt") or "",
                            })

                events.sort(key=lambda x: _parse_event_timestamp(x.get("created_at")))

                has_bot_approval = False
                has_human_approval = False
                has_cr = False
                for ev in events:
                    state = ev["state"]
                    body = ev["body"]
                    ev_author = ev["author"]
                    if state == "CHANGES_REQUESTED" or has_change_request_marker(body):
                        has_cr = True
                        has_human_approval = False
                        has_bot_approval = False
                    elif state == "DISMISSED":
                        has_human_approval = False
                        has_bot_approval = False
                    elif state == "APPROVED" or (has_approval_marker(body) and not has_change_request_marker(body)):
                        has_cr = False
                        if is_bot_event(body, ev_author):
                            has_bot_approval = True
                        else:
                            has_human_approval = True

                if has_cr:
                    is_approved = False
                elif has_human_approval or has_bot_approval:
                    is_approved = True
                elif review_decision == "CHANGES_REQUESTED":
                    is_approved = False
                elif review_decision == "APPROVED":
                    is_approved = True
                else:
                    is_approved = False

                if is_draft:
                    is_approved = False

                if is_approved:
                    approved_results.append({
                        "number": num,
                        "title": title,
                        "author": author,
                        "url": url,
                        "repo_full_name": repo_full_name,
                    })

                status = "OPEN"
                if is_approved:
                    status = "APPROVED"
                elif has_cr or review_decision == "CHANGES_REQUESTED":
                    status = "CHANGES_REQUESTED"
                elif is_draft:
                    status = "DRAFT"
                elif review_decision:
                    status = review_decision
                else:
                    status = "AWAITING_REVIEW"

                open_pr_results.append({
                    "number": num,
                    "repo_full_name": repo_full_name,
                    "title": title,
                    "author": author,
                    "url": url,
                    "is_draft": is_draft,
                    "status": status,
                    "updated_at": updated_at,
                })

        return approved_results, open_pr_results

    def _sync_single_repo(self, cwd: Optional[str] = None) -> List[Dict[str, Any]]:
        """Synchronize approved PRs for a single repository directory (backwards compatible)."""
        approved, _ = self._sync_single_repo_details(cwd=cwd)
        return approved

    def _sync_directory(self, d: Path) -> Tuple[str, List[Dict[str, Any]]]:
        """Synchronize repository directory via gh CLI, updating open PRs cache and returning approved PRs."""
        repo_full_name = self._get_repo_full_name_for_dir(d)
        approved_prs, open_prs = self._sync_single_repo_details(cwd=str(d), repo_full_name=repo_full_name)
        with self._lock:
            # Refresh open PRs cache for this repository
            to_remove = [k for k in self._open_prs if isinstance(k, tuple) and k[0] == repo_full_name]
            for k in to_remove:
                self._open_prs.pop(k, None)
            for item in open_prs:
                num = item["number"]
                self._open_prs[(repo_full_name, num)] = item
        return repo_full_name, approved_prs

    def _resolve_target_dirs(
        self, repo_root: Optional[Path] = None, repos_dir: Optional[Path] = None
    ) -> List[Path]:
        """Discover and deduplicate target repository directories."""
        target_dirs: List[Path] = []
        resolved_seen: Set[Path] = set()
        repo_root_failed = False

        if repo_root and Path(repo_root).exists():
            try:
                resolved_root = Path(repo_root).resolve()
                target_dirs.append(resolved_root)
                resolved_seen.add(resolved_root)
            except Exception as e:
                repo_root_failed = True
                logger.warning(f"Failed to resolve repo_root '{repo_root}': {e}")

        if repos_dir and Path(repos_dir).exists():
            for item in Path(repos_dir).iterdir():
                try:
                    if item.is_dir() and (item / ".git").exists():
                        resolved_item = item.resolve()
                        if resolved_item not in resolved_seen:
                            target_dirs.append(resolved_item)
                            resolved_seen.add(resolved_item)
                except Exception as e:
                    logger.warning(f"Failed inspecting repository directory '{item}': {e}")

        if not target_dirs:
            fallback_base = Path(repo_root) if (repo_root and not repo_root_failed) else Path.cwd()
            try:
                fallback_dir = fallback_base.resolve()
            except Exception as e:
                logger.warning(f"Failed to resolve fallback directory '{fallback_base}': {e}")
                try:
                    fallback_dir = Path.cwd().resolve()
                except Exception:
                    fallback_dir = Path.cwd()
            target_dirs.append(fallback_dir)

        return target_dirs

    def sync_github_prs(
        self,
        repo_root: Optional[Path] = None,
        repos_dir: Optional[Path] = None,
        force: bool = False,
        ttl: Optional[float] = None,
    ) -> None:
        """
        Synchronize approved and open PRs state using `gh pr list`.
        Fetch open PRs across repo_root and managed repositories inside repos_dir.
        Caches results for `ttl` seconds unless force=True.
        """
        effective_ttl = self._cache_ttl if ttl is None else ttl
        now = time.time()
        if not force:
            with self._lock:
                if self._last_sync_time > 0 and (now - self._last_sync_time) < effective_ttl:
                    return

        try:
            new_approved = {}
            target_dirs = self._resolve_target_dirs(repo_root=repo_root, repos_dir=repos_dir)

            success_count = 0
            max_workers = min(10, max(1, len(target_dirs)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_dir = {
                    executor.submit(self._sync_directory, d): d
                    for d in target_dirs
                }
                for future in as_completed(future_to_dir):
                    d = future_to_dir[future]
                    try:
                        res = future.result()
                        repo_full_name = res[0]
                        prs = res[1]
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
                        success_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to sync PRs for repository directory '{d}': {e}")

            if success_count > 0 or not target_dirs:
                with self._lock:
                    self._approved_prs = new_approved
                    self._last_sync_time = time.time()
                logger.info(f"PRTracker synced {len(new_approved)} approved PR(s) via gh CLI.")
            else:
                logger.warning("PRTracker sync failed for all target directories; preserving existing approved PRs.")
        except Exception as e:
            logger.warning(f"PRTracker initial background sync skipped/failed: {e}")

    def _sync_issues_single_repo(
        self, cwd: Optional[str] = None, repo_full_name: str = ""
    ) -> List[Dict[str, Any]]:
        """Fetch open issues for a single repository directory via gh CLI."""
        cmd = [
            "gh",
            "issue",
            "list",
            "--limit",
            "300",
            "--json",
            "number,title,url,author,labels,updatedAt",
        ]
        try:
            res = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
            data = json.loads(res.stdout)
        except Exception as e:
            logger.debug(f"Failed to query issues via gh CLI for {cwd}: {e}")
            return []

        results = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    num = int(item.get("number"))
                except (ValueError, TypeError):
                    continue
                title = item.get("title") or ""
                url = item.get("url") or ""
                author_raw = item.get("author")
                author = (author_raw.get("login") or "") if isinstance(author_raw, dict) else str(author_raw or "")
                labels_raw = item.get("labels") or []
                labels = []
                if isinstance(labels_raw, list):
                    for l in labels_raw:
                        if isinstance(l, dict) and "name" in l:
                            labels.append(str(l["name"]))
                        elif isinstance(l, str):
                            labels.append(l)
                updated_at = str(item.get("updatedAt") or "")
                results.append({
                    "number": num,
                    "repo_full_name": repo_full_name,
                    "title": title,
                    "author": author,
                    "url": url,
                    "labels": labels,
                    "updated_at": updated_at,
                })
        return results

    def _sync_issues_directory(self, d: Path) -> Tuple[str, List[Dict[str, Any]]]:
        """Synchronize open issues for a repository directory."""
        repo_full_name = self._get_repo_full_name_for_dir(d)
        issues = self._sync_issues_single_repo(cwd=str(d), repo_full_name=repo_full_name)
        return repo_full_name, issues

    def sync_github_issues(
        self,
        repo_root: Optional[Path] = None,
        repos_dir: Optional[Path] = None,
        force: bool = False,
        ttl: Optional[float] = None,
    ) -> None:
        """
        Synchronize open issues state across target repositories using `gh issue list`.
        Caches results for `ttl` seconds unless force=True.
        """
        effective_ttl = self._cache_ttl if ttl is None else ttl
        now = time.time()
        if not force:
            with self._lock:
                if self._last_issues_sync_time > 0 and (now - self._last_issues_sync_time) < effective_ttl:
                    return self.get_open_issues()

        try:
            target_dirs = self._resolve_target_dirs(repo_root=repo_root, repos_dir=repos_dir)
            new_issues: Dict[Tuple[str, int], Dict[str, Any]] = {}
            success_count = 0
            max_workers = min(10, max(1, len(target_dirs)))
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_dir = {
                    executor.submit(self._sync_issues_directory, d): d
                    for d in target_dirs
                }
                for future in as_completed(future_to_dir):
                    d = future_to_dir[future]
                    try:
                        res = future.result()
                        repo_full_name = res[0]
                        issues = res[1]
                        for item in issues:
                            num = item["number"]
                            key = (repo_full_name, num)
                            new_issues[key] = item
                        success_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to sync issues for repository directory '{d}': {e}")

            if success_count > 0 or not target_dirs:
                with self._lock:
                    self._open_issues = new_issues
                    self._last_issues_sync_time = time.time()
                logger.info(f"PRTracker synced {len(new_issues)} open issue(s) via gh CLI.")
        except Exception as e:
            logger.warning(f"PRTracker issues sync skipped/failed: {e}")

        return self.get_open_issues()

    def sync_repository_status(
        self,
        repo_root: Optional[Path] = None,
        repos_dir: Optional[Path] = None,
        force: bool = False,
        ttl: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Synchronize both pull requests (approved and open) and open issues across repositories."""
        self.sync_github_prs(repo_root=repo_root, repos_dir=repos_dir, force=force, ttl=ttl)
        self.sync_github_issues(repo_root=repo_root, repos_dir=repos_dir, force=force, ttl=ttl)
        return self.get_repo_status()

    def start_background_polling(
        self,
        repo_root: Optional[Path] = None,
        repos_dir: Optional[Path] = None,
        poll_interval: float = 30.0,
        on_sync: Optional[Callable[..., None]] = None,
    ) -> None:
        """Start asynchronous background polling thread for live repository status updates."""
        try:
            val = float(poll_interval)
            poll_interval = val if val > 0.0 and math.isfinite(val) else 30.0
        except (ValueError, TypeError):
            poll_interval = 30.0

        with self._lock:
            if self._polling_thread and self._polling_thread.is_alive():
                return
            self._poll_interval = poll_interval
            self._repo_root = repo_root
            self._repos_dir = repos_dir
            self._on_sync = on_sync
            self._stop_polling_event.clear()
            self._polling_thread = threading.Thread(
                target=self._background_polling_loop,
                args=(repo_root, repos_dir, poll_interval),
                name="RepoStatusPollingThread",
                daemon=True,
            )
            self._polling_thread.start()
            logger.info(f"PRTracker repository background polling thread started (interval: {poll_interval}s).")

    def stop_background_polling(self, timeout: float = 2.0) -> None:
        """Stop asynchronous background polling thread gracefully."""
        self._stop_polling_event.set()
        with self._lock:
            thread = self._polling_thread
            self._polling_thread = None
        if thread and thread.is_alive() and thread != threading.current_thread():
            thread.join(timeout=timeout)
        logger.info("PRTracker repository background polling thread stopped.")

    def is_polling(self) -> bool:
        """Return True if background polling thread is active."""
        with self._lock:
            return (
                self._polling_thread is not None
                and self._polling_thread.is_alive()
                and not self._stop_polling_event.is_set()
            )

    def _background_polling_loop(
        self,
        repo_root: Optional[Path] = None,
        repos_dir: Optional[Path] = None,
        poll_interval: float = 30.0,
    ) -> None:
        """Background thread loop calling sync_repository_status periodically."""
        while not self._stop_polling_event.is_set():
            try:
                st = self.sync_repository_status(repo_root=repo_root, repos_dir=repos_dir, force=True)
                if self._on_sync:
                    try:
                        self._on_sync(st)
                    except TypeError:
                        self._on_sync()
                    except Exception as e:
                        logger.debug(f"Error in on_sync callback: {e}")
            except Exception as e:
                logger.warning(f"Error in PRTracker background polling loop: {e}")

            try:
                self._stop_polling_event.wait(timeout=poll_interval)
            except Exception as e:
                logger.warning(f"Error waiting in PRTracker background polling loop: {e}")

    def sync_in_background(
        self, repo_root: Optional[Path] = None, repos_dir: Optional[Path] = None
    ) -> threading.Thread:
        """Start background thread to sync PRs and repository status via gh CLI."""
        t = threading.Thread(
            target=self.sync_repository_status,
            args=(repo_root, repos_dir),
            daemon=True,
            name="PRTrackerSync",
        )
        t.start()
        return t


# Aliases for module consumers
RepoTracker = PRTracker
RepoStatusTracker = PRTracker
