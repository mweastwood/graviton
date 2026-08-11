"""
Modular component rendering utilities for Graviton Server Terminal UI (TUI) panels.
"""

import re
import unicodedata
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from lib.quota import QuotaTracker, QuotaWindow, format_quota_badge
from lib.scheduler import ScheduledJob, TaskScheduler, parse_iso_timestamp
from lib.tasks import TaskManager, TaskStatus

ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def get_display_width(s: str) -> int:
    """Return visual display width of string, stripping ANSI escape codes and accounting for wide characters."""
    if s is None:
        s = ""
    elif not isinstance(s, str):
        s = str(s)
    clean_str = ANSI_REGEX.sub("", s)
    return sum(2 if unicodedata.east_asian_width(c) in ("F", "W") else 1 for c in clean_str)


def truncate_to_display_width(s: str, max_w: int) -> str:
    """Truncate string so its visual display width does not exceed max_w."""
    if s is None:
        s = ""
    elif not isinstance(s, str):
        s = str(s)
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


def truncate_with_ellipsis(s: str, max_w: int) -> str:
    """Truncate string with '..' suffix if display width exceeds max_w, preserving and resetting ANSI styles properly."""
    if s is None:
        s = ""
    elif not isinstance(s, str):
        s = str(s)
    if get_display_width(s) <= max_w:
        return s

    has_ansi = bool(ANSI_REGEX.search(s))
    if max_w >= 2:
        trunc = truncate_to_display_width(s, max(0, max_w - 2))
        if has_ansi:
            if trunc.endswith("\033[0m"):
                trunc = trunc[:-4]
            return trunc + "..\033[0m"
        return trunc + ".."

    trunc = truncate_to_display_width(s, max_w)
    if has_ansi and not trunc.endswith("\033[0m"):
        trunc += "\033[0m"
    return trunc


def pad_to_display_width(s: str, target_w: int, align: str = "left") -> str:
    """Pad string with spaces so its visual display width matches target_w."""
    if s is None:
        s = ""
    elif not isinstance(s, str):
        s = str(s)
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
    if s is None:
        s = ""
    elif not isinstance(s, str):
        s = str(s)
    return pad_to_display_width(truncate_to_display_width(s, target_w), target_w, align=align)


def format_row_columns(values: List[Any], widths: List[int], sep: str = " ") -> str:
    """Format row of text cells fitted to their respective column widths."""
    return sep.join(fit_to_display_width(v, w) for v, w in zip(values, widths))


def render_panel_header(width: int, title: str, color_code: str = "\033[96m\033[1m") -> str:
    """
    Render top border header line for a panel box with title truncation and ANSI styling.

    :param width: Total panel width in characters.
    :param title: Title text to display in header bar.
    :param color_code: ANSI color formatting code for title text.
    :return: Formatted top border string (e.g. '┌─ TITLE ───────┐').
    """
    if title is None:
        title = ""
    elif not isinstance(title, str):
        title = str(title)
    panel_title = f" {title.strip()} "
    title_dw = get_display_width(panel_title)
    if title_dw > width - 3:
        panel_title = truncate_to_display_width(panel_title, max(0, width - 3))
        title_dw = get_display_width(panel_title)
    pad_len = max(0, width - 3 - title_dw)
    return "┌─" + f"{color_code}{panel_title}\033[0m" + ("─" * pad_len) + "┐"


def format_panel_header(width: int, title: str, color_code: str = "\033[96m\033[1m") -> str:
    """Format top border header line for a panel box with title truncation and ANSI styling."""
    return render_panel_header(width, title, color_code=color_code)


def allocate_approved_pr_columns(inner_w: int, has_repo: bool = False) -> Tuple[List[str], List[int]]:
    """
    Allocate column headers and widths for approved pull requests panel table.

    Ensures sum of column widths plus spacing does not exceed inner_w even for narrow container widths (e.g. inner_w < 24).
    """
    if has_repo:
        spacing = 4
        avail = max(0, inner_w - spacing)
        if inner_w < 44:
            max_fixed = max(0, avail - 2) if avail < 5 else max(3, avail - 2)
            min_col_w = 0 if avail < 5 else 1
            pr_col_w = max(min_col_w, min(8, int(avail * 0.15)))
            repo_col_w = max(min_col_w, min(16, int(avail * 0.25)))
            author_col_w = max(min_col_w, min(14, int(avail * 0.20)))
            fixed_sum = pr_col_w + repo_col_w + author_col_w
            if fixed_sum > max_fixed:
                scale = max_fixed / fixed_sum if fixed_sum > 0 else 0
                pr_col_w = max(min_col_w, int(pr_col_w * scale))
                repo_col_w = max(0, int(repo_col_w * scale))
                author_col_w = max(0, max_fixed - pr_col_w - repo_col_w)
        else:
            pr_col_w = 8
            repo_col_w = 16
            author_col_w = 14

        remaining = max(0, avail - pr_col_w - repo_col_w - author_col_w)
        if remaining > 0:
            title_col_w = max(0, (remaining - 1) // 2)
            url_col_w = remaining - title_col_w
        else:
            title_col_w = 0
            url_col_w = 0

        headers = ["PR #", "REPO", "TITLE", "AUTHOR", "URL"]
        widths = [pr_col_w, repo_col_w, title_col_w, author_col_w, url_col_w]
    else:
        spacing = 3
        avail = max(0, inner_w - spacing)
        if inner_w < 28:
            max_fixed = max(0, avail - 2) if avail < 4 else max(2, avail - 2)
            min_col_w = 0 if avail < 4 else 1
            pr_col_w = max(min_col_w, min(8, int(avail * 0.20)))
            author_col_w = max(min_col_w, min(15, int(avail * 0.30)))
            fixed_sum = pr_col_w + author_col_w
            if fixed_sum > max_fixed:
                scale = max_fixed / fixed_sum if fixed_sum > 0 else 0
                pr_col_w = max(min_col_w, int(pr_col_w * scale))
                author_col_w = max(0, max_fixed - pr_col_w)
        else:
            pr_col_w = 8
            author_col_w = 15

        remaining = max(0, avail - pr_col_w - author_col_w)
        if remaining > 0:
            title_col_w = max(0, (remaining - 1) // 2)
            url_col_w = remaining - title_col_w
        else:
            title_col_w = 0
            url_col_w = 0

        headers = ["PR #", "TITLE", "AUTHOR", "URL"]
        widths = [pr_col_w, title_col_w, author_col_w, url_col_w]

    return headers, widths


calculate_pr_column_widths = allocate_approved_pr_columns


def allocate_scheduled_job_columns(inner_w: int) -> Tuple[List[str], List[int]]:
    """
    Allocate column headers and widths for scheduled jobs panel table mode.

    Ensures total sum of column widths plus spacing does not exceed inner_w even for narrow container widths (inner_w < 45).
    """
    num_cols = 8
    spacers = num_cols - 1  # 7 spaces
    if inner_w >= 45:
        sel_w = 2
        intv_w = 6
        last_w = 10
        next_w = 10
        rem_w = 10
        flex_avail = inner_w - 45

        # Distribute flex_avail (weights: JOB ID: 12, NAME: 24, AGENT: 16 -> total 52)
        min_scale = min(1.0, flex_avail / 52.0)
        min_id = max(1 if flex_avail >= 3 else 0, int(8 * min_scale))
        min_name = max(1 if flex_avail >= 2 else 0, int(12 * min_scale))
        min_agent = max(1 if flex_avail >= 1 else 0, int(10 * min_scale))

        id_w = max(min_id, min(flex_avail - min_name - min_agent, int(flex_avail * (12 / 52))))
        name_w = max(min_name, min(flex_avail - id_w - min_agent, int(flex_avail * (24 / 52))))
        agent_w = max(0, flex_avail - id_w - name_w)
    else:
        id_w = 0
        name_w = 0
        agent_w = 0
        avail_cols = max(0, inner_w - spacers)
        scale = avail_cols / 38.0
        sel_w = max(1 if avail_cols >= 5 else 0, int(2 * scale))
        intv_w = max(1 if avail_cols >= 4 else 0, int(6 * scale))
        last_w = max(1 if avail_cols >= 3 else 0, int(10 * scale))
        next_w = max(1 if avail_cols >= 2 else 0, int(10 * scale))
        rem_w = max(0, avail_cols - sel_w - intv_w - last_w - next_w)

    headers = [" ", "JOB ID", "NAME", "AGENT", "INTV", "LAST RUN", "NEXT RUN", "REMAIN"]
    widths = [sel_w, id_w, name_w, agent_w, intv_w, last_w, next_w, rem_w]
    return headers, widths


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
    dt = parse_iso_timestamp(ts)
    if dt is not None:
        return dt.strftime("%H:%M:%S")
    return ts[:8]


def format_remaining(job: ScheduledJob, now_dt: datetime) -> str:
    """Format time remaining until scheduled job next run."""
    if getattr(job, "is_running", False):
        return "RUNNING"
    if not job.enabled:
        return "DISABLED"

    next_dt = parse_iso_timestamp(job.next_run)

    if next_dt is None and job.last_run:
        last_dt = parse_iso_timestamp(job.last_run)
        if last_dt is not None:
            next_dt = last_dt + timedelta(seconds=job.interval_seconds)

    if next_dt is None:
        return "DUE"

    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=timezone.utc)

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
        rem_pct = getattr(quota_tr, "remaining_percentage", 100.0)
        w_5h = getattr(
            quota_tr,
            "window_5h",
            QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=rem_pct),
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

    header_bar = render_panel_header(width, f"ANTIGRAVITY MODEL QUOTA ({pool.upper()})", color_code="\033[96m\033[1m")

    body_line_1 = fit_to_display_width(f"{color_5h}{badge_5h_text}\033[0m", inner_w)
    body_line_2 = fit_to_display_width(f"{color_1w}{badge_1w_text}\033[0m", inner_w)

    return [
        header_bar,
        f"│ {body_line_1} │",
        f"│ {body_line_2} │",
        "└" + "─" * (width - 2) + "┘",
    ]


def render_active_tasks_panel(width: int, tasks: List[Any], max_workers: int) -> List[str]:
    """Render active running tasks panel."""
    inner_w = width - 4
    active_cnt = len(tasks)
    panel_title = f"ACTIVE TASKS (RUNNING) [{active_cnt}/{max_workers} Workers Active]"
    header_bar = render_panel_header(width, panel_title, color_code="\033[94m\033[1m")

    res = [header_bar]
    if not tasks:
        msg = "(No active tasks currently running)"
        msg_styled = f"\033[2m{msg}\033[0m"
        res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
    else:
        headers = ["ID", "AGENT", "TARGET", "WORKER", "ATTEMPT", "ELAPSED", "PROMPT"]
        widths = [8, 14, 8, 9, 7, 9, 15]
        col_hdr = format_row_columns(headers, widths)
        hdr_styled = f"\033[1m{col_hdr}\033[0m"
        res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
        for t in tasks:
            prompt_trunc = truncate_with_ellipsis(t.prompt, 15)
            row_vals = [
                t.id,
                t.agent,
                t.target_id or "-",
                t.worker_thread_id or "-",
                f"{t.attempt}/{t.max_attempts}",
                f"{t.elapsed_time:.1f}s",
                prompt_trunc,
            ]
            row = format_row_columns(row_vals, widths)
            res.append(f"│ {fit_to_display_width(row, inner_w)} │")

    res.append("└" + "─" * (width - 2) + "┘")
    return res


def render_queued_tasks_panel(width: int, tasks: List[Any]) -> List[str]:
    """Render pending queued tasks panel."""
    inner_w = width - 4
    queued_cnt = len(tasks)
    panel_title = f"TASK QUEUE (QUEUED) [{queued_cnt} Pending]"
    header_bar = render_panel_header(width, panel_title, color_code="\033[93m\033[1m")

    res = [header_bar]
    if not tasks:
        msg = "(Task queue is empty)"
        msg_styled = f"\033[2m{msg}\033[0m"
        res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
    else:
        headers = ["ID", "AGENT", "TARGET", "WAIT", "PROMPT"]
        widths = [8, 15, 10, 10, 26]
        col_hdr = format_row_columns(headers, widths)
        hdr_styled = f"\033[1m{col_hdr}\033[0m"
        res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
        for t in tasks:
            prompt_trunc = truncate_with_ellipsis(t.prompt, 26)
            row_vals = [
                t.id,
                t.agent,
                t.target_id or "-",
                f"{t.wait_time:.1f}s",
                prompt_trunc,
            ]
            row = format_row_columns(row_vals, widths)
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
    panel_title = "SCHEDULED JOBS [(j/k) select | (space) toggle | (e/d) state | (r)un ]"
    header_bar = render_panel_header(width, panel_title, color_code="\033[96m\033[1m")

    res = [header_bar]
    if not scheduler or not scheduler.jobs:
        if not scheduler:
            msg = "(Scheduler disabled)"
        else:
            msg = "(No scheduled jobs configured)"
        msg_styled = f"\033[2m{msg}\033[0m"
        res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
    else:
        with getattr(scheduler, "_lock", nullcontext()):
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

                if getattr(job, "is_running", False):
                    status_badge = "\033[93m\033[1m[⚡ RUNNING]\033[0m"
                elif job.enabled:
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
            headers, widths = allocate_scheduled_job_columns(inner_w)
            col_hdr = format_row_columns(headers, widths)
            hdr_styled = f"\033[1m{col_hdr}\033[0m"
            res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")

            now_dt = datetime.now(timezone.utc)
            for idx, job in enumerate(jobs_list):
                is_selected = (idx == selected_job_index)
                cursor_str = "> " if is_selected else "  "
                cursor_styled = f"\033[93m\033[1m{cursor_str}\033[0m" if is_selected else cursor_str

                id_trunc = truncate_with_ellipsis(job.job_id, widths[1])
                name_trunc = truncate_with_ellipsis(job.name, widths[2])
                agent_trunc = truncate_with_ellipsis(job.agent, widths[3])

                row_vals = [
                    cursor_styled,
                    id_trunc,
                    name_trunc,
                    agent_trunc,
                    format_interval(job.interval_seconds),
                    format_timestamp(job.last_run),
                    format_timestamp(job.next_run),
                    format_remaining(job, now_dt),
                ]
                row = format_row_columns(row_vals, widths)
                res.append(f"│ {fit_to_display_width(row, inner_w)} │")

    res.append("└" + "─" * (width - 2) + "┘")
    return res


def render_approved_prs_panel(width: int, approved_prs: List[Dict[str, Any]]) -> List[str]:
    """Render approved pull requests panel."""
    inner_w = width - 4
    approved_cnt = len(approved_prs)
    panel_title = f"APPROVED PULL REQUESTS (READY TO MERGE) [{approved_cnt} Ready]"
    header_bar = render_panel_header(width, panel_title, color_code="\033[92m\033[1m")

    res = [header_bar]
    if not approved_prs:
        msg = "(No approved PRs awaiting merge)"
        msg_styled = f"\033[2m{msg}\033[0m"
        res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
    else:
        has_repo = any(bool(pr.get("repo_full_name")) for pr in approved_prs)
        headers, widths = allocate_approved_pr_columns(inner_w, has_repo=has_repo)

        col_hdr = format_row_columns(headers, widths)
        hdr_styled = f"\033[1m{col_hdr}\033[0m"
        res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")

        for pr in approved_prs:
            num_str = f"#{pr.get('number', '')}"
            title_str = pr.get("title", "")
            author_str = pr.get("author", "")
            url_str = pr.get("url", "")
            if has_repo:
                repo_str = pr.get("repo_full_name", "") or "-"
                row_vals = [num_str, repo_str, title_str, author_str, url_str]
            else:
                row_vals = [num_str, title_str, author_str, url_str]

            row = format_row_columns(row_vals, widths)
            res.append(f"│ {fit_to_display_width(row, inner_w)} │")

    res.append("└" + "─" * (width - 2) + "┘")
    return res


def render_history_tasks_panel(width: int, tasks: List[Any], stats: Dict[str, Any]) -> List[str]:
    """Render task execution history panel."""
    inner_w = width - 4
    passed = stats.get("completed", 0)
    failed = stats.get("failed", 0)
    panel_title = f"TASK HISTORY (COMPLETED & FAILED) [Passed: {passed} | Failed: {failed}]"
    header_bar = render_panel_header(width, panel_title, color_code="\033[95m\033[1m")

    res = [header_bar]
    if not tasks:
        msg = "(No task history recorded yet)"
        msg_styled = f"\033[2m{msg}\033[0m"
        res.append(f"│ {fit_to_display_width(msg_styled, inner_w)} │")
    else:
        headers = ["ID", "STATUS", "AGENT", "ATTEMPT", "RETURN", "DURATION", "TARGET"]
        widths = [8, 11, 14, 7, 8, 9, 8]
        col_hdr = format_row_columns(headers, widths)
        hdr_styled = f"\033[1m{col_hdr}\033[0m"
        res.append(f"│ {fit_to_display_width(hdr_styled, inner_w)} │")
        for t in tasks:
            status_color = "\033[92m" if t.status == TaskStatus.COMPLETED else "\033[91m"
            status_str = f"{status_color}{t.status}\033[0m"
            ret_val = str(t.return_code) if t.return_code is not None else "-"
            row_vals = [
                t.id,
                status_str,
                t.agent,
                f"{t.attempt}/{t.max_attempts}",
                ret_val,
                f"{t.elapsed_time:.1f}s",
                t.target_id or "-",
            ]
            row = format_row_columns(row_vals, widths)
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
    panel_title = "EVENT LOGS"
    header_bar = render_panel_header(width, panel_title, color_code="\033[96m\033[1m")

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
