"""
PR Tracker Module for Graviton.

Tracks GitHub Pull Requests that are approved and ready for human merge.
"""

import json
import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lib.security import contains_bot_marker

logger = logging.getLogger("graviton.pr_tracker")


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
    if not text:
        return False
    text_lower = text.lower()

    # Reject questions (trailing '?' or question words/phrasing like 'Is this PR approved?', 'Was this approved?', 'Has this been approved?')
    if "?" in text_lower:
        return False

    question_pattern = r'\b(how|when|why|what|if|whether)\b(?:\s+\w+){0,4}\s+(approved|lgtm)\b'
    inverted_question_pattern = r'(?:^|[\.\!\?\n;])\s*\b(is|was)\b(?:\s+\w+){0,4}\s+(approved|lgtm)\b'
    if re.search(question_pattern, text_lower) or re.search(inverted_question_pattern, text_lower):
        return False

    # Reject discussion / noun phrases (e.g. "approved pull requests panel", "in approved prs")
    noun_phrase_pattern = r'\bapproved\s+(pull\s+requests?|prs?|panel|box|list|section|screen|widget|table|feature)\b'
    preposition_pattern = r'\b(in|of|from|on|about|view|show|display|fix|fixed|update|updated)\s+approved\b'
    if re.search(noun_phrase_pattern, text_lower) or re.search(preposition_pattern, text_lower):
        return False

    multi_word_neg = r'\b(not|no|cannot|can\'t|won\'t|don\'t|doesn\'t|never|needs?|requires?|awaiting|pending)\b(?:\s+\w+){0,3}\s+(approved|lgtm)\b'
    prefix_neg = r'\b(un|non|dis)[-\s]?(approved|lgtm)\b'
    if re.search(multi_word_neg, text_lower) or re.search(prefix_neg, text_lower):
        return False

    negative_phrases = (
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
    if any(neg in text_lower for neg in negative_phrases):
        return False

    if "lgtm" in text_lower or "approved" in text_lower:
        return True
    return False


def has_change_request_marker(text: str) -> bool:
    """Check if text contains change request markers or commands."""
    if not text:
        return False
    text_lower = text.lower()
    if "/fix" in text_lower:
        return True

    cr_pattern = r'\bchanges[_\s]requested\b'
    matches = list(re.finditer(cr_pattern, text_lower))
    if not matches:
        return False

    neg_cr_pattern = r'\b(no|not|without|zero|never|don\'t|didn\'t|haven\'t)\b(?:\s+\w+){0,3}\s+changes[_\s]requested\b'
    neg_matches = list(re.finditer(neg_cr_pattern, text_lower))

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
    if re.search(r'(?:^|[-_])bot(?:[-_]|$)', login):
        return True
    explicit_bots = (
        "antigravity",
        "code_reviewer",
        "code_fixer",
        "issue_triager",
        "pr_drafter",
        "codebase_auditor",
    )
    if any(b in login for b in explicit_bots):
        return True
    return False


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
        )
        data = json.loads(res.stdout)
        results = []
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or item.get("isDraft") is True:
                    continue

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
                    author = ev["author"]
                    if state == "CHANGES_REQUESTED" or has_change_request_marker(body):
                        has_cr = True
                        has_human_approval = False
                        has_bot_approval = False
                    elif state == "DISMISSED":
                        has_human_approval = False
                        has_bot_approval = False
                    elif state == "APPROVED" or (has_approval_marker(body) and not has_change_request_marker(body)):
                        has_cr = False
                        if is_bot_event(body, author):
                            has_bot_approval = True
                        else:
                            has_human_approval = True

                review_decision = str(item.get("reviewDecision") or "").upper()

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

                if is_approved:
                    try:
                        num = int(item.get("number"))
                    except (ValueError, TypeError):
                        continue
                    title = item.get("title") or ""
                    url = item.get("url") or ""
                    author_raw = item.get("author")
                    author = (author_raw.get("login") or "") if isinstance(author_raw, dict) else str(author_raw or "")
                    results.append({
                        "number": num,
                        "title": title,
                        "author": author,
                        "url": url,
                    })
        return results

    def _sync_directory(self, d: Path) -> Tuple[str, List[Dict[str, Any]]]:
        repo_full_name = ""
        git_res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(d),
            capture_output=True,
            text=True,
        )
        if git_res.returncode == 0 and git_res.stdout:
            origin_url = git_res.stdout.strip()
            clean_url = origin_url.strip().rstrip("/").removesuffix(".git").rstrip("/")
            match = re.search(r'[:/]([^/]+/[^/]+)$', clean_url)
            if match:
                repo_full_name = match.group(1)

        prs = self._sync_single_repo(cwd=str(d))
        return repo_full_name, prs

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
                        repo_full_name, prs = future.result()
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
                logger.info(f"PRTracker synced {len(new_approved)} approved PR(s) via gh CLI.")
            else:
                logger.warning("PRTracker sync failed for all target directories; preserving existing approved PRs.")
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
