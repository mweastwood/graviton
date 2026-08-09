"""
Modular component rendering utilities for Graviton Server Terminal UI (TUI) panels.
"""

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from lib.quota import QuotaTracker, QuotaWindow, format_quota_badge
from lib.scheduler import ScheduledJob, TaskScheduler
from lib.tasks import TaskManager, TaskStatus

ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


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


def format_interval(sec: int) -> str:
    """Format interval in seconds into human-readable shorthand (e.g. 1d, 2h, 5m, 30s)."""
    if sec >= 86400 and sec % 86400 == 0:
        return f"{sec // 86400}d"
    elif sec >= 3600 and sec % 3600 == 0:
        return f"{sec // 3600}h"
    elif sec >= 60 and sec % 60 == 0:
        return f"{sec // 60}m"
    else:
        return f"{sec}s"


def format_timestamp(ts: Optional[str]) -> str:
    """Format ISO timestamp string into HH:MM:SS format."""
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts[:8]


def format_remaining(job: ScheduledJob, now_dt: datetime) -> str:
    """Format time remaining until scheduled job next run."""
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


def render_header_panel(
    width: int,
    host: str,
    port: int,
    commit: str,
    branch: str,
    reload_state: str,
    uptime: str,
    active_screen: str = "main",
) -> List[str]:
    """Render top header panel with host, port, git version, branch, uptime, and navigation state."""
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

    info_line = f"Host: {host}:{port} │ Branch: {branch} │ Commit: {commit} │ Uptime: {uptime}"
    info_content = fit_to_display_width(info_line, inner_w)

    if active_screen == "jobs":
        nav_hint = "Nav: [↑/↓] Select │ [Space] Toggle │ [e/d] Enable/Disable │ [Esc] Main Screen"
    elif active_screen == "logs":
        nav_hint = "Nav: [Esc] Main Screen"
    else:
        nav_hint = "Nav: [j] Periodic Jobs │ [e] Event Logs"
    nav_content = fit_to_display_width(f"\033[93m\033[1m{nav_hint}\033[0m", inner_w)

    return [
        "┌" + "─" * (width - 2) + "┐",
        f"│ {line1_content} │",
        f"│ \033[2m{info_content}\033[0m │",
        f"│ {nav_content} │",
        "└" + "─" * (width - 2) + "┘",
    ]


def render_quota_panel(
    width: int,
    quota_tracker: Optional[QuotaTracker] = None,
    task_manager: Optional[TaskManager] = None,
) -> List[str]:
    """Render model quota panel for 5-hour and 1-week windows."""
    inner_w = width - 4
    quota_tr = quota_tracker or (getattr(task_manager, "quota_tracker", None) if task_manager else None)
    pool = getattr(quota_tr, "quota_pool", "gemini") if quota_tr else "gemini"

    if quota_tr:
        w_5h = getattr(
            quota_tr,
            "window_5h",
            QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=quota_tr.remaining_percentage),
        )
        w_1w = getattr(
            quota_tr,
            "window_1w",
            QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0),
        )
    else:
        w_5h = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=100.0)
        w_1w = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)

    badge_5h_text = format_quota_badge(w_5h, quota_pool=pool)
    badge_1w_text = format_quota_badge(w_1w, quota_pool=pool)

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

    panel_title = f" ANTIGRAVITY MODEL QUOTA ({pool.upper()}) "
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


def render_active_tasks_panel(width: int, tasks: list, max_workers: int) -> List[str]:
    """Render active running tasks panel."""
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


def render_queued_tasks_panel(width: int, tasks: list) -> List[str]:
    """Render pending queued tasks panel."""
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


def render_scheduled_jobs_panel(
    width: int,
    scheduler: Optional[TaskScheduler],
    selected_job_index: int = 0,
    mode: str = "card",
) -> List[str]:
    """Render periodic scheduled jobs panel in card or table view mode."""
    inner_w = width - 4
    panel_title = " SCHEDULED JOBS [(j/k) select | (space) toggle | (e/d) state | (r)un ] "
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
        jobs_list = list(scheduler.jobs.values())
        if jobs_list:
            selected_job_index = max(0, min(selected_job_index, len(jobs_list) - 1))
        else:
            selected_job_index = 0

        if mode == "card":
            now_dt = datetime.now(timezone.utc)
            for idx, job in enumerate(jobs_list):
                is_selected = (idx == selected_job_index)
                cursor_str = "> " if is_selected else "  "
                cursor_styled = f"\033[93m\033[1m{cursor_str}\033[0m" if is_selected else cursor_str

                if job.enabled:
                    status_badge = "\033[92m\033[1m[● ENABLED]\033[0m"
                else:
                    status_badge = "\033[90m[○ DISABLED]\033[0m"

                header_line = f"{cursor_styled}{status_badge} \033[1m{job.job_id}\033[0m  \033[96m[Agent: {job.agent}]\033[0m"
                res.append(f"│ {fit_to_display_width(header_line, inner_w)} │")

                interval_str = format_interval(job.interval_seconds)
                last_run_str = format_timestamp(job.last_run)
                next_run_str = format_timestamp(job.next_run)
                rem_str = format_remaining(job, now_dt)
                sched_line = (
                    f"  Name: {job.name} │ Interval: {interval_str} │ "
                    f"Last Run: {last_run_str} │ Next Run: {next_run_str} │ "
                    f"Remaining: {rem_str}"
                )
                res.append(f"│ {fit_to_display_width(sched_line, inner_w)} │")

                prompt_line = f"  \033[2mPrompt: {job.prompt}\033[0m"
                res.append(f"│ {fit_to_display_width(prompt_line, inner_w)} │")

                if idx < len(jobs_list) - 1:
                    sep_line = "\033[90m" + ("─" * inner_w) + "\033[0m"
                    res.append(f"│ {fit_to_display_width(sep_line, inner_w)} │")
        else:
            fixed_w = 2 + 6 + 10 + 10 + 10  # SEL, INTV, LAST RUN, NEXT RUN, REMAIN
            num_cols = 8
            spacers = num_cols - 1  # 7 spaces
            flex_avail = max(15, inner_w - fixed_w - spacers)

            # Distribute remaining width (weights: JOB ID: 12, NAME: 24, AGENT: 16 -> total 52)
            id_w = max(8, int(flex_avail * (12 / 52)))
            name_w = max(12, int(flex_avail * (24 / 52)))
            agent_w = max(10, flex_avail - id_w - name_w)

            col_hdr = (
                f"{fit_to_display_width(' ', 2)} "
                f"{fit_to_display_width('JOB ID', id_w)} "
                f"{fit_to_display_width('NAME', name_w)} "
                f"{fit_to_display_width('AGENT', agent_w)} "
                f"{fit_to_display_width('INTV', 6)} "
                f"{fit_to_display_width('LAST RUN', 10)} "
                f"{fit_to_display_width('NEXT RUN', 10)} "
                f"{fit_to_display_width('REMAIN', 10)}"
            )
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")

            now_dt = datetime.now(timezone.utc)
            for idx, job in enumerate(jobs_list):
                is_selected = (idx == selected_job_index)
                cursor_str = "> " if is_selected else "  "
                cursor_styled = f"\033[93m\033[1m{cursor_str}\033[0m" if is_selected else cursor_str

                if get_display_width(job.job_id) > id_w:
                    id_trunc = truncate_to_display_width(job.job_id, max(1, id_w - 2)) + ".."
                else:
                    id_trunc = job.job_id

                if get_display_width(job.name) > name_w:
                    name_trunc = truncate_to_display_width(job.name, max(1, name_w - 2)) + ".."
                else:
                    name_trunc = job.name

                if get_display_width(job.agent) > agent_w:
                    agent_trunc = truncate_to_display_width(job.agent, max(1, agent_w - 2)) + ".."
                else:
                    agent_trunc = job.agent

                id_str = fit_to_display_width(id_trunc, id_w)
                name_str = fit_to_display_width(name_trunc, name_w)
                agent_str = fit_to_display_width(agent_trunc, agent_w)
                interval_str = fit_to_display_width(format_interval(job.interval_seconds), 6)
                last_run_str = fit_to_display_width(format_timestamp(job.last_run), 10)
                next_run_str = fit_to_display_width(format_timestamp(job.next_run), 10)
                rem_str = fit_to_display_width(format_remaining(job, now_dt), 10)
                row = f"{cursor_styled} {id_str} {name_str} {agent_str} {interval_str} {last_run_str} {next_run_str} {rem_str}"
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

    res.append("└" + "─" * (width - 2) + "┘")
    return res


def render_approved_prs_panel(width: int, approved_prs: list) -> List[str]:
    """Render approved pull requests panel."""
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


def render_history_tasks_panel(width: int, tasks: list, stats: dict) -> List[str]:
    """Render task execution history panel."""
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


def render_event_logs_panel(
    width: int,
    log_handler: Optional[Any] = None,
    limit: int = 15,
) -> List[str]:
    """Render recent event log records panel."""
    inner_w = width - 4
    panel_title = " EVENT LOGS "
    title_dw = get_display_width(panel_title)
    pad_len = max(0, width - 3 - title_dw)
    header_bar = "┌─" + f"\033[96m\033[1m{panel_title}\033[0m" + ("─" * pad_len) + "┐"

    res = [header_bar]
    recent_logs = log_handler.get_logs(limit=limit) if log_handler else []
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
