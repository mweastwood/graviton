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

    def test_is_valid_repo_name(self):
        """Check strict repo_name validation logic."""
        from lib.security import is_valid_repo_name

        self.assertTrue(is_valid_repo_name("repo-alpha"))
        self.assertTrue(is_valid_repo_name("graviton"))
        self.assertTrue(is_valid_repo_name("my_repo_1"))

        self.assertFalse(is_valid_repo_name("/tmp/bad"))
        self.assertFalse(is_valid_repo_name("../bad"))
        self.assertFalse(is_valid_repo_name(".."))
        self.assertFalse(is_valid_repo_name("."))
        self.assertFalse(is_valid_repo_name("a/b"))
        self.assertFalse(is_valid_repo_name("a\\b"))
        self.assertFalse(is_valid_repo_name(""))
        self.assertFalse(is_valid_repo_name(None))


if __name__ == "__main__":
    unittest.main()
