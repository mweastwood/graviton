"""
Time, countdown, and pool key normalization utilities for Quota management.
"""

import math
from datetime import datetime, timezone
from typing import Optional, Union


def parse_reset_time_to_datetime(
    reset_time: Optional[Union[str, float, int, datetime]],
) -> Optional[datetime]:
    """Parse numeric timestamp, ISO 8601 string, or datetime object to timezone-aware UTC datetime."""
    if reset_time is None or isinstance(reset_time, bool):
        return None
    if isinstance(reset_time, datetime):
        return reset_time.astimezone(timezone.utc) if reset_time.tzinfo else reset_time.replace(tzinfo=timezone.utc)

    # 1. Try numeric conversion (int, float, or stringified float/int e.g., "1786266000.0")
    try:
        ts = float(reset_time)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        pass

    # 2. Try ISO string parsing (handling trailing 'Z' for Python <= 3.10)
    if not isinstance(reset_time, str):
        return None

    try:
        s = reset_time.strip()
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def parse_reset_time_to_timestamp(
    reset_time: Optional[Union[str, float, int, datetime]],
) -> Optional[float]:
    """Parse reset time to epoch float timestamp."""
    dt = parse_reset_time_to_datetime(reset_time)
    if dt is None:
        return None
    try:
        return dt.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _normalize_now_datetime(now: Optional[Union[float, int, datetime]]) -> Optional[datetime]:
    """Convert timestamp float/int or datetime object into a timezone-aware UTC datetime."""
    if now is None:
        return None
    if isinstance(now, bool):
        return None
    if isinstance(now, datetime):
        return now.astimezone(timezone.utc) if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if isinstance(now, (int, float)):
        try:
            return datetime.fromtimestamp(now, tz=timezone.utc)
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return None


def _normalize_pool_key(pool: Optional[str]) -> str:
    """Normalize quota pool string to canonical pool key ('gemini' or 'claude_gpt')."""
    p = str(pool or "gemini").lower()
    if "claude" in p or "gpt" in p or "3p" in p or "third" in p:
        return "claude_gpt"
    return "gemini"


def format_reset_countdown(
    reset_time: Optional[Union[str, float, int, datetime]] = None,
    now_dt: Optional[Union[float, int, datetime]] = None,
    window_name: Optional[str] = None,
) -> str:
    """Format reset timestamp into HH:MM:SS or Xd Yh countdown string."""
    if reset_time is None:
        return "N/A"
    dt = parse_reset_time_to_datetime(reset_time)
    if dt is None:
        return str(reset_time)
    now_dt_norm = _normalize_now_datetime(now_dt)
    if now_dt_norm is None:
        now_dt_norm = datetime.now(timezone.utc)
    diff = (dt - now_dt_norm).total_seconds()
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


def format_pacing_recovery_countdown(
    seconds: Optional[Union[float, int]] = None,
) -> str:
    """Format pacing recovery seconds into Xd Yh or HH:MM:SS countdown string."""
    if seconds is None:
        return "00:00:00"
    try:
        sec = float(seconds)
    except (ValueError, TypeError):
        return "00:00:00"
    if math.isnan(sec) or math.isinf(sec) or sec <= 0:
        return "00:00:00"
    try:
        if sec >= 86400:
            days = int(sec // 86400)
            hours = int((sec % 86400) // 3600)
            return f"{days}d {hours:02d}h"
        else:
            hours = int(sec // 3600)
            mins = int((sec % 3600) // 60)
            secs = int(sec % 60)
            return f"{hours:02d}:{mins:02d}:{secs:02d}"
    except (ValueError, TypeError, OverflowError):
        return "00:00:00"
