"""
Unit tests for lib/security.py
"""

import hashlib
import hmac
import unittest
from lib.security import verify_signature, contains_bot_marker, BOT_MARKER


class TestSecurity(unittest.TestCase):

    def test_verify_signature_no_secret(self):
        """When secret is empty, verification should pass."""
        self.assertTrue(verify_signature(b"payload", "", ""))

    def test_verify_signature_missing_or_malformed_header(self):
        """When secret is configured, missing or malformed header should fail."""
        secret = "supersecret"
        self.assertFalse(verify_signature(b"payload", secret, ""))
        self.assertFalse(verify_signature(b"payload", secret, "invalid_header"))

    def test_verify_signature_valid(self):
        """Valid HMAC SHA256 signature should pass."""
        secret = "mysecretkey"
        payload = b'{"action": "opened"}'
        expected_sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        header = f"sha256={expected_sig}"

        self.assertTrue(verify_signature(payload, secret, header))

    def test_verify_signature_invalid(self):
        """Invalid HMAC SHA256 signature should fail."""
        secret = "mysecretkey"
        payload = b'{"action": "opened"}'
        header = "sha256=0000000000000000000000000000000000000000000000000000000000000000"

        self.assertFalse(verify_signature(payload, secret, header))

    def test_contains_bot_marker(self):
        """Check bot marker detection logic."""
        self.assertFalse(contains_bot_marker(""))
        self.assertFalse(contains_bot_marker("Please fix this bug in line 10."))
        self.assertTrue(contains_bot_marker(f"LGTM! {BOT_MARKER}"))


if __name__ == "__main__":
    unittest.main()
