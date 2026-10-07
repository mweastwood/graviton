"""
Pure JSON and HTTP response parsing for Antigravity Quota payloads.
Contains no network I/O or HTTP client dependencies.
"""

import logging
import os
from typing import Dict, List, Optional, Tuple, Union

from .models import QuotaWindow
from .time_utils import _normalize_pool_key

logger = logging.getLogger("graviton.quota")
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


def _parse_buckets(buckets: list) -> Optional[Tuple[QuotaWindow, QuotaWindow]]:
    """Parse list of bucket dictionaries into (QuotaWindow_5h, QuotaWindow_1w)."""
    if not buckets or not isinstance(buckets, list):
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


def parse_all_antigravity_quota_json(
    data: Union[dict, list]
) -> Dict[str, Tuple[QuotaWindow, QuotaWindow]]:
    """
    Parse v1internal:retrieveUserQuotaSummary RPC response JSON body for all quota pools.
    Extracts both Gemini and 3rd-party (Claude/GPT) quota window pairs from a single response.
    Returns a dictionary mapping canonical pool keys ('gemini', 'claude_gpt') to (QuotaWindow_5h, QuotaWindow_1w).
    """
    if not isinstance(data, dict) or "error" in data:
        logger.warning(f"Invalid or error response in Antigravity RPC payload: {data}")
        return {}

    groups = None
    if "groups" in data and isinstance(data["groups"], list):
        groups = data["groups"]
    else:
        for wrap in ("result", "data", "response", "payload"):
            if isinstance(data.get(wrap), dict) and isinstance(data[wrap].get("groups"), list):
                groups = data[wrap]["groups"]
                break

    parsed_pools: Dict[str, Tuple[QuotaWindow, QuotaWindow]] = {}

    if groups:
        if len(groups) == 1:
            g = groups[0]
            g_buckets = g.get("buckets") if isinstance(g, dict) else None
            w = _parse_buckets(g_buckets)
            if w:
                if _group_matches_pool(g, "claude_gpt"):
                    parsed_pools["claude_gpt"] = w
                else:
                    parsed_pools["gemini"] = w
        else:
            for canonical_pool in ("gemini", "claude_gpt"):
                for g in groups:
                    if _group_matches_pool(g, canonical_pool):
                        g_buckets = g.get("buckets") if isinstance(g, dict) else None
                        w = _parse_buckets(g_buckets)
                        if w:
                            parsed_pools[canonical_pool] = w
                        break

    if not parsed_pools:
        # Check for top-level or wrapped flat buckets
        buckets = []
        if "buckets" in data and isinstance(data["buckets"], list):
            buckets = data["buckets"]
        else:
            for wrap in ("result", "data", "response", "payload"):
                if isinstance(data.get(wrap), dict) and isinstance(data[wrap].get("buckets"), list):
                    buckets = data[wrap]["buckets"]
                    break

        if buckets:
            w = _parse_buckets(buckets)
            if w:
                parsed_pools["gemini"] = w

    return parsed_pools


def parse_antigravity_quota_json(
    data: Union[dict, list], pool: str = "gemini", quota_pool: Optional[str] = None
) -> Optional[Tuple[QuotaWindow, QuotaWindow]]:
    """
    Parse v1internal:retrieveUserQuotaSummary RPC response JSON body for quota windows.
    Supports grouped 5-hour and weekly quota buckets.
    Returns (QuotaWindow_5h, QuotaWindow_1w) or None if payload invalid or no matching quota buckets found.
    """
    effective_pool = quota_pool if quota_pool is not None else pool
    if not effective_pool:
        effective_pool = os.getenv("ANTIGRAVITY_QUOTA_POOL", "gemini")

    all_pools = parse_all_antigravity_quota_json(data)
    if not all_pools:
        return None

    norm_pool = _normalize_pool_key(effective_pool)
    if norm_pool in all_pools:
        return all_pools[norm_pool]

    if effective_pool in all_pools:
        return all_pools[effective_pool]

    if len(all_pools) == 1:
        return next(iter(all_pools.values()))

    return None


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


