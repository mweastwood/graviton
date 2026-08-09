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
class QuotaInfo:
    remaining_percentage: float = 100.0
    state: str = QuotaState.NORMAL
    reset_time: Optional[float] = None
    active_backoff_delay: float = 0.0
    requests_remaining: Optional[int] = None
    tokens_remaining: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "remaining_percentage": round(self.remaining_percentage, 1),
            "state": self.state,
            "reset_time": self.reset_time,
            "active_backoff_delay": round(self.active_backoff_delay, 2),
            "requests_remaining": self.requests_remaining,
            "tokens_remaining": self.tokens_remaining,
        }


def parse_quota_headers(headers: dict) -> dict:
    """
    Parse HTTP response headers or dictionary for quota & rate limit details.
    Returns a dictionary with parsed fields (remaining_percentage, reset_time,
    requests_remaining, tokens_remaining).
    """
    lower_headers = {str(k).lower(): v for k, v in headers.items()}
    res = {}

    # 1. Remaining percentage
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

    # 2. Reset time
    for key in ("x-ratelimit-reset", "reset_time", "x-quota-reset", "reset"):
        if key in lower_headers:
            try:
                res["reset_time"] = float(lower_headers[key])
                break
            except (ValueError, TypeError):
                res["reset_time"] = lower_headers[key]
                break

    # 3. Requests remaining
    for key in ("x-ratelimit-remaining", "requests_remaining"):
        if key in lower_headers:
            try:
                res["requests_remaining"] = int(lower_headers[key])
                break
            except (ValueError, TypeError):
                pass

    # 4. Tokens remaining
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
    Calculates exponential back-off delays during low quota conditions.
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

        self._backoff_count = 0
        self._active_backoff_delay = 0.0

    @property
    def remaining_percentage(self) -> float:
        with self._lock:
            return self._remaining_percentage

    @remaining_percentage.setter
    def remaining_percentage(self, val: float):
        with self._lock:
            self._remaining_percentage = max(0.0, min(100.0, float(val)))
            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = 0.0

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

    @property
    def active_backoff_delay(self) -> float:
        with self._lock:
            return self._active_backoff_delay

    def update_quota(
        self,
        remaining_percentage: float,
        reset_time: Optional[float] = None,
        requests_remaining: Optional[int] = None,
        tokens_remaining: Optional[int] = None,
    ):
        """Update quota levels and reset time."""
        with self._lock:
            self._remaining_percentage = max(0.0, min(100.0, float(remaining_percentage)))
            if reset_time is not None:
                self._reset_time = reset_time
            if requests_remaining is not None:
                self._requests_remaining = requests_remaining
            if tokens_remaining is not None:
                self._tokens_remaining = tokens_remaining

            if self._remaining_percentage >= self.LOW_QUOTA_THRESHOLD:
                self._backoff_count = 0
                self._active_backoff_delay = 0.0

            current_state = self._state_unlocked()

        logger.info(
            f"Quota updated: {remaining_percentage:.1f}% state={current_state} reset={reset_time}"
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
            )

    def get_backoff_delay(self, attempt: Optional[int] = None) -> float:
        """
        Calculate and return exponential back-off delay.
        If attempt is provided (>= 1), calculates delay based on (attempt - 1).
        Increment backoff count if in LOW_QUOTA state.
        Reset backoff count if in NORMAL state.
        """
        with self._lock:
            current_state = self._state_unlocked()
            if current_state == QuotaState.NORMAL:
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
