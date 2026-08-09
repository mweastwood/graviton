"""
Unit tests for lib/quota.py (QuotaTracker, QuotaInfo, and parse_quota_headers).
"""

import time
import unittest
from lib.quota import QuotaInfo, QuotaState, QuotaTracker, parse_quota_headers


class TestQuotaTracker(unittest.TestCase):

    def test_quota_initial_state(self):
        tracker = QuotaTracker()
        self.assertEqual(tracker.remaining_percentage, 100.0)
        self.assertEqual(tracker.state, QuotaState.NORMAL)
        self.assertIsNone(tracker.reset_time)
        self.assertEqual(tracker.active_backoff_delay, 0.0)

        info = tracker.get_info()
        self.assertEqual(info.remaining_percentage, 100.0)
        self.assertEqual(info.state, QuotaState.NORMAL)

        d = info.to_dict()
        self.assertEqual(d["remaining_percentage"], 100.0)
        self.assertEqual(d["state"], "NORMAL")

    def test_quota_state_transitions(self):
        tracker = QuotaTracker()

        # NORMAL threshold (>= 15%)
        tracker.update_quota(50.0)
        self.assertEqual(tracker.state, QuotaState.NORMAL)

        tracker.update_quota(15.0)
        self.assertEqual(tracker.state, QuotaState.NORMAL)

        # LOW_QUOTA threshold (< 15% and > 0%)
        tracker.update_quota(14.9)
        self.assertEqual(tracker.state, QuotaState.LOW_QUOTA)

        tracker.update_quota(1.0)
        self.assertEqual(tracker.state, QuotaState.LOW_QUOTA)

        # EXHAUSTED threshold (<= 0%)
        tracker.update_quota(0.0)
        self.assertEqual(tracker.state, QuotaState.EXHAUSTED)

        # Recover back to NORMAL
        tracker.update_quota(80.0)
        self.assertEqual(tracker.state, QuotaState.NORMAL)

    def test_standalone_parse_quota_headers(self):
        headers = {
            "X-Quota-Remaining-Percent": "8.5",
            "X-RateLimit-Reset": "1700000000",
            "X-RateLimit-Remaining": "20",
            "X-RateLimit-Tokens-Remaining": "5000",
        }
        parsed = parse_quota_headers(headers)
        self.assertEqual(parsed["remaining_percentage"], 8.5)
        self.assertEqual(parsed["reset_time"], 1700000000.0)
        self.assertEqual(parsed["requests_remaining"], 20)
        self.assertEqual(parsed["tokens_remaining"], 5000)

    def test_quota_header_parsing(self):
        tracker = QuotaTracker()

        # Standard header keys
        headers = {
            "x-quota-remaining-percent": "12.5",
            "x-ratelimit-reset": "1700000000",
            "x-ratelimit-remaining": "50",
        }
        tracker.parse_quota_headers(headers)

        self.assertEqual(tracker.remaining_percentage, 12.5)
        self.assertEqual(tracker.state, QuotaState.LOW_QUOTA)
        self.assertEqual(tracker.reset_time, 1700000000.0)

        # Case-insensitive / alternative dict keys
        tracker.parse_quota_headers({"Quota_Percent": 0.0, "Reset_Time": "18:00:00"})
        self.assertEqual(tracker.remaining_percentage, 0.0)
        self.assertEqual(tracker.state, QuotaState.EXHAUSTED)
        self.assertEqual(tracker.reset_time, "18:00:00")

    def test_exponential_backoff_calculation(self):
        tracker = QuotaTracker(base_backoff_delay=1.0, max_backoff_delay=10.0, backoff_factor=2.0)

        # In NORMAL state, delay is 0.0
        self.assertEqual(tracker.get_backoff_delay(), 0.0)

        # Transition to LOW_QUOTA
        tracker.update_quota(10.0)
        self.assertEqual(tracker.state, QuotaState.LOW_QUOTA)

        # 1st call: 1.0 * (2^0) = 1.0
        d1 = tracker.get_backoff_delay()
        self.assertEqual(d1, 1.0)
        self.assertEqual(tracker.active_backoff_delay, 1.0)

        # 2nd call: 1.0 * (2^1) = 2.0
        d2 = tracker.get_backoff_delay()
        self.assertEqual(d2, 2.0)

        # 3rd call: 1.0 * (2^2) = 4.0
        d3 = tracker.get_backoff_delay()
        self.assertEqual(d3, 4.0)

        # 4th call: 1.0 * (2^3) = 8.0
        d4 = tracker.get_backoff_delay()
        self.assertEqual(d4, 8.0)

        # 5th call: 1.0 * (2^4) = 16.0 -> capped at max_backoff_delay (10.0)
        d5 = tracker.get_backoff_delay()
        self.assertEqual(d5, 10.0)

        # Reset via update to NORMAL state
        tracker.update_quota(90.0)
        self.assertEqual(tracker.get_backoff_delay(), 0.0)
        self.assertEqual(tracker.active_backoff_delay, 0.0)

    def test_exponential_backoff_with_attempt_parameter(self):
        tracker = QuotaTracker(base_backoff_delay=1.0, max_backoff_delay=10.0, backoff_factor=2.0)
        tracker.update_quota(10.0)  # LOW_QUOTA state

        # attempt=1 -> exp=0 -> 1.0 * (2^0) = 1.0
        self.assertEqual(tracker.get_backoff_delay(attempt=1), 1.0)

        # attempt=2 -> exp=1 -> 1.0 * (2^1) = 2.0
        self.assertEqual(tracker.get_backoff_delay(attempt=2), 2.0)

        # attempt=3 -> exp=2 -> 1.0 * (2^2) = 4.0
        self.assertEqual(tracker.get_backoff_delay(attempt=3), 4.0)

        # attempt=5 -> exp=4 -> 1.0 * (2^4) = 16.0 -> max 10.0
        self.assertEqual(tracker.get_backoff_delay(attempt=5), 10.0)

    def test_reset_time_formatting(self):
        tracker = QuotaTracker()
        self.assertEqual(tracker.get_reset_time_str(), "N/A")

        tracker.reset_time = "14:30:00"
        self.assertEqual(tracker.get_reset_time_str(), "14:30:00")

        future_ts = time.time() + 60
        tracker.reset_time = future_ts
        self.assertTrue(tracker.get_reset_time_str().startswith("in "))


if __name__ == "__main__":
    unittest.main()
