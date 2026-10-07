"""
Network I/O operations for fetching live model quotas and discovering models.
"""

import json
import logging
import os
import subprocess
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from .models import QuotaWindow
from .auth import load_oauth_token
from .endpoint import DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT, resolve_antigravity_quota_endpoint
from .parse import parse_all_antigravity_quota_json, parse_antigravity_quota_json

logger = logging.getLogger("graviton.quota")
DEFAULT_GEMINI_MODELS: List[str] = [
    "gemini-3.8-flash-medium",
    "gemini-3.6-flash-high",
    "gemini-3.6-flash-medium",
    "gemini-3.6-flash-low",
    "gemini-3.5-flash-high",
    "gemini-3.5-flash-medium",
    "gemini-3.5-flash-low",
    "gemini-3.1-pro-high",
    "gemini-3.1-pro-low",
]

DEFAULT_THIRD_PARTY_MODELS: List[str] = [
    "claude-sonnet-5-5-medium",
    "claude-opus-5-5-medium",
    "gpt-oss-120b-medium",
]


def fetch_cli_models(timeout: float = 5.0) -> Tuple[List[str], List[str]]:
    """
    Query `agy models` at runtime to dynamically discover available models.
    Returns (available_gemini_models, available_third_party_models).
    Falls back to (DEFAULT_GEMINI_MODELS.copy(), DEFAULT_THIRD_PARTY_MODELS.copy()) on error.
    """
    try:
        res = subprocess.run(
            ["agy", "models"], capture_output=True, text=True, timeout=timeout
        )
        if res.returncode != 0 or not res.stdout:
            logger.warning(
                f"'agy models' command returned exit code {res.returncode if hasattr(res, 'returncode') else 'N/A'}"
            )
            return DEFAULT_GEMINI_MODELS.copy(), DEFAULT_THIRD_PARTY_MODELS.copy()

        gemini_models: List[str] = []
        third_party_models: List[str] = []

        for line in res.stdout.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("#"):
                continue
            parts = line_str.split()
            model_id = parts[0].strip()
            if not model_id:
                continue
            if model_id.lower().startswith("gemini"):
                if model_id not in gemini_models:
                    gemini_models.append(model_id)
            else:
                if model_id not in third_party_models:
                    third_party_models.append(model_id)

        final_gemini = gemini_models if gemini_models else DEFAULT_GEMINI_MODELS.copy()
        final_3p = third_party_models if third_party_models else DEFAULT_THIRD_PARTY_MODELS.copy()
        return final_gemini, final_3p
    except Exception as err:
        logger.warning(f"Failed to fetch models from 'agy models': {err}")
        return DEFAULT_GEMINI_MODELS.copy(), DEFAULT_THIRD_PARTY_MODELS.copy()


def fetch_all_live_antigravity_quota(
    token: Optional[str] = None,
    api_url: Optional[str] = None,
    timeout: float = 10.0,
) -> Optional[Dict[str, Tuple[QuotaWindow, QuotaWindow]]]:
    """
    Query v1internal:retrieveUserQuotaSummary to fetch live model quota metrics for all pools in a single RPC roundtrip.
    Returns {pool_name: (QuotaWindow_5h, QuotaWindow_1w)} or None if fetch fails or payload is empty.
    """
    if not token:
        token = load_oauth_token()

    if not token:
        logger.warning("No OAuth token available for fetching live Antigravity quota.")
        return None

    target_url = resolve_antigravity_quota_endpoint(api_url)
    payload = json.dumps({}).encode("utf-8")

    try:
        req = urllib.request.Request(
            target_url,
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
                raise urllib.error.HTTPError(
                    target_url,
                    resp.status,
                    f"Antigravity RPC endpoint returned HTTP status {resp.status}",
                    getattr(resp, "headers", None),
                    None,
                )
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            res = parse_all_antigravity_quota_json(data)
            return res if res else None
    except Exception as e:
        logger.warning(f"Failed to fetch live Antigravity quota from {target_url}: {e}")
        if target_url != DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT:
            try:
                logger.info(
                    f"Retrying live Antigravity quota fetch using default endpoint {DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT}"
                )
                req = urllib.request.Request(
                    DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT,
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
                        logger.warning(
                            f"Fallback Antigravity RPC endpoint returned HTTP status {resp.status}"
                        )
                        return None
                    body = resp.read().decode("utf-8")
                    data = json.loads(body)
                    res = parse_all_antigravity_quota_json(data)
                    if res:
                        return res
            except Exception as retry_err:
                logger.warning(f"Fallback fetch to default endpoint also failed: {retry_err}")
        return None


def fetch_live_antigravity_quota(
    token: Optional[str] = None,
    api_url: Optional[str] = None,
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
    target_url = resolve_antigravity_quota_endpoint(api_url)
    payload = json.dumps({}).encode("utf-8")

    try:
        req = urllib.request.Request(
            target_url,
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
                raise urllib.error.HTTPError(
                    target_url,
                    resp.status,
                    f"Antigravity RPC endpoint returned HTTP status {resp.status}",
                    getattr(resp, "headers", None),
                    None,
                )
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return parse_antigravity_quota_json(data, pool=pool)
    except Exception as e:
        logger.warning(f"Failed to fetch live Antigravity quota from {target_url}: {e}")
        if target_url != DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT:
            try:
                logger.info(
                    f"Retrying live Antigravity quota fetch using default endpoint {DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT}"
                )
                req = urllib.request.Request(
                    DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT,
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
                        logger.warning(
                            f"Fallback Antigravity RPC endpoint returned HTTP status {resp.status}"
                        )
                        return None
                    body = resp.read().decode("utf-8")
                    data = json.loads(body)
                    return parse_antigravity_quota_json(data, pool=pool)
            except Exception as retry_err:
                logger.warning(f"Fallback fetch to default endpoint also failed: {retry_err}")
        return None

