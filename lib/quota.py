"""
Antigravity Model Quota Tracker & Rate Limit Manager for Graviton.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

logger = logging.getLogger("graviton.quota")


class QuotaState:
    NORMAL = "NORMAL"
    LOW_QUOTA = "LOW_QUOTA"
    EXHAUSTED = "EXHAUSTED"


def parse_reset_time_to_datetime(reset_time: Optional[Union[str, float, int]]) -> Optional[datetime]:
    """Parse numeric timestamp or ISO 8601 string to timezone-aware UTC datetime."""
    if reset_time is None:
        return None
    if isinstance(reset_time, datetime):
        return reset_time if reset_time.tzinfo else reset_time.replace(tzinfo=timezone.utc)

    # 1. Try numeric conversion (int, float, or stringified float/int e.g., "1786266000.0")
    try:
        ts = float(reset_time)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError):
        pass

    # 2. Try ISO string parsing (handling trailing 'Z' for Python <= 3.10)
    try:
        s = str(reset_time).strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def parse_reset_time_to_timestamp(reset_time: Optional[Union[str, float, int]]) -> Optional[float]:
    """Parse reset time to epoch float timestamp."""
    dt = parse_reset_time_to_datetime(reset_time)
    return dt.timestamp() if dt is not None else None


def _normalize_now_datetime(now: Optional[Union[float, int, datetime]]) -> Optional[datetime]:
    """Convert timestamp float/int or datetime object into a timezone-aware UTC datetime."""
    if now is None:
        return None
    if isinstance(now, bool):
        return None
    if isinstance(now, datetime):
        return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if isinstance(now, (int, float)):
        return datetime.fromtimestamp(now, tz=timezone.utc)
    return None


def _normalize_pool_key(pool: Optional[str]) -> str:
    """Normalize quota pool string to canonical pool key ('gemini' or 'claude_gpt')."""
    p = str(pool or "gemini").lower()
    if "claude" in p or "gpt" in p or "3p" in p or "third" in p:
        return "claude_gpt"
    return "gemini"




class QuotaWindow:
    def __init__(
        self,
        name: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        remaining_percentage: float = 100.0,
        reset_time: Optional[Union[str, float, int]] = None,
        reset_timestamp: Optional[float] = None,
        window_name: Optional[str] = None,
        total_duration_seconds: Optional[float] = None,
    ):
        raw_name = name or window_name or "5H"
        self.name = raw_name.upper()
        self.window_name = raw_name.lower()

        dur = duration_seconds if duration_seconds is not None else total_duration_seconds
        self.duration_seconds = float(dur if dur is not None else 18000.0)
        self.total_duration_seconds = self.duration_seconds

        self.remaining_percentage = float(remaining_percentage)

        res = reset_time if reset_time is not None else reset_timestamp
        self.reset_time = str(res) if res is not None else None
        self.reset_timestamp = parse_reset_time_to_timestamp(res)

    def get_remaining_seconds(
        self, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> float:
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        if res is None:
            return 0.0
        effective_now = now_dt if now_dt is not None else now
        now_dt_norm = _normalize_now_datetime(effective_now)
        if now_dt_norm is None:
            now_dt_norm = datetime.now(timezone.utc)
        dt = parse_reset_time_to_datetime(res)
        if dt is None:
            return 0.0
        return max(0.0, (dt - now_dt_norm).total_seconds())

    def remaining_time_seconds(self, now: Optional[Union[float, datetime]] = None) -> float:
        now_dt = _normalize_now_datetime(now)
        return self.get_remaining_seconds(now_dt)

    @property
    def quota_fraction(self) -> float:
        return max(0.0, min(1.0, float(self.remaining_percentage) / 100.0))

    def get_time_fraction(
        self, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> float:
        effective_now = now_dt if now_dt is not None else now
        rem_sec = self.get_remaining_seconds(effective_now)
        if self.duration_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, rem_sec / self.duration_seconds))

    def time_fraction(self, now: Optional[Union[float, datetime]] = None) -> float:
        now_dt = _normalize_now_datetime(now)
        return self.get_time_fraction(now_dt)

    def get_pacing_status(
        self, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> Tuple[str, float]:
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        if res is None:
            return "OK", 0.0
        effective_now = now_dt if now_dt is not None else now
        q_frac = self.quota_fraction
        t_frac = self.get_time_fraction(effective_now)

        if q_frac < t_frac:
            deficit = t_frac - q_frac
            backoff = round(max(0.0, deficit * 10.0), 1)
            return "BEHIND_PACING", backoff
        else:
            return "OK", 0.0

    def pacing_status(self, now: Optional[Union[float, datetime]] = None) -> str:
        now_dt = _normalize_now_datetime(now)
        status, _ = self.get_pacing_status(now_dt)
        return status

    def get_pacing_recovery_seconds(
        self, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> float:
        effective_now = now_dt if now_dt is not None else now
        norm_dt = _normalize_now_datetime(effective_now)
        pacing_status, _ = self.get_pacing_status(norm_dt)
        if pacing_status != "BEHIND_PACING":
            return 0.0
        rem_sec = self.get_remaining_seconds(norm_dt)
        q_frac = self.quota_fraction
        recovery = rem_sec - (q_frac * self.duration_seconds)
        return max(0.0, float(recovery))

    def pacing_recovery_seconds(
        self, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> float:
        return self.get_pacing_recovery_seconds(now_dt=now_dt, now=now)

    def format_pacing_countdown(
        self, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> str:
        effective_now = now_dt if now_dt is not None else now
        norm_dt = _normalize_now_datetime(effective_now)
        rec_sec = self.get_pacing_recovery_seconds(norm_dt)
        if rec_sec <= 0:
            return "00:00:00"
        if rec_sec >= 86400:
            days = int(rec_sec // 86400)
            hours = int((rec_sec % 86400) // 3600)
            return f"{days}d {hours:02d}h"
        else:
            hours = int(rec_sec // 3600)
            mins = int((rec_sec % 3600) // 60)
            secs = int(rec_sec % 60)
            return f"{hours:02d}:{mins:02d}:{secs:02d}"

    def format_reset_countdown(self, now: Optional[Union[float, datetime]] = None) -> str:
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        now_dt = _normalize_now_datetime(now)
        return format_reset_countdown(res, now_dt=now_dt, window_name=self.name)

    def to_dict(self) -> dict:
        pacing_status, backoff = self.get_pacing_status()
        return {
            "name": self.name,
            "duration_seconds": self.duration_seconds,
            "remaining_percentage": round(self.remaining_percentage, 1),
            "reset_time": self.reset_time,
            "reset_timestamp": self.reset_timestamp,
            "pacing_status": pacing_status,
            "backoff_delay": backoff,
            "pacing_recovery_seconds": round(self.get_pacing_recovery_seconds(), 1),
            "pacing_recovery_countdown": self.format_pacing_countdown(),
        }


def format_reset_countdown(
    reset_time: Optional[Union[str, float, int]] = None,
    now_dt: Optional[datetime] = None,
    window_name: Optional[str] = None,
) -> str:
    """Format reset timestamp into HH:MM:SS or Xd Yh countdown string."""
    if reset_time is None:
        return "N/A"
    dt = parse_reset_time_to_datetime(reset_time)
    if dt is None:
        return str(reset_time)
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    diff = (dt - now_dt).total_seconds()
    if diff <= 0:
        return "00:00:00"

    if diff >= 86400 or (window_name and window_name.lower() == "1w"):
        days = int(diff // 86400)
        hours = int((diff % 86400) // 3600)
        return f"{days}d {hours:02d}h"
    else:
        hours = int(diff // 3600)
        mins = int((diff % 3600) // 60)
        secs = int(diff % 60)
        return f"{hours:02d}:{mins:02d}:{secs:02d}"


def format_quota_badge(
    window: QuotaWindow,
    now_dt: Optional[datetime] = None,
    quota_pool: Optional[str] = None,
) -> str:
    """Render quota badge string for TUI header/panel."""
    pct = window.remaining_percentage
    pct_str = f"{int(pct)}%" if pct.is_integer() else f"{pct:.1f}%"
    countdown = window.format_reset_countdown(now_dt)
    pacing_status, backoff = window.get_pacing_status(now_dt)

    if pacing_status == "BEHIND_PACING":
        recovery_cd = window.format_pacing_countdown(now_dt)
        pacing_str = f"PACING: BEHIND (NEW TASKS SUSPENDED - RESUME IN {recovery_cd})"
    else:
        pacing_str = "PACING: OK"

    pool_prefix = f"{quota_pool.upper()} " if quota_pool else ""
    return f"[ {pool_prefix}{window.name.upper()} QUOTA: {pct_str} | RESET: {countdown} | {pacing_str} ]"


def _extract_token_from_object(obj) -> Optional[str]:
    """Helper to extract access token string from JSON dict or primitive."""
    if isinstance(obj, str):
        s = obj.strip()
        if s and not (s.startswith("{") and s.endswith("}")):
            return s
        return None

    if isinstance(obj, dict):
        for key in ("access_token", "oauth_token", "auth_token", "token"):
            if key in obj and obj[key]:
                extracted = _extract_token_from_object(obj[key])
                if extracted:
                    return extracted

        for val in obj.values():
            if isinstance(val, dict):
                extracted = _extract_token_from_object(val)
                if extracted:
                    return extracted

    return None


def load_oauth_token(token_file: Optional[Path] = None) -> Optional[str]:
    """Load OAuth access token from stored token file or environment."""
    candidate_files = []
    if token_file is not None:
        candidate_files.append(Path(token_file))

    default_token_file = Path.home() / ".gemini" / "antigravity-cli" / "token.json"
    alt_token_file = Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"

    if default_token_file not in candidate_files:
        candidate_files.append(default_token_file)
    if alt_token_file not in candidate_files:
        candidate_files.append(alt_token_file)

    for path in candidate_files:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if not content:
                    continue

                try:
                    data = json.loads(content)
                    token = _extract_token_from_object(data)
                    if token:
                        return token
                except json.JSONDecodeError:
                    pass

                if content and not content.startswith("{"):
                    return content
            except Exception as e:
                logger.warning(f"Failed to read token file {path}: {e}")

    return os.getenv("ANTIGRAVITY_TOKEN")


def _group_matches_pool(group: dict, pool: str) -> bool:
    """Check if group matches requested pool (e.g. 'gemini' or 'claude_gpt')."""
    if not isinstance(group, dict):
        return False
    p = str(pool).lower()
    disp = " ".join([
        str(group.get("displayName") or ""),
        str(group.get("description") or ""),
        str(group.get("name") or ""),
        str(group.get("id") or ""),
        str(group.get("groupId") or ""),
    ]).lower()

    if "gemini" in p:
        return "gemini" in disp
    elif "claude" in p or "gpt" in p or "3p" in p:
        return "claude" in disp or "gpt" in disp or "3p" in disp
    else:
        return p in disp


def parse_antigravity_quota_json(
    data: Union[dict, list], pool: str = "gemini", quota_pool: Optional[str] = None
) -> Optional[Tuple[QuotaWindow, QuotaWindow]]:
    """
    Parse v1internal:retrieveUserQuotaSummary RPC response JSON body for quota windows.
    Supports grouped 5-hour and weekly quota buckets.
    Returns (QuotaWindow_5h, QuotaWindow_1w) or None if payload invalid or no matching quota buckets found.
    """
    if not isinstance(data, dict) or "error" in data:
        logger.warning(f"Invalid or error response in Antigravity RPC payload: {data}")
        return None

    effective_pool = quota_pool if quota_pool is not None else pool
    if not effective_pool:
        effective_pool = os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini")

    groups = None
    if "groups" in data and isinstance(data["groups"], list):
        groups = data["groups"]
    else:
        for wrap in ("result", "data", "response", "payload"):
            if isinstance(data.get(wrap), dict) and isinstance(data[wrap].get("groups"), list):
                groups = data[wrap]["groups"]
                break

    target_group = None
    if groups:
        for g in groups:
            if _group_matches_pool(g, effective_pool):
                target_group = g
                break
        if target_group is None and len(groups) == 1:
            target_group = groups[0]

    buckets = []
    if target_group and isinstance(target_group.get("buckets"), list):
        buckets = target_group["buckets"]
    elif "buckets" in data and isinstance(data["buckets"], list):
        buckets = data["buckets"]
    else:
        for wrap in ("result", "data", "response", "payload"):
            if isinstance(data.get(wrap), dict) and isinstance(data[wrap].get("buckets"), list):
                buckets = data[wrap]["buckets"]
                break

    if not buckets:
        return None

    pct_5h: Optional[float] = None
    reset_5h: Optional[str] = None
    pct_1w: Optional[float] = None
    reset_1w: Optional[str] = None

    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue

        win_ident = " ".join([
            str(bucket.get("window") or ""),
            str(bucket.get("bucketId") or ""),
            str(bucket.get("displayName") or ""),
        ]).lower()

        rem_frac = bucket.get("remainingFraction")
        if rem_frac is None:
            rem_frac = bucket.get("remaining_fraction")

        if rem_frac is None:
            continue

        try:
            val = float(rem_frac)
            pct = val * 100.0 if val <= 1.0 else val
            pct = max(0.0, min(100.0, pct))
        except (ValueError, TypeError):
            continue

        rst = bucket.get("resetTime") or bucket.get("reset_time")
        rst_str = str(rst) if rst is not None else None

        if "5h" in win_ident or "five" in win_ident or "short" in win_ident or "hourly" in win_ident:
            pct_5h = pct
            reset_5h = rst_str
        elif "weekly" in win_ident or "1w" in win_ident or "long" in win_ident:
            pct_1w = pct
            reset_1w = rst_str

    if pct_5h is None and pct_1w is not None:
        pct_5h, reset_5h = 100.0, None
    elif pct_1w is None and pct_5h is not None:
        pct_1w, reset_1w = 100.0, None

    if pct_5h is None and pct_1w is None:
        return None

    w_5h = QuotaWindow(
        name="5H",
        duration_seconds=18000.0,
        remaining_percentage=pct_5h if pct_5h is not None else 100.0,
        reset_time=reset_5h,
    )

    w_1w = QuotaWindow(
        name="1W",
        duration_seconds=604800.0,
        remaining_percentage=pct_1w if pct_1w is not None else 100.0,
        reset_time=reset_1w,
    )

    return w_5h, w_1w


def fetch_live_antigravity_quota(
    token: Optional[str] = None,
    api_url: str = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    timeout: float = 10.0,
    quota_pool: Optional[str] = None,
) -> Optional[Tuple[QuotaWindow, QuotaWindow]]:
    """
    Query v1internal:retrieveUserQuotaSummary to fetch live model quota metrics and reset timestamps.
    Returns (QuotaWindow_5h, QuotaWindow_1w) or None if fetch fails.
    """
    if not token:
        token = load_oauth_token()

    if not token:
        logger.warning("No OAuth token available for fetching live Antigravity quota.")
        return None

    pool = quota_pool if quota_pool is not None else os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini")

    try:
        payload = json.dumps({}).encode("utf-8")
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity-cli",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if hasattr(resp, "status") and resp.status != 200:
                logger.warning(f"Antigravity RPC endpoint returned HTTP status {resp.status}")
                return None
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return parse_antigravity_quota_json(data, pool=pool)
    except Exception as e:
        logger.warning(f"Failed to fetch live Antigravity quota: {e}")
        return None


@dataclass
class QuotaInfo:
    remaining_percentage: float = 100.0
    state: str = QuotaState.NORMAL
    reset_time: Optional[float] = None
    active_backoff_delay: float = 0.0
    requests_remaining: Optional[int] = None
    tokens_remaining: Optional[int] = None
    window_5h: Optional[Union[QuotaWindow, dict]] = None
    window_1w: Optional[Union[QuotaWindow, dict]] = None
    quota_pool: str = "gemini"

    def to_dict(self) -> dict:
        d = {
            "quota_pool": self.quota_pool,
            "remaining_percentage": round(self.remaining_percentage, 1),
            "state": self.state,
            "reset_time": self.reset_time,
            "active_backoff_delay": round(self.active_backoff_delay, 2),
            "requests_remaining": self.requests_remaining,
            "tokens_remaining": self.tokens_remaining,
        }
        if self.window_5h is not None:
            if isinstance(self.window_5h, QuotaWindow):
                d["window_5h"] = self.window_5h.to_dict()
            else:
                d["window_5h"] = self.window_5h
        if self.window_1w is not None:
            if isinstance(self.window_1w, QuotaWindow):
                d["window_1w"] = self.window_1w.to_dict()
            else:
                d["window_1w"] = self.window_1w
        return d


def parse_quota_headers(headers: dict) -> dict:
    """
    Parse HTTP response headers or dictionary for quota & rate limit details.
    Returns a dictionary with parsed fields (remaining_percentage, reset_time,
    remaining_percentage_5h, reset_time_5h, remaining_percentage_1w, reset_time_1w,
    requests_remaining, tokens_remaining).
    """
    lower_headers = {str(k).lower(): v for k, v in headers.items()}
    res = {}

    # 1. 5h Window Remaining Percentage
    for key in (
        "x-quota-remaining-5h",
        "x-quota-remaining-percent-5h",
        "quota_percent_5h",
        "remaining_percentage_5h",
        "quota_5h",
    ):
        if key in lower_headers:
            try:
                res["remaining_percentage_5h"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 2. 5h Window Reset Time
    for key in ("x-quota-reset-5h", "x-ratelimit-reset-5h", "reset_time_5h", "reset_5h"):
        if key in lower_headers:
            try:
                res["reset_time_5h"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                res["reset_time_5h"] = lower_headers[key]
                break

    # 3. 1w Window Remaining Percentage
    for key in (
        "x-quota-remaining-1w",
        "x-quota-remaining-percent-1w",
        "quota_percent_1w",
        "remaining_percentage_1w",
        "quota_1w",
    ):
        if key in lower_headers:
            try:
                res["remaining_percentage_1w"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 4. 1w Window Reset Time
    for key in ("x-quota-reset-1w", "x-ratelimit-reset-1w", "reset_time_1w", "reset_1w"):
        if key in lower_headers:
            try:
                res["reset_time_1w"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                res["reset_time_1w"] = lower_headers[key]
                break

    # 5. General Remaining percentage
    for key in (
        "x-quota-remaining-percent",
        "x-quota-remaining",
        "quota_percent",
        "remaining_percentage",
        "quota",
    ):
        if key in lower_headers:
            try:
                res["remaining_percentage"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 6. General Reset time
    for key in ("x-ratelimit-reset", "reset_time", "x-quota-reset", "reset"):
        if key in lower_headers:
            try:
                res["reset_time"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                res["reset_time"] = lower_headers[key]
                break

    # 7. Requests remaining
    for key in ("x-ratelimit-remaining", "requests_remaining"):
        if key in lower_headers:
            try:
                res["requests_remaining"] = int(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 8. Tokens remaining
    for key in ("x-ratelimit-tokens-remaining", "tokens_remaining"):
        if key in lower_headers:
            try:
                res["tokens_remaining"] = int(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    return res


class _PacingStatusResult(str):
    def __new__(cls, tracker, now: Optional[Union[float, datetime]] = None):
        val = "BEHIND_PACING" if tracker.is_behind_pacing(now=now) else "OK"
        obj = super().__new__(cls, val)
        obj._tracker = tracker
        return obj

    def __call__(self, now: Optional[Union[float, datetime]] = None) -> str:
        return "BEHIND_PACING" if self._tracker.is_behind_pacing(now=now) else "OK"


class QuotaTracker:
    """
    Thread-safe tracker for Antigravity API model quota levels and rate limits.
    Calculates exponential back-off delays during low quota conditions
    and pacing back-off for dual 5h and 1w windows.
    """

    LOW_QUOTA_THRESHOLD = 15.0  # Percentage < 15% triggers LOW_QUOTA
    EXHAUSTED_THRESHOLD = 0.0   # Percentage == 0% triggers EXHAUSTED

    def __init__(
        self,
        remaining_percentage: float = 100.0,
        reset_time: Optional[float] = None,
        base_backoff_delay: float = 1.0,
        max_backoff_delay: float = 60.0,
        backoff_factor: float = 2.0,
        window_5h: Optional[QuotaWindow] = None,
        window_1w: Optional[QuotaWindow] = None,
        quota_pool: Optional[str] = None,
        active_gemini_model: str = "gemini-2.5-flash",
        active_third_party_model: str = "claude-3-5-sonnet",
        available_gemini_models: Optional[List[str]] = None,
        available_third_party_models: Optional[List[str]] = None,
    ):
        self._lock = threading.RLock()
        self.quota_pool = quota_pool if quota_pool is not None else os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini")
        self._remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))
        self._reset_time = reset_time
        self._requests_remaining: Optional[int] = None
        self._tokens_remaining: Optional[int] = None

        self.active_gemini_model = active_gemini_model
        self.active_third_party_model = active_third_party_model
        self.available_gemini_models = available_gemini_models or [
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-1.5-pro",
        ]
        self.available_third_party_models = available_third_party_models or [
            "claude-3-5-sonnet",
            "claude-3-opus",
            "claude-3-5-haiku",
        ]

        def default_5h():
            return QuotaWindow(
                name="5H",
                duration_seconds=18000.0,
                remaining_percentage=self._remaining_percentage,
                reset_time=str(reset_time) if reset_time is not None else None,
            )

        def default_1w():
            return QuotaWindow(
                name="1W", duration_seconds=604800.0, remaining_percentage=100.0
            )

        if quota_pool is None:
            self.gemini_window_5h = window_5h or default_5h()
            self.gemini_window_1w = window_1w or default_1w()
            self.claude_window_5h = window_5h or default_5h()
            self.claude_window_1w = window_1w or default_1w()
        else:
            p = str(quota_pool).lower()
            if "claude" in p or "gpt" in p or "3p" in p or "third" in p:
                self.claude_window_5h = window_5h or default_5h()
                self.claude_window_1w = window_1w or default_1w()
                self.gemini_window_5h = default_5h()
                self.gemini_window_1w = default_1w()
            else:
                self.gemini_window_5h = window_5h or default_5h()
                self.gemini_window_1w = window_1w or default_1w()
                self.claude_window_5h = default_5h()
                self.claude_window_1w = default_1w()

        self.base_backoff_delay = base_backoff_delay
        self.max_backoff_delay = max_backoff_delay
        self.backoff_factor = backoff_factor

        self._backoff_count = 0
        self._active_backoff_delay = 0.0

        self.interval_5h = 60.0
        self.interval_1w = 60.0
        self._last_fetch_5h: Dict[str, float] = {}
        self._last_fetch_1w: Dict[str, float] = {}
        self._in_flight: bool = False
        self._stop_polling_event = threading.Event()
        self._polling_thread: Optional[threading.Thread] = None

    @property
    def window_5h(self) -> QuotaWindow:
        with self._lock:
            return self.gemini_window_5h

    @window_5h.setter
    def window_5h(self, val: QuotaWindow):
        with self._lock:
            self.gemini_window_5h = val

    @property
    def window_1w(self) -> QuotaWindow:
        with self._lock:
            return self.gemini_window_1w

    @window_1w.setter
    def window_1w(self, val: QuotaWindow):
        with self._lock:
            self.gemini_window_1w = val

    @property
    def remaining_percentage(self) -> float:
        with self._lock:
            return self._remaining_percentage

    @remaining_percentage.setter
    def remaining_percentage(self, val: float):
        with self._lock:
            val_float = max(0.0, min(100.0, float(val)))
            self._remaining_percentage = val_float
            w5, _ = self.get_pool_windows(self.quota_pool)
            w5.remaining_percentage = val_float
            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = self.get_pacing_backoff_delay()

    def get_pool_windows(self, pool: str) -> Tuple[QuotaWindow, QuotaWindow]:
        with self._lock:
            p = str(pool).lower()
            if "claude" in p or "gpt" in p or "3p" in p or "third" in p:
                return self.claude_window_5h, self.claude_window_1w
            else:
                return self.gemini_window_5h, self.gemini_window_1w

    def get_pool_remaining_percentage(self, pool: str) -> float:
        with self._lock:
            w5, w1 = self.get_pool_windows(pool)
            return min(w5.remaining_percentage, w1.remaining_percentage)

    def is_pool_behind_pacing(
        self, pool: str, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> bool:
        with self._lock:
            w5, w1 = self.get_pool_windows(pool)
            effective_now = now_dt if now_dt is not None else now
            norm_dt = _normalize_now_datetime(effective_now)
            if norm_dt is None:
                norm_dt = datetime.now(timezone.utc)
            s5, _ = w5.get_pacing_status(norm_dt)
            s1, _ = w1.get_pacing_status(norm_dt)
            return s5 == "BEHIND_PACING" or s1 == "BEHIND_PACING"

    def get_pool_state(self, pool: str) -> str:
        with self._lock:
            pct = self.get_pool_remaining_percentage(pool)
            if pct <= self.EXHAUSTED_THRESHOLD:
                return QuotaState.EXHAUSTED
            elif pct < self.LOW_QUOTA_THRESHOLD:
                return QuotaState.LOW_QUOTA
            else:
                return QuotaState.NORMAL

    def get_active_model(self, pool: str) -> str:
        with self._lock:
            p = str(pool).lower()
            if "claude" in p or "gpt" in p or "3p" in p or "third" in p:
                return self.active_third_party_model
            else:
                return self.active_gemini_model

    def set_active_model(self, pool: str, model: str):
        with self._lock:
            p = str(pool).lower()
            if "claude" in p or "gpt" in p or "3p" in p or "third" in p:
                self.active_third_party_model = model
            else:
                self.active_gemini_model = model

    def _state_unlocked(self) -> str:
        gemini_state = self.get_pool_state("gemini")
        claude_state = self.get_pool_state("claude_gpt")
        if gemini_state == QuotaState.EXHAUSTED and claude_state == QuotaState.EXHAUSTED:
            return QuotaState.EXHAUSTED
        elif gemini_state == QuotaState.NORMAL or claude_state == QuotaState.NORMAL:
            return QuotaState.NORMAL
        elif gemini_state == QuotaState.LOW_QUOTA or claude_state == QuotaState.LOW_QUOTA:
            return QuotaState.LOW_QUOTA
        else:
            return QuotaState.EXHAUSTED

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_unlocked()

    @property
    def reset_time(self) -> Optional[float]:
        with self._lock:
            return self._reset_time

    @reset_time.setter
    def reset_time(self, val: Optional[float]):
        with self._lock:
            self._reset_time = val
            if val is not None:
                self.window_5h.reset_time = str(val)
                self.window_5h.reset_timestamp = parse_reset_time_to_timestamp(val)

    @property
    def active_backoff_delay(self) -> float:
        with self._lock:
            return self._active_backoff_delay

    def is_behind_pacing(
        self, now_dt: Optional[Union[float, datetime]] = None, now: Optional[Union[float, datetime]] = None
    ) -> bool:
        """Check if all quota pools are behind target pacing (returns True only when all pools are behind pacing, blocking task execution across both pools)."""
        with self._lock:
            effective_now = now_dt if now_dt is not None else now
            norm_dt = _normalize_now_datetime(effective_now)
            if norm_dt is None:
                norm_dt = datetime.now(timezone.utc)
            g_behind = self.is_pool_behind_pacing("gemini", norm_dt)
            c_behind = self.is_pool_behind_pacing("claude_gpt", norm_dt)
            return g_behind and c_behind

    @property
    def pacing_status(self) -> Union[str, _PacingStatusResult]:
        """Return 'BEHIND_PACING' if either quota window is behind target pacing, else 'OK'."""
        return _PacingStatusResult(self)

    def get_pacing_backoff_delay(
        self,
        window: Optional[QuotaWindow] = None,
        now_dt: Optional[Union[float, datetime]] = None,
        now: Optional[Union[float, datetime]] = None,
    ) -> float:
        """
        Calculate proportional pacing backoff delay for a specific window or max across all windows.
        pacing_deficit = max(0.0, time_fraction - quota_fraction).
        """
        with self._lock:
            effective_now = now_dt if now_dt is not None else now
            norm_dt = _normalize_now_datetime(effective_now)
            if window is not None:
                p_status, backoff = window.get_pacing_status(norm_dt)
                if p_status == "OK":
                    return 0.0
                return min(self.max_backoff_delay, backoff)

            all_windows = [
                self.gemini_window_5h,
                self.gemini_window_1w,
                self.claude_window_5h,
                self.claude_window_1w,
            ]
            return max(w.get_pacing_status(norm_dt)[1] for w in all_windows)

    def get_pacing_recovery_seconds(
        self,
        window: Optional[QuotaWindow] = None,
        now_dt: Optional[Union[float, datetime]] = None,
        now: Optional[Union[float, datetime]] = None,
    ) -> float:
        """Calculate pacing recovery time in seconds for a specific window or max across all dual pool windows."""
        with self._lock:
            effective_now = now_dt if now_dt is not None else now
            norm_dt = _normalize_now_datetime(effective_now)
            if window is not None:
                return window.get_pacing_recovery_seconds(norm_dt)
            all_windows = [
                self.gemini_window_5h,
                self.gemini_window_1w,
                self.claude_window_5h,
                self.claude_window_1w,
            ]
            return max(w.get_pacing_recovery_seconds(norm_dt) for w in all_windows)

    def pacing_recovery_seconds(
        self,
        window: Optional[QuotaWindow] = None,
        now_dt: Optional[Union[float, datetime]] = None,
        now: Optional[Union[float, datetime]] = None,
    ) -> float:
        return self.get_pacing_recovery_seconds(window=window, now_dt=now_dt, now=now)

    def format_pacing_countdown(
        self,
        window: Optional[QuotaWindow] = None,
        now_dt: Optional[Union[float, datetime]] = None,
        now: Optional[Union[float, datetime]] = None,
    ) -> str:
        """Format pacing recovery countdown string for a specific window or max across all dual pool windows."""
        with self._lock:
            effective_now = now_dt if now_dt is not None else now
            norm_dt = _normalize_now_datetime(effective_now)
            if window is not None:
                return window.format_pacing_countdown(norm_dt)
            all_windows = [
                self.gemini_window_5h,
                self.gemini_window_1w,
                self.claude_window_5h,
                self.claude_window_1w,
            ]
            target_window = max(all_windows, key=lambda w: w.get_pacing_recovery_seconds(norm_dt))
            return target_window.format_pacing_countdown(norm_dt)

    def update_quota(
        self,
        remaining_percentage: float,
        reset_time: Optional[float] = None,
        requests_remaining: Optional[int] = None,
        tokens_remaining: Optional[int] = None,
        remaining_percentage_5h: Optional[float] = None,
        reset_time_5h: Optional[Union[float, str]] = None,
        remaining_percentage_1w: Optional[float] = None,
        reset_time_1w: Optional[Union[float, str]] = None,
        quota_pool: Optional[str] = None,
    ):
        """Update quota levels and reset time for dual windows."""
        with self._lock:
            pools_to_update = []
            if quota_pool is None:
                pools_to_update = ["gemini", "claude_gpt"]
            else:
                pools_to_update = [quota_pool]

            for pool in pools_to_update:
                w5, w1 = self.get_pool_windows(pool)

                if remaining_percentage_5h is not None:
                    w5.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage_5h)))
                else:
                    w5.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))

                if reset_time_5h is not None:
                    w5.reset_time = str(reset_time_5h)
                    w5.reset_timestamp = parse_reset_time_to_timestamp(reset_time_5h)
                elif reset_time is not None:
                    w5.reset_time = str(reset_time)
                    w5.reset_timestamp = parse_reset_time_to_timestamp(reset_time)

                if remaining_percentage_1w is not None:
                    w1.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage_1w)))
                else:
                    w1.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))

                if reset_time_1w is not None:
                    w1.reset_time = str(reset_time_1w)
                    w1.reset_timestamp = parse_reset_time_to_timestamp(reset_time_1w)
                elif reset_time is not None:
                    w1.reset_time = str(reset_time)
                    w1.reset_timestamp = parse_reset_time_to_timestamp(reset_time)

            target_pool = quota_pool if quota_pool is not None else self.quota_pool
            target_w5, target_w1 = self.get_pool_windows(target_pool)
            self._remaining_percentage = min(
                target_w5.remaining_percentage,
                target_w1.remaining_percentage,
            )

            if reset_time is not None:
                self._reset_time = reset_time
            else:
                self._reset_time = target_w5.reset_timestamp or target_w1.reset_timestamp

            if requests_remaining is not None:
                self._requests_remaining = requests_remaining
            if tokens_remaining is not None:
                self._tokens_remaining = tokens_remaining

            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = self.get_pacing_backoff_delay()

            current_state = self._state_unlocked()

        logger.info(
            f"Quota updated ({quota_pool or 'all'}): state={current_state} reset={reset_time}"
        )

    def update_windows(self, window_5h: QuotaWindow, window_1w: QuotaWindow, quota_pool: Optional[str] = None):
        """Update 5h and 1w dual quota windows."""
        with self._lock:
            now = time.time()
            if quota_pool is None:
                self._last_fetch_5h["gemini"] = now
                self._last_fetch_5h["claude_gpt"] = now
                self._last_fetch_1w["gemini"] = now
                self._last_fetch_1w["claude_gpt"] = now
                self.gemini_window_5h = window_5h
                self.gemini_window_1w = window_1w
                self.claude_window_5h = window_5h
                self.claude_window_1w = window_1w
            else:
                pk = _normalize_pool_key(quota_pool)
                self._last_fetch_5h[pk] = now
                self._last_fetch_1w[pk] = now
                p = str(quota_pool).lower()
                if "claude" in p or "gpt" in p or "3p" in p or "third" in p:
                    self.claude_window_5h = window_5h
                    self.claude_window_1w = window_1w
                else:
                    self.gemini_window_5h = window_5h
                    self.gemini_window_1w = window_1w

            target_w5, target_w1 = self.get_pool_windows(self.quota_pool)
            effective_pct = min(target_w5.remaining_percentage, target_w1.remaining_percentage)
            self._remaining_percentage = max(0.0, min(100.0, float(effective_pct)))
            if target_w5.reset_time is not None or target_w5.reset_timestamp is not None:
                res = target_w5.reset_timestamp if target_w5.reset_timestamp is not None else target_w5.reset_time
                try:
                    self._reset_time = float(res)
                except (ValueError, TypeError):
                    self._reset_time = res

            status_5h, backoff_5h = window_5h.get_pacing_status()
            status_1w, backoff_1w = window_1w.get_pacing_status()
            target_status_5h, target_backoff_5h = target_w5.get_pacing_status()
            target_status_1w, target_backoff_1w = target_w1.get_pacing_status()
            pacing_backoff = max(target_backoff_5h, target_backoff_1w)

            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD and pacing_backoff == 0.0:
                self._backoff_count = 0
                self._active_backoff_delay = 0.0
            elif pacing_backoff > 0.0 and self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._active_backoff_delay = pacing_backoff

            current_state = self._state_unlocked()

        logger.info(
            f"Dual quota updated ({quota_pool}): 5H={window_5h.remaining_percentage:.1f}% ({status_5h}), "
            f"1W={window_1w.remaining_percentage:.1f}% ({status_1w}), state={current_state}"
        )

    def poll_live_quota(
        self,
        token: Optional[str] = None,
        quota_pool: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[QuotaWindow, QuotaWindow]:
        """
        Fetch live Antigravity quota and update dual windows based on uniform 60s TTL intervals (both 5H and 1W windows) or when force is True.
        """
        now = time.time()
        pool = quota_pool if quota_pool is not None else self.quota_pool
        pk = _normalize_pool_key(pool)
        with self._lock:
            if self._in_flight:
                return self.get_pool_windows(pool)

            last_5h = self._last_fetch_5h.get(pk, 0.0)
            last_1w = self._last_fetch_1w.get(pk, 0.0)

            due_5h = force or (last_5h == 0.0) or ((now - last_5h) >= self.interval_5h)
            due_1w = force or (last_1w == 0.0) or ((now - last_1w) >= self.interval_1w)
            if not (due_5h or due_1w):
                return self.get_pool_windows(pool)

            self._in_flight = True

        try:
            res = fetch_live_antigravity_quota(token=token, quota_pool=pool)
        finally:
            with self._lock:
                self._in_flight = False

        with self._lock:
            now = time.time()
            if res is not None:
                w_5h, w_1w = res
                self.update_windows(w_5h, w_1w, quota_pool=pool)
            else:
                if due_5h:
                    self._last_fetch_5h[pk] = now
                if due_1w:
                    self._last_fetch_1w[pk] = now
                logger.warning("Live Antigravity quota fetch returned None; preserving existing QuotaTracker metrics.")

            return self.get_pool_windows(pool)

    def poll_live_quota_async(
        self,
        token: Optional[str] = None,
        quota_pool: Optional[str] = None,
        force: bool = True,
        thread_name: str = "QuotaTrackerAsyncPollThread",
    ) -> threading.Thread:
        """
        Trigger live quota polling asynchronously in a background daemon thread
        to prevent blocking worker execution threads.
        """
        def _runner():
            try:
                self.poll_live_quota(token=token, quota_pool=quota_pool, force=force)
            except Exception as err:
                logger.warning(f"Async live quota poll failed: {err}")

        t = threading.Thread(
            target=_runner,
            daemon=True,
            name=thread_name,
        )
        t.start()
        return t

    def start_background_polling(
        self, token: Optional[str] = None, quota_pool: Optional[str] = None, poll_interval: float = 1.0
    ):
        """Start asynchronous background polling thread for live quota updates."""
        with self._lock:
            if self._polling_thread and self._polling_thread.is_alive():
                return
            self._stop_polling_event.clear()
            self._polling_thread = threading.Thread(
                target=self._background_polling_loop,
                args=(token, quota_pool, poll_interval),
                daemon=True,
                name="QuotaTrackerPollingThread",
            )
            self._polling_thread.start()
            logger.info("QuotaTracker background polling thread started.")

    def stop_background_polling(self, timeout: float = 2.0):
        """Stop asynchronous background polling thread gracefully."""
        self._stop_polling_event.set()
        with self._lock:
            thread = self._polling_thread
            self._polling_thread = None
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        logger.info("QuotaTracker background polling thread stopped.")

    def is_polling(self) -> bool:
        """Return True if background polling thread is active."""
        with self._lock:
            return (
                self._polling_thread is not None
                and self._polling_thread.is_alive()
                and not self._stop_polling_event.is_set()
            )


    def _background_polling_loop(
        self, token: Optional[str] = None, quota_pool: Optional[str] = None, poll_interval: float = 1.0
    ):
        """Background thread loop calling poll_live_quota() periodically."""
        while not self._stop_polling_event.is_set():
            try:
                self.poll_live_quota(token=token, quota_pool=quota_pool, force=False)
            except Exception as e:
                logger.warning(f"Error in QuotaTracker background polling loop: {e}")
            self._stop_polling_event.wait(timeout=poll_interval)

    def parse_quota_headers(self, headers: dict):
        """
        Parse HTTP response headers or dictionary for quota & rate limit details.
        """
        parsed = parse_quota_headers(headers)
        if parsed:
            new_pct = parsed.get("remaining_percentage", self.remaining_percentage)
            self.update_quota(
                remaining_percentage=new_pct,
                reset_time=parsed.get("reset_time"),
                requests_remaining=parsed.get("requests_remaining"),
                tokens_remaining=parsed.get("tokens_remaining"),
                remaining_percentage_5h=parsed.get("remaining_percentage_5h"),
                reset_time_5h=parsed.get("reset_time_5h"),
                remaining_percentage_1w=parsed.get("remaining_percentage_1w"),
                reset_time_1w=parsed.get("reset_time_1w"),
            )

    def get_backoff_delay(self, attempt: Optional[int] = None) -> float:
        """
        Calculate and return exponential back-off delay during LOW_QUOTA state.
        If attempt is provided (>= 1), calculates delay based on (attempt - 1).
        Increment backoff count if in LOW_QUOTA state.
        Reset backoff count if in NORMAL state.
        Pacing deficit throttling is enforced via task admission control rather than delaying active worker threads.
        """
        with self._lock:
            current_state = self._state_unlocked()
            if current_state == QuotaState.LOW_QUOTA:
                if attempt is not None and attempt > 0:
                    exp = attempt - 1
                else:
                    exp = self._backoff_count

                exp_delay = min(
                    self.max_backoff_delay,
                    self.base_backoff_delay * (self.backoff_factor ** exp),
                )
                self._backoff_count += 1
                self._active_backoff_delay = exp_delay
                return exp_delay
            else:
                self._backoff_count = 0
                self._active_backoff_delay = 0.0
                return 0.0

    def reset_backoff(self):
        """Reset exponential backoff counter."""
        with self._lock:
            self._backoff_count = 0
            self._active_backoff_delay = 0.0

    def get_info(self) -> QuotaInfo:
        with self._lock:
            return QuotaInfo(
                remaining_percentage=self._remaining_percentage,
                state=self._state_unlocked(),
                reset_time=self._reset_time,
                active_backoff_delay=self._active_backoff_delay,
                requests_remaining=self._requests_remaining,
                tokens_remaining=self._tokens_remaining,
                window_5h=self.window_5h,
                window_1w=self.window_1w,
                quota_pool=self.quota_pool,
            )

    def get_reset_time_str(self) -> str:
        """Return formatted reset time or relative seconds string."""
        with self._lock:
            if self._reset_time is None:
                return "N/A"
            if isinstance(self._reset_time, (int, float)):
                now = time.time()
                if self._reset_time > now:
                    diff = int(self._reset_time - now)
                    return f"in {diff}s"
                else:
                    return f"{int(self._reset_time)}s"
            return str(self._reset_time)
