"""
Terminal UI Dashboard for Graviton Server.
"""

import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Optional, TextIO

from lib.tasks import TaskManager, TaskStatus
from lib.updater import get_git_info, get_hot_reload_state, get_uptime_str


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
    ):
        self.task_manager = task_manager
        self.host = host
        self.port = port
        self.repo_root = repo_root
        self.refresh_interval = refresh_interval
        self.out_stream = out_stream or sys.stdout

        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """Start the background dashboard rendering loop thread."""
        if self._running:
            return
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

    def render(self, width: Optional[int] = None) -> str:
        """Construct and return the dashboard frame string."""
        if width is None:
            try:
                cols = shutil.get_terminal_size((80, 24)).columns
            except Exception:
                cols = 80
            width = max(78, min(120, cols))

        lines = []

        commit, branch = get_git_info(self.repo_root)
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
        raw_title = "⚡ GRAVITON SERVER DASHBOARD ⚡"
        raw_badge = f"[ HOT-RELOAD: {reload_state} ]"

        pad_len = max(1, inner_w - len(raw_title) - len(raw_badge))
        line1_content = f"{title}{' ' * pad_len}{reload_badge}"

        info_line = f"Host: {self.host}:{self.port} │ Branch: {branch} │ Commit: {commit} │ Uptime: {uptime}"
        info_padded = info_line.ljust(inner_w)

        return [
            "┌" + "─" * (width - 2) + "┐",
            f"│ {line1_content} │",
            f"│ \033[2m{info_padded}\033[0m │",
            "└" + "─" * (width - 2) + "┘",
        ]

    def _render_active_tasks(self, width: int, tasks: list, max_workers: int) -> list:
        inner_w = width - 4
        active_cnt = len(tasks)
        panel_title = f" ACTIVE TASKS (RUNNING) [{active_cnt}/{max_workers} Workers Active] "
        header_bar = "┌─" + f"\033[94m\033[1m{panel_title}\033[0m" + "─" * max(0, width - 3 - len(panel_title)) + "┐"

        res = [header_bar]
        if not tasks:
            msg = "\033[2m(No active tasks currently running)\033[0m"
            pad = max(0, inner_w - len("(No active tasks currently running)"))
            res.append(f"│ {msg}{' ' * pad} │")
        else:
            col_hdr = f"{'ID':<8} {'AGENT':<15} {'TARGET':<10} {'WORKER':<10} {'ELAPSED':<10} {'PROMPT':<18}"
            res.append(f"│ \033[1m{col_hdr[:inner_w].ljust(inner_w)}\033[0m │")
            for t in tasks:
                prompt_trunc = (t.prompt[:16] + "..") if len(t.prompt) > 18 else t.prompt
                target_str = t.target_id or "-"
                worker_str = t.worker_thread_id or "-"
                elapsed_str = f"{t.elapsed_time:.1f}s"
                row = f"{t.id:<8} {t.agent:<15} {target_str:<10} {worker_str:<10} {elapsed_str:<10} {prompt_trunc:<18}"
                res.append(f"│ {row[:inner_w].ljust(inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    def _render_queued_tasks(self, width: int, tasks: list) -> list:
        inner_w = width - 4
        queued_cnt = len(tasks)
        panel_title = f" TASK QUEUE (QUEUED) [{queued_cnt} Pending] "
        header_bar = "┌─" + f"\033[93m\033[1m{panel_title}\033[0m" + "─" * max(0, width - 3 - len(panel_title)) + "┐"

        res = [header_bar]
        if not tasks:
            msg = "\033[2m(Task queue is empty)\033[0m"
            pad = max(0, inner_w - len("(Task queue is empty)"))
            res.append(f"│ {msg}{' ' * pad} │")
        else:
            col_hdr = f"{'ID':<8} {'AGENT':<15} {'TARGET':<10} {'WAIT':<10} {'PROMPT':<26}"
            res.append(f"│ \033[1m{col_hdr[:inner_w].ljust(inner_w)}\033[0m │")
            for t in tasks:
                prompt_trunc = (t.prompt[:24] + "..") if len(t.prompt) > 26 else t.prompt
                target_str = t.target_id or "-"
                wait_str = f"{t.wait_time:.1f}s"
                row = f"{t.id:<8} {t.agent:<15} {target_str:<10} {wait_str:<10} {prompt_trunc:<26}"
                res.append(f"│ {row[:inner_w].ljust(inner_w)} │")

        res.append("└" + "─" * (width - 2) + "┘")
        return res

    def _render_history_tasks(self, width: int, tasks: list, stats: dict) -> list:
        inner_w = width - 4
        passed = stats.get("completed", 0)
        failed = stats.get("failed", 0)
        panel_title = f" TASK HISTORY & EVENT LOG [Passed: {passed} | Failed: {failed}] "
        header_bar = "┌─" + f"\033[95m\033[1m{panel_title}\033[0m" + "─" * max(0, width - 3 - len(panel_title)) + "┐"

        res = [header_bar]
        if not tasks:
            msg = "\033[2m(No task history recorded yet)\033[0m"
            pad = max(0, inner_w - len("(No task history recorded yet)"))
            res.append(f"│ {msg}{' ' * pad} │")
        else:
            col_hdr = f"{'ID':<8} {'STATUS':<11} {'AGENT':<15} {'RETURN':<8} {'DURATION':<10} {'TARGET':<8}"
            res.append(f"│ \033[1m{col_hdr[:inner_w].ljust(inner_w)}\033[0m │")
            for t in tasks:
                status_color = "\033[92m" if t.status == TaskStatus.COMPLETED else "\033[91m"
                ret_str = str(t.return_code) if t.return_code is not None else "-"
                dur_str = f"{t.elapsed_time:.1f}s"
                target_str = t.target_id or "-"
                # Render line with ANSI status color
                line_plain = f"{t.id:<8} {t.status:<11} {t.agent:<15} {ret_str:<8} {dur_str:<10} {target_str:<8}"
                line_plain_trunc = line_plain[:inner_w].ljust(inner_w)
                # Apply status color to status column
                line_colored = line_plain_trunc.replace(t.status, f"{status_color}{t.status}\033[0m", 1)
                res.append(f"│ {line_colored} │")

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
