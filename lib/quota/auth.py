"""
Authentication, OAuth token discovery, and atomic JSON persistence helpers for Quota.
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("graviton.quota")
def _atomic_write_json(target_path: Path, data: Any, indent: int = 2):
    """
    Atomically write JSON data to target_path using a temporary file in the target directory.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(target_path.parent),
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as f:
            tmp_path = Path(f.name)
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


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


