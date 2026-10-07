#!/usr/bin/env python3
"""
Common utilities, constants, formatting, and target URL resolution for Graviton Dashboard.
Zero external dependencies (Python standard library only).
"""

import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Union

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

logger = logging.getLogger("graviton.dashboard")

_DETECTED_REPO: Optional[str] = None
_DETECTED_REPO_CHECKED: bool = False


def _reset_detected_repo_cache() -> None:
    """Reset the cached repository detection state (primarily for unit tests)."""
    global _DETECTED_REPO, _DETECTED_REPO_CHECKED
    _DETECTED_REPO = None
    _DETECTED_REPO_CHECKED = False


def _detect_git_repo_full_name() -> Optional[str]:
    """Detect current repository full name (e.g. 'owner/repo') from git remote or env."""
    global _DETECTED_REPO, _DETECTED_REPO_CHECKED
    if _DETECTED_REPO_CHECKED:
        return _DETECTED_REPO
    repo = os.getenv("GITHUB_REPOSITORY")
    if repo and repo.strip():
        val = repo.strip()
        if re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", val):
            _DETECTED_REPO = val
            _DETECTED_REPO_CHECKED = True
            return _DETECTED_REPO
    try:
        res = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=1,
        )
        if res.returncode == 0 and res.stdout.strip():
            m = re.search(r"github\.com[:/]([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+?)(?:\.git)?/?$", res.stdout.strip())
            if m:
                _DETECTED_REPO = m.group(1)
    except Exception:
        pass
    _DETECTED_REPO_CHECKED = True
    return _DETECTED_REPO


def format_duration(seconds: Optional[float]) -> str:
    """Format elapsed seconds into human readable duration string (e.g., 2m 14s)."""
    if seconds is None or seconds < 0:
        return "0s"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def format_percentage(val: Any, default: str = "N/A") -> str:
    """
    Format a percentage value (float, int, or string) cleanly without trailing .0%.
    Handles numbers, whole floats, decimal floats, and strings with or without trailing '%'.
    Safe across Python 3.10, 3.11, and 3.12.
    """
    if val is None or val == "":
        return default
    if isinstance(val, bool):
        return default
    if isinstance(val, (int, float)):
        flt = float(val)
        return f"{int(flt)}%" if flt.is_integer() else f"{flt:.1f}%"
    val_str = str(val).strip()
    if val_str.lower() in ("n/a", "n/a%", "none", "null", "unknown"):
        return default
    clean_str = val_str[:-1].strip() if val_str.endswith("%") else val_str
    try:
        flt = float(clean_str)
        return f"{int(flt)}%" if flt.is_integer() else f"{flt:.1f}%"
    except (ValueError, TypeError):
        return default


def is_safe_url(url: Optional[str]) -> bool:
    """Validate that a URL uses safe http or https schemes to prevent javascript: XSS."""
    if not url or not isinstance(url, str):
        return False
    clean = url.strip()
    if any(c in clean for c in (" ", "\t", "\r", "\n", '"', "'", "<", ">")):
        return False
    clean_lower = clean.lower()
    return clean_lower.startswith("http://") or clean_lower.startswith("https://")


def get_quota_color(pct: Optional[Union[float, int, str]]) -> str:
    """Return adaptive status color for quota percentage thresholds."""
    if pct is None:
        return "#58a6ff"
    try:
        if isinstance(pct, str):
            pct_clean = pct[:-1].strip() if pct.strip().endswith("%") else pct.strip()
            pct_val = float(pct_clean)
        else:
            pct_val = float(pct)
    except (ValueError, TypeError):
        return "#58a6ff"
    if pct_val > 50:
        return "#3fb950"
    if pct_val >= 20:
        return "#d29922"
    return "#f85149"


def parse_countdown_to_seconds(cd_str: Optional[str]) -> Optional[float]:
    """Parse a countdown string (e.g., '04:51:12', '02h 15m', '5d 04h', '00:00:00') into total seconds."""
    if not cd_str:
        return None
    s = str(cd_str).replace("`", "").strip().lower()
    if s in ("n/a", "none", ""):
        return None
    if s in ("00:00:00", "0", "0s"):
        return 0.0

    # Colon-separated: "02:15:00" or "02:15" (supports descriptive words/suffixes)
    m_hms = re.search(r"(\d+):(\d{1,2}):(\d{1,2})", s)
    if m_hms:
        sec = float(m_hms.group(1)) * 3600.0 + float(m_hms.group(2)) * 60.0 + float(m_hms.group(3))
        m_d = re.search(r"(\d+)\s*d", s)
        if m_d:
            sec += float(m_d.group(1)) * 86400.0
        return sec

    m_ms = re.search(r"(\d+):(\d{1,2})", s)
    if m_ms:
        sec = float(m_ms.group(1)) * 60.0 + float(m_ms.group(2))
        m_d = re.search(r"(\d+)\s*d", s)
        if m_d:
            sec += float(m_d.group(1)) * 86400.0
        return sec

    # Days and hours: e.g. "5d 04h" or "5d"
    if "d" in s:
        m_d = re.search(r"(\d+)\s*d", s)
        m_h = re.search(r"(\d+)\s*h", s)
        m_m = re.search(r"(\d+)\s*m", s)
        m_s = re.search(r"(\d+)\s*s", s)
        if not (m_d or m_h or m_m or m_s):
            return None
        sec = 0.0
        if m_d:
            sec += float(m_d.group(1)) * 86400.0
        if m_h:
            sec += float(m_h.group(1)) * 3600.0
        if m_m:
            sec += float(m_m.group(1)) * 60.0
        if m_s:
            sec += float(m_s.group(1))
        return sec

    # Hours, mins, secs: e.g. "02h 15m" or "02h" or "15m" or "45s"
    if any(u in s for u in ("h", "m", "s")):
        m_h = re.search(r"(\d+)\s*h", s)
        m_m = re.search(r"(\d+)\s*m", s)
        m_s = re.search(r"(\d+)\s*s", s)
        if not (m_h or m_m or m_s):
            return None
        sec = 0.0
        if m_h:
            sec += float(m_h.group(1)) * 3600.0
        if m_m:
            sec += float(m_m.group(1)) * 60.0
        if m_s:
            sec += float(m_s.group(1))
        return sec

    return None


def _extract_countdown(details_str: Optional[str]) -> Optional[str]:
    """Extract countdown string from details string (e.g. 'Reset: 02:30:00 | Pacing: OK')."""
    if not details_str:
        return None
    m = re.search(r"Reset:\s*([^|\n]+)", details_str, re.IGNORECASE)
    if m:
        val = m.group(1).replace("`", "").strip()
        return val if val and val.upper() != "N/A" and val.lower() != "none" else None
    return None


def calculate_target_pacing_from_details(details_str: Optional[str], duration_seconds: float) -> Optional[float]:
    """Extract reset countdown from details string and calculate linear pacing threshold percentage."""
    if not details_str or duration_seconds <= 0:
        return None
    cd_val = _extract_countdown(details_str)
    if not cd_val:
        return None
    rem_sec = parse_countdown_to_seconds(cd_val)
    if rem_sec is None:
        return None
    frac = max(0.0, min(1.0, rem_sec / duration_seconds))
    return round(frac * 100.0, 1)


def _build_pacing_details(cd: str, st: Optional[str], rec_cd: Optional[str] = None) -> str:
    """Build standardized details string showing reset countdown and pacing status with recovery time."""
    if st == "BEHIND_PACING" and rec_cd and rec_cd != "00:00:00":
        return f"Reset: {cd} | Pacing: {st} (Resume in {rec_cd})"
    return f"Reset: {cd} | Pacing: {st or 'OK'}"


def resolve_target_url(
    target: Optional[str],
    repo: Optional[str] = None,
    agent: Optional[str] = None,
) -> Optional[str]:
    """
    Resolve a task target into a clickable GitHub URL (PR or Issue).
    Agent heuristics default pr_drafter and issue_triager to /issues/ (since
    their target is the input issue being implemented or triaged), while
    code_reviewer and code_fixer default to /pull/.
    Returns None if target cannot be resolved to a valid safe URL.
    """
    if not target or not isinstance(target, str):
        return None
    raw = target.strip().strip("`").strip()
    if not raw or raw in ("N/A", "-", "None"):
        return None

    # Markdown link check [text](url)
    m_md = re.match(r"^\[(.*?)\]\((.*?)\)$", raw)
    if m_md:
        extracted = m_md.group(2).strip()
        return extracted if is_safe_url(extracted) else None

    # Direct URL or bare github.com
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw if is_safe_url(raw) else None
    if raw.startswith("github.com/"):
        cand = f"https://{raw}"
        return cand if is_safe_url(cand) else None

    eff_repo = repo or _detect_git_repo_full_name()
    if eff_repo:
        eff_repo = eff_repo.strip()
        if not re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", eff_repo):
            eff_repo = None
    agent_str = str(agent).lower() if agent else ""
    is_agent_issue = "issue" in agent_str or "drafter" in agent_str

    # Path-style with repo: owner/repo/pull/123, owner/repo/pulls/123, owner/repo/issues/123, owner/repo/pr/123
    m = re.match(
        r"^([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/(?:(pr|pulls?|issues?))/(\d+)$",
        raw,
        re.IGNORECASE,
    )
    if m:
        target_repo = m.group(1)
        type_prefix = m.group(2).lower()
        num = m.group(3)
        subpath = "issues" if type_prefix.startswith("issue") else "pull"
        return f"https://github.com/{target_repo}/{subpath}/{num}"

    # Delimiter-style with repo: owner/repo#number, owner/repo PR #123, owner/repo:123
    m = re.match(
        r"^([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)[\s#:]+(?:(pr|pulls?|issues?)[\s\-/:]*)?#?(\d+)$",
        raw,
        re.IGNORECASE,
    )
    if m:
        target_repo = m.group(1)
        type_prefix = m.group(2).lower() if m.group(2) else ""
        num = m.group(3)
        if type_prefix:
            subpath = "issues" if type_prefix.startswith("issue") else "pull"
        else:
            subpath = "issues" if is_agent_issue else "pull"
        return f"https://github.com/{target_repo}/{subpath}/{num}"

    # #number, PR #123, Issue #123, pull/123, pulls/123, issues/123, #pr-123, pr-123, or bare number with eff_repo
    m = re.match(
        r"^#?[\s:]*(?:(pr|pulls?|issues?)[\s\-/:]*)?#?(\d+)$",
        raw,
        re.IGNORECASE,
    )
    if m:
        if not eff_repo:
            return None
        type_prefix = m.group(1).lower() if m.group(1) else ""
        num = m.group(2)
        if type_prefix:
            subpath = "issues" if type_prefix.startswith("issue") else "pull"
        else:
            subpath = "issues" if is_agent_issue else "pull"
        return f"https://github.com/{eff_repo}/{subpath}/{num}"

    # owner/repo without number
    m = re.match(r"^([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)$", raw)
    if m:
        return f"https://github.com/{m.group(1)}"

    return None
