"""
Terminal UI Dashboard for Graviton Server.
"""

import collections
import logging
import re
import shutil
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, TextIO, Tuple, Union

try:
    import select
    import termios
    import tty
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False

from lib.scheduler import ScheduledJob, TaskScheduler
from lib.tasks import TaskManager, TaskStatus
from lib.quota import QuotaState, QuotaTracker, QuotaWindow, format_quota_badge
from lib.updater import get_git_info, get_hot_reload_state, get_uptime_str

ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class TUILogHandler(logging.Handler):
    """Log handler that buffers log records in a ring buffer for TUI display."""

    def __init__(self, max_records: int = 50, level=logging.NOTSET):
        super().__init__(level=level)
        self.records: collections.deque = collections.deque(maxlen=max_records)
        self.setFormatter(
            logging.Formatter(
                fmt="[%(asctime)s] %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            with self.lock:
                self.records.append(msg)
        except Exception:
            self.handleError(record)

    def get_logs(self, limit: int = 5) -> list:
        with self.lock:
            if limit <= 0:
                return []
            return list(self.records)[-limit:]

    def clear(self):
        with self.lock:
            self.records.clear()


def get_display_width(s: str) -> int:
    """Return visual display width of string, stripping ANSI escape codes and accounting for wide characters."""
    clean_str = ANSI_REGEX.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in clean_str)


def truncate_to_display_width(s: str, max_w: int) -> str:
    """Truncate string so its visual display width does not exceed max_w."""
    if get_display_width(s) <= max_w:
        return s

    res = []
    cur_w = 0
    i = 0
    n = len(s)
    has_ansi = False

    while i < n:
        match = ANSI_REGEX.match(s, i)
        if match:
            has_ansi = True
            res.append(match.group(0))
            i = match.end()
        else:
            c = s[i]
            w_c = 2 if unicodedata.east_asian_width(c) in ("F", "W") else 1
            if cur_w + w_c > max_w:
                if cur_w < max_w:
                    res.append(" ")
                    cur_w += 1
                break
            res.append(c)
            cur_w += w_c
            i += 1

    if has_ansi and (not res or not res[-1].endswith("\033[0m")):
        res.append("\033[0m")

    return "".join(res)


def pad_to_display_width(s: str, target_w: int, align: str = "left") -> str:
    """Pad string with spaces so its visual display width matches target_w."""
    cur_w = get_display_width(s)
    if cur_w >= target_w:
        return s
    pad_len = target_w - cur_w
    if align == "left":
        return s + (" " * pad_len)
    elif align == "right":
        return (" " * pad_len) + s
    else:
        left_pad = pad_len // 2
        right_pad = pad_len - left_pad
        return (" " * left_pad) + s + (" " * right_pad)


def fit_to_display_width(s: str, target_w: int, align: str = "left") -> str:
    """Truncate and pad string so its visual display width is exactly target_w."""
    return pad_to_display_width(truncate_to_display_width(s, target_w), target_w, align=align)


class TerminalDashboard:
    """
    Live terminal UI dashboard for Graviton server, displaying:
    - Header: host, port, git version, branch, server uptime, hot-reload state.
    - Antigravity Model Quota panel (level, state, reset time, backoff delay).
    - Active Tasks (RUNNING) panel.
    - Task Queue (QUEUED) panel.
    - Scheduled Jobs panel (periodic TaskScheduler status & upcoming jobs).
    - Approved Pull Requests panel.
    - Task History (COMPLETED/FAILED) panel.
    - Event Logs panel.
    """

    def __init__(
        self,
        task_manager: TaskManager,
        host: str = "0.0.0.0",
        port: int = 8000,
        repo_root: Optional[Path] = None,
        refresh_interval: float = 0.5,
        out_stream: Optional[TextIO] = None,
        enable_log_redirection: bool = True,
        log_file: Optional[Union[str, Path]] = "graviton.log",
        log_handler: Optional[TUILogHandler] = None,
        git_cache_ttl: float = 10.0,
        scheduler: Optional[TaskScheduler] = None,
        pr_tracker: Optional[Any] = None,
        quota_tracker: Optional[QuotaTracker] = None,
    ):
        self.task_manager = task_manager
        self.host = host
        self.port = port
        self.repo_root = repo_root
        self.refresh_interval = refresh_interval
        self.out_stream = out_stream or sys.stdout
        self.enable_log_redirection = enable_log_redirection
        self.git_cache_ttl = git_cache_ttl
        self.pr_tracker = pr_tracker
        self.scheduler = scheduler
        self.quota_tracker = quota_tracker

        if log_file is not None:
            log_path = Path(log_file)
            if not log_path.is_absolute() and repo_root:
                log_path = repo_root / log_path
            self.log_file: Optional[Path] = log_path
        else:
            self.log_file = None

        self.log_handler = log_handler or TUILogHandler()

        self.active_screen: str = "main"
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stdin_thread: Optional[threading.Thread] = None
        self._old_term_settings: Optional[Any] = None
        self._log_redirected = False
        self._detached_handlers: list = []
        self._file_handler: Optional[logging.FileHandler] = None

        self._git_info_cache: Optional[Tuple[str, str]] = None
        self._git_info_last_fetch: float = 0.0

    def start(self):
        """Start the background dashboard rendering loop thread and hotkey listener."""
        if self._running:
            return
        if self.enable_log_redirection:
            self._attach_log_redirection()
        self._running = True
        self._thread = threading.Thread(
            target=self._refresh_loop, daemon=True, name="DashboardTUI"
        )
        self._thread.start()
        self._start_stdin_listener()

    def stop(self):
        """Stop the dashboard rendering loop and stdin listener."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._stdin_thread and self._stdin_thread.is_alive():
            self._stdin_thread.join(timeout=1.0)
        self._restore_termios()
        if self.enable_log_redirection:
            self._detach_log_redirection()

    def _start_stdin_listener(self):
        """Start background stdin hotkey listener thread if running in an interactive TTY."""
        if not HAS_TERMIOS:
            return
        try:
            if not sys.stdin.isatty():
                return
        except Exception:
            return

        self._stdin_thread = threading.Thread(
            target=self._stdin_loop, daemon=True, name="DashboardStdinListener"
        )
        self._stdin_thread.start()

    def _stdin_loop(self):
        """Background thread reading character hotkeys from stdin."""
        if not HAS_TERMIOS:
            return
        try:
            if not sys.stdin.isatty():
                return
            fd = sys.stdin.fileno()
            self._old_term_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except Exception:
            self._old_term_settings = None
            return

        try:
            while self._running:
                rlist, _, _ = select.select([sys.stdin], [], [], 0.1)
                if rlist:
                    try:
                        ch = sys.stdin.read(1)
                    except Exception:
                        break
                    if not ch:
                        break
                    if ch in ("j", "J"):
                        if self.active_screen != "jobs":
                            self.active_screen = "jobs"
                            self._force_refresh()
                    elif ch == "\x1b":
                        if self.active_screen != "main":
                            self.active_screen = "main"
                            self._force_refresh()
        except Exception:
            pass
        finally:
            self._restore_termios()

    def _restore_termios(self):
        """Restore original terminal attributes if previously modified."""
        if self._old_term_settings is not None and HAS_TERMIOS:
            try:
                if sys.stdin.isatty():
                    fd = sys.stdin.fileno()
                    termios.tcsetattr(fd, termios.TCSADRAIN, self._old_term_settings)
            except Exception:
                pass
            self._old_term_settings = None

    def _force_refresh(self):
        """Force an immediate dashboard frame render and output flush."""
        try:
            if self._running and self.out_stream:
                frame = self.render()
                self.out_stream.write("\033[H\033[2J" + frame + "\n")
                self.out_stream.flush()
        except Exception:
            pass

    def invalidate_git_cache(self):
        """Invalidate cached git metadata to force a fresh fetch on next render."""
        self._git_info_cache = None
        self._git_info_last_fetch = 0.0

    def _attach_log_redirection(self):
        if self._log_redirected:
            return

        loggers_to_check = [logging.getLogger()] + [
            logging.getLogger(name)
            for name in logging.Logger.manager.loggerDict
            if isinstance(logging.Logger.manager.loggerDict[name], logging.Logger)
        ]

        self._detached_handlers = []
        for logger_obj in loggers_to_check:
            for handler in list(logger_obj.handlers):
                if (
                    isinstance(handler, logging.StreamHandler)
                    and not isinstance(handler, logging.FileHandler)
                    and handler is not self.log_handler
                    and handler is not self._file_handler
                ):
                    logger_obj.removeHandler(handler)
                    self._detached_handlers.append((logger_obj, handler))

        root_logger = logging.getLogger()
        if self.log_handler not in root_logger.handlers:
            root_logger.addHandler(self.log_handler)

        if self.log_file and self._file_handler is None:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(str(self.log_file), encoding="utf-8")
                file_handler.setFormatter(
                    logging.Formatter(
                        fmt="[%(asctime)s] %(levelname)s - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                self._file_handler = file_handler
                if self._file_handler not in root_logger.handlers:
                    root_logger.addHandler(self._file_handler)
            except Exception:
                pass

        self._log_redirected = True

    def _detach_log_redirection(self):
        if not self._log_redirected:
            return

        root_logger = logging.getLogger()

        if self.log_handler and self.log_handler in root_logger.handlers:
            root_logger.removeHandler(self.log_handler)

        if self._file_handler:
            if self._file_handler in root_logger.handlers:
                root_logger.removeHandler(self._file_handler)
            self._file_handler.close()
            self._file_handler = None

        for logger_obj, handler in self._detached_handlers:
            if handler not in logger_obj.handlers:
                logger_obj.addHandler(handler)
        self._detached_handlers = []

        self._log_redirected = False

    def render(self, width: Optional[int] = None) -> str:
        """Construct and return the dashboard frame string for active screen."""
        if width is None:
            try:
                cols = shutil.get_terminal_size((80, 24)).columns
            except Exception:
                cols = 80
            width = max(78, min(120, cols))

        lines = []

        now = time.time()
        if (
            self._git_info_cache is None
            or (now - self._git_info_last_fetch) >= self.git_cache_ttl
        ):
            self._git_info_cache = get_git_info(self.repo_root)
            self._git_info_last_fetch = now

        commit, branch = self._git_info_cache
        reload_state = get_hot_reload_state()
        uptime = get_uptime_str()
        stats = self.task_manager.get_stats()

        # 1. Header Panel
        lines.extend(self._render_header(width, commit, branch, reload_state, uptime))
        lines.append("")

        if self.active_screen == "jobs":
            # Dedicated Periodic Jobs Screen View
            banner_text = "Press [Esc] to return to Main Screen"
            banner_line = fit_to_display_width(f"\033[93m\033[1m{banner_text}\033[0m", width)
            lines.append(banner_line)
            lines.append("")
            lines.extend(self._render_scheduled_jobs(width, self.scheduler))
            return "\n".join(lines)

        # Main Screen Layout
        # 2. Antigravity Quota Panel
        lines.extend(self._render_quota_panel(width))
        lines.append("")

        # 3. Active Tasks Panel
        active_tasks = self.task_manager.get_active_tasks()
        lines.extend(self._render_active_tasks(width, active_tasks, stats["max_workers"]))
        lines.append("")

        # 4. Queued Tasks Panel
        queued_tasks = self.task_manager.get_queued_tasks()
        lines.extend(self._render_queued_tasks(width, queued_tasks))
        lines.append("")

        # 5. Approved Pull Requests Panel
        approved_prs = self.pr_tracker.get_approved_prs() if self.pr_tracker else []
        lines.extend(self._render_approved_prs(width, approved_prs))
        lines.append("")

        # 6. Task History Panel
        history_tasks = self.task_manager.get_task_history(limit=5)
        lines.extend(self._render_history_tasks(width, history_tasks, stats))
        lines.append("")

        # 7. Event Logs Panel
        lines.extend(self._render_event_logs(width))

        return "\n".join(lines)

    def _render_quota_panel(self, width: int) -> list:
        inner_w = width - 4
        quota_tracker = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)

        if quota_tracker:
            w_5h = getattr(
                quota_tracker,
                "window_5h",
                QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=quota_tracker.remaining_percentage),
            )
            w_1w = getattr(
                quota_tracker,
                "window_1w",
                QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0),
            )
        else:
            w_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=100.0)
            w_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)

        badge_5h_text = format_quota_badge(w_5h)
        badge_1w_text = format_quota_badge(w_1w)

        status_5h, _ = w_5h.get_pacing_status()
        status_1w, _ = w_1w.get_pacing_status()

        color_5h = (
            "\033[92m\033[1m"
            if status_5h == "OK" and w_5h.remaining_percentage > 0
            else ("\033[91m\033[1m" if w_5h.remaining_percentage == 0 else "\033[93m\033[1m")
        )
        color_1w = (
            "\033[92m\033[1m"
            if status_1w == "OK" and w_1w.remaining_percentage > 0
            else ("\033[91m\033[1m" if w_1w.remaining_percentage == 0 else "\033[93m\033[1m")
        )

        panel_title = " ANTIGRAVITY MODEL QUOTA "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)

        header_bar = (
            "┌─"
            + f"\033[96m\033[1m{panel_title}\033[0m"
            + ("─" * pad_len)
            + "┐"
        )

        body_line_1 = fit_to_display_width(f"{color_5h}{badge_5h_text}\033[0m", inner_w)
        body_line_2 = fit_to_display_width(f"{color_1w}{badge_1w_text}\033[0m", inner_w)

        return [
            header_bar,
            f"│ {body_line_1} │",
            f"│ {body_line_2} │",
            "└" + "─" * (width - 2) + "┘",
        ]

    def _render_header(
        self, width: int, commit: str, branch: str, reload_state: str, uptime: str
    ) -> list:
        state_colors = {
            "IDLE": "\033[92m\033[1m",
            "PULLING_GIT": "\033[93m\033[1m",
            "REBUILDING_CONTAINER": "\033[93m\033[1m",
            "RELOADING": "\033[95m\033[1m",
        }
        color_code = state_colors.get(reload_state, "\033[1m")
        reload_badge = f"{color_code}[ HOT-RELOAD: {reload_state} ]\033[0m"

        inner_w = width - 4
        title = "\033[96m\033[1m⚡ GRAVITON SERVER DASHBOARD ⚡\033[0m"

        title_dw = get_display_width(title)
        badge_dw = get_display_width(reload_badge)
        pad_len = max(1, inner_w - title_dw - badge_dw)
        line1_raw = f"{title}{' ' * pad_len}{reload_badge}"
        line1_content = fit_to_display_width(line1_raw, inner_w)

        info_line = f"Host: {self.host}:{self.port} │ Branch: {branch} │ Commit: {commit} │ Uptime: {uptime}"
        info_content = fit_to_display_width(info_line, inner_w)

        if self.active_screen == "jobs":
            nav_hint = "Nav: [Esc] Main Screen"
        else:
            nav_hint = "Nav: [j] Periodic Jobs"
        nav_content = fit_to_display_width(f"\033[93m\033[1m{nav_hint}\033[0m", inner_w)

        return [
            "┌" + "─" * (width - 2) + "┐",
            f"│ {line1_content} │",
            f"│ \033[2m{info_content}\033[0m │",
            f"│ {nav_content} │",
            "└" + "─" * (width - 2) + "┘",
        ]

    def _render_active_tasks(self, width: int, tasks: list, max_workers: int) -> list:
        inner_w = width - 4
        active_cnt = len(tasks)
        panel_title = f" ACTIVE TASKS (RUNNING) [{active_cnt}/{max_workers} Workers Active] "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)
        header_bar = "┌─" + f"\033[94m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

        res = [header_bar]
        if not tasks:
            msg = "(No active tasks currently running)"
            msg_styled = f"\033[2m{msg}\033[0m"
            res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
        else:
            col_hdr = (
                f"{fit_to_display_width('ID', 8)} "
                f"{fit_to_display_width('AGENT', 14)} "
                f"{fit_to_display_width('TARGET', 8)} "
                f"{fit_to_display_width('WORKER', 9)} "
                f"{fit_to_display_width('ATTEMPT', 7)} "
                f"{fit_to_display_width('ELAPSED', 9)} "
                f"{fit_to_display_width('PROMPT', 15)}"
            )
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
            for t in tasks:
                if get_display_width(t.prompt) > 15:
                    prompt_trunc = truncate_to_display_width(t.prompt, 13) + ".."
                else:
                    prompt_trunc = t.prompt
                id_str = fit_to_display_width(t.id, 8)
                agent_str = fit_to_display_width(t.agent, 14)
                target_str = fit_to_display_width(t.target_id or "-", 8)
                worker_str = fit_to_display_width(t.worker_thread_id or "-", 9)
                attempt_str = fit_to_display_width(f"{t.attempt}/{t.max_attempts}", 7)
                elapsed_str = fit_to_display_width(f"{t.elapsed_time:.1f}s", 9)
                prompt_str = fit_to_display_width(prompt_trunc, 15)
                row = f"{id_str} {agent_str} {target_str} {worker_str} {attempt_str} {elapsed_str} {prompt_str}"
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    def _render_queued_tasks(self, width: int, tasks: list) -> list:
        inner_w = width - 4
        queued_cnt = len(tasks)
        panel_title = f" TASK QUEUE (QUEUED) [{queued_cnt} Pending] "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)
        header_bar = "┌─" + f"\033[93m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

        res = [header_bar]
        if not tasks:
            msg = "(Task queue is empty)"
            msg_styled = f"\033[2m{msg}\033[0m"
            res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
        else:
            col_hdr = f"{fit_to_display_width('ID', 8)} {fit_to_display_width('AGENT', 15)} {fit_to_display_width('TARGET', 10)} {fit_to_display_width('WAIT', 10)} {fit_to_display_width('PROMPT', 26)}"
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
            for t in tasks:
                if get_display_width(t.prompt) > 26:
                    prompt_trunc = truncate_to_display_width(t.prompt, 24) + ".."
                else:
                    prompt_trunc = t.prompt
                id_str = fit_to_display_width(t.id, 8)
                agent_str = fit_to_display_width(t.agent, 15)
                target_str = fit_to_display_width(t.target_id or "-", 10)
                wait_str = fit_to_display_width(f"{t.wait_time:.1f}s", 10)
                prompt_str = fit_to_display_width(prompt_trunc, 26)
                row = f"{id_str} {agent_str} {target_str} {wait_str} {prompt_str}"
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    @staticmethod
    def _format_interval(sec: int) -> str:
        if sec >= 86400 and sec % 86400 == 0:
            return f"{sec // 86400}d"
        elif sec >= 3600 and sec % 3600 == 0:
            return f"{sec // 3600}h"
        elif sec >= 60 and sec % 60 == 0:
            return f"{sec // 60}m"
        else:
            return f"{sec}s"

    @staticmethod
    def _format_timestamp(ts: Optional[str]) -> str:
        if not ts:
            return "-"
        try:
            dt = datetime.fromisoformat(ts)
            return dt.strftime("%H:%M:%S")
        except Exception:
            return ts[:8]

    @staticmethod
    def _format_remaining(job: ScheduledJob, now_dt: datetime) -> str:
        if not job.enabled:
            return "DISABLED"

        next_dt = None
        if job.next_run:
            try:
                next_dt = datetime.fromisoformat(job.next_run)
                if next_dt.tzinfo is None:
                    next_dt = next_dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass

        if next_dt is None and job.last_run:
            try:
                last_dt = datetime.fromisoformat(job.last_run)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                next_dt = datetime.fromtimestamp(last_dt.timestamp() + job.interval_seconds, tz=timezone.utc)
            except Exception:
                pass

        if next_dt is None:
            return "DUE"

        rem_sec = (next_dt - now_dt).total_seconds()
        if rem_sec <= 0:
            return "DUE"

        if rem_sec >= 86400:
            d = int(rem_sec // 86400)
            h = int((rem_sec % 86400) // 3600)
            return f"in {d}d {h}h"
        elif rem_sec >= 3600:
            h = int(rem_sec // 3600)
            m = int((rem_sec % 3600) // 60)
            return f"in {h}h {m}m"
        elif rem_sec >= 60:
            m = int(rem_sec // 60)
            return f"in {m}m"
        else:
            s = int(rem_sec)
            return f"in {s}s"

    def _render_scheduled_jobs(self, width: int, scheduler: Optional[TaskScheduler]) -> list:
        inner_w = width - 4
        status_enabled = "ENABLED" if scheduler is not None else "DISABLED"
        status_running = "RUNNING" if (scheduler and scheduler.is_running()) else "STOPPED"
        panel_title = f" SCHEDULED JOBS [{status_enabled} | {status_running}] "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)
        header_bar = "┌─" + f"\033[96m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

        res = [header_bar]
        if not scheduler or not scheduler.jobs:
            if not scheduler:
                msg = "(Scheduler disabled)"
            else:
                msg = "(No scheduled jobs configured)"
            msg_styled = f"\033[2m{msg}\033[0m"
            res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
        else:
            col_hdr = f"{fit_to_display_width('JOB ID', 12)} {fit_to_display_width('NAME', 14)} {fit_to_display_width('AGENT', 16)} {fit_to_display_width('INTV', 4)} {fit_to_display_width('LAST RUN', 8)} {fit_to_display_width('NEXT RUN', 8)} {fit_to_display_width('REMAIN', 8)}"
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")

            now_dt = datetime.now(timezone.utc)
            for job in list(scheduler.jobs.values()):
                if get_display_width(job.job_id) > 12:
                    id_trunc = truncate_to_display_width(job.job_id, 10) + ".."
                else:
                    id_trunc = job.job_id

                if get_display_width(job.name) > 14:
                    name_trunc = truncate_to_display_width(job.name, 12) + ".."
                else:
                    name_trunc = job.name

                if get_display_width(job.agent) > 16:
                    agent_trunc = truncate_to_display_width(job.agent, 14) + ".."
                else:
                    agent_trunc = job.agent

                id_str = fit_to_display_width(id_trunc, 12)
                name_str = fit_to_display_width(name_trunc, 14)
                agent_str = fit_to_display_width(agent_trunc, 16)
                interval_str = fit_to_display_width(self._format_interval(job.interval_seconds), 4)
                last_run_str = fit_to_display_width(self._format_timestamp(job.last_run), 8)
                next_run_str = fit_to_display_width(self._format_timestamp(job.next_run), 8)
                rem_str = fit_to_display_width(self._format_remaining(job, now_dt), 8)
                row = f"{id_str} {name_str} {agent_str} {interval_str} {last_run_str} {next_run_str} {rem_str}"
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    def _render_approved_prs(self, width: int, approved_prs: list) -> list:
        inner_w = width - 4
        approved_cnt = len(approved_prs)
        panel_title = f" APPROVED PULL REQUESTS (READY TO MERGE) [{approved_cnt} Ready] "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)
        header_bar = "┌─" + f"\033[92m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

        res = [header_bar]
        if not approved_prs:
            msg = "(No approved PRs awaiting merge)"
            msg_styled = f"\033[2m{msg}\033[0m"
            res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
        else:
            pr_col_w = 8
            author_col_w = 15
            spacing = 3
            remaining = max(20, inner_w - pr_col_w - author_col_w - spacing)
            title_col_w = max(15, remaining // 2 - 2)
            url_col_w = remaining - title_col_w

            col_hdr = (
                f"{fit_to_display_width('PR #', pr_col_w)} "
                f"{fit_to_display_width('TITLE', title_col_w)} "
                f"{fit_to_display_width('AUTHOR', author_col_w)} "
                f"{fit_to_display_width('URL', url_col_w)}"
            )
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")

            for pr in approved_prs:
                num_str = f"#{pr.get('number', '')}"
                title_str = pr.get("title", "")
                author_str = pr.get("author", "")
                url_str = pr.get("url", "")

                pr_formatted = fit_to_display_width(num_str, pr_col_w)
                title_formatted = fit_to_display_width(title_str, title_col_w)
                author_formatted = fit_to_display_width(author_str, author_col_w)
                url_formatted = fit_to_display_width(url_str, url_col_w)

                row = f"{pr_formatted} {title_formatted} {author_formatted} {url_formatted}"
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    def _render_history_tasks(self, width: int, tasks: list, stats: dict) -> list:
        inner_w = width - 4
        passed = stats.get("completed", 0)
        failed = stats.get("failed", 0)
        panel_title = f" TASK HISTORY (COMPLETED & FAILED) [Passed: {passed} | Failed: {failed}] "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)
        header_bar = "┌─" + f"\033[95m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

        res = [header_bar]
        if not tasks:
            msg = "(No task history recorded yet)"
            msg_styled = f"\033[2m{msg}\033[0m"
            res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
        else:
            col_hdr = (
                f"{fit_to_display_width('ID', 8)} "
                f"{fit_to_display_width('STATUS', 11)} "
                f"{fit_to_display_width('AGENT', 14)} "
                f"{fit_to_display_width('ATTEMPT', 7)} "
                f"{fit_to_display_width('RETURN', 8)} "
                f"{fit_to_display_width('DURATION', 9)} "
                f"{fit_to_display_width('TARGET', 8)}"
            )
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
            for t in tasks:
                status_color = "\033[92m" if t.status == TaskStatus.COMPLETED else "\033[91m"
                id_str = fit_to_display_width(t.id, 8)
                status_str = fit_to_display_width(f"{status_color}{t.status}\033[0m", 11)
                agent_str = fit_to_display_width(t.agent, 14)
                attempt_str = fit_to_display_width(f"{t.attempt}/{t.max_attempts}", 7)
                ret_val = str(t.return_code) if t.return_code is not None else "-"
                ret_str = fit_to_display_width(ret_val, 8)
                dur_str = fit_to_display_width(f"{t.elapsed_time:.1f}s", 9)
                target_str = fit_to_display_width(t.target_id or "-", 8)
                row = f"{id_str} {status_str} {agent_str} {attempt_str} {ret_str} {dur_str} {target_str}"
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    def _render_event_logs(self, width: int) -> list:
        inner_w = width - 4
        panel_title = " EVENT LOGS "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)
        header_bar = "┌─" + f"\033[96m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

        res = [header_bar]
        recent_logs = self.log_handler.get_logs(limit=5) if self.log_handler else []
        if not recent_logs:
            msg = "(No event logs recorded yet)"
            msg_styled = f"\033[2m{msg}\033[0m"
            res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
        else:
            for log_entry in recent_logs:
                clean_entry = log_entry.replace("\r\n", " ").replace("\n", " ")
                log_styled = f"\033[2m{clean_entry}\033[0m"
                res.append(f"│ {fit_to_display_width(log_styled, inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    def _refresh_loop(self):
        while self._running:
            try:
                frame = self.render()
                self.out_stream.write("\033[H\033[2J" + frame + "\n")
                self.out_stream.flush()
            except Exception:
                pass
            time.sleep(self.refresh_interval)
