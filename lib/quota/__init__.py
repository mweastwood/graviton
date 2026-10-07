"""
Antigravity Model Quota Tracker & Rate Limit Manager for Graviton.

This package provides:
- time_utils: Timestamp parsing and countdown formatting
- models: QuotaState, QuotaWindow, QuotaInfo dataclass
- auth: OAuth token discovery and atomic JSON serialization
- endpoint: Quota RPC endpoint detection and resolution
- parse: Pure JSON and response parsing without network dependencies
- fetch: Network operations for fetching live quotas
- tracker: Thread-safe QuotaTracker pacing engine
"""

import json
import logging
import math
import os
import subprocess
import time
import urllib.error
import urllib.request

from .time_utils import (
    _normalize_now_datetime,
    _normalize_pool_key,
    format_pacing_recovery_countdown,
    format_reset_countdown,
    parse_reset_time_to_datetime,
    parse_reset_time_to_timestamp,
)
from .models import (
    QuotaInfo,
    QuotaState,
    QuotaWindow,
    _PacingStatusResult,
)
from .auth import (
    _atomic_write_json,
    _extract_token_from_object,
    load_oauth_token,
)
from .endpoint import (
    DAILY_ANTIGRAVITY_QUOTA_ENDPOINT,
    DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT,
    detect_antigravity_quota_endpoint_from_logs,
    normalize_antigravity_quota_endpoint,
    resolve_antigravity_quota_endpoint,
)
from .parse import (
    _group_matches_pool,
    _parse_buckets,
    parse_all_antigravity_quota_json,
    parse_antigravity_quota_json,
    parse_quota_headers,
)
from .fetch import (
    DEFAULT_GEMINI_MODELS,
    DEFAULT_THIRD_PARTY_MODELS,
    fetch_all_live_antigravity_quota,
    fetch_cli_models,
    fetch_live_antigravity_quota,
)
from .tracker import (
    QuotaTracker,
    logger,
)

__all__ = [
    "DEFAULT_GEMINI_MODELS",
    "DEFAULT_THIRD_PARTY_MODELS",
    "fetch_cli_models",
    "_atomic_write_json",
    "QuotaState",
    "parse_reset_time_to_datetime",
    "parse_reset_time_to_timestamp",
    "_normalize_now_datetime",
    "_normalize_pool_key",
    "QuotaWindow",
    "format_reset_countdown",
    "format_pacing_recovery_countdown",
    "_extract_token_from_object",
    "load_oauth_token",
    "DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT",
    "DAILY_ANTIGRAVITY_QUOTA_ENDPOINT",
    "normalize_antigravity_quota_endpoint",
    "detect_antigravity_quota_endpoint_from_logs",
    "resolve_antigravity_quota_endpoint",
    "_group_matches_pool",
    "_parse_buckets",
    "parse_all_antigravity_quota_json",
    "parse_antigravity_quota_json",
    "fetch_all_live_antigravity_quota",
    "fetch_live_antigravity_quota",
    "QuotaInfo",
    "parse_quota_headers",
    "_PacingStatusResult",
    "QuotaTracker",
    "logger",
]
