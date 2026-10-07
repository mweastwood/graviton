"""
Focused unit tests for lib.quota.parse and lib.quota.time_utils.
These tests verify pure parsing and time formatting logic without requiring any HTTP mocks.
"""

import unittest
from datetime import datetime, timezone

from lib.quota.time_utils import (
    parse_reset_time_to_datetime,
    parse_reset_time_to_timestamp,
    _normalize_now_datetime,
    _normalize_pool_key,
    format_reset_countdown,
    format_pacing_recovery_countdown,
)
from lib.quota.parse import (
    _group_matches_pool,
    _parse_buckets,
    parse_all_antigravity_quota_json,
    parse_antigravity_quota_json,
    parse_quota_headers,
)


class TestQuotaTimeUtilsFocused(unittest.TestCase):
    """Focused tests for pure time_utils functions requiring no HTTP mocks."""

    def test_parse_reset_time_iso(self):
        dt = parse_reset_time_to_datetime("2026-10-07T12:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 10)
        self.assertEqual(dt.day, 7)
        self.assertEqual(dt.hour, 12)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_parse_reset_time_timestamp(self):
        ts = 1791374400.0
        dt = parse_reset_time_to_datetime(ts)
        self.assertIsNotNone(dt)
        self.assertEqual(parse_reset_time_to_timestamp(ts), ts)

    def test_parse_reset_time_invalid(self):
        self.assertIsNone(parse_reset_time_to_datetime(None))
        self.assertIsNone(parse_reset_time_to_datetime(True))
        self.assertIsNone(parse_reset_time_to_datetime(False))
        self.assertIsNone(parse_reset_time_to_datetime("invalid-date-string"))
        self.assertIsNone(parse_reset_time_to_timestamp(None))

    def test_normalize_pool_key(self):
        self.assertEqual(_normalize_pool_key("gemini"), "gemini")
        self.assertEqual(_normalize_pool_key("Gemini-3.8"), "gemini")
        self.assertEqual(_normalize_pool_key("claude"), "claude_gpt")
        self.assertEqual(_normalize_pool_key("gpt"), "claude_gpt")
        self.assertEqual(_normalize_pool_key("3p"), "claude_gpt")
        self.assertEqual(_normalize_pool_key("third-party"), "claude_gpt")
        self.assertEqual(_normalize_pool_key(None), "gemini")

    def test_format_reset_countdown(self):
        now = datetime(2026, 10, 7, 12, 0, 0, tzinfo=timezone.utc)
        reset = datetime(2026, 10, 7, 13, 30, 45, tzinfo=timezone.utc)
        cd = format_reset_countdown(reset, now_dt=now)
        self.assertEqual(cd, "01:30:45")

        # Multi-day countdown
        multi_day = datetime(2026, 10, 10, 14, 0, 0, tzinfo=timezone.utc)
        cd_days = format_reset_countdown(multi_day, now_dt=now)
        self.assertEqual(cd_days, "3d 02h")

        # Expired countdown
        past = datetime(2026, 10, 7, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(format_reset_countdown(past, now_dt=now), "00:00:00")

    def test_format_pacing_recovery_countdown(self):
        self.assertEqual(format_pacing_recovery_countdown(None), "00:00:00")
        self.assertEqual(format_pacing_recovery_countdown(-10), "00:00:00")
        self.assertEqual(format_pacing_recovery_countdown(3665), "01:01:05")
        self.assertEqual(format_pacing_recovery_countdown(90000), "1d 01h")


class TestQuotaParseFocused(unittest.TestCase):
    """Focused tests for pure parse functions requiring no HTTP mocks."""

    def test_group_matches_pool(self):
        gemini_group = {"displayName": "Gemini Pro Model Quota"}
        claude_group = {"displayName": "Claude 3.5 Sonnet Quota"}

        self.assertTrue(_group_matches_pool(gemini_group, "gemini"))
        self.assertFalse(_group_matches_pool(gemini_group, "claude_gpt"))

        self.assertTrue(_group_matches_pool(claude_group, "claude_gpt"))
        self.assertFalse(_group_matches_pool(claude_group, "gemini"))

    def test_parse_buckets_empty_and_valid(self):
        self.assertIsNone(_parse_buckets([]))
        self.assertIsNone(_parse_buckets(None))

        buckets = [
            {
                "window": "5h",
                "remainingFraction": 0.85,
                "resetTime": "2026-10-07T15:00:00Z",
            },
            {
                "window": "1w",
                "remainingFraction": 0.95,
                "resetTime": "2026-10-14T00:00:00Z",
            },
        ]
        w5, w1 = _parse_buckets(buckets)
        self.assertEqual(w5.name, "5H")
        self.assertEqual(w5.remaining_percentage, 85.0)
        self.assertEqual(w1.name, "1W")
        self.assertEqual(w1.remaining_percentage, 95.0)

    def test_parse_all_antigravity_quota_json(self):
        payload = {
            "groups": [
                {
                    "displayName": "Gemini models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 0.70},
                        {"window": "1w", "remainingFraction": 0.90},
                    ],
                },
                {
                    "displayName": "Claude models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 0.40},
                        {"window": "1w", "remainingFraction": 0.60},
                    ],
                },
            ]
        }
        res = parse_all_antigravity_quota_json(payload)
        self.assertIn("gemini", res)
        self.assertIn("claude_gpt", res)
        self.assertEqual(res["gemini"][0].remaining_percentage, 70.0)
        self.assertEqual(res["claude_gpt"][0].remaining_percentage, 40.0)

    def test_parse_antigravity_quota_json_with_pool(self):
        payload = {
            "groups": [
                {
                    "displayName": "Gemini models",
                    "buckets": [
                        {"window": "5h", "remainingFraction": 0.75},
                        {"window": "1w", "remainingFraction": 0.85},
                    ],
                }
            ]
        }
        windows = parse_antigravity_quota_json(payload, pool="gemini")
        self.assertIsNotNone(windows)
        w5, w1 = windows
        self.assertEqual(w5.remaining_percentage, 75.0)
        self.assertEqual(w1.remaining_percentage, 85.0)

    def test_parse_quota_headers(self):
        headers = {
            "X-Quota-Remaining-5h": "88.5",
            "X-Quota-Reset-5h": "1791374400",
            "X-Quota-Remaining-1w": "99.0",
            "X-Quota-Reset-1w": "1791979200",
            "X-Ratelimit-Remaining": "500",
            "X-Ratelimit-Tokens-Remaining": "100000",
        }
        parsed = parse_quota_headers(headers)
        self.assertEqual(parsed["remaining_percentage_5h"], 88.5)
        self.assertEqual(parsed["reset_time_5h"], 1791374400.0)
        self.assertEqual(parsed["remaining_percentage_1w"], 99.0)
        self.assertEqual(parsed["requests_remaining"], 500)
        self.assertEqual(parsed["tokens_remaining"], 100000)


if __name__ == "__main__":
    unittest.main()
