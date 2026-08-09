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
from pathlib import Path
from typing import Optional, TextIO, Tuple, Union

from lib.tasks import TaskManager, TaskStatus
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
    - Active Tasks (RUNNING) panel.
    - Task Queue (QUEUED) panel.
    - Task History & Event Log (COMPLETED/FAILED) panel.
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
    ):
        self.task_manager = task_manager
        self.host = host
        self.port = port
        self.repo_root = repo_root
        self.refresh_interval = refresh_interval
        self.out_stream = out_stream or sys.stdout
        self.enable_log_redirection = enable_log_redirection
        self.git_cache_ttl = git_cache_ttl

        if log_file is not None:
            log_path = Path(log_file)
            if not log_path.is_absolute() and repo_root:
                log_path = repo_root / log_path
            self.log_file: Optional[Path] = log_path
        else:
            self.log_file = None

        self.log_handler = log_handler or TUILogHandler()

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._log_redirected = False
        self._detached_handlers: list = []
        self._file_handler: Optional[logging.FileHandler] = None

        self._git_info_cache: Optional[Tuple[str, str]] = None
        self._git_info_last_fetch: float = 0.0

    def start(self):
        """Start the background dashboard rendering loop thread."""
        if self._running:
            return
        if self.enable_log_redirection:
            self._attach_log_redirection()
        self._running = True
        self._thread = threading.Thread(
            target=self._refresh_loop, daemon=True, name="DashboardTUI"
        )
        self._thread.start()

    def stop(self):
        """Stop the dashboard rendering loop."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self.enable_log_redirection:
            self._detach_log_redirection()

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
        """Construct and return the dashboard frame string."""
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

        # 2. Active Tasks Panel
        active_tasks = self.task_manager.get_active_tasks()
        lines.extend(self._render_active_tasks(width, active_tasks, stats["max_workers"]))
        lines.append("")

        # 3. Queued Tasks Panel
        queued_tasks = self.task_manager.get_queued_tasks()
        lines.extend(self._render_queued_tasks(width, queued_tasks))
        lines.append("")

        # 4. Task History & Event Log Panel
        history_tasks = self.task_manager.get_task_history(limit=5)
        lines.extend(self._render_history_tasks(width, history_tasks, stats))

        return "\n".join(lines)

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

        return [
            "┌" + "─" * (width - 2) + "┐",
            f"│ {line1_content} │",
            f"│ \033[2m{info_content}\033[0m │",
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
            col_hdr = f"{fit_to_display_width('ID', 8)} {fit_to_display_width('AGENT', 15)} {fit_to_display_width('TARGET', 10)} {fit_to_display_width('WORKER', 10)} {fit_to_display_width('ELAPSED', 10)} {fit_to_display_width('PROMPT', 18)}"
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
            for t in tasks:
                if get_display_width(t.prompt) > 18:
                    prompt_trunc = truncate_to_display_width(t.prompt, 16) + ".."
                else:
                    prompt_trunc = t.prompt
                id_str = fit_to_display_width(t.id, 8)
                agent_str = fit_to_display_width(t.agent, 15)
                target_str = fit_to_display_width(t.target_id or "-", 10)
                worker_str = fit_to_display_width(t.worker_thread_id or "-", 10)
                elapsed_str = fit_to_display_width(f"{t.elapsed_time:.1f}s", 10)
                prompt_str = fit_to_display_width(prompt_trunc, 18)
                row = f"{id_str} {agent_str} {target_str} {worker_str} {elapsed_str} {prompt_str}"
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

    def _render_history_tasks(self, width: int, tasks: list, stats: dict) -> list:
        inner_w = width - 4
        passed = stats.get("completed", 0)
        failed = stats.get("failed", 0)
        panel_title = f" TASK HISTORY & EVENT LOG [Passed: {passed} | Failed: {failed}] "
        title_dw = get_display_width(panel_title)
        pad_len = max(0, width - 3 - title_dw)
        header_bar = "┌─" + f"\033[95m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

        res = [header_bar]
        if not tasks:
            msg = "(No task history recorded yet)"
            msg_styled = f"\033[2m{msg}\033[0m"
            res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
        else:
            col_hdr = f"{fit_to_display_width('ID', 8)} {fit_to_display_width('STATUS', 11)} {fit_to_display_width('AGENT', 15)} {fit_to_display_width('RETURN', 8)} {fit_to_display_width('DURATION', 10)} {fit_to_display_width('TARGET', 8)}"
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
            for t in tasks:
                status_color = "\033[92m" if t.status == TaskStatus.COMPLETED else "\033[91m"
                id_str = fit_to_display_width(t.id, 8)
                status_str = fit_to_display_width(f"{status_color}{t.status}\033[0m", 11)
                agent_str = fit_to_display_width(t.agent, 15)
                ret_val = str(t.return_code) if t.return_code is not None else "-"
                ret_str = fit_to_display_width(ret_val, 8)
                dur_str = fit_to_display_width(f"{t.elapsed_time:.1f}s", 10)
                target_str = fit_to_display_width(t.target_id or "-", 8)
                row = f"{id_str} {status_str} {agent_str} {ret_str} {dur_str} {target_str}"
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

        recent_logs = self.log_handler.get_logs(limit=5) if self.log_handler else []
        if recent_logs:
            sub_hdr = "─ Recent Log Events ─"
            hdr_text = f"\033[1m{sub_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_text, inner_w)} │")
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
