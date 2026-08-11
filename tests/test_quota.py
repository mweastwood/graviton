"""
Unit tests for lib/quota.py (QuotaTracker, QuotaInfo, parse_quota_headers, QuotaWindow, and fetch_live_antigravity_quota).
"""

import json
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


from lib.quota import (
    QuotaInfo,
    QuotaState,
    QuotaTracker,
    QuotaWindow,
    fetch_live_antigravity_quota,
    format_quota_badge,
    format_reset_countdown,
    parse_antigravity_quota_json,
    parse_quota_headers,
)


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
        self.assertIn("window_5h", d)
        self.assertIn("window_1w", d)

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

        # In EXHAUSTED state (0%), delay returns 0.0 float and resets active_backoff_delay
        tracker.update_quota(0.0)
        self.assertEqual(tracker.state, QuotaState.EXHAUSTED)
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

    def test_quota_window_dataclass_and_methods(self):
        now = time.time()
        w5h = QuotaWindow(
            name="5H",
            duration_seconds=18000.0,
            remaining_percentage=65.0,
            reset_time=str(now + 11565.0),
        )
        self.assertEqual(w5h.format_reset_countdown(now=now), "03:12:45")
        self.assertEqual(w5h.quota_fraction, 0.65)
        self.assertAlmostEqual(w5h.time_fraction(now=now), 11565.0 / 18000.0)
        self.assertEqual(w5h.pacing_status(now=now), "OK")

        w1w = QuotaWindow(
            name="1W",
            duration_seconds=604800.0,
            remaining_percentage=20.0,
            reset_time=str(now + 374400.0),
        )
        self.assertEqual(w1w.format_reset_countdown(now=now), "4d 08h")
        self.assertEqual(w1w.quota_fraction, 0.20)
        self.assertAlmostEqual(w1w.time_fraction(now=now), 374400.0 / 604800.0)
        self.assertEqual(w1w.pacing_status(now=now), "BEHIND_PACING")

    def test_multi_window_header_parsing(self):
        tracker = QuotaTracker()
        now = time.time()
        headers = {
            "x-quota-remaining-5h": "65.0",
            "x-quota-reset-5h": str(now + 10000),
            "x-quota-remaining-1w": "20.0",
            "x-quota-reset-1w": str(now + 300000),
        }
        tracker.parse_quota_headers(headers)
        self.assertEqual(tracker.window_5h.remaining_percentage, 65.0)
        self.assertAlmostEqual(tracker.window_5h.reset_timestamp, now + 10000, places=1)
        self.assertEqual(tracker.window_1w.remaining_percentage, 20.0)
        self.assertAlmostEqual(tracker.window_1w.reset_timestamp, now + 300000, places=1)
        self.assertEqual(tracker.remaining_percentage, 20.0)

    def test_pacing_backoff_calculation(self):
        now = time.time()
        tracker = QuotaTracker(max_backoff_delay=10.0)
        tracker.update_quota(
            remaining_percentage=20.0,
            remaining_percentage_5h=100.0,
            remaining_percentage_1w=20.0,
            reset_time_1w=now + 362880.0,
        )
        self.assertEqual(tracker.window_1w.pacing_status(now=now), "BEHIND_PACING")
        backoff = tracker.get_pacing_backoff_delay(tracker.window_1w, now=now)
        self.assertAlmostEqual(backoff, 4.0)
        self.assertTrue(tracker.is_behind_pacing(now=now))
        self.assertEqual(tracker.get_backoff_delay(now=now), 0.0)
        self.assertEqual(tracker.pacing_status, "BEHIND_PACING")
        self.assertEqual(tracker.pacing_status(now=now), "BEHIND_PACING")
        ok_tracker = QuotaTracker(remaining_percentage=100.0)
        self.assertEqual(ok_tracker.pacing_status, "OK")
        self.assertEqual(ok_tracker.pacing_status(now=now), "OK")

    def test_parse_antigravity_quota_json(self):
        data = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "description": "Models within this group: Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "displayName": "Weekly Limit",
                            "window": "weekly",
                            "resetTime": "2026-08-12T06:51:41Z",
                            "remainingFraction": 0.68779945,
                        },
                        {
                            "bucketId": "gemini-5h",
                            "displayName": "Five Hour Limit",
                            "window": "5h",
                            "resetTime": "2026-08-09T12:18:17Z",
                            "remainingFraction": 0.89673233,
                        },
                    ],
                }
            ]
        }
        res = parse_antigravity_quota_json(data)
        self.assertIsNotNone(res)
        w_5h, w_1w = res
        self.assertEqual(w_5h.name, "5H")
        self.assertEqual(w_5h.duration_seconds, 18000.0)
        self.assertAlmostEqual(w_5h.remaining_percentage, 89.673233, places=4)
        self.assertEqual(w_5h.reset_time, "2026-08-09T12:18:17Z")

        self.assertEqual(w_1w.name, "1W")
        self.assertEqual(w_1w.duration_seconds, 604800.0)
        self.assertAlmostEqual(w_1w.remaining_percentage, 68.779945, places=4)
        self.assertEqual(w_1w.reset_time, "2026-08-12T06:51:41Z")

    def test_parse_antigravity_quota_json_error_handling(self):
        # Error payload returns None
        error_data = {"error": {"code": 401, "message": "Invalid token"}}
        self.assertIsNone(parse_antigravity_quota_json(error_data))

        # Payload without buckets returns None
        invalid_data = {"result": "success"}
        self.assertIsNone(parse_antigravity_quota_json(invalid_data))

    def test_format_reset_countdown(self):
        now_dt = datetime(2026, 8, 9, 5, 6, 0, tzinfo=timezone.utc)

        # 5h window countdown under 24h
        reset_5h = "2026-08-09T08:18:45Z"
        self.assertEqual(format_reset_countdown(reset_5h, now_dt=now_dt), "03:12:45")

        # 1w window countdown over 24h
        reset_1w = "2026-08-13T13:06:00Z"
        self.assertEqual(format_reset_countdown(reset_1w, now_dt=now_dt), "4d 08h")

        # Stringified numeric timestamp e.g. "1786266000.0"
        reset_numeric_str = "1786266000.0"
        cd_num = format_reset_countdown(reset_numeric_str, now_dt=now_dt)
        self.assertNotEqual(cd_num, "1786266000.0")

        # None -> N/A
        self.assertEqual(format_reset_countdown(None, now_dt=now_dt), "N/A")

    def test_pacing_ratio_and_badge_formatting(self):
        now_dt = datetime(2026, 8, 9, 5, 6, 0, tzinfo=timezone.utc)

        # 5H: 65% remaining
        w_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=65.0, reset_time="2026-08-09T08:18:45Z")
        badge_5h = format_quota_badge(w_5h, now_dt=now_dt)
        self.assertEqual(badge_5h, "[ 5H QUOTA: 65% | RESET: 03:12:45 | PACING: OK ]")

        # 1W: 20% remaining -> BEHIND
        w_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=20.0, reset_time="2026-08-13T13:06:00Z")
        badge_1w = format_quota_badge(w_1w, now_dt=now_dt)
        self.assertTrue(badge_1w.startswith("[ 1W QUOTA: 20% | RESET: 4d 08h | PACING: BEHIND"))
        self.assertIn("PACING: BEHIND (NEW TASKS SUSPENDED)", badge_1w)

    def test_load_oauth_token_nested_json(self):
        import tempfile
        from pathlib import Path
        from lib.quota import load_oauth_token

        # Test nested token dict {"token": {"access_token": "ya29.nested"}}
        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
            tf.write(json.dumps({"token": {"access_token": "ya29.nested"}}))
            tf.flush()
            tmp_path = Path(tf.name)

        try:
            token = load_oauth_token(token_file=tmp_path)
            self.assertEqual(token, "ya29.nested")
        finally:
            tmp_path.unlink()

        # Test plain text token string
        with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
            tf.write("ya29.raw_token")
            tf.flush()
            tmp_path = Path(tf.name)

        try:
            token = load_oauth_token(token_file=tmp_path)
            self.assertEqual(token, "ya29.raw_token")
        finally:
            tmp_path.unlink()

    def test_parse_antigravity_quota_json_groups_schema(self):
        data = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "description": "Models within this group: Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "displayName": "Weekly Limit",
                            "window": "weekly",
                            "resetTime": "2026-08-12T06:51:41Z",
                            "remainingFraction": 0.85,
                        },
                        {
                            "bucketId": "gemini-5h",
                            "displayName": "Five Hour Limit",
                            "window": "5h",
                            "resetTime": "2026-08-09T12:18:17Z",
                            "remainingFraction": 0.90,
                        },
                    ],
                },
                {
                    "displayName": "Claude and GPT models",
                    "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
                    "buckets": [
                        {
                            "bucketId": "3p-weekly",
                            "displayName": "Weekly Limit",
                            "window": "weekly",
                            "resetTime": "2026-08-12T08:55:05Z",
                            "remainingFraction": 0.21,
                        },
                        {
                            "bucketId": "3p-5h",
                            "displayName": "Five Hour Limit",
                            "window": "5h",
                            "resetTime": "2026-08-09T13:23:08Z",
                            "remainingFraction": 1.0,
                        },
                    ],
                },
            ]
        }
        # 1. Gemini pool
        res_gemini = parse_antigravity_quota_json(data, pool="gemini")
        self.assertIsNotNone(res_gemini)
        w_5h_g, w_1w_g = res_gemini
        self.assertEqual(w_5h_g.remaining_percentage, 90.0)
        self.assertEqual(w_1w_g.remaining_percentage, 85.0)

        # 2. Claude/GPT pool
        res_claude = parse_antigravity_quota_json(data, pool="claude_gpt")
        self.assertIsNotNone(res_claude)
        w_5h_c, w_1w_c = res_claude
        self.assertEqual(w_5h_c.remaining_percentage, 100.0)
        self.assertEqual(w_1w_c.remaining_percentage, 21.0)

    def test_parse_antigravity_quota_json_single_group_or_flat_buckets(self):
        data = {
            "buckets": [
                {
                    "bucketId": "gemini-weekly",
                    "window": "weekly",
                    "resetTime": "2026-08-12T06:51:41Z",
                    "remainingFraction": 0.70,
                },
                {
                    "bucketId": "gemini-5h",
                    "window": "5h",
                    "resetTime": "2026-08-09T12:18:17Z",
                    "remainingFraction": 0.80,
                },
            ]
        }
        res = parse_antigravity_quota_json(data, pool="gemini")
        self.assertIsNotNone(res)
        w_5h, w_1w = res
        self.assertEqual(w_5h.remaining_percentage, 80.0)
        self.assertEqual(w_1w.remaining_percentage, 70.0)

    def test_quota_tracker_pool_configuration(self):
        # Explicit quota_pool
        t1 = QuotaTracker(quota_pool="claude_gpt")
        self.assertEqual(t1.quota_pool, "claude_gpt")
        self.assertEqual(t1.get_info().quota_pool, "claude_gpt")
        self.assertEqual(t1.get_info().to_dict()["quota_pool"], "claude_gpt")

        # Environment variable fallback
        with patch.dict("os.environ", {"ANTIGRAVITY_QUOTA_POOL": "claude_gpt"}):
            t2 = QuotaTracker()
            self.assertEqual(t2.quota_pool, "claude_gpt")

    def test_format_quota_badge_with_pool(self):
        w_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=65.0, reset_time="2026-08-09T08:18:45Z")
        badge = format_quota_badge(w_5h, quota_pool="gemini")
        self.assertIn("[ GEMINI 5H QUOTA: 65%", badge)

        badge_c = format_quota_badge(w_5h, quota_pool="claude_gpt")
        self.assertIn("[ CLAUDE_GPT 5H QUOTA: 65%", badge_c)

    @patch("lib.quota.urllib.request.urlopen")
    def test_fetch_live_antigravity_quota_mocked(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "buckets": [
                        {
                            "bucketId": "gemini-5h",
                            "window": "5h",
                            "remainingFraction": 0.65,
                            "resetTime": "2026-08-09T09:00:00Z",
                        },
                        {
                            "bucketId": "gemini-weekly",
                            "window": "weekly",
                            "remainingFraction": 0.75,
                            "resetTime": "2026-08-16T09:00:00Z",
                        },
                    ],
                }
            ]
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = fetch_live_antigravity_quota(token="test-oauth-token", quota_pool="gemini")
        self.assertIsNotNone(res)
        w_5h, w_1w = res
        self.assertEqual(w_5h.remaining_percentage, 65.0)
        self.assertEqual(w_1w.remaining_percentage, 75.0)

        # Verify request parameters
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.full_url, "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary")
        self.assertEqual(req.headers.get("User-agent"), "antigravity-cli")
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload, {})

    @patch("lib.quota.urllib.request.urlopen")
    def test_fetch_live_antigravity_quota_error_returns_none(self, mock_urlopen):
        # Simulated API HTTP error payload
        mock_resp = MagicMock()
        mock_resp.status = 401
        mock_resp.read.return_value = json.dumps({
            "error": {"code": 401, "message": "Invalid token"}
        }).encode("utf-8")
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        res = fetch_live_antigravity_quota(token="test-oauth-token")
        self.assertIsNone(res)

    def test_update_windows_and_poll_live_quota(self):
        tracker = QuotaTracker()
        w_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=65.0, reset_time="2026-08-09T09:00:00Z")
        w_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=20.0, reset_time="2026-08-16T09:00:00Z")

        tracker.update_windows(w_5h, w_1w)
        self.assertEqual(tracker.window_5h.remaining_percentage, 65.0)
        self.assertEqual(tracker.window_1w.remaining_percentage, 20.0)
        self.assertEqual(tracker.remaining_percentage, 20.0)

        # Test poll_live_quota fallback when fetch returns None
        with patch("lib.quota.fetch_live_antigravity_quota", return_value=None):
            polled_5h, polled_1w = tracker.poll_live_quota(token="invalid-token", force=True)
            # Must preserve existing state instead of overwriting with dummy 100%
            self.assertEqual(polled_5h.remaining_percentage, 65.0)
            self.assertEqual(polled_1w.remaining_percentage, 20.0)

    def test_uniform_ttl_window_updates(self):
        tracker = QuotaTracker()
        w5h_v1 = QuotaWindow(name="5H", remaining_percentage=90.0)
        w1w_v1 = QuotaWindow(name="1W", remaining_percentage=80.0)

        w5h_v2 = QuotaWindow(name="5H", remaining_percentage=70.0)
        w1w_v2 = QuotaWindow(name="1W", remaining_percentage=60.0)

        start_time = 1000.0

        with patch("lib.quota.time.time", return_value=start_time):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h_v1, w1w_v1)) as mock_fetch:
                w_5h, w_1w = tracker.poll_live_quota()
                self.assertEqual(mock_fetch.call_count, 1)
                self.assertEqual(w_5h.remaining_percentage, 90.0)
                self.assertEqual(w_1w.remaining_percentage, 80.0)

        # 5 seconds later: no TTL expired, should not fetch
        with patch("lib.quota.time.time", return_value=start_time + 5.0):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h_v2, w1w_v2)) as mock_fetch:
                w_5h, w_1w = tracker.poll_live_quota()
                self.assertEqual(mock_fetch.call_count, 0)
                self.assertEqual(w_5h.remaining_percentage, 90.0)
                self.assertEqual(w_1w.remaining_percentage, 80.0)

        # 12 seconds later: 60s TTL not expired yet -> should not fetch
        with patch("lib.quota.time.time", return_value=start_time + 12.0):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h_v2, w1w_v2)) as mock_fetch:
                w_5h, w_1w = tracker.poll_live_quota()
                self.assertEqual(mock_fetch.call_count, 0)
                self.assertEqual(w_5h.remaining_percentage, 90.0)
                self.assertEqual(w_1w.remaining_percentage, 80.0)

        # 62 seconds later: 60s TTL expired -> fetch triggers and updates BOTH windows from API payload
        with patch("lib.quota.time.time", return_value=start_time + 62.0):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h_v2, w1w_v2)) as mock_fetch:
                w_5h, w_1w = tracker.poll_live_quota()
                self.assertEqual(mock_fetch.call_count, 1)
                self.assertEqual(w_5h.remaining_percentage, 70.0)
                self.assertEqual(w_1w.remaining_percentage, 60.0)

    def test_poll_live_quota_force_parameter(self):
        tracker = QuotaTracker()
        w5h_v1 = QuotaWindow(name="5H", remaining_percentage=90.0)
        w1w_v1 = QuotaWindow(name="1W", remaining_percentage=80.0)

        w5h_v2 = QuotaWindow(name="5H", remaining_percentage=50.0)
        w1w_v2 = QuotaWindow(name="1W", remaining_percentage=40.0)

        start_time = 2000.0

        with patch("lib.quota.time.time", return_value=start_time):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h_v1, w1w_v1)):
                tracker.poll_live_quota()

        # At start_time + 2s, force=False does not fetch
        with patch("lib.quota.time.time", return_value=start_time + 2.0):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h_v2, w1w_v2)) as mock_fetch:
                w_5h, w_1w = tracker.poll_live_quota(force=False)
                self.assertEqual(mock_fetch.call_count, 0)
                self.assertEqual(w_5h.remaining_percentage, 90.0)

        # At start_time + 2s, force=True forces fetch and updates both windows
        with patch("lib.quota.time.time", return_value=start_time + 2.0):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h_v2, w1w_v2)) as mock_fetch:
                w_5h, w_1w = tracker.poll_live_quota(force=True)
                self.assertEqual(mock_fetch.call_count, 1)
                self.assertEqual(w_5h.remaining_percentage, 50.0)
                self.assertEqual(w_1w.remaining_percentage, 40.0)

    def test_background_polling_lifecycle(self):
        tracker = QuotaTracker()
        w5h = QuotaWindow(name="5H", remaining_percentage=85.0)
        w1w = QuotaWindow(name="1W", remaining_percentage=75.0)

        self.assertFalse(tracker.is_polling())

        with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h, w1w)):
            tracker.start_background_polling(poll_interval=0.05)
            self.assertTrue(tracker.is_polling())

            # Idempotent call to start_background_polling
            tracker.start_background_polling(poll_interval=0.05)
            self.assertTrue(tracker.is_polling())

            time.sleep(0.15)
            tracker.stop_background_polling()
            self.assertFalse(tracker.is_polling())

    def test_concurrent_poll_live_quota_in_flight_deduplication(self):
        tracker = QuotaTracker()
        w5h = QuotaWindow(name="5H", remaining_percentage=85.0)
        w1w = QuotaWindow(name="1W", remaining_percentage=75.0)
        fetch_call_count = 0
        fetch_lock = threading.Lock()

        def slow_fetch(*args, **kwargs):
            nonlocal fetch_call_count
            with fetch_lock:
                fetch_call_count += 1
            time.sleep(0.1)
            return w5h, w1w

        with patch("lib.quota.fetch_live_antigravity_quota", side_effect=slow_fetch):
            t1 = threading.Thread(target=tracker.poll_live_quota, kwargs={"force": True})
            t2 = threading.Thread(target=tracker.poll_live_quota, kwargs={"force": True})
            t1.start()
            t2.start()
            t1.join()
            t2.join()

        self.assertEqual(fetch_call_count, 1)

    def test_fetch_failure_updates_timestamps_to_prevent_endpoint_hammering(self):
        tracker = QuotaTracker()
        w5h = QuotaWindow(name="5H", remaining_percentage=85.0)
        w1w = QuotaWindow(name="1W", remaining_percentage=75.0)
        start_time = 5000.0

        # Initial fetch returns None (API error)
        with patch("lib.quota.time.time", return_value=start_time):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=None) as mock_fetch:
                tracker.poll_live_quota()
                self.assertEqual(mock_fetch.call_count, 1)

        # 1 second later: TTL (60s) not reached, must NOT retry
        with patch("lib.quota.time.time", return_value=start_time + 1.0):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h, w1w)) as mock_fetch:
                tracker.poll_live_quota()
                self.assertEqual(mock_fetch.call_count, 0)

        # 60 seconds later: TTL expired, retries API call
        with patch("lib.quota.time.time", return_value=start_time + 60.0):
            with patch("lib.quota.fetch_live_antigravity_quota", return_value=(w5h, w1w)) as mock_fetch:
                tracker.poll_live_quota()
                self.assertEqual(mock_fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()






