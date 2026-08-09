"""
Antigravity Model Quota Tracker & Rate Limit Manager for Graviton.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("graviton.quota")


class QuotaState:
    NORMAL = "NORMAL"
    LOW_QUOTA = "LOW_QUOTA"
    EXHAUSTED = "EXHAUSTED"


@dataclass
class QuotaWindow:
    window_name: str  # "5h" or "1w"
    total_duration_seconds: float  # 18000.0 for 5h; 604800.0 for 1w
    remaining_percentage: float = 100.0
    reset_timestamp: Optional[float] = None

    def remaining_time_seconds(self, now: Optional[float] = None) -> float:
        if self.reset_timestamp is None:
            return 0.0
        now_val = time.time() if now is None else float(now)
        return max(0.0, float(self.reset_timestamp) - now_val)

    @property
    def quota_fraction(self) -> float:
        return max(0.0, min(100.0, float(self.remaining_percentage))) / 100.0

    def time_fraction(self, now: Optional[float] = None) -> float:
        if self.total_duration_seconds <= 0:
            return 0.0
        return self.remaining_time_seconds(now) / self.total_duration_seconds

    def pacing_status(self, now: Optional[float] = None) -> str:
        if self.reset_timestamp is None:
            return "OK"
        if self.quota_fraction < self.time_fraction(now):
            return "BEHIND_PACING"
        return "OK"

    def format_reset_countdown(self, now: Optional[float] = None) -> str:
        if self.reset_timestamp is None:
            return "N/A"
        rem = round(self.remaining_time_seconds(now))
        if self.window_name == "1w":
            days = int(rem // 86400)
            hours = int((rem % 86400) // 3600)
            return f"{days}d {hours:02d}h"
        else:
            hours = int(rem // 3600)
            minutes = int((rem % 3600) // 60)
            seconds = int(rem % 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@dataclass
class QuotaInfo:
    remaining_percentage: float = 100.0
    state: str = QuotaState.NORMAL
    reset_time: Optional[float] = None
    active_backoff_delay: float = 0.0
    requests_remaining: Optional[int] = None
    tokens_remaining: Optional[int] = None
    window_5h: Optional[QuotaWindow] = None
    window_1w: Optional[QuotaWindow] = None

    def to_dict(self) -> dict:
        d = {
            "remaining_percentage": round(self.remaining_percentage, 1),
            "state": self.state,
            "reset_time": self.reset_time,
            "active_backoff_delay": round(self.active_backoff_delay, 2),
            "requests_remaining": self.requests_remaining,
            "tokens_remaining": self.tokens_remaining,
        }
        if self.window_5h is not None:
            d["window_5h"] = {
                "remaining_percentage": round(self.window_5h.remaining_percentage, 1),
                "reset_timestamp": self.window_5h.reset_timestamp,
                "pacing_status": self.window_5h.pacing_status(),
            }
        if self.window_1w is not None:
            d["window_1w"] = {
                "remaining_percentage": round(self.window_1w.remaining_percentage, 1),
                "reset_timestamp": self.window_1w.reset_timestamp,
                "pacing_status": self.window_1w.pacing_status(),
            }
        return d


def parse_quota_headers(headers: dict) -> dict:
    """
    Parse HTTP response headers or dictionary for quota & rate limit details.
    Returns a dictionary with parsed fields (remaining_percentage, reset_time,
    remaining_percentage_5h, reset_time_5h, remaining_percentage_1w, reset_time_1w,
    requests_remaining, tokens_remaining).
    """
    lower_headers = {str(k).lower(): v for k, v in headers.items()}
    res = {}

    # 1. 5h Window Remaining Percentage
    for key in (
        "x-quota-remaining-5h",
        "x-quota-remaining-percent-5h",
        "quota_percent_5h",
        "remaining_percentage_5h",
        "quota_5h",
    ):
        if key in lower_headers:
            try:
                res["remaining_percentage_5h"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 2. 5h Window Reset Time
    for key in ("x-quota-reset-5h", "x-ratelimit-reset-5h", "reset_time_5h", "reset_5h"):
        if key in lower_headers:
            try:
                res["reset_time_5h"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                res["reset_time_5h"] = lower_headers[key]
                break

    # 3. 1w Window Remaining Percentage
    for key in (
        "x-quota-remaining-1w",
        "x-quota-remaining-percent-1w",
        "quota_percent_1w",
        "remaining_percentage_1w",
        "quota_1w",
    ):
        if key in lower_headers:
            try:
                res["remaining_percentage_1w"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 4. 1w Window Reset Time
    for key in ("x-quota-reset-1w", "x-ratelimit-reset-1w", "reset_time_1w", "reset_1w"):
        if key in lower_headers:
            try:
                res["reset_time_1w"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                res["reset_time_1w"] = lower_headers[key]
                break

    # 5. General Remaining percentage
    for key in (
        "x-quota-remaining-percent",
        "x-quota-remaining",
        "quota_percent",
        "remaining_percentage",
        "quota",
    ):
        if key in lower_headers:
            try:
                res["remaining_percentage"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 6. General Reset time
    for key in ("x-ratelimit-reset", "reset_time", "x-quota-reset", "reset"):
        if key in lower_headers:
            try:
                res["reset_time"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                res["reset_time"] = lower_headers[key]
                break

    # 7. Requests remaining
    for key in ("x-ratelimit-remaining", "requests_remaining"):
        if key in lower_headers:
            try:
                res["requests_remaining"] = int(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 8. Tokens remaining
    for key in ("x-ratelimit-tokens-remaining", "tokens_remaining"):
        if key in lower_headers:
            try:
                res["tokens_remaining"] = int(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    return res


class QuotaTracker:
    """
    Thread-safe tracker for Antigravity API model quota levels and rate limits.
    Calculates exponential back-off delays during low quota conditions
    and pacing back-off for dual 5h and 1w windows.
    """

    LOW_QUOTA_THRESHOLD = 15.0  # Percentage < 15% triggers LOW_QUOTA
    EXHAUSTED_THRESHOLD = 0.0   # Percentage == 0% triggers EXHAUSTED

    def __init__(
        self,
        remaining_percentage: float = 100.0,
        reset_time: Optional[float] = None,
        base_backoff_delay: float = 1.0,
        max_backoff_delay: float = 60.0,
        backoff_factor: float = 2.0,
    ):
        self._lock = threading.RLock()
        self._remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))
        self._reset_time = reset_time
        self._requests_remaining: Optional[int] = None
        self._tokens_remaining: Optional[int] = None

        self.base_backoff_delay = base_backoff_delay
        self.max_backoff_delay = max_backoff_delay
        self.backoff_factor = backoff_factor

        self.window_5h = QuotaWindow(
            window_name="5h",
            total_duration_seconds=18000.0,
            remaining_percentage=self._remaining_percentage,
            reset_timestamp=reset_time if isinstance(reset_time, (int, float)) else None,
        )
        self.window_1w = QuotaWindow(
            window_name="1w",
            total_duration_seconds=604800.0,
            remaining_percentage=self._remaining_percentage,
            reset_timestamp=reset_time if isinstance(reset_time, (int, float)) else None,
        )

        self._backoff_count = 0
        self._active_backoff_delay = 0.0

    @property
    def remaining_percentage(self) -> float:
        with self._lock:
            return self._remaining_percentage

    @remaining_percentage.setter
    def remaining_percentage(self, val: float):
        with self._lock:
            val_float = max(0.0, min(100.0, float(val)))
            self._remaining_percentage = val_float
            self.window_5h.remaining_percentage = val_float
            self.window_1w.remaining_percentage = val_float
            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = self.get_pacing_backoff_delay()

    def _state_unlocked(self) -> str:
        if self._remaining_percentage <= self.EXHAUSTED_THRESHOLD:
            return QuotaState.EXHAUSTED
        elif self._remaining_percentage < self.LOW_QUOTA_THRESHOLD:
            return QuotaState.LOW_QUOTA
        else:
            return QuotaState.NORMAL

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_unlocked()

    @property
    def reset_time(self) -> Optional[float]:
        with self._lock:
            return self._reset_time

    @reset_time.setter
    def reset_time(self, val: Optional[float]):
        with self._lock:
            self._reset_time = val
            if isinstance(val, (int, float)):
                if self.window_5h.reset_timestamp is None:
                    self.window_5h.reset_timestamp = float(val)
                if self.window_1w.reset_timestamp is None:
                    self.window_1w.reset_timestamp = float(val)

    @property
    def active_backoff_delay(self) -> float:
        with self._lock:
            return self._active_backoff_delay

    def get_pacing_backoff_delay(
        self, window: Optional[QuotaWindow] = None, now: Optional[float] = None
    ) -> float:
        """
        Calculate proportional pacing backoff delay for a specific window or max across all windows.
        pacing_deficit = max(0.0, time_fraction - quota_fraction).
        """
        with self._lock:
            if window is not None:
                if window.reset_timestamp is None or window.pacing_status(now) == "OK":
                    return 0.0
                pacing_deficit = max(0.0, window.time_fraction(now) - window.quota_fraction)
                return min(self.max_backoff_delay, round(pacing_deficit * self.max_backoff_delay, 2))

            d5 = self.get_pacing_backoff_delay(self.window_5h, now=now)
            d1 = self.get_pacing_backoff_delay(self.window_1w, now=now)
            return max(d5, d1)

    def update_quota(
        self,
        remaining_percentage: float,
        reset_time: Optional[float] = None,
        requests_remaining: Optional[int] = None,
        tokens_remaining: Optional[int] = None,
        remaining_percentage_5h: Optional[float] = None,
        reset_time_5h: Optional[float] = None,
        remaining_percentage_1w: Optional[float] = None,
        reset_time_1w: Optional[float] = None,
    ):
        """Update quota levels and reset time for dual windows."""
        with self._lock:
            if remaining_percentage_5h is not None:
                self.window_5h.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage_5h)))
            else:
                self.window_5h.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))

            if reset_time_5h is not None and isinstance(reset_time_5h, (int, float)):
                self.window_5h.reset_timestamp = float(reset_time_5h)
            elif reset_time is not None and isinstance(reset_time, (int, float)):
                self.window_5h.reset_timestamp = float(reset_time)

            if remaining_percentage_1w is not None:
                self.window_1w.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage_1w)))
            else:
                self.window_1w.remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))

            if reset_time_1w is not None and isinstance(reset_time_1w, (int, float)):
                self.window_1w.reset_timestamp = float(reset_time_1w)
            elif reset_time is not None and isinstance(reset_time, (int, float)):
                self.window_1w.reset_timestamp = float(reset_time)

            self._remaining_percentage = min(
                self.window_5h.remaining_percentage,
                self.window_1w.remaining_percentage,
            )

            if reset_time is not None:
                self._reset_time = reset_time
            else:
                self._reset_time = self.window_5h.reset_timestamp or self.window_1w.reset_timestamp

            if requests_remaining is not None:
                self._requests_remaining = requests_remaining
            if tokens_remaining is not None:
                self._tokens_remaining = tokens_remaining

            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = self.get_pacing_backoff_delay()

            current_state = self._state_unlocked()

        logger.info(
            f"Quota updated: 5h={self.window_5h.remaining_percentage:.1f}%, 1w={self.window_1w.remaining_percentage:.1f}% state={current_state} reset={reset_time}"
        )

    def parse_quota_headers(self, headers: dict):
        """
        Parse HTTP response headers or dictionary for quota & rate limit details.
        """
        parsed = parse_quota_headers(headers)
        if parsed:
            new_pct = parsed.get("remaining_percentage", self.remaining_percentage)
            self.update_quota(
                remaining_percentage=new_pct,
                reset_time=parsed.get("reset_time"),
                requests_remaining=parsed.get("requests_remaining"),
                tokens_remaining=parsed.get("tokens_remaining"),
                remaining_percentage_5h=parsed.get("remaining_percentage_5h"),
                reset_time_5h=parsed.get("reset_time_5h"),
                remaining_percentage_1w=parsed.get("remaining_percentage_1w"),
                reset_time_1w=parsed.get("reset_time_1w"),
            )

    def get_backoff_delay(self, attempt: Optional[int] = None, now: Optional[float] = None) -> float:
        """
        Calculate and return exponential or pacing back-off delay.
        If attempt is provided (>= 1), calculates delay based on (attempt - 1).
        Increment backoff count if in LOW_QUOTA state.
        Reset backoff count if in NORMAL state (unless behind pacing).
        """
        with self._lock:
            current_state = self._state_unlocked()
            if current_state == QuotaState.NORMAL:
                pacing_delay = self.get_pacing_backoff_delay(now=now)
                if pacing_delay > 0.0:
                    self._active_backoff_delay = pacing_delay
                    return pacing_delay
                self._backoff_count = 0
                self._active_backoff_delay = 0.0
                return 0.0
            elif current_state == QuotaState.EXHAUSTED:
                self._active_backoff_delay = self.max_backoff_delay
                return self.max_backoff_delay
            else:  # LOW_QUOTA
                if attempt is not None and attempt > 0:
                    exp = attempt - 1
                else:
                    exp = self._backoff_count
                delay = min(
                    self.max_backoff_delay,
                    self.base_backoff_delay * (self.backoff_factor ** exp),
                )
                self._backoff_count += 1
                self._active_backoff_delay = delay
                return delay

    def reset_backoff(self):
        """Reset exponential backoff counter."""
        with self._lock:
            self._backoff_count = 0
            self._active_backoff_delay = 0.0

    def get_info(self) -> QuotaInfo:
        with self._lock:
            return QuotaInfo(
                remaining_percentage=self._remaining_percentage,
                state=self._state_unlocked(),
                reset_time=self._reset_time,
                active_backoff_delay=self._active_backoff_delay,
                requests_remaining=self._requests_remaining,
                tokens_remaining=self._tokens_remaining,
                window_5h=self.window_5h,
                window_1w=self.window_1w,
            )

    def get_reset_time_str(self) -> str:
        """Return formatted reset time or relative seconds string."""
        with self._lock:
            if self._reset_time is None:
                return "N/A"
            if isinstance(self._reset_time, (int, float)):
                now = time.time()
                if self._reset_time > now:
                    diff = int(self._reset_time - now)
                    return f"in {diff}s"
                else:
                    return f"{int(self._reset_time)}s"
            return str(self._reset_time)

