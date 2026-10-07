"""
Antigravity Quota RPC Endpoint resolution and log auto-discovery.
"""

import logging
import os
import re
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("graviton.quota")
DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT: str = (
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
)
# Internal/daily Antigravity quota retrieval endpoint exported for testing,
# debugging, and manual override configuration in internal/daily CLI environments.
DAILY_ANTIGRAVITY_QUOTA_ENDPOINT: str = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"
)


def normalize_antigravity_quota_endpoint(url: str) -> str:
    """Normalize a quota endpoint URL to ensure proper endpoint path."""
    if not url or not str(url).strip():
        return ""
    url = str(url).strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        url = f"https://{url}"
    if not url.endswith(":retrieveUserQuotaSummary"):
        url = url.rstrip("/")
        if not url.endswith("/v1internal"):
            url = f"{url}/v1internal:retrieveUserQuotaSummary"
        else:
            url = f"{url}:retrieveUserQuotaSummary"
    return url


def detect_antigravity_quota_endpoint_from_logs(
    log_file: Optional[Path] = None,
    max_bytes: int = 65536,
) -> Optional[str]:
    """
    Attempt to discover active Antigravity API endpoint from recent agy CLI logs.
    Scans the latest cli.log or recent logs in ~/.gemini/antigravity-cli/log.
    """
    candidate_paths: List[Path] = []
    if log_file is not None:
        candidate_paths.append(Path(log_file))
    else:
        base_dir = Path.home() / ".gemini" / "antigravity-cli"
        cli_log = base_dir / "cli.log"
        candidate_paths.append(cli_log)

        log_dir = base_dir / "log"
        if log_dir.is_dir():
            try:
                recent_logs = sorted(
                    log_dir.glob("cli-*.log"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for rlog in recent_logs[:3]:
                    if rlog not in candidate_paths:
                        candidate_paths.append(rlog)
            except Exception:
                pass

    domain_re = re.compile(r"https://([a-zA-Z0-9.-]*cloudcode-pa\.googleapis\.com)(?:/|:|$)")

    for path in candidate_paths:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    chunk = f.read(max_bytes)
                m = domain_re.search(chunk)
                if m:
                    base = f"https://{m.group(1)}"
                    return f"{base}/v1internal:retrieveUserQuotaSummary"
            except Exception as e:
                logger.debug(f"Failed scanning {path} for quota endpoint: {e}")

    return None


def resolve_antigravity_quota_endpoint(api_url: Optional[str] = None) -> str:
    """
    Resolve the Antigravity quota retrieval endpoint.
    Order of precedence:
    1. Explicitly passed api_url
    2. ANTIGRAVITY_QUOTA_ENDPOINT / ANTIGRAVITY_API_URL / ANTIGRAVITY_ENDPOINT env vars
    3. Auto-detected endpoint from local antigravity-cli logs
    4. DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT (production cloudcode-pa)
    """
    if api_url and api_url.strip():
        return normalize_antigravity_quota_endpoint(api_url)

    for env_var in ("ANTIGRAVITY_QUOTA_ENDPOINT", "ANTIGRAVITY_API_URL", "ANTIGRAVITY_ENDPOINT"):
        val = os.getenv(env_var)
        if val and val.strip():
            return normalize_antigravity_quota_endpoint(val)

    detected = detect_antigravity_quota_endpoint_from_logs()
    if detected:
        return detected

    return DEFAULT_ANTIGRAVITY_QUOTA_ENDPOINT


