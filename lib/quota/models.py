"""
Data models and window abstractions for Antigravity Quota tracking.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from .time_utils import (
    parse_reset_time_to_datetime,
    _normalize_now_datetime,
    format_reset_countdown,
    format_pacing_recovery_countdown,
)
class QuotaState:
    NORMAL = "NORMAL"
    LOW_QUOTA = "LOW_QUOTA"
    EXHAUSTED = "EXHAUSTED"


class QuotaWindow:
    def __init__(
        self,
        name: Optional[str] = None,
        duration_seconds: Optional[float] = None,
        remaining_percentage: Optional[float] = 100.0,
        reset_time: Optional[Union[str, float, int, datetime]] = None,
        reset_timestamp: Optional[float] = None,
        window_name: Optional[str] = None,
        total_duration_seconds: Optional[float] = None,
        reset_datetime: Optional[datetime] = None,
    ):
        raw_name = name or window_name or "5H"
        self.name = raw_name.upper()
        self.window_name = raw_name.lower()

        dur = duration_seconds if duration_seconds is not None else total_duration_seconds
        self.duration_seconds = float(dur if dur is not None else 18000.0)
        self.total_duration_seconds = self.duration_seconds

        self.remaining_percentage = float(remaining_percentage) if remaining_percentage is not None else None

        res = reset_time if reset_time is not None else reset_timestamp
        if reset_datetime is not None:
            self.reset_datetime = parse_reset_time_to_datetime(reset_datetime)
        else:
            self.reset_datetime = parse_reset_time_to_datetime(res)

        if isinstance(res, datetime):
            self.reset_time = self.reset_datetime.isoformat() if self.reset_datetime else None
        else:
            self.reset_time = str(res) if res is not None else (self.reset_datetime.isoformat() if self.reset_datetime else None)
        self.reset_timestamp = self.reset_datetime.timestamp() if self.reset_datetime is not None else None
        self._last_reset_time = self.reset_time
        self._last_reset_timestamp = self.reset_timestamp
        self._last_reset_datetime = self.reset_datetime

    def _sync_reset_datetime(self) -> Optional[datetime]:
        cur_time = self.reset_time
        cur_ts = self.reset_timestamp
        cur_dt = self.reset_datetime
        last_time = getattr(self, "_last_reset_time", None)
        last_ts = getattr(self, "_last_reset_timestamp", None)
        last_dt = getattr(self, "_last_reset_datetime", None)

        # 1. Direct mutation of reset_datetime
        if cur_dt != last_dt:
            if cur_dt is None:
                self.reset_time = None
                self.reset_timestamp = None
                self._last_reset_time = None
                self._last_reset_timestamp = None
                self._last_reset_datetime = None
                return None
            norm_dt = parse_reset_time_to_datetime(cur_dt)
            self.reset_datetime = norm_dt
            if norm_dt is None:
                self.reset_time = None
                self.reset_timestamp = None
            else:
                if cur_time == last_time:
                    self.reset_time = norm_dt.isoformat()
                if cur_ts == last_ts:
                    self.reset_timestamp = norm_dt.timestamp()
            self._last_reset_time = self.reset_time
            self._last_reset_timestamp = self.reset_timestamp
            self._last_reset_datetime = norm_dt
            return norm_dt

        # 2. Fast-path: no mutations (hot loop)
        if cur_time == last_time and cur_ts == last_ts and cur_dt == last_dt:
            return cur_dt

        # 3. Dynamic mutation of reset_time or reset_timestamp
        if cur_time != last_time:
            res = cur_time
        elif cur_ts != last_ts:
            res = cur_ts
        else:
            res = cur_time if cur_time is not None else cur_ts

        if res is None:
            self.reset_time = None
            self.reset_timestamp = None
            self.reset_datetime = None
            self._last_reset_time = None
            self._last_reset_timestamp = None
            self._last_reset_datetime = None
            return None

        dt = parse_reset_time_to_datetime(res)
        self.reset_datetime = dt
        if dt is not None:
            if isinstance(cur_time, datetime):
                self.reset_time = dt.isoformat()
            elif cur_time == last_time and cur_ts != last_ts:
                self.reset_time = dt.isoformat()
            self.reset_timestamp = dt.timestamp()
        else:
            self.reset_timestamp = None
        self._last_reset_time = self.reset_time
        self._last_reset_timestamp = self.reset_timestamp
        self._last_reset_datetime = dt
        return dt

    def copy(self) -> "QuotaWindow":
        self._sync_reset_datetime()
        return QuotaWindow(
            name=self.name,
            duration_seconds=self.duration_seconds,
            remaining_percentage=self.remaining_percentage,
            reset_time=self.reset_time,
            reset_timestamp=self.reset_timestamp,
            reset_datetime=self.reset_datetime,
        )

    def clone(self) -> "QuotaWindow":
        return self.copy()

    def get_remaining_seconds(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> float:
        dt = self._sync_reset_datetime()
        if dt is None:
            return 0.0
        effective_now = now_dt if now_dt is not None else now
        now_dt_norm = _normalize_now_datetime(effective_now)
        if now_dt_norm is None:
            now_dt_norm = datetime.now(timezone.utc)
        return max(0.0, (dt - now_dt_norm).total_seconds())

    def remaining_time_seconds(self, now: Optional[Union[float, int, datetime]] = None) -> float:
        now_dt = _normalize_now_datetime(now)
        return self.get_remaining_seconds(now_dt)

    @property
    def quota_fraction(self) -> float:
        if self.remaining_percentage is None:
            return 1.0
        return max(0.0, min(1.0, float(self.remaining_percentage) / 100.0))

    def get_time_fraction(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> float:
        effective_now = now_dt if now_dt is not None else now
        rem_sec = self.get_remaining_seconds(effective_now)
        if self.duration_seconds <= 0:
            return 0.0
        return max(0.0, min(1.0, rem_sec / self.duration_seconds))

    def time_fraction(self, now: Optional[Union[float, int, datetime]] = None) -> float:
        now_dt = _normalize_now_datetime(now)
        return self.get_time_fraction(now_dt)

    def get_target_quota_fraction(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> float:
        """
        Calculate the target quota fraction threshold for linear pacing: remaining time fraction (y = x).
        """
        return self.get_time_fraction(now_dt=now_dt, now=now)

    def target_quota_fraction(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> float:
        return self.get_target_quota_fraction(now_dt=now_dt, now=now)

    def get_target_pacing_percentage(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> Optional[float]:
        """
        Calculate the target quota percentage threshold for linear pacing (y = x).
        Returns None if reset time is not set or duration is non-positive.
        """
        if self.duration_seconds <= 0:
            return None
        self._sync_reset_datetime()
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        if res is None:
            return None
        dt = getattr(self, "reset_datetime", None)
        if dt is None:
            return None
        frac = self.get_target_quota_fraction(now_dt=now_dt, now=now)
        return round(max(0.0, min(100.0, frac * 100.0)), 1)

    @property
    def target_pacing_percentage(self) -> Optional[float]:
        return self.get_target_pacing_percentage()

    def target_pacing_pct(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> Optional[float]:
        return self.get_target_pacing_percentage(now_dt=now_dt, now=now)

    def get_pacing_status(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> Tuple[str, float]:
        self._sync_reset_datetime()
        res = self.reset_time if self.reset_time is not None else self.reset_timestamp
        if res is None:
            return "OK", 0.0
        effective_now = now_dt if now_dt is not None else now
        q_frac = self.quota_fraction
        target_q_frac = self.get_target_quota_fraction(effective_now)

        if q_frac < target_q_frac:
            deficit = target_q_frac - q_frac
            backoff = round(max(0.0, deficit * 10.0), 1)
            return "BEHIND_PACING", backoff
        else:
            return "OK", 0.0

    def pacing_status(self, now: Optional[Union[float, int, datetime]] = None) -> str:
        now_dt = _normalize_now_datetime(now)
        status, _ = self.get_pacing_status(now_dt)
        return status

    def get_pacing_recovery_seconds(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> float:
        effective_now = now_dt if now_dt is not None else now
        norm_dt = _normalize_now_datetime(effective_now)
        pacing_status, _ = self.get_pacing_status(norm_dt)
        if pacing_status != "BEHIND_PACING":
            return 0.0
        rem_sec = self.get_remaining_seconds(norm_dt)
        q_frac = self.quota_fraction
        recovery = rem_sec - (q_frac * self.duration_seconds)
        return max(0.0, float(recovery))

    def pacing_recovery_seconds(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> float:
        return self.get_pacing_recovery_seconds(now_dt=now_dt, now=now)

    def format_pacing_countdown(
        self, now_dt: Optional[Union[float, int, datetime]] = None, now: Optional[Union[float, int, datetime]] = None
    ) -> str:
        effective_now = now_dt if now_dt is not None else now
        norm_dt = _normalize_now_datetime(effective_now)
        rec_sec = self.get_pacing_recovery_seconds(norm_dt)
        return format_pacing_recovery_countdown(rec_sec)

    def format_reset_countdown(
        self,
        now_dt: Optional[Union[float, int, datetime]] = None,
        now: Optional[Union[float, int, datetime]] = None,
    ) -> str:
        dt = self._sync_reset_datetime()
        target = dt if dt is not None else (self.reset_time if self.reset_time is not None else self.reset_timestamp)
        effective_now = now_dt if now_dt is not None else now
        norm_dt = _normalize_now_datetime(effective_now)
        return format_reset_countdown(target, now_dt=norm_dt, window_name=self.name)

    def to_dict(self) -> dict:
        pacing_status, backoff = self.get_pacing_status()
        return {
            "name": self.name,
            "duration_seconds": self.duration_seconds,
            "remaining_percentage": round(self.remaining_percentage, 1) if self.remaining_percentage is not None else None,
            "reset_time": self.reset_time,
            "reset_timestamp": self.reset_timestamp,
            "reset_countdown": self.format_reset_countdown(),
            "pacing_status": pacing_status,
            "target_pacing_percentage": self.get_target_pacing_percentage(),
            "backoff_delay": backoff,
            "pacing_recovery_seconds": round(self.get_pacing_recovery_seconds(), 1),
            "pacing_recovery_countdown": self.format_pacing_countdown(),
        }


@dataclass
class QuotaInfo:
    remaining_percentage: Optional[float] = 100.0
    state: str = QuotaState.NORMAL
    reset_time: Optional[float] = None
    active_backoff_delay: float = 0.0
    requests_remaining: Optional[int] = None
    tokens_remaining: Optional[int] = None
    window_5h: Optional[Union[QuotaWindow, dict]] = None
    window_1w: Optional[Union[QuotaWindow, dict]] = None
    quota_pool: str = "gemini"
    gemini_window_5h: Optional[Union[QuotaWindow, dict]] = None
    gemini_window_1w: Optional[Union[QuotaWindow, dict]] = None
    claude_window_5h: Optional[Union[QuotaWindow, dict]] = None
    claude_window_1w: Optional[Union[QuotaWindow, dict]] = None

    def to_dict(self) -> dict:
        d = {
            "quota_pool": self.quota_pool,
            "remaining_percentage": round(self.remaining_percentage, 1) if self.remaining_percentage is not None else None,
            "state": self.state,
            "reset_time": self.reset_time,
            "active_backoff_delay": round(self.active_backoff_delay, 2) if self.active_backoff_delay is not None else 0.0,
            "requests_remaining": self.requests_remaining,
            "tokens_remaining": self.tokens_remaining,
        }
        if self.window_5h is not None:
            if isinstance(self.window_5h, QuotaWindow):
                d["window_5h"] = self.window_5h.to_dict()
            else:
                d["window_5h"] = self.window_5h
        if self.window_1w is not None:
            if isinstance(self.window_1w, QuotaWindow):
                d["window_1w"] = self.window_1w.to_dict()
            else:
                d["window_1w"] = self.window_1w
        if self.gemini_window_5h is not None:
            if isinstance(self.gemini_window_5h, QuotaWindow):
                d["gemini_window_5h"] = self.gemini_window_5h.to_dict()
            else:
                d["gemini_window_5h"] = self.gemini_window_5h
        if self.gemini_window_1w is not None:
            if isinstance(self.gemini_window_1w, QuotaWindow):
                d["gemini_window_1w"] = self.gemini_window_1w.to_dict()
            else:
                d["gemini_window_1w"] = self.gemini_window_1w
        if self.claude_window_5h is not None:
            if isinstance(self.claude_window_5h, QuotaWindow):
                d["claude_window_5h"] = self.claude_window_5h.to_dict()
            else:
                d["claude_window_5h"] = self.claude_window_5h
        if self.claude_window_1w is not None:
            if isinstance(self.claude_window_1w, QuotaWindow):
                d["claude_window_1w"] = self.claude_window_1w.to_dict()
            else:
                d["claude_window_1w"] = self.claude_window_1w
        def _extract_win_metrics(w, default_name=None):
            if w is None:
                return None, None, None, "OK", None, 0.0, None
            if isinstance(w, QuotaWindow):
                st, _ = w.get_pacing_status()
                pct = round(w.remaining_percentage, 1) if w.remaining_percentage is not None else None
                tgt = w.get_target_pacing_percentage()
                rec_sec = round(w.get_pacing_recovery_seconds(), 1) if st == "BEHIND_PACING" else 0.0
                rec_cd = w.format_pacing_countdown() if st == "BEHIND_PACING" else None
                if rec_cd == "00:00:00":
                    rec_cd = None
                return pct, w.reset_time, w.format_reset_countdown(), st, tgt, rec_sec, rec_cd
            elif isinstance(w, dict):
                pct = w.get("remaining_percentage")
                if pct is not None:
                    try:
                        pct = round(float(pct), 1)
                    except (ValueError, TypeError):
                        pass
                res = w.get("reset_time")
                cd = w.get("reset_countdown")
                if cd is None and res is not None:
                    cd = format_reset_countdown(res, window_name=w.get("name") or default_name)
                st = w.get("pacing_status") or "OK"
                tgt = w.get("target_pacing_percentage")
                if st != "BEHIND_PACING":
                    rec_sec = 0.0
                    rec_cd = None
                else:
                    rec_sec = w.get("pacing_recovery_seconds")
                    if rec_sec is not None:
                        try:
                            rec_sec = float(rec_sec)
                            if math.isnan(rec_sec) or math.isinf(rec_sec):
                                rec_sec = 0.0
                        except (ValueError, TypeError):
                            rec_sec = 0.0
                    else:
                        rec_sec = 0.0
                    rec_cd = w.get("pacing_recovery_countdown")
                    if (not rec_cd or rec_cd == "00:00:00") and rec_sec > 0:
                        rec_cd = format_pacing_recovery_countdown(rec_sec)
                    if (not rec_cd or rec_cd == "00:00:00") and res is not None and pct is not None:
                        try:
                            win_name = w.get("name") or default_name or "5H"
                            dur = w.get("duration_seconds")
                            if dur is None:
                                dur = 18000.0 if str(win_name).upper() == "5H" else 604800.0
                            qw = QuotaWindow(
                                name=win_name,
                                duration_seconds=dur,
                                remaining_percentage=pct,
                                reset_time=res,
                            )
                            qw_sec = qw.get_pacing_recovery_seconds()
                            qw_cd = qw.format_pacing_countdown()
                            if rec_sec <= 0:
                                rec_sec = qw_sec
                            if not rec_cd or rec_cd == "00:00:00":
                                rec_cd = qw_cd
                        except Exception:
                            pass
                    if rec_cd == "00:00:00":
                        rec_cd = None
                    try:
                        rec_sec = float(rec_sec)
                        if math.isnan(rec_sec) or math.isinf(rec_sec):
                            rec_sec = 0.0
                        else:
                            rec_sec = round(rec_sec, 1)
                    except (ValueError, TypeError, OverflowError):
                        rec_sec = 0.0
                return pct, res, cd, st, tgt, rec_sec, rec_cd
            return None, None, None, "OK", None, 0.0, None

        p = str(self.quota_pool or "").lower()
        is_tp = "claude" in p or "gpt" in p or "3p" in p or "third" in p

        gemini_5h = self.gemini_window_5h if self.gemini_window_5h is not None else (None if is_tp else self.window_5h)
        gemini_1w = self.gemini_window_1w if self.gemini_window_1w is not None else (None if is_tp else self.window_1w)
        claude_5h = self.claude_window_5h if self.claude_window_5h is not None else (self.window_5h if is_tp else None)
        claude_1w = self.claude_window_1w if self.claude_window_1w is not None else (self.window_1w if is_tp else None)

        g5_pct, g5_res, g5_cd, g5_st, g5_tgt, g5_rec_sec, g5_rec_cd = _extract_win_metrics(gemini_5h, default_name="5H")
        g1_pct, g1_res, g1_cd, g1_st, g1_tgt, g1_rec_sec, g1_rec_cd = _extract_win_metrics(gemini_1w, default_name="1W")
        c5_pct, c5_res, c5_cd, c5_st, c5_tgt, c5_rec_sec, c5_rec_cd = _extract_win_metrics(claude_5h, default_name="5H")
        c1_pct, c1_res, c1_cd, c1_st, c1_tgt, c1_rec_sec, c1_rec_cd = _extract_win_metrics(claude_1w, default_name="1W")

        def _to_win_dict(w):
            if w is None:
                return None
            if isinstance(w, QuotaWindow):
                return w.to_dict()
            return w

        if gemini_5h is not None and "gemini_window_5h" not in d:
            d["gemini_window_5h"] = _to_win_dict(gemini_5h)
        if gemini_1w is not None and "gemini_window_1w" not in d:
            d["gemini_window_1w"] = _to_win_dict(gemini_1w)
        if claude_5h is not None and "claude_window_5h" not in d:
            d["claude_window_5h"] = _to_win_dict(claude_5h)
        if claude_1w is not None and "claude_window_1w" not in d:
            d["claude_window_1w"] = _to_win_dict(claude_1w)

        d["gemini_5h_remaining_percentage"] = g5_pct
        d["gemini_5h_reset_time"] = g5_res
        d["gemini_5h_countdown"] = g5_cd
        d["gemini_5h_pacing_status"] = g5_st
        d["gemini_5h_target_pacing_percentage"] = g5_tgt
        d["gemini_5h_pacing_recovery_seconds"] = g5_rec_sec
        d["gemini_5h_pacing_recovery_countdown"] = g5_rec_cd

        d["gemini_1w_remaining_percentage"] = g1_pct
        d["gemini_1w_reset_time"] = g1_res
        d["gemini_1w_countdown"] = g1_cd
        d["gemini_1w_pacing_status"] = g1_st
        d["gemini_1w_target_pacing_percentage"] = g1_tgt
        d["gemini_1w_pacing_recovery_seconds"] = g1_rec_sec
        d["gemini_1w_pacing_recovery_countdown"] = g1_rec_cd

        d["third_party_5h_remaining_percentage"] = c5_pct
        d["third_party_5h_reset_time"] = c5_res
        d["third_party_5h_countdown"] = c5_cd
        d["third_party_5h_pacing_status"] = c5_st
        d["third_party_5h_target_pacing_percentage"] = c5_tgt
        d["third_party_5h_pacing_recovery_seconds"] = c5_rec_sec
        d["third_party_5h_pacing_recovery_countdown"] = c5_rec_cd

        d["third_party_1w_remaining_percentage"] = c1_pct
        d["third_party_1w_reset_time"] = c1_res
        d["third_party_1w_countdown"] = c1_cd
        d["third_party_1w_pacing_status"] = c1_st
        d["third_party_1w_target_pacing_percentage"] = c1_tgt
        d["third_party_1w_pacing_recovery_seconds"] = c1_rec_sec
        d["third_party_1w_pacing_recovery_countdown"] = c1_rec_cd

        if g5_pct is not None and g1_pct is not None:
            d["gemini_remaining_percentage"] = min(g5_pct, g1_pct)
        elif g5_pct is not None:
            d["gemini_remaining_percentage"] = g5_pct
        elif g1_pct is not None:
            d["gemini_remaining_percentage"] = g1_pct
        elif not is_tp:
            d["gemini_remaining_percentage"] = d["remaining_percentage"]

        if c5_pct is not None and c1_pct is not None:
            d["third_party_remaining_percentage"] = min(c5_pct, c1_pct)
        elif c5_pct is not None:
            d["third_party_remaining_percentage"] = c5_pct
        elif c1_pct is not None:
            d["third_party_remaining_percentage"] = c1_pct
        elif is_tp:
            d["third_party_remaining_percentage"] = d["remaining_percentage"]

        return d


class _PacingStatusResult(str):
    def __new__(cls, tracker, now: Optional[Union[float, datetime]] = None):
        val = "BEHIND_PACING" if tracker.is_behind_pacing(now=now) else "OK"
        obj = super().__new__(cls, val)
        obj._tracker = tracker
        return obj

    def __call__(self, now: Optional[Union[float, datetime]] = None) -> str:
        return "BEHIND_PACING" if self._tracker.is_behind_pacing(now=now) else "OK"


