"""
Terminal UI Dashboard for Graviton Server.
"""

import collections
import logging
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, TextIO, Tuple, Union

try:
    import select
    import termios
    import tty
    HAS_TERMIOS = True
except ImportError:
    HAS_TERMIOS = False

from lib.quota import QuotaTracker
from lib.scheduler import ScheduledJob, TaskScheduler
from lib.tasks import TaskManager
from lib.tui_panels import (
    ANSI_REGEX,
    fit_to_display_width,
    format_interval,
    format_remaining,
    format_timestamp,
    get_display_width,
    pad_to_display_width,
    render_active_tasks_panel,
    render_approved_prs_panel,
    render_event_logs_panel,
    render_header_panel,
    render_history_tasks_panel,
    render_queued_tasks_panel,
    render_quota_panel,
    render_scheduled_jobs_panel,
    truncate_to_display_width,
)
from lib.updater import get_git_info, get_hot_reload_state, get_uptime_str


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
        self.selected_job_index: int = 0
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
        quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
        if quota_tr and hasattr(quota_tr, "start_background_polling"):
            quota_tr.start_background_polling()
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
        quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
        if quota_tr and hasattr(quota_tr, "stop_background_polling"):
            quota_tr.stop_background_polling()
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

    @staticmethod
    def _is_incomplete_escape_sequence(raw_bytes: bytes) -> bool:
        """Check if raw_bytes represents a partial/incomplete ANSI escape sequence or UTF-8 sequence at its end."""
        if not raw_bytes:
            return False
        last_esc_idx = raw_bytes.rfind(b"\x1b")
        if last_esc_idx != -1:
            tail = raw_bytes[last_esc_idx:]
            if len(tail) == 1:
                return True
            if tail.startswith(b"\x1b[") or tail.startswith(b"\x1bO"):
                if len(tail) == 2:
                    return True
                if not any(0x40 <= b <= 0x7E and b != 0x5B for b in tail[2:]):
                    return True
        try:
            raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            if e.reason == "unexpected end of data":
                return True
        return False

    @staticmethod
    def _split_incomplete_utf8_tail(raw_bytes: bytes) -> tuple[bytes, bytes]:
        """Split raw_bytes into (complete_prefix, incomplete_utf8_tail)."""
        if not raw_bytes:
            return raw_bytes, b""
        for k in range(1, min(4, len(raw_bytes) + 1)):
            tail = raw_bytes[-k:]
            try:
                tail.decode("utf-8")
            except UnicodeDecodeError as e:
                if e.reason == "unexpected end of data":
                    return raw_bytes[:-k], tail
                else:
                    continue
        return raw_bytes, b""

    @staticmethod
    def _split_incomplete_escape_tail(raw_bytes: bytes) -> tuple[bytes, bytes]:
        """Split raw_bytes into (complete_prefix, incomplete_escape_tail).

        If raw_bytes ends with an incomplete multi-byte escape sequence prefix (where len(tail) > 1,
        e.g. b"\\x1b[" or b"\\x1bO"), the incomplete tail is returned to be retained in leftover_bytes.
        Single-byte b"\\x1b" (len(tail) == 1) is not split and remains in prefix to be processed
        immediately as standalone ESC after timeout.
        """
        if not raw_bytes:
            return raw_bytes, b""
        last_esc_idx = raw_bytes.rfind(b"\x1b")
        if last_esc_idx != -1:
            tail = raw_bytes[last_esc_idx:]
            if len(tail) > 1:
                if tail.startswith(b"\x1b[") or tail.startswith(b"\x1bO"):
                    if len(tail) == 2 or not any(0x40 <= b <= 0x7E and b != 0x5B for b in tail[2:]):
                        return raw_bytes[:last_esc_idx], tail
        return raw_bytes, b""

    @staticmethod
    def _parse_keys(raw_bytes: bytes) -> list:
        """Tokenize raw input bytes into individual keys or ANSI escape sequences.

        Handles multi-byte UTF-8 sequences by inspecting character length prior to slicing,
        and uses errors='ignore' when decoding recognized non-standard or malformed ESC escape
        sequences to safely bypass unparseable byte sequences.
        """
        if not raw_bytes:
            return []
        keys = []
        i = 0
        n = len(raw_bytes)
        while i < n:
            b = raw_bytes[i]
            if b == 0x1B:  # ESC
                if i + 1 >= n:
                    keys.append("\x1b")
                    i += 1
                else:
                    next_b = raw_bytes[i + 1]
                    if next_b in (0x5B, 0x4F):  # '[' or 'O'
                        term_idx = -1
                        end_idx = n
                        for j in range(i + 2, n):
                            if raw_bytes[j] == 0x1B:
                                end_idx = j
                                break
                            if 0x40 <= raw_bytes[j] <= 0x7E and raw_bytes[j] != 0x5B:
                                term_idx = j
                                break
                        if term_idx != -1:
                            seq = raw_bytes[i : term_idx + 1].decode("utf-8", errors="ignore")
                            keys.append(seq)
                            i = term_idx + 1
                        else:
                            seq = raw_bytes[i : end_idx].decode("utf-8", errors="ignore")
                            keys.append(seq)
                            i = end_idx
                    elif next_b == 0x1B:
                        keys.append("\x1b")
                        i += 1
                    else:
                        char_len = 1
                        for length in range(1, min(5, n - (i + 1) + 1)):
                            try:
                                chunk = raw_bytes[i + 1 : i + 1 + length].decode("utf-8")
                                if len(chunk) == 1:
                                    char_len = length
                                    break
                            except UnicodeDecodeError:
                                continue
                        seq = raw_bytes[i : i + 1 + char_len].decode("utf-8", errors="ignore")
                        keys.append(seq)
                        i += 1 + char_len
            else:
                decoded_ch = None
                decoded_len = 0
                for length in range(1, min(5, n - i + 1)):
                    try:
                        chunk = raw_bytes[i : i + length].decode("utf-8")
                        if len(chunk) == 1:
                            decoded_ch = chunk
                            decoded_len = length
                            break
                    except UnicodeDecodeError:
                        continue
                if decoded_ch is not None:
                    keys.append(decoded_ch)
                    i += decoded_len
                else:
                    is_incomplete = False
                    try:
                        raw_bytes[i:].decode("utf-8")
                    except UnicodeDecodeError as e:
                        if e.reason == "unexpected end of data":
                            is_incomplete = True
                    if is_incomplete:
                        break
                    i += 1
        return keys

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

        leftover_bytes = b""
        try:
            while self._running:
                try:
                    rlist, _, _ = select.select([fd], [], [], 0.1)
                except (BlockingIOError, InterruptedError):
                    time.sleep(0.01)
                    continue
                except Exception:
                    break

                if rlist:
                    try:
                        chunk = os.read(fd, 32)
                    except (BlockingIOError, InterruptedError):
                        time.sleep(0.01)
                        continue
                    except Exception:
                        break

                    if not chunk:
                        break

                    raw_bytes = leftover_bytes + chunk
                    leftover_bytes = b""

                    while self._running and self._is_incomplete_escape_sequence(raw_bytes):
                        try:
                            rlist_seq, _, _ = select.select([fd], [], [], 0.05)
                        except (BlockingIOError, InterruptedError):
                            time.sleep(0.01)
                            continue
                        except Exception:
                            break

                        if rlist_seq:
                            try:
                                seq_bytes = os.read(fd, 31)
                                if not seq_bytes:
                                    break
                                raw_bytes = raw_bytes + seq_bytes
                            except (BlockingIOError, InterruptedError):
                                time.sleep(0.01)
                                continue
                            except Exception:
                                break
                        else:
                            break

                    raw_bytes, leftover_esc = self._split_incomplete_escape_tail(raw_bytes)
                    prefix, leftover_utf8 = self._split_incomplete_utf8_tail(raw_bytes)
                    leftover_bytes = leftover_utf8 + leftover_esc
                    for key in self._parse_keys(prefix):
                        self.handle_key(key)
                else:
                    if leftover_bytes:
                        for key in self._parse_keys(leftover_bytes):
                            self.handle_key(key)
                        leftover_bytes = b""
        except Exception:
            pass
        finally:
            self._restore_termios()

    def select_next_job(self):
        """Select next scheduled job in the TUI selector."""
        if not self.scheduler or not self.scheduler.jobs:
            return
        num_jobs = len(self.scheduler.jobs)
        if num_jobs > 0:
            self.selected_job_index = min(self.selected_job_index + 1, num_jobs - 1)

    def select_prev_job(self):
        """Select previous scheduled job in the TUI selector."""
        if not self.scheduler or not self.scheduler.jobs:
            return
        if self.selected_job_index > 0:
            self.selected_job_index -= 1

    def enable_selected_job(self) -> Optional[ScheduledJob]:
        """Enable the currently selected scheduled job and save config."""
        if not self.scheduler or not self.scheduler.jobs:
            return None
        jobs_list = list(self.scheduler.jobs.values())
        if 0 <= self.selected_job_index < len(jobs_list):
            job = jobs_list[self.selected_job_index]
            job.enabled = True
            self.scheduler.save_config()
            return job
        return None

    def disable_selected_job(self) -> Optional[ScheduledJob]:
        """Disable the currently selected scheduled job and save config."""
        if not self.scheduler or not self.scheduler.jobs:
            return None
        jobs_list = list(self.scheduler.jobs.values())
        if 0 <= self.selected_job_index < len(jobs_list):
            job = jobs_list[self.selected_job_index]
            job.enabled = False
            self.scheduler.save_config()
            return job
        return None

    def toggle_selected_job(self) -> Optional[ScheduledJob]:
        """Toggle enable/disable state for the currently selected scheduled job and save config."""
        if not self.scheduler or not self.scheduler.jobs:
            return None
        jobs_list = list(self.scheduler.jobs.values())
        if 0 <= self.selected_job_index < len(jobs_list):
            job = jobs_list[self.selected_job_index]
            job.enabled = not job.enabled
            self.scheduler.save_config()
            return job
        return None

    def run_selected_job_now(self) -> bool:
        """Immediately execute the currently selected scheduled job."""
        if not self.scheduler or not self.scheduler.jobs:
            return False
        jobs_list = list(self.scheduler.jobs.values())
        if 0 <= self.selected_job_index < len(jobs_list):
            job = jobs_list[self.selected_job_index]
            return self.scheduler.trigger_job(job.job_id)
        return False

    def handle_key(self, key: str):
        """Handle hotkey or navigation key press."""
        if self.active_screen == "jobs":
            if key in ("k", "K", "up", "\x1b[A", "\x1bOA"):
                self.select_prev_job()
            elif key in ("j", "J", "down", "\x1b[B", "\x1bOB"):
                self.select_next_job()
            elif key in ("e", "E"):
                self.enable_selected_job()
            elif key in ("d", "D"):
                self.disable_selected_job()
            elif key == " ":
                self.toggle_selected_job()
            elif key in ("r", "R"):
                self.run_selected_job_now()
            elif key in ("\x1b", "esc", "ESC"):
                self.active_screen = "main"
        elif self.active_screen == "logs":
            if key in ("\x1b", "esc", "ESC"):
                self.active_screen = "main"
        elif self.active_screen == "main":
            if key in ("j", "J"):
                self.active_screen = "jobs"
            elif key in ("e", "E"):
                self.active_screen = "logs"

        self._force_refresh()

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
            banner_text = "Press [Esc] to return to Main Screen │ Controls: [↑/↓ or j/k] Select │ [Space] Toggle │ [e/d] Enable/Disable │ [r] Run"
            banner_line = fit_to_display_width(f"\033[93m\033[1m{banner_text}\033[0m", width)
            lines.append(banner_line)
            lines.append("")
            lines.extend(self._render_scheduled_jobs(width, self.scheduler))
            return "\n".join(lines)

        if self.active_screen == "logs":
            # Dedicated Event Logs Screen View
            banner_text = "Press [Esc] to return to Main Screen"
            banner_line = fit_to_display_width(f"\033[93m\033[1m{banner_text}\033[0m", width)
            lines.append(banner_line)
            lines.append("")
            lines.extend(self._render_event_logs(width, limit=15))
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

        return "\n".join(lines)

    def _render_quota_panel(self, width: int) -> list:
        return render_quota_panel(width, self.quota_tracker, self.task_manager)

    def _render_header(
        self, width: int, commit: str, branch: str, reload_state: str, uptime: str
    ) -> list:
        return render_header_panel(
            width, self.host, self.port, commit, branch, reload_state, uptime, self.active_screen
        )

    def _render_active_tasks(self, width: int, tasks: list, max_workers: int) -> list:
        return render_active_tasks_panel(width, tasks, max_workers)

    def _render_queued_tasks(self, width: int, tasks: list) -> list:
        return render_queued_tasks_panel(width, tasks)

    @staticmethod
    def _format_interval(sec: int) -> str:
        return format_interval(sec)

    @staticmethod
    def _format_timestamp(ts: Optional[str]) -> str:
        return format_timestamp(ts)

    @staticmethod
    def _format_remaining(job: ScheduledJob, now_dt: datetime) -> str:
        return format_remaining(job, now_dt)

    def _render_scheduled_jobs(
        self, width: int, scheduler: Optional[TaskScheduler], mode: str = "card"
    ) -> list:
        if scheduler and scheduler.jobs:
            jobs_list = list(scheduler.jobs.values())
            if jobs_list:
                self.selected_job_index = max(0, min(self.selected_job_index, len(jobs_list) - 1))
            else:
                self.selected_job_index = 0
        else:
            self.selected_job_index = 0
        return render_scheduled_jobs_panel(width, scheduler, self.selected_job_index, mode=mode)

    def _render_approved_prs(self, width: int, approved_prs: list) -> list:
        return render_approved_prs_panel(width, approved_prs)

    def _render_history_tasks(self, width: int, tasks: list, stats: dict) -> list:
        return render_history_tasks_panel(width, tasks, stats)

    def _render_event_logs(self, width: int, limit: int = 15) -> list:
        return render_event_logs_panel(width, self.log_handler, limit)

    def _refresh_loop(self):
        while self._running:
            try:
                frame = self.render()
                self.out_stream.write("\033[H\033[2J" + frame + "\n")
                self.out_stream.flush()
            except Exception:
                pass
            time.sleep(self.refresh_interval)
