"""
Security and HMAC signature verification utilities for Graviton.
"""

import hashlib
import hmac

BOT_MARKER = "<!-- antigravity-auto-reply -->"


def verify_signature(payload_bytes: bytes, secret: str, signature_header: str) -> bool:
    """
    Verify HMAC SHA256 signature from GitHub webhook.

    :param payload_bytes: Raw HTTP request body bytes.
    :param secret: Configured webhook secret string.
    :param signature_header: Content of X-Hub-Signature-256 header.
    :return: True if signature is valid, False otherwise.
    """
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected_sig = signature_header.split("sha256=")[1]
    computed_sig = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig, computed_sig)


def contains_bot_marker(text: str) -> bool:
    """
    Check if the text contains the bot auto-reply HTML comment marker.

    :param text: Body of review comment or issue comment.
    :return: True if text contains bot marker, False otherwise.
    """
    if not text:
        return False
    return BOT_MARKER in text


def is_valid_repo_name(repo_name: str) -> bool:
    """
    Validate that repo_name is a simple repository directory name
    without path separators, absolute paths, or traversal components.

    :param repo_name: Repository name string to validate.
    :return: True if repo_name is valid and safe, False otherwise.
    """
    if not repo_name or not isinstance(repo_name, str):
        return False
    if "/" in repo_name or "\\" in repo_name:
        return False
    if repo_name in (".", ".."):
        return False
    from pathlib import Path
    p = Path(repo_name)
    if p.is_absolute() or p.name != repo_name:
        return False
    return True

