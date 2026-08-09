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
from typing import Dict, Optional, Tuple, Union

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

    def get_remaining_seconds(self, now_dt: Optional[datetime] = None) -> float:
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        if res is None:
            return 0.0
        if now_dt is None:
            now_dt = datetime.now(timezone.utc)
        dt = parse_reset_time_to_datetime(res)
        if dt is None:
            return 0.0
        return max(0.0, (dt - now_dt).total_seconds())

    def remaining_time_seconds(self, now: Optional[float] = None) -> float:
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else None
        return self.get_remaining_seconds(now_dt)

    @property
    def quota_fraction(self) -> float:
        return max(0.0, min(1.0, float(self.remaining_percentage) / 100.0))

    def get_time_fraction(self, now_dt: Optional[datetime] = None) -> float:
        rem_sec = self.get_remaining_seconds(now_dt)
        if self.duration_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, rem_sec / self.duration_seconds))

    def time_fraction(self, now: Optional[float] = None) -> float:
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else None
        return self.get_time_fraction(now_dt)

    def get_pacing_status(self, now_dt: Optional[datetime] = None) -> Tuple[str, float]:
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        if res is None:
            return "OK", 0.0
        q_frac = self.quota_fraction
        t_frac = self.get_time_fraction(now_dt)

        if q_frac < t_frac:
            deficit = t_frac - q_frac
            backoff = round(max(0.0, deficit * 10.0), 1)
            return "BEHIND_PACING", backoff
        else:
            return "OK", 0.0

    def pacing_status(self, now: Optional[float] = None) -> str:
        now_dt = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else None
        status, _ = self.get_pacing_status(now_dt)
        return status

    def format_reset_countdown(self, now: Optional[Union[float, datetime]] = None) -> str:
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        now_dt = None
        if isinstance(now, (int, float)):
            now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
        elif isinstance(now, datetime):
            now_dt = now
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
        pacing_str = f"PACING: BEHIND (Backoff: {backoff:.1f}s)"
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


def _extract_pct_and_reset(info_dict: dict, include_weekly: bool = True) -> Tuple[Optional[float], Optional[str]]:
    """Extract remaining_percentage (0.0 to 100.0) and reset_time string from quota dict."""
    if not isinstance(info_dict, dict):
        return None, None
    pct = None
    pct_keys = [
        "remainingFraction",
        "remaining_fraction",
        "quotaRemaining",
        "remainingQuota",
        "quota_remaining",
        "remaining_percentage",
        "remainingPercentage",
        "quotaRemainingFraction",
        "quota_remaining_fraction",
        "five_hour_remaining_fraction",
        "fiveHourRemainingFraction",
        "5h_remaining_fraction",
        "5hRemainingFraction",
        "quota_remaining_5h",
        "quotaRemaining5h",
        "remaining_fraction_5h",
        "remainingFraction5h",
        "quota_5h",
        "quota5h",
    ]
    if include_weekly:
        pct_keys.extend([
            "weekly_remaining_fraction",
            "weeklyRemainingFraction",
            "weeklyQuotaRemaining",
            "weekly_quota_remaining",
            "weekly_remaining",
            "weeklyRemaining",
            "weekly_percentage",
            "weeklyPercentage",
            "1w_remaining_fraction",
            "1wRemainingFraction",
            "quota_remaining_1w",
            "quotaRemaining1w",
            "remaining_fraction_1w",
            "remainingFraction1w",
            "quota_1w",
            "quota1w",
        ])
    pct_keys.extend([
        "quota",
        "remaining",
        "percentage",
    ])
    for k in pct_keys:
        if k in info_dict and info_dict[k] is not None and not isinstance(info_dict[k], (dict, list)):
            try:
                val = float(info_dict[k])
                pct = round(val * 100.0, 4) if val <= 1.0 else val
                break
            except (ValueError, TypeError):
                pass

    reset_time = None
    reset_keys = [
        "resetTime",
        "reset_time",
        "five_hour_reset_time",
        "fiveHourResetTime",
        "5h_reset_time",
        "5hResetTime",
        "reset_time_5h",
        "resetTime5h",
    ]
    if include_weekly:
        reset_keys.extend([
            "weeklyResetTime",
            "weekly_reset_time",
            "weeklyReset",
            "weekly_reset",
            "1w_reset_time",
            "1wResetTime",
            "reset_time_1w",
            "resetTime1w",
            "reset_1w",
            "reset1w",
        ])
    reset_keys.append("reset")
    for k in reset_keys:
        if k in info_dict and info_dict[k] is not None and not isinstance(info_dict[k], (dict, list)):
            reset_time = str(info_dict[k])
            break

    # If missing pct or reset_time, check nested container dictionaries
    if pct is None or reset_time is None:
        sub_keys = []
        if include_weekly:
            sub_keys.extend([
                "weeklyQuotaInfo",
                "weekly_quota_info",
                "weeklyQuota",
                "weekly_quota",
                "weekly",
                "weeklyQuotaDetails",
                "weekly_quota_details",
                "longTermQuota",
                "long_term_quota",
                "quota1w",
                "quota_1w",
                "longTerm",
            ])
        sub_keys.extend([
            "fiveHourQuota",
            "five_hour_quota",
            "5hQuota",
            "5h_quota",
            "fiveHour",
            "five_hour",
            "hourlyQuota",
            "hourly_quota",
            "shortTermQuota",
            "short_term_quota",
            "quota5h",
            "quota_5h",
            "hourly",
            "shortTerm",
            "quotaInfo",
            "quota_info",
            "quota",
            "remaining",
        ])
        for sub_key in sub_keys:
            if sub_key in info_dict and isinstance(info_dict[sub_key], dict):
                sub_pct, sub_reset = _extract_pct_and_reset(info_dict[sub_key], include_weekly=include_weekly)
                if pct is None and sub_pct is not None:
                    pct = sub_pct
                if reset_time is None and sub_reset is not None:
                    reset_time = sub_reset
                if pct is not None and reset_time is not None:
                    break

    return pct, reset_time


def _extract_from_windows_or_limits(
    dict_data: dict,
) -> Tuple[Optional[Tuple[float, Optional[str]]], Optional[Tuple[float, Optional[str]]]]:
    """Extract (5H_window, 1W_window) tuples from windows/limits arrays inside dict_data or nested dicts."""
    if not isinstance(dict_data, dict):
        return None, None

    res_5h: Optional[Tuple[float, Optional[str]]] = None
    res_1w: Optional[Tuple[float, Optional[str]]] = None

    dicts_to_check = [dict_data]
    sub_keys = (
        "quotaInfo",
        "quota_info",
        "weeklyQuotaInfo",
        "weekly_quota_info",
        "fiveHourQuota",
        "five_hour_quota",
        "5hQuota",
        "5h_quota",
        "weeklyQuota",
        "weekly_quota",
        "quota",
        "result",
        "data",
        "response",
        "payload",
    )
    for sub in sub_keys:
        if sub in dict_data and isinstance(dict_data[sub], dict):
            dicts_to_check.append(dict_data[sub])

    array_keys = (
        "windows",
        "limits",
        "window_limits",
        "windowLimits",
        "quota_windows",
        "quotaWindows",
        "quota_limits",
        "quotaLimits",
        "rate_limits",
        "rateLimits",
        "model_limits",
        "modelLimits",
    )

    for target_dict in dicts_to_check:
        for key in array_keys:
            if key in target_dict and isinstance(target_dict[key], list):
                for item in target_dict[key]:
                    if not isinstance(item, dict):
                        continue
                    ident = str(
                        item.get("name")
                        or item.get("window")
                        or item.get("type")
                        or item.get("window_name")
                        or item.get("windowName")
                        or item.get("label")
                        or item.get("id")
                        or item.get("kind")
                        or item.get("window_type")
                        or item.get("windowType")
                        or ""
                    ).upper()

                    dur_sec = None
                    for dur_key in (
                        "duration_seconds",
                        "window_seconds",
                        "durationSeconds",
                        "windowSeconds",
                        "duration",
                        "window_size",
                        "windowSize",
                        "ttl",
                        "ttl_seconds",
                        "ttlSeconds",
                    ):
                        if dur_key in item and item[dur_key] is not None:
                            try:
                                dur_sec = float(item[dur_key])
                                break
                            except (ValueError, TypeError):
                                pass

                    is_1w = (
                        (dur_sec is not None and abs(dur_sec - 604800.0) < 1.0)
                        or any(w in ident for w in ("1W", "WEEKLY", "7D", "1WEEK", "WEEK", "LONG_TERM", "LONGTERM"))
                    )
                    is_5h = (
                        (dur_sec is not None and abs(dur_sec - 18000.0) < 1.0)
                        or any(
                            w in ident
                            for w in ("5H", "5HOUR", "5_HOUR", "FIVE_HOUR", "5HOURS", "HOURLY", "SHORT_TERM", "SHORTTERM")
                        )
                    )

                    if is_1w and res_1w is None:
                        p, r = _extract_pct_and_reset(item, include_weekly=True)
                        if p is not None:
                            res_1w = (p, r)
                    if is_5h and res_5h is None:
                        p, r = _extract_pct_and_reset(item, include_weekly=False)
                        if p is not None:
                            res_5h = (p, r)

    return res_5h, res_1w


def _extract_1w_from_dict(d: dict) -> Tuple[Optional[float], Optional[str]]:
    """Extract 1W quota percentage and reset time from any dict (m_data, q_info, pool_target, flat target)."""
    if not isinstance(d, dict):
        return None, None

    # 1. Check explicit weekly container keys in d
    weekly_container_keys = (
        "weeklyQuotaInfo",
        "weekly_quota_info",
        "weeklyQuota",
        "weekly_quota",
        "weekly",
        "weeklyQuotaDetails",
        "weekly_quota_details",
        "longTermQuota",
        "long_term_quota",
        "quota1w",
        "quota_1w",
        "longTerm",
    )
    for k in weekly_container_keys:
        if k in d and isinstance(d[k], dict):
            w_pct, w_rst = _extract_pct_and_reset(d[k], include_weekly=True)
            if w_pct is not None:
                return w_pct, w_rst

    # 2. Check windows or limits array inside d
    w5h, w1w = _extract_from_windows_or_limits(d)
    if w1w is not None and w1w[0] is not None:
        return w1w

    # 3. Check direct 1W fraction/pct and reset keys in d
    direct_pct = None
    direct_pct_keys = (
        "weekly_remaining_fraction",
        "weeklyRemainingFraction",
        "weeklyQuotaRemaining",
        "weekly_quota_remaining",
        "weekly_remaining",
        "weeklyRemaining",
        "weekly_percentage",
        "weeklyPercentage",
        "weekly_quota",
        "weeklyQuota",
        "1w_remaining_fraction",
        "1wRemainingFraction",
        "quota_remaining_1w",
        "quotaRemaining1w",
        "remaining_fraction_1w",
        "remainingFraction1w",
        "quota_1w",
        "quota1w",
    )
    for k in direct_pct_keys:
        if k in d and d[k] is not None and not isinstance(d[k], (dict, list)):
            try:
                val = float(d[k])
                direct_pct = round(val * 100.0, 4) if val <= 1.0 else val
                break
            except (ValueError, TypeError):
                pass

    direct_reset = None
    direct_reset_keys = (
        "weeklyResetTime",
        "weekly_reset_time",
        "weeklyReset",
        "weekly_reset",
        "1w_reset_time",
        "1wResetTime",
        "reset_time_1w",
        "resetTime1w",
        "reset_1w",
        "reset1w",
    )
    for k in direct_reset_keys:
        if k in d and d[k] is not None and not isinstance(d[k], (dict, list)):
            direct_reset = str(d[k])
            break

    if direct_pct is not None:
        return direct_pct, direct_reset

    # 4. Check nested quotaInfo or quota_info in d
    for q_key in ("quotaInfo", "quota_info"):
        if q_key in d and isinstance(d[q_key], dict):
            w_pct, w_rst = _extract_1w_from_dict(d[q_key])
            if w_pct is not None:
                return w_pct, w_rst

    return None, None


def _extract_5h_from_dict(d: dict) -> Tuple[Optional[float], Optional[str]]:
    """Extract 5H quota percentage and reset time from any dict (m_data, pool_target, flat target)."""
    if not isinstance(d, dict):
        return None, None

    # 1. Check explicit 5H container keys in d
    five_hour_container_keys = (
        "fiveHourQuota",
        "five_hour_quota",
        "5hQuota",
        "5h_quota",
        "fiveHour",
        "five_hour",
        "hourlyQuota",
        "hourly_quota",
        "shortTermQuota",
        "short_term_quota",
        "quota5h",
        "quota_5h",
        "hourly",
        "shortTerm",
    )
    for k in five_hour_container_keys:
        if k in d and isinstance(d[k], dict):
            q_pct, q_rst = _extract_pct_and_reset(d[k], include_weekly=False)
            if q_pct is not None:
                return q_pct, q_rst

    # 2. Check windows or limits array inside d
    w5h, w1w = _extract_from_windows_or_limits(d)
    if w5h is not None and w5h[0] is not None:
        return w5h

    # 3. Check direct 5H fraction/pct and reset keys in d
    direct_pct = None
    direct_pct_keys = (
        "five_hour_remaining_fraction",
        "fiveHourRemainingFraction",
        "5h_remaining_fraction",
        "5hRemainingFraction",
        "quota_remaining_5h",
        "quotaRemaining5h",
        "remaining_fraction_5h",
        "remainingFraction5h",
        "quota_5h",
        "quota5h",
        "remaining_5h",
        "remaining5h",
    )
    for k in direct_pct_keys:
        if k in d and d[k] is not None and not isinstance(d[k], (dict, list)):
            try:
                val = float(d[k])
                direct_pct = round(val * 100.0, 4) if val <= 1.0 else val
                break
            except (ValueError, TypeError):
                pass

    direct_reset = None
    direct_reset_keys = (
        "five_hour_reset_time",
        "fiveHourResetTime",
        "5h_reset_time",
        "5hResetTime",
        "reset_time_5h",
        "resetTime5h",
        "reset_5h",
        "reset5h",
    )
    for k in direct_reset_keys:
        if k in d and d[k] is not None and not isinstance(d[k], (dict, list)):
            direct_reset = str(d[k])
            break

    if direct_pct is not None:
        return direct_pct, direct_reset

    # 4. Check quotaInfo or quota_info container in d
    for q_key in ("quotaInfo", "quota_info"):
        if q_key in d and isinstance(d[q_key], dict):
            q_pct, q_rst = _extract_pct_and_reset(d[q_key], include_weekly=False)
            if q_pct is not None:
                return q_pct, q_rst

    # 5. Direct extraction on d itself
    return _extract_pct_and_reset(d, include_weekly=False)


def _model_matches_pool(model_id: str, pool: str) -> bool:
    """Check if model_id belongs to the requested quota pool."""
    m_id = str(model_id).lower()
    p = str(pool).lower()
    if p == "gemini" or "gemini" in p:
        return "gemini" in m_id
    elif p == "claude_gpt" or "claude" in p or "gpt" in p:
        return "claude" in m_id or "gpt" in m_id
    else:
        return p in m_id


def parse_antigravity_quota_json(
    data: Union[dict, list], pool: str = "gemini", quota_pool: Optional[str] = None
) -> Optional[Tuple[QuotaWindow, QuotaWindow]]:
    """
    Parse RPC response JSON body for quota remaining & reset time.
    Supports models dictionary/list schema (v1internal:fetchAvailableModels),
    top-level list payloads, top-level pool dictionary schemas, and flat schemas.
    Returns None if error response or no quota fields are present.
    """
    if isinstance(data, list):
        data = {"models": data}

    if not isinstance(data, dict) or "error" in data:
        logger.warning(f"Invalid or error response in Antigravity RPC payload: {data}")
        return None

    effective_pool = quota_pool if quota_pool is not None else pool
    if not effective_pool:
        effective_pool = os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini")

    pct_5h: Optional[float] = None
    reset_5h: Optional[str] = None
    pct_1w: Optional[float] = None
    reset_1w: Optional[str] = None

    search_dicts = [data]
    for wrap in ("result", "data", "response", "payload"):
        if isinstance(data.get(wrap), dict):
            search_dicts.append(data[wrap])

    # 1. Handle top-level pool dictionary schema e.g. data["pools"][pool] or data[pool]
    pool_target = None
    for s_dict in search_dicts:
        if "pools" in s_dict and isinstance(s_dict["pools"], dict):
            pools_dict = s_dict["pools"]
            if effective_pool in pools_dict and isinstance(pools_dict[effective_pool], dict):
                pool_target = pools_dict[effective_pool]
                break
            elif effective_pool.lower() in ("claude_gpt", "claude", "gpt"):
                for alt in ("claude_gpt", "claude", "gpt"):
                    if alt in pools_dict and isinstance(pools_dict[alt], dict):
                        pool_target = pools_dict[alt]
                        break
                if pool_target is not None:
                    break

        if pool_target is None and effective_pool in s_dict and isinstance(s_dict[effective_pool], dict):
            pool_target = s_dict[effective_pool]
            break
        elif pool_target is None and effective_pool.lower() in ("claude_gpt", "claude", "gpt"):
            for alt in ("claude_gpt", "claude", "gpt"):
                if alt in s_dict and isinstance(s_dict[alt], dict):
                    pool_target = s_dict[alt]
                    break
            if pool_target is not None:
                break

    if pool_target is not None:
        collection_keys = (
            "models",
            "modelList",
            "model_list",
            "availableModels",
            "available_models",
            "quotaModels",
            "quota_models",
        )
        has_collection = any(k in pool_target and isinstance(pool_target[k], (dict, list)) for k in collection_keys)
        if has_collection:
            data = pool_target
            search_dicts.insert(0, pool_target)
        else:
            w_pct, w_rst = _extract_1w_from_dict(pool_target)
            if w_pct is not None:
                pct_1w, reset_1w = w_pct, w_rst
            q_pct, q_rst = _extract_5h_from_dict(pool_target)
            if q_pct is not None:
                pct_5h, reset_5h = q_pct, q_rst

    # 2. Handle fetchAvailableModels / model collection schemas
    if pct_5h is None and pct_1w is None:
        collection_keys = (
            "models",
            "modelList",
            "model_list",
            "availableModels",
            "available_models",
            "quotaModels",
            "quota_models",
        )
        raw_models = None
        for s_dict in search_dicts:
            for k in collection_keys:
                if k in s_dict and isinstance(s_dict[k], (dict, list)):
                    raw_models = s_dict[k]
                    break
            if raw_models is not None:
                break

        if raw_models is not None:
            if isinstance(raw_models, dict):
                model_items = list(raw_models.items())
            else:
                model_items = [(None, m) for m in raw_models if isinstance(m, dict)]

            for dict_key, m_data in model_items:
                if not isinstance(m_data, dict):
                    continue

                # Extract candidate model identifiers
                candidate_ids = []
                for id_key in (
                    "id",
                    "model_id",
                    "modelId",
                    "name",
                    "model",
                    "model_name",
                    "modelName",
                    "displayName",
                    "display_name",
                    "slug",
                ):
                    val = m_data.get(id_key)
                    if val and isinstance(val, str):
                        candidate_ids.append(val)

                if dict_key is not None and isinstance(dict_key, str) and dict_key:
                    candidate_ids.append(dict_key)

                is_match = False
                if candidate_ids:
                    is_match = any(_model_matches_pool(cid, effective_pool) for cid in candidate_ids)
                else:
                    w_pct_test, _ = _extract_1w_from_dict(m_data)
                    q_pct_test, _ = _extract_5h_from_dict(m_data)
                    if w_pct_test is not None or q_pct_test is not None:
                        is_match = True

                if not is_match:
                    continue

                if pct_1w is None:
                    w_pct, w_rst = _extract_1w_from_dict(m_data)
                    if w_pct is not None:
                        pct_1w, reset_1w = w_pct, w_rst

                if pct_5h is None:
                    q_pct, q_rst = _extract_5h_from_dict(m_data)
                    if q_pct is not None:
                        pct_5h, reset_5h = q_pct, q_rst

            if pct_5h is None and pct_1w is not None:
                pct_5h, reset_5h = 100.0, None
            elif pct_1w is None and pct_5h is not None:
                pct_1w, reset_1w = 100.0, None

    # 3. Backwards-compatible flat quota payload parsing
    if pct_5h is None and pct_1w is None:
        target = data
        for k in ("quotaInfo", "userQuota", "quota", "result", "data", "response", "payload"):
            if isinstance(target.get(k), dict):
                target = target[k]
                break

        w_pct, w_rst = _extract_1w_from_dict(target)
        if w_pct is not None:
            pct_1w, reset_1w = w_pct, w_rst

        q_pct, q_rst = _extract_5h_from_dict(target)
        if q_pct is not None:
            pct_5h, reset_5h = q_pct, q_rst

    if pct_5h is None and pct_1w is None:
        return None

    w_5h = QuotaWindow(
        name="5H",
        duration_seconds=18000.0,
        remaining_percentage=max(0.0, min(100.0, pct_5h if pct_5h is not None else 100.0)),
        reset_time=str(reset_5h) if reset_5h is not None else None,
    )

    w_1w = QuotaWindow(
        name="1W",
        duration_seconds=604800.0,
        remaining_percentage=max(0.0, min(100.0, pct_1w if pct_1w is not None else 100.0)),
        reset_time=str(reset_1w) if reset_1w is not None else None,
    )

    return w_5h, w_1w


def fetch_live_antigravity_quota(
    token: Optional[str] = None,
    api_url: str = "https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
    timeout: float = 10.0,
    quota_pool: Optional[str] = None,
) -> Optional[Tuple[QuotaWindow, QuotaWindow]]:
    """
    Query v1internal:fetchAvailableModels to fetch live model quota metrics and reset timestamps.
    Returns (QuotaWindow_5h, QuotaWindow_1w) or None if fetch fails.
    """
    if not token:
        token = load_oauth_token()

    if not token:
        logger.warning("No OAuth token available for fetching live Antigravity quota.")
        return None

    pool = quota_pool if quota_pool is not None else os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini")

    try:
        project_id = os.getenv("ANTIGRAVITY_PROJECT", "")
        payload = json.dumps({"project": project_id}).encode("utf-8")
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
    ):
        self._lock = threading.RLock()
        self.quota_pool = quota_pool if quota_pool is not None else os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini")
        self._remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))
        self._reset_time = reset_time
        self._requests_remaining: Optional[int] = None
        self._tokens_remaining: Optional[int] = None

        self.window_5h = window_5h or QuotaWindow(
            name="5H",
            duration_seconds=18000.0,
            remaining_percentage=self._remaining_percentage,
            reset_time=str(reset_time) if reset_time is not None else None,
        )
        self.window_1w = window_1w or QuotaWindow(
            name="1W", duration_seconds=604800.0, remaining_percentage=100.0
        )

        self.base_backoff_delay = base_backoff_delay
        self.max_backoff_delay = max_backoff_delay
        self.backoff_factor = backoff_factor

        self._backoff_count = 0
        self._active_backoff_delay = 0.0

    @property
    def remaining_percentage(self) -> float:
        with self._lock:
            return self._remaining_percentage

    @remaining_percentage.setter
    def remaining_percentage(self, val: float):
        with self._lock:
            val_float = max(0.0, min(100.0, float(val)))
            self._remaining_percentage = val_float
            self.window_5h.remaining_percentage = val_float
            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = self.get_pacing_backoff_delay()

    def _state_unlocked(self) -> str:
        if self._remaining_percentage <= self.EXHAUSTED_THRESHOLD:
            return QuotaState.EXHAUSTED
        elif self._remaining_percentage < self.LOW_QUOTA_THRESHOLD:
            return QuotaState.LOW_QUOTA
        else:
            return QuotaState.NORMAL

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

    def get_pacing_backoff_delay(
        self, window: Optional[QuotaWindow] = None, now: Optional[float] = None
    ) -> float:
        """
        Calculate proportional pacing backoff delay for a specific window or max across all windows.
        pacing_deficit = max(0.0, time_fraction - quota_fraction).
        """
        with self._lock:
            now_dt = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else None
            if window is not None:
                p_status, backoff = window.get_pacing_status(now_dt)
                if p_status == "OK":
                    return 0.0
                return min(self.max_backoff_delay, backoff)

            _, d5 = self.window_5h.get_pacing_status(now_dt)
            _, d1 = self.window_1w.get_pacing_status(now_dt)
            return max(d5, d1)

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
    ):
        """Update quota levels and reset time for dual windows."""
        with self._lock:
            if remaining_percentage_5h is not None:
                self.window_5h.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage_5h)))
            else:
                self.window_5h.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))

            if reset_time_5h is not None:
                self.window_5h.reset_time = str(reset_time_5h)
                self.window_5h.reset_timestamp = parse_reset_time_to_timestamp(reset_time_5h)
            elif reset_time is not None:
                self.window_5h.reset_time = str(reset_time)
                self.window_5h.reset_timestamp = parse_reset_time_to_timestamp(reset_time)

            if remaining_percentage_1w is not None:
                self.window_1w.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage_1w)))
            else:
                self.window_1w.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))

            if reset_time_1w is not None:
                self.window_1w.reset_time = str(reset_time_1w)
                self.window_1w.reset_timestamp = parse_reset_time_to_timestamp(reset_time_1w)
            elif reset_time is not None:
                self.window_1w.reset_time = str(reset_time)
                self.window_1w.reset_timestamp = parse_reset_time_to_timestamp(reset_time)

            self._remaining_percentage = min(
                self.window_5h.remaining_percentage,
                self.window_1w.remaining_percentage,
            )

            if reset_time is not None:
                self._reset_time = reset_time
            else:
                self._reset_time = self.window_5h.reset_timestamp or self.window_1w.reset_timestamp

            if requests_remaining is not None:
                self._requests_remaining = requests_remaining
            if tokens_remaining is not None:
                self._tokens_remaining = tokens_remaining

            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = self.get_pacing_backoff_delay()

            current_state = self._state_unlocked()

        logger.info(
            f"Quota updated: 5h={self.window_5h.remaining_percentage:.1f}%, 1w={self.window_1w.remaining_percentage:.1f}% state={current_state} reset={reset_time}"
        )

    def update_windows(self, window_5h: QuotaWindow, window_1w: QuotaWindow):
        """Update 5h and 1w dual quota windows."""
        with self._lock:
            self.window_5h = window_5h
            self.window_1w = window_1w

            effective_pct = min(window_5h.remaining_percentage, window_1w.remaining_percentage)
            self._remaining_percentage = max(0.0, min(100.0, float(effective_pct)))
            if window_5h.reset_time is not None or window_5h.reset_timestamp is not None:
                res = window_5h.reset_timestamp if window_5h.reset_timestamp is not None else window_5h.reset_time
                try:
                    self._reset_time = float(res)
                except (ValueError, TypeError):
                    self._reset_time = res

            status_5h, backoff_5h = window_5h.get_pacing_status()
            status_1w, backoff_1w = window_1w.get_pacing_status()
            pacing_backoff = max(backoff_5h, backoff_1w)

            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD and pacing_backoff == 0.0:
                self._backoff_count = 0
                self._active_backoff_delay = 0.0
            elif pacing_backoff > 0.0 and self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._active_backoff_delay = pacing_backoff

            current_state = self._state_unlocked()

        logger.info(
            f"Dual quota updated: 5H={window_5h.remaining_percentage:.1f}% ({status_5h}), "
            f"1W={window_1w.remaining_percentage:.1f}% ({status_1w}), state={current_state}"
        )

    def poll_live_quota(self, token: Optional[str] = None, quota_pool: Optional[str] = None) -> Tuple[QuotaWindow, QuotaWindow]:
        """Fetch live Antigravity quota and update dual windows."""
        pool = quota_pool if quota_pool is not None else self.quota_pool
        res = fetch_live_antigravity_quota(token=token, quota_pool=pool)
        if res is not None:
            w_5h, w_1w = res
            self.update_windows(w_5h, w_1w)
            return w_5h, w_1w
        else:
            logger.warning("Live Antigravity quota fetch returned None; preserving existing QuotaTracker metrics.")
            return self.window_5h, self.window_1w

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

    def get_backoff_delay(self, attempt: Optional[int] = None, now: Optional[float] = None) -> float:
        """
        Calculate and return exponential or pacing back-off delay.
        If attempt is provided (>= 1), calculates delay based on (attempt - 1).
        Increment backoff count if in LOW_QUOTA state.
        Reset backoff count if in NORMAL state (unless behind pacing).
        """
        with self._lock:
            now_dt = datetime.fromtimestamp(now, tz=timezone.utc) if now is not None else None
            status_5h, backoff_5h = self.window_5h.get_pacing_status(now_dt)
            status_1w, backoff_1w = self.window_1w.get_pacing_status(now_dt)
            pacing_backoff = max(backoff_5h, backoff_1w)

            current_state = self._state_unlocked()
            if current_state == QuotaState.NORMAL and pacing_backoff == 0.0:
                self._backoff_count = 0
                self._active_backoff_delay = 0.0
                return 0.0
            elif current_state == QuotaState.EXHAUSTED:
                self._active_backoff_delay = self.max_backoff_delay
                return self.max_backoff_delay
            else:
                if attempt is not None and attempt > 0:
                    exp = attempt - 1
                else:
                    exp = self._backoff_count

                exp_delay = (
                    min(
                        self.max_backoff_delay,
                        self.base_backoff_delay * (self.backoff_factor ** exp),
                    )
                    if current_state == QuotaState.LOW_QUOTA
                    else 0.0
                )
                delay = max(pacing_backoff, exp_delay)
                self._backoff_count += 1
                self._active_backoff_delay = delay
                return delay

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
                active_backoff_delay=self.get_backoff_delay(),
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
