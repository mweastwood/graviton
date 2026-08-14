"""
Terminal UI Dashboard for Graviton Server.
"""

import atexit
import collections
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from contextlib import nullcontext
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
    format_wait_time,
    get_display_width,
    pad_to_display_width,
    render_active_tasks_panel,
    render_approved_prs_panel,
    render_event_logs_panel,
    render_gemini_models_panel,
    render_header_panel,
    render_history_tasks_panel,
    render_queued_tasks_panel,
    render_quota_panel,
    render_scheduled_jobs_panel,
    render_third_party_models_panel,
    truncate_to_display_width,
)
from lib.updater import get_git_info, get_hot_reload_state, set_hot_reload_state, get_uptime_str


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


def run_graceful_shutdown(
    task_manager: Optional[TaskManager] = None,
    scheduler: Optional[TaskScheduler] = None,
    dashboard: Optional[Any] = None,
    httpd: Optional[Any] = None,
    grace_period: float = 3.0,
    timeout: Optional[float] = None,
    on_quit: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Execute 4-step graceful shutdown sequence:
    1. Drain Active Tasks (task_manager.drain_active_tasks)
    2. Webhook Grace Buffer (sleep grace_period seconds)
    3. Shutdown HTTP Listener (httpd.shutdown) & Persist Task Queue (task_manager.dump_queue_state)
    4. Clean Abort & Termination Teardown (scheduler, on_quit, httpd.server_close, dashboard, task_manager)
    """
    log = logger or logging.getLogger("graviton")
    try:
        # Step 1: Drain Active Tasks
        set_hot_reload_state("SHUTDOWN: DRAINING_TASKS")
        log.info("Graceful shutdown Step 1/4: Draining active tasks...")
        if task_manager:
            try:
                task_manager.drain_active_tasks(timeout=timeout)
            except Exception as e:
                log.warning(f"Error draining active tasks during shutdown: {e}")

        # Step 2: Webhook Grace Buffer
        if grace_period > 0:
            set_hot_reload_state("SHUTDOWN: WAITING_WEBHOOKS")
            log.info(f"Graceful shutdown Step 2/4: Waiting {grace_period:.1f}s webhook grace buffer...")
            time.sleep(grace_period)

        # Step 3: Shutdown HTTP Listener & Persist Task Queue
        set_hot_reload_state("SHUTDOWN: PERSISTING_QUEUE")
        log.info("Graceful shutdown Step 3/4: Closing HTTP listener and persisting task queue state...")
        if httpd:
            try:
                httpd.shutdown()
            except Exception as e:
                log.warning(f"Error shutting down HTTP server: {e}")

        if task_manager:
            try:
                task_manager.dump_queue_state()
            except Exception as e:
                log.warning(f"Error dumping task queue state: {e}")

    finally:
        # Step 4: Clean Abort & Termination Teardown
        log.info("Graceful shutdown Step 4/4: Clean abort & termination...")
        if scheduler:
            try:
                scheduler.stop()
            except Exception as e:
                log.warning(f"Error stopping scheduler during shutdown: {e}")

        if on_quit:
            try:
                on_quit()
            except Exception as e:
                log.warning(f"Error in on_quit callback during shutdown: {e}")

        if httpd:
            try:
                httpd.server_close()
            except Exception as e:
                log.warning(f"Error closing HTTP server socket during shutdown: {e}")

        if dashboard:
            try:
                dashboard.stop()
            except Exception as e:
                log.warning(f"Error stopping dashboard during shutdown: {e}")

        if task_manager:
            try:
                task_manager.stop()
            except Exception as e:
                log.warning(f"Error stopping task_manager during shutdown: {e}")

        set_hot_reload_state("IDLE")


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
        quit_grace_period: float = 3.0,
        httpd: Optional[Any] = None,
        on_quit: Optional[Any] = None,
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
        self.quit_grace_period = quit_grace_period
        self.httpd = httpd
        self.on_quit = on_quit
        self._is_shutting_down = False
        self._shutdown_thread: Optional[threading.Thread] = None
        self._shutdown_lock = threading.Lock()

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
        self.selected_queue_index: int = 0
        self.selected_gemini_index: int = 0
        self.selected_third_party_index: int = 0
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._stdin_thread: Optional[threading.Thread] = None
        self._old_term_settings: Optional[Any] = None
        self._termios_restored = False
        self._log_redirected = False
        self._detached_handlers: list = []
        self._file_handler: Optional[logging.FileHandler] = None
        self._atexit_registered = False
        self._signals_registered = False
        self._old_signal_handlers: dict = {}

        self._git_info_cache: Optional[Tuple[str, str]] = None
        self._git_info_last_fetch: float = 0.0
        self._need_refresh: bool = False
        self._refresh_event: threading.Event = threading.Event()

    def start(self):
        """Start the background dashboard rendering loop thread and hotkey listener."""
        if self._running:
            return
        self._termios_restored = False
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        self._register_signal_handlers()
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
        if hasattr(self, "_refresh_event"):
            self._refresh_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._stdin_thread and self._stdin_thread.is_alive():
            self._stdin_thread.join(timeout=1.0)
        self._restore_termios()
        self._unregister_signal_handlers()
        if self._atexit_registered:
            try:
                atexit.unregister(self.stop)
            except Exception:
                pass
            self._atexit_registered = False
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
        if not self.scheduler:
            return
        with getattr(self.scheduler, "_lock", nullcontext()):
            num_jobs = len(self.scheduler.jobs) if self.scheduler.jobs else 0
            if num_jobs > 0:
                self.selected_job_index = min(self.selected_job_index + 1, num_jobs - 1)

    def select_prev_job(self):
        """Select previous scheduled job in the TUI selector."""
        if not self.scheduler:
            return
        with getattr(self.scheduler, "_lock", nullcontext()):
            num_jobs = len(self.scheduler.jobs) if self.scheduler.jobs else 0
            if self.selected_job_index > 0:
                self.selected_job_index -= 1

    def select_next_queued_task(self):
        """Select next queued task in the TUI queue selector."""
        if not self.task_manager:
            return
        queued_tasks = self.task_manager.get_queued_tasks()
        num_tasks = len(queued_tasks)
        if num_tasks > 0:
            self.selected_queue_index = min(self.selected_queue_index + 1, num_tasks - 1)
        else:
            self.selected_queue_index = 0

    def select_prev_queued_task(self):
        """Select previous queued task in the TUI queue selector."""
        if not self.task_manager:
            return
        if self.selected_queue_index > 0:
            self.selected_queue_index -= 1

    def prioritize_selected_task(self) -> bool:
        """Bump priority of the currently selected queued task and retain cursor tracking on prioritized task."""
        if not self.task_manager:
            return False
        queued_tasks = self.task_manager.get_queued_tasks()
        if not queued_tasks:
            self.selected_queue_index = 0
            return False
        self.selected_queue_index = max(0, min(self.selected_queue_index, len(queued_tasks) - 1))
        task = queued_tasks[self.selected_queue_index]
        res = self.task_manager.prioritize_task(task.id, priority_bump=1)
        if res:
            new_queued_tasks = self.task_manager.get_queued_tasks()
            for idx, t in enumerate(new_queued_tasks):
                if t.id == task.id:
                    self.selected_queue_index = idx
                    break
        return res

    def enable_selected_job(self) -> Optional[ScheduledJob]:
        """Enable the currently selected scheduled job and save state."""
        if not self.scheduler:
            return None
        with getattr(self.scheduler, "_lock", nullcontext()):
            if not self.scheduler.jobs:
                return None
            jobs_list = list(self.scheduler.jobs.values())
            if 0 <= self.selected_job_index < len(jobs_list):
                job = jobs_list[self.selected_job_index]
                job.enabled = True
                self.scheduler.save_state()
                return job
        return None

    def disable_selected_job(self) -> Optional[ScheduledJob]:
        """Disable the currently selected scheduled job and save state."""
        if not self.scheduler:
            return None
        with getattr(self.scheduler, "_lock", nullcontext()):
            if not self.scheduler.jobs:
                return None
            jobs_list = list(self.scheduler.jobs.values())
            if 0 <= self.selected_job_index < len(jobs_list):
                job = jobs_list[self.selected_job_index]
                job.enabled = False
                self.scheduler.save_state()
                return job
        return None

    def toggle_selected_job(self) -> Optional[ScheduledJob]:
        """Toggle enable/disable state for the currently selected scheduled job and save state."""
        if not self.scheduler:
            return None
        with getattr(self.scheduler, "_lock", nullcontext()):
            if not self.scheduler.jobs:
                return None
            jobs_list = list(self.scheduler.jobs.values())
            if 0 <= self.selected_job_index < len(jobs_list):
                job = jobs_list[self.selected_job_index]
                job.enabled = not job.enabled
                self.scheduler.save_state()
                return job
        return None

    def pause(self):
        """Pause task acceptance in TaskManager."""
        if self.task_manager:
            self.task_manager.pause()
        self._force_refresh()

    def resume(self):
        """Resume task acceptance in TaskManager."""
        if self.task_manager:
            self.task_manager.resume()
        self._force_refresh()

    def toggle_pause(self):
        """Toggle pause/resume state of task acceptance in TaskManager."""
        if self.task_manager:
            self.task_manager.toggle_pause()
        self._force_refresh()

    def run_selected_job_now(self) -> bool:
        """Immediately execute the currently selected scheduled job."""
        if not self.scheduler:
            return False
        with getattr(self.scheduler, "_lock", nullcontext()):
            if not self.scheduler.jobs:
                return False
            jobs_list = list(self.scheduler.jobs.values())
            if 0 <= self.selected_job_index < len(jobs_list):
                job = jobs_list[self.selected_job_index]
                job_id = job.job_id
            else:
                return False
        return self.scheduler.trigger_job(job_id)

    def quit(
        self, timeout: Optional[float] = None, grace_period: Optional[float] = None
    ) -> threading.Thread:
        """Trigger graceful server shutdown in a background thread."""
        return self.graceful_shutdown(timeout=timeout, grace_period=grace_period)

    def graceful_shutdown(
        self, timeout: Optional[float] = None, grace_period: Optional[float] = None
    ) -> threading.Thread:
        """
        Execute 4-step graceful shutdown in a background thread:
        1. Drain active tasks (task_manager.drain_active_tasks)
        2. Webhook grace buffer (sleep for quit_grace_period)
        3. Shutdown HTTP Listener (httpd.shutdown) & Persist task queue (task_manager.dump_queue_state)
        4. Clean abort & termination (stop scheduler, on_quit, httpd, dashboard, task_manager)
        """
        with self._shutdown_lock:
            if self._is_shutting_down and self._shutdown_thread is not None:
                return self._shutdown_thread
            self._is_shutting_down = True

            gp = grace_period if grace_period is not None else getattr(self, "quit_grace_period", 3.0)

            def _sequence():
                run_graceful_shutdown(
                    task_manager=self.task_manager,
                    scheduler=self.scheduler,
                    dashboard=self,
                    httpd=self.httpd,
                    grace_period=gp,
                    timeout=timeout,
                    on_quit=self.on_quit,
                    logger=logging.getLogger("graviton.tui"),
                )

            t = threading.Thread(target=_sequence, daemon=True, name="TUIGracefulShutdown")
            self._shutdown_thread = t
            t.start()
            return t

    def handle_key(self, key: str):
        """Handle hotkey or navigation key press."""
        if key in ("q", "Q"):
            self.quit()
            self._force_refresh()
            return

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
        elif self.active_screen == "gemini_models":
            quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
            models = (
                quota_tr.available_gemini_models
                if quota_tr and hasattr(quota_tr, "available_gemini_models")
                else ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro"]
            )
            if key in ("k", "K", "up", "\x1b[A", "\x1bOA"):
                self.selected_gemini_index = max(0, self.selected_gemini_index - 1)
            elif key in ("j", "J", "down", "\x1b[B", "\x1bOB"):
                self.selected_gemini_index = min(len(models) - 1, self.selected_gemini_index + 1)
            elif key in (" ", "\r", "\n", "enter", "ENTER"):
                if 0 <= self.selected_gemini_index < len(models):
                    chosen = models[self.selected_gemini_index]
                    if quota_tr and hasattr(quota_tr, "set_active_model"):
                        quota_tr.set_active_model("gemini", chosen)
                    elif quota_tr:
                        quota_tr.active_gemini_model = chosen
            elif key in ("\x1b", "esc", "ESC"):
                self.active_screen = "main"
        elif self.active_screen == "third_party_models":
            quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
            models = (
                quota_tr.available_third_party_models
                if quota_tr and hasattr(quota_tr, "available_third_party_models")
                else ["claude-3-5-sonnet", "claude-3-opus", "claude-3-5-haiku"]
            )
            if key in ("k", "K", "up", "\x1b[A", "\x1bOA"):
                self.selected_third_party_index = max(0, self.selected_third_party_index - 1)
            elif key in ("j", "J", "down", "\x1b[B", "\x1bOB"):
                self.selected_third_party_index = min(len(models) - 1, self.selected_third_party_index + 1)
            elif key in (" ", "\r", "\n", "enter", "ENTER"):
                if 0 <= self.selected_third_party_index < len(models):
                    chosen = models[self.selected_third_party_index]
                    if quota_tr and hasattr(quota_tr, "set_active_model"):
                        quota_tr.set_active_model("claude_gpt", chosen)
                    elif quota_tr:
                        quota_tr.active_third_party_model = chosen
            elif key in ("\x1b", "esc", "ESC"):
                self.active_screen = "main"
        elif self.active_screen == "main":
            if key in ("up", "\x1b[A", "\x1bOA"):
                self.select_prev_queued_task()
            elif key in ("down", "\x1b[B", "\x1bOB"):
                self.select_next_queued_task()
            elif key in ("p", "P"):
                self.prioritize_selected_task()
            elif key in ("g", "G"):
                self.active_screen = "gemini_models"
                quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
                models = (
                    quota_tr.available_gemini_models
                    if quota_tr and hasattr(quota_tr, "available_gemini_models")
                    else ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro"]
                )
                active = (
                    quota_tr.get_active_model("gemini")
                    if quota_tr and hasattr(quota_tr, "get_active_model")
                    else "gemini-2.5-flash"
                )
                if active in models:
                    self.selected_gemini_index = models.index(active)
                else:
                    self.selected_gemini_index = 0

            elif key in ("c", "C"):
                self.active_screen = "third_party_models"
                quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
                models = (
                    quota_tr.available_third_party_models
                    if quota_tr and hasattr(quota_tr, "available_third_party_models")
                    else ["claude-3-5-sonnet", "claude-3-opus", "claude-3-5-haiku"]
                )
                active = (
                    quota_tr.get_active_model("claude_gpt")
                    if quota_tr and hasattr(quota_tr, "get_active_model")
                    else "claude-3-5-sonnet"
                )
                if active in models:
                    self.selected_third_party_index = models.index(active)
                else:
                    self.selected_third_party_index = 0
            elif key in ("j", "J"):
                self.active_screen = "jobs"
            elif key in ("e", "E"):
                self.active_screen = "logs"
            elif key in ("v", "V"):
                self.toggle_pause()

        self._force_refresh()

    def _restore_termios(self):
        """Restore original terminal attributes if previously modified, restore cursor, and reset formatting."""
        if self._old_term_settings is not None and HAS_TERMIOS:
            try:
                if sys.stdin.isatty():
                    fd = sys.stdin.fileno()
                    termios.tcsetattr(fd, termios.TCSAFLUSH, self._old_term_settings)
            except Exception:
                pass
            self._old_term_settings = None

        if not self._termios_restored:
            if self.out_stream:
                try:
                    self.out_stream.write("\033[?25h\033[0m")
                    self.out_stream.flush()
                except Exception:
                    pass
            self._termios_restored = True

    def _register_signal_handlers(self):
        """Register SIGINT and SIGTERM handlers to restore terminal state on exit signals."""
        if self._signals_registered:
            return
        self._signals_registered = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev_handler = signal.getsignal(sig)
                if prev_handler in (signal.SIG_IGN, None):
                    continue

                def _make_handler(sig_num, orig_h):
                    def _signal_handler(signum, frame):
                        self._restore_termios()
                        try:
                            if callable(orig_h) and orig_h not in (signal.SIG_DFL, signal.SIG_IGN):
                                orig_h(signum, frame)
                            elif signum == signal.SIGINT:
                                self.stop()
                                raise KeyboardInterrupt()
                            else:
                                self.stop()
                                sys.exit(128 + signum)
                        except BaseException:
                            self.stop()
                            raise

                    return _signal_handler

                new_handler = _make_handler(sig, prev_handler)
                signal.signal(sig, new_handler)
                self._old_signal_handlers[sig] = prev_handler
            except (ValueError, TypeError, AttributeError):
                pass

    def _unregister_signal_handlers(self):
        """Restore previous signal handlers if registered."""
        if not self._signals_registered:
            return
        for sig, old_h in list(self._old_signal_handlers.items()):
            try:
                if old_h is not None:
                    signal.signal(sig, old_h)
            except (ValueError, TypeError, AttributeError):
                pass
        self._old_signal_handlers.clear()
        self._signals_registered = False

    def _force_refresh(self):
        """Signal the rendering loop to trigger an immediate frame refresh off the input listener thread."""
        self._need_refresh = True
        if hasattr(self, "_refresh_event"):
            self._refresh_event.set()
        if not self._running and self.out_stream:
            try:
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

        if self.active_screen == "gemini_models":
            # Dedicated Gemini Model Selection Screen View
            banner_text = "Press [Esc] to return to Main Screen │ Controls: [↑/↓ or j/k] Select │ [Space/Enter] Set Active Gemini Model"
            banner_line = fit_to_display_width(f"\033[93m\033[1m{banner_text}\033[0m", width)
            lines.append(banner_line)
            lines.append("")
            quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
            models = (
                quota_tr.available_gemini_models
                if quota_tr and hasattr(quota_tr, "available_gemini_models")
                else ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro"]
            )
            active_m = (
                quota_tr.get_active_model("gemini")
                if quota_tr and hasattr(quota_tr, "get_active_model")
                else "gemini-2.5-flash"
            )
            lines.extend(render_gemini_models_panel(width, models, self.selected_gemini_index, active_m))
            return "\n".join(lines)

        if self.active_screen == "third_party_models":
            # Dedicated 3rd Party Model Selection Screen View
            banner_text = "Press [Esc] to return to Main Screen │ Controls: [↑/↓ or j/k] Select │ [Space/Enter] Set Active 3rd Party Model"
            banner_line = fit_to_display_width(f"\033[93m\033[1m{banner_text}\033[0m", width)
            lines.append(banner_line)
            lines.append("")
            quota_tr = self.quota_tracker or getattr(self.task_manager, "quota_tracker", None)
            models = (
                quota_tr.available_third_party_models
                if quota_tr and hasattr(quota_tr, "available_third_party_models")
                else ["claude-3-5-sonnet", "claude-3-opus", "claude-3-5-haiku"]
            )
            active_m = (
                quota_tr.get_active_model("claude_gpt")
                if quota_tr and hasattr(quota_tr, "get_active_model")
                else "claude-3-5-sonnet"
            )
            lines.extend(render_third_party_models_panel(width, models, self.selected_third_party_index, active_m))
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
        is_paused = self.task_manager.is_paused if self.task_manager else False
        return render_header_panel(
            width, self.host, self.port, commit, branch, reload_state, uptime, self.active_screen, is_paused=is_paused
        )

    def _render_active_tasks(self, width: int, tasks: list, max_workers: int) -> list:
        return render_active_tasks_panel(width, tasks, max_workers)

    def _render_queued_tasks(self, width: int, tasks: list) -> list:
        is_paused = self.task_manager.is_paused if self.task_manager else False
        return render_queued_tasks_panel(
            width, tasks, is_paused=is_paused, selected_queue_index=self.selected_queue_index
        )

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
            with getattr(scheduler, "_lock", nullcontext()):
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
            self._need_refresh = False
            self._refresh_event.clear()
            try:
                frame = self.render()
                self.out_stream.write("\033[H\033[2J" + frame + "\n")
                self.out_stream.flush()
            except Exception:
                pass
            self._refresh_event.wait(timeout=self.refresh_interval)
