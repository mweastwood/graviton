"""
Modular component rendering utilities for Graviton Server Terminal UI (TUI) panels.
"""

import re
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


def truncate_with_ellipsis(s: str, max_w: int, ellipsis: str = "..") -> str:
    """Truncate string so its visual display width does not exceed max_w, appending ellipsis if truncated."""
    if s is None:
        s = ""
    elif not isinstance(s, str):
        s = str(s)
    if ellipsis is None:
        ellipsis = ".."
    elif not isinstance(ellipsis, str):
        ellipsis = str(ellipsis)
    if get_display_width(s) <= max_w:
        return s
    el_w = get_display_width(ellipsis)
    if max_w >= el_w:
        truncated = truncate_to_display_width(s, max_w - el_w)
        if ANSI_REGEX.search(truncated):
            if truncated.endswith("\033[0m"):
                truncated = truncated[:-4]
            return truncated + ellipsis + "\033[0m"
        return truncated + ellipsis
    return truncate_to_display_width(s, max_w)


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


def format_table_row(items: Sequence[Tuple[str, int]], sep: str = " ") -> str:
    """Format row cells from (value, width) pairs joined by separator."""
    return sep.join(fit_to_display_width(val, w) for val, w in items)


def format_target_for_display(target: Optional[str], max_w: int) -> str:
    """
    Format target string for display in TUI panels.
    - Removes username/org prefix if present (e.g., 'mweastwood/graviton#148' -> 'graviton#148').
    - Preserves PR/issue number suffix when clipping is necessary (e.g., 'gr..#148').
    """
    if target is None:
        target = "-"
    elif not isinstance(target, str):
        target = str(target)

    if "/" in target:
        target = target.rstrip("/")
        if "/" in target:
            target = target.split("/")[-1]

    if not target:
        target = "-"

    if get_display_width(target) <= max_w:
        return target

    match = re.search(r"^(.*?)(#\d+)$", target)
    if match:
        repo_part = match.group(1)
        tag_part = match.group(2)
        tag_w = get_display_width(tag_part)
        if max_w >= tag_w + 2:
            avail_repo_w = max_w - 2 - tag_w
            trunc_repo = truncate_to_display_width(repo_part, avail_repo_w)
            return f"{trunc_repo}..{tag_part}"

    return truncate_with_ellipsis(target, max_w)



def render_panel_header(width: int, title: str, color_code: str = "\033[96m\033[1m") -> str:
    """Build top border line for a TUI panel with styled title."""
    if title is None:
        title = ""
    elif not isinstance(title, str):
        title = str(title)
    panel_title = f" {title.strip()} " if title.strip() else title
    title_dw = get_display_width(panel_title)
    if title_dw > width - 3:
        panel_title = truncate_to_display_width(panel_title, max(1, width - 3))
        title_dw = get_display_width(panel_title)
    pad_len = max(0, width - 3 - title_dw)
    return "┌─" + f"{color_code}{panel_title}\033[0m" + ("─" * pad_len) + "┐"



def render_panel_frame(header_bar: str, content_lines: Sequence[str], width: int) -> List[str]:
    """Wrap content lines inside a TUI panel box given header bar and total width."""
    inner_w = max(0, width - 4)
    res = [header_bar]
    for line in content_lines:
        res.append(f"│ {fit_to_display_width(line, inner_w)} │")
    res.append("└" + "─" * max(2, width - 2) + "┘")
    return res


def split_flex_columns(remaining: int) -> Tuple[int, int]:
    """Split remaining width between title and URL columns for PR table layout."""
    if remaining <= 0:
        return 0, 0
    if remaining >= 8:
        title_w = max(1, remaining // 2 - 2)
    else:
        title_w = max(1, min(remaining - 1, remaining // 2))
    url_w = remaining - title_w
    return title_w, url_w


@dataclass
class ColumnSpec:
    """Declarative specification for a panel table column."""

    name: str
    ratio: float
    fixed_w: Optional[int] = None
    min_w: int = 0
    max_w: Optional[int] = None
    min_avail_threshold: int = 0
    is_flex: bool = False
    narrow_ratio: Optional[float] = None


@dataclass
class TableLayoutSpec:
    """Declarative layout specification for a multi-column panel table."""

    columns: List[ColumnSpec]
    spacing: int
    wide_threshold: int
    narrow_threshold: int


APPROVED_PR_HAS_REPO_SPEC = TableLayoutSpec(
    columns=[
        ColumnSpec("pr", ratio=0.15, fixed_w=8, min_w=4, max_w=8, min_avail_threshold=5),
        ColumnSpec("repo", ratio=0.25, fixed_w=16, min_w=8, max_w=16, min_avail_threshold=4),
        ColumnSpec("author", ratio=0.20, fixed_w=14, min_w=6, max_w=14, min_avail_threshold=3),
        ColumnSpec("title", ratio=0.20, fixed_w=None, min_w=0, max_w=None, min_avail_threshold=2, is_flex=True),
        ColumnSpec("url", ratio=0.0, fixed_w=None, min_w=0, max_w=None, min_avail_threshold=0, is_flex=True),
    ],
    spacing=4,
    wide_threshold=44,
    narrow_threshold=20,
)

APPROVED_PR_NO_REPO_SPEC = TableLayoutSpec(
    columns=[
        ColumnSpec("pr", ratio=0.20, fixed_w=8, min_w=4, max_w=8, min_avail_threshold=4),
        ColumnSpec("author", ratio=0.30, fixed_w=15, min_w=6, max_w=15, min_avail_threshold=3, narrow_ratio=0.25),
        ColumnSpec("title", ratio=0.25, fixed_w=None, min_w=0, max_w=None, min_avail_threshold=2, is_flex=True),
        ColumnSpec("url", ratio=0.0, fixed_w=None, min_w=0, max_w=None, min_avail_threshold=0, is_flex=True),
    ],
    spacing=3,
    wide_threshold=28,
    narrow_threshold=14,
)

SCHEDULED_JOB_COLUMN_SPECS = [
    ColumnSpec("id", ratio=12 / 52, min_w=8, min_avail_threshold=3),
    ColumnSpec("name", ratio=24 / 52, min_w=12, min_avail_threshold=2),
    ColumnSpec("agent", ratio=16 / 52, min_w=10, min_avail_threshold=1),
]


def allocate_declarative_columns(spec: TableLayoutSpec, inner_w: int) -> Dict[str, int]:
    """Calculate column widths based on a declarative TableLayoutSpec and container inner width."""
    avail = max(0, inner_w - spec.spacing)
    res: Dict[str, int] = {}

    flex_cols = [col for col in spec.columns if col.is_flex]
    non_flex_cols = [col for col in spec.columns if not col.is_flex]

    def _allocate_flex(remaining_w: int) -> None:
        if not flex_cols:
            return
        if len(flex_cols) == 1:
            res[flex_cols[0].name] = max(0, remaining_w)
        elif spec in (APPROVED_PR_HAS_REPO_SPEC, APPROVED_PR_NO_REPO_SPEC):
            w1, w2 = split_flex_columns(remaining_w)
            res[flex_cols[0].name] = w1
            res[flex_cols[1].name] = w2
        else:
            total_flex_ratio = sum(col.ratio for col in flex_cols)
            if total_flex_ratio > 0:
                used_flex = 0
                for i, col in enumerate(flex_cols):
                    if i == len(flex_cols) - 1:
                        res[col.name] = max(0, remaining_w - used_flex)
                    else:
                        r = col.ratio / total_flex_ratio
                        w = max(0, int(remaining_w * r))
                        res[col.name] = w
                        used_flex += w
            elif len(flex_cols) == 2:
                w1, w2 = split_flex_columns(remaining_w)
                res[flex_cols[0].name] = w1
                res[flex_cols[1].name] = w2
            else:
                used_flex = 0
                for i, col in enumerate(flex_cols):
                    if i == len(flex_cols) - 1:
                        res[col.name] = max(0, remaining_w - used_flex)
                    else:
                        r = 1.0 / len(flex_cols)
                        w = max(0, int(remaining_w * r))
                        res[col.name] = w
                        used_flex += w

    if inner_w >= spec.wide_threshold:
        fixed_used = 0
        for col in non_flex_cols:
            w = col.fixed_w if col.fixed_w is not None else max(col.min_w, min(col.max_w or 999, int(avail * col.ratio)))
            res[col.name] = w
            fixed_used += w
        rem = inner_w - fixed_used - spec.spacing
        _allocate_flex(rem)
    elif avail < spec.narrow_threshold:
        used = 0
        for idx, col in enumerate(spec.columns):
            if idx == len(spec.columns) - 1:
                res[col.name] = max(0, avail - used) if avail >= col.min_avail_threshold else 0
            else:
                r = col.narrow_ratio if col.narrow_ratio is not None else col.ratio
                val = max(1 if avail >= col.min_avail_threshold else 0, int(avail * r))
                res[col.name] = val
                used += val
    else:
        non_flex_used = 0
        for col in non_flex_cols:
            val = max(col.min_w, min(col.max_w or 999, int(avail * col.ratio)))
            res[col.name] = val
            non_flex_used += val

        rem = avail - non_flex_used
        if flex_cols:
            _allocate_flex(rem)

    total = sum(res.values()) + spec.spacing
    if total > inner_w:
        over = total - inner_w
        indexed_cols = list(enumerate(spec.columns))
        sorted_cols = sorted(indexed_cols, key=lambda pair: (not pair[1].is_flex, -pair[0]))
        reduction_order = [col.name for _, col in sorted_cols]

        for key in reduction_order:
            if key in res and over > 0:
                if res[key] >= over:
                    res[key] -= over
                    over = 0
                    break
                else:
                    over -= res[key]
                    res[key] = 0

    return res


def allocate_approved_pr_columns(inner_w: int, has_repo: bool = False) -> Dict[str, int]:
    """Calculate column widths for approved PRs panel based on container width and repo visibility.

    Note: Fixed columns and flex columns enforce minimum width floor values for narrow container widths,
    scaling down when necessary to ensure column widths and separators do not exceed inner_w.
    """
    spec = APPROVED_PR_HAS_REPO_SPEC if has_repo else APPROVED_PR_NO_REPO_SPEC
    return allocate_declarative_columns(spec, inner_w)


def allocate_scheduled_job_columns(inner_w: int) -> Tuple[int, int, int]:
    """Calculate dynamic flexible column widths (id_w, name_w, agent_w) for scheduled jobs table mode."""
    fixed_w = 38  # 2 + 6 + 10 + 10 + 10 (SEL, INTV, LAST RUN, NEXT RUN, REMAIN)
    spacers = 7   # 8 columns -> 7 space separators
    flex_avail = max(0, inner_w - fixed_w - spacers)

    min_scale = min(1.0, flex_avail / 52.0)
    min_bounds = {
        col.name: max(1 if flex_avail >= col.min_avail_threshold else 0, int(col.min_w * min_scale))
        for col in SCHEDULED_JOB_COLUMN_SPECS
    }

    widths: Dict[str, int] = {}
    for idx, col in enumerate(SCHEDULED_JOB_COLUMN_SPECS):
        if idx == len(SCHEDULED_JOB_COLUMN_SPECS) - 1:
            widths[col.name] = max(0, flex_avail - sum(widths.values()))
        else:
            subsequent_min = sum(min_bounds[c.name] for c in SCHEDULED_JOB_COLUMN_SPECS[idx + 1:])
            alloc_w = int(flex_avail * col.ratio)
            max_allowed = flex_avail - sum(widths.values()) - subsequent_min
            widths[col.name] = max(min_bounds[col.name], min(max_allowed, alloc_w))

    return widths.get("id", 0), widths.get("name", 0), widths.get("agent", 0)


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

    if next_dt.tzinfo is None:
        next_dt = next_dt.replace(tzinfo=timezone.utc)

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
    is_paused: bool = False,
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

    info_line = f"Host: {host}:{port} │ Branch: {branch} │ Commit: {commit} │ Uptime: {uptime}"

    if active_screen == "jobs":
        nav_hint = "Nav: [↑/↓] Select │ [Space] Toggle │ [e/d] Enable/Disable │ [Esc] Main Screen"
    elif active_screen == "logs":
        nav_hint = "Nav: [Esc] Main Screen"
    elif active_screen == "gemini_models":
        nav_hint = "Nav: [↑/↓] Navigate │ [Space/Enter] Select Model │ [Esc] Main Screen"
    elif active_screen == "third_party_models":
        nav_hint = "Nav: [↑/↓] Navigate │ [Space/Enter] Select Model │ [Esc] Main Screen"
    else:
        pause_hint = "[v] Resume Tasks" if is_paused else "[v] Pause Tasks"
        nav_hint = f"Nav: [g] Gemini │ [c] Claude │ [↑/↓] Select Task │ [p] Prioritize │ [j] Jobs │ [e] Logs │ {pause_hint}"

    lines = [
        line1_raw,
        f"\033[2m{info_line}\033[0m",
        f"\033[93m\033[1m{nav_hint}\033[0m",
    ]
    header_bar = "┌" + "─" * (width - 2) + "┐"
    return render_panel_frame(header_bar, lines, width)


def render_quota_panel(
    width: int,
    quota_tracker: Optional[QuotaTracker] = None,
    task_manager: Optional[TaskManager] = None,
    now_dt: Optional[datetime] = None,
) -> List[str]:
    """Render model quota panel for 5-hour and 1-week windows for dual pools (Gemini and 3rd Party)."""
    quota_tr = quota_tracker or (getattr(task_manager, "quota_tracker", None) if task_manager else None)

    if quota_tr and hasattr(quota_tr, "get_pool_windows"):
        w_5h_g, w_1w_g = quota_tr.get_pool_windows("gemini")
        w_5h_c, w_1w_c = quota_tr.get_pool_windows("claude_gpt")
    elif quota_tr:
        rem_pct = getattr(quota_tr, "remaining_percentage", 100.0)
        w_5h_g = getattr(quota_tr, "gemini_window_5h", None) or getattr(quota_tr, "window_5h", None) or QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=rem_pct)
        w_1w_g = getattr(quota_tr, "gemini_window_1w", None) or getattr(quota_tr, "window_1w", None) or QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)
        w_5h_c = getattr(quota_tr, "claude_window_5h", None) or QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=100.0)
        w_1w_c = getattr(quota_tr, "claude_window_1w", None) or QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)
    else:
        w_5h_g = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=100.0)
        w_1w_g = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)
        w_5h_c = QuotaWindow(name="5H", duration_seconds=18000.0, remaining_percentage=100.0)
        w_1w_c = QuotaWindow(name="1W", duration_seconds=604800.0, remaining_percentage=100.0)

    badge_5h_g = format_quota_badge(w_5h_g, now_dt=now_dt, quota_pool="GEMINI")
    badge_1w_g = format_quota_badge(w_1w_g, now_dt=now_dt, quota_pool="GEMINI")
    badge_5h_c = format_quota_badge(w_5h_c, now_dt=now_dt, quota_pool="3RD PARTY")
    badge_1w_c = format_quota_badge(w_1w_c, now_dt=now_dt, quota_pool="3RD PARTY")

    status_5h_g, _ = w_5h_g.get_pacing_status(now_dt)
    status_1w_g, _ = w_1w_g.get_pacing_status(now_dt)
    status_5h_c, _ = w_5h_c.get_pacing_status(now_dt)
    status_1w_c, _ = w_1w_c.get_pacing_status(now_dt)

    color_5h_g = (
        "\033[92m\033[1m"
        if status_5h_g == "OK" and w_5h_g.remaining_percentage > 0
        else ("\033[91m\033[1m" if w_5h_g.remaining_percentage == 0 else "\033[93m\033[1m")
    )
    color_1w_g = (
        "\033[92m\033[1m"
        if status_1w_g == "OK" and w_1w_g.remaining_percentage > 0
        else ("\033[91m\033[1m" if w_1w_g.remaining_percentage == 0 else "\033[93m\033[1m")
    )
    color_5h_c = (
        "\033[92m\033[1m"
        if status_5h_c == "OK" and w_5h_c.remaining_percentage > 0
        else ("\033[91m\033[1m" if w_5h_c.remaining_percentage == 0 else "\033[93m\033[1m")
    )
    color_1w_c = (
        "\033[92m\033[1m"
        if status_1w_c == "OK" and w_1w_c.remaining_percentage > 0
        else ("\033[91m\033[1m" if w_1w_c.remaining_percentage == 0 else "\033[93m\033[1m")
    )

    header_bar = render_panel_header(width, "ANTIGRAVITY MODEL QUOTA (DUAL-POOL)", "\033[96m\033[1m")

    lines = [
        f"{color_5h_g}{badge_5h_g}\033[0m",
        f"{color_1w_g}{badge_1w_g}\033[0m",
        f"{color_5h_c}{badge_5h_c}\033[0m",
        f"{color_1w_c}{badge_1w_c}\033[0m",
    ]
    return render_panel_frame(header_bar, lines, width)


def render_gemini_models_panel(
    width: int,
    models: List[str],
    selected_index: int = 0,
    active_model: str = "gemini-2.5-flash",
) -> List[str]:
    """Render Gemini (1st party) model selection panel."""
    header_bar = render_panel_header(width, "GEMINI MODEL SELECTION (1ST PARTY)", "\033[96m\033[1m")
    lines = []
    for idx, model_name in enumerate(models):
        is_selected = (idx == selected_index)
        is_active = (model_name == active_model)
        prefix = "> " if is_selected else "  "
        active_tag = " \033[92m\033[1m[ACTIVE]\033[0m" if is_active else ""
        if is_selected:
            line_str = f"\033[93m\033[1m{prefix}{model_name}\033[0m{active_tag}"
        else:
            line_str = f"{prefix}{model_name}{active_tag}"
        lines.append(line_str)

    if not lines:
        lines.append("\033[2m(No Gemini models available)\033[0m")

    return render_panel_frame(header_bar, lines, width)


def render_third_party_models_panel(
    width: int,
    models: List[str],
    selected_index: int = 0,
    active_model: str = "claude-3-5-sonnet",
) -> List[str]:
    """Render 3rd party (Claude/GPT) model selection panel."""
    header_bar = render_panel_header(width, "3RD PARTY MODEL SELECTION (CLAUDE / GPT)", "\033[96m\033[1m")
    lines = []
    for idx, model_name in enumerate(models):
        is_selected = (idx == selected_index)
        is_active = (model_name == active_model)
        prefix = "> " if is_selected else "  "
        active_tag = " \033[92m\033[1m[ACTIVE]\033[0m" if is_active else ""
        if is_selected:
            line_str = f"\033[93m\033[1m{prefix}{model_name}\033[0m{active_tag}"
        else:
            line_str = f"{prefix}{model_name}{active_tag}"
        lines.append(line_str)

    if not lines:
        lines.append("\033[2m(No 3rd party models available)\033[0m")

    return render_panel_frame(header_bar, lines, width)


def render_active_tasks_panel(width: int, tasks: List[Any], max_workers: int) -> List[str]:
    """Render active running tasks panel."""
    inner_w = max(0, width - 4)
    active_cnt = len(tasks)
    panel_title = f"ACTIVE TASKS (RUNNING) [{active_cnt}/{max_workers} Workers Active]"
    header_bar = render_panel_header(width, panel_title, "\033[94m\033[1m")

    if not tasks:
        msg_styled = "\033[2m(No active tasks currently running)\033[0m"
        return render_panel_frame(header_bar, [msg_styled], width)

    target_w = max(8, inner_w - 51)
    cols = [
        ("ID", 8),
        ("AGENT", 14),
        ("TARGET", target_w),
        ("ATTEMPT", 16),
        ("ELAPSED", 9),
    ]
    content = [f"\033[1m{format_table_row(cols)}\033[0m"]

    for t in tasks:
        target_str = format_target_for_display(t.target_id, target_w)
        is_cached = getattr(t, "requeue_count", 0) > 0
        att_str = f"{t.attempt}/{t.max_attempts} (cached)" if is_cached else f"{t.attempt}/{t.max_attempts}"
        row_cells = [
            (t.id, 8),
            (t.agent, 14),
            (target_str, target_w),
            (att_str, 16),
            (f"{t.elapsed_time:.1f}s", 9),
        ]
        content.append(format_table_row(row_cells))

    return render_panel_frame(header_bar, content, width)


def render_queued_tasks_panel(
    width: int,
    tasks: List[Any],
    is_paused: bool = False,
    selected_queue_index: int = 0,
) -> List[str]:
    """Render pending queued tasks panel."""
    inner_w = max(0, width - 4)
    queued_cnt = len(tasks)
    paused_badge = " [PAUSED]" if is_paused else ""
    panel_title = f"TASK QUEUE (QUEUED) [{queued_cnt} Pending]{paused_badge}"
    header_bar = render_panel_header(width, panel_title, "\033[93m\033[1m")

    if not tasks:
        msg_styled = "\033[2m(Task queue is empty)\033[0m"
        return render_panel_frame(header_bar, [msg_styled], width)

    if tasks:
        selected_queue_index = max(0, min(selected_queue_index, len(tasks) - 1))
    else:
        selected_queue_index = 0

    # Fixed column widths: cursor(2) + ID(8) + PRIO(6) + AGENT(14) + ATTEMPT(16) + WAIT(9) = 55.
    # Plus 6 single-space column separators = 61 total fixed characters.
    target_w = max(8, inner_w - 61)
    cols = [
        (" ", 2),
        ("ID", 8),
        ("PRIO", 6),
        ("AGENT", 14),
        ("TARGET", target_w),
        ("ATTEMPT", 16),
        ("WAIT", 9),
    ]
    content = [f"\033[1m{format_table_row(cols)}\033[0m"]

    for idx, t in enumerate(tasks):
        is_selected = (idx == selected_queue_index)
        cursor_str = "> " if is_selected else "  "
        cursor_styled = f"\033[93m\033[1m{cursor_str}\033[0m" if is_selected else cursor_str
        prio_str = str(getattr(t, "priority", 0))
        target_str = format_target_for_display(t.target_id, target_w)
        is_cached = getattr(t, "requeue_count", 0) > 0
        att_str = f"{t.attempt}/{t.max_attempts} (cached)" if is_cached else f"{t.attempt}/{t.max_attempts}"
        row_cells = [
            (cursor_styled, 2),
            (t.id, 8),
            (prio_str, 6),
            (t.agent, 14),
            (target_str, target_w),
            (att_str, 16),
            (f"{t.wait_time:.1f}s", 9),
        ]
        content.append(format_table_row(row_cells))

    return render_panel_frame(header_bar, content, width)


def render_scheduled_jobs_panel(
    width: int,
    scheduler: Optional[TaskScheduler],
    selected_job_index: int = 0,
    mode: str = "card",
) -> List[str]:
    """Render periodic scheduled jobs panel in card or table view mode."""
    inner_w = width - 4
    panel_title = "SCHEDULED JOBS [(j/k) select | (space) toggle | (e/d) state | (r)un ]"
    header_bar = render_panel_header(width, panel_title, "\033[96m\033[1m")

    if not scheduler or not scheduler.jobs:
        if not scheduler:
            msg = "(Scheduler disabled)"
        else:
            msg = "(No scheduled jobs configured)"
        msg_styled = f"\033[2m{msg}\033[0m"
        return render_panel_frame(header_bar, [msg_styled], width)

    with getattr(scheduler, "_lock", nullcontext()):
        jobs_list = list(scheduler.jobs.values())
    if jobs_list:
        selected_job_index = max(0, min(selected_job_index, len(jobs_list) - 1))
    else:
        selected_job_index = 0

    content = []
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
            content.append(header_line)

            interval_str = format_interval(job.interval_seconds)
            last_run_str = format_timestamp(job.last_run)
            next_run_str = format_timestamp(job.next_run)
            rem_str = format_remaining(job, now_dt)
            sched_line = (
                f"  Name: {job.name} │ Interval: {interval_str} │ "
                f"Last Run: {last_run_str} │ Next Run: {next_run_str} │ "
                f"Remaining: {rem_str}"
            )
            content.append(sched_line)

            prompt_line = f"  \033[2mPrompt: {job.prompt}\033[0m"
            content.append(prompt_line)

            if idx < len(jobs_list) - 1:
                sep_line = "\033[90m" + ("─" * inner_w) + "\033[0m"
                content.append(sep_line)
    else:
        id_w, name_w, agent_w = allocate_scheduled_job_columns(inner_w)

        hdr_cells = [
            (" ", 2),
            ("JOB ID", id_w),
            ("NAME", name_w),
            ("AGENT", agent_w),
            ("INTV", 6),
            ("LAST RUN", 10),
            ("NEXT RUN", 10),
            ("REMAIN", 10),
        ]
        content.append(f"\033[1m{format_table_row(hdr_cells)}\033[0m")

        now_dt = datetime.now(timezone.utc)
        for idx, job in enumerate(jobs_list):
            is_selected = (idx == selected_job_index)
            cursor_str = "> " if is_selected else "  "
            cursor_styled = f"\033[93m\033[1m{cursor_str}\033[0m" if is_selected else cursor_str

            id_trunc = truncate_with_ellipsis(job.job_id, id_w)
            name_trunc = truncate_with_ellipsis(job.name, name_w)
            agent_trunc = truncate_with_ellipsis(job.agent, agent_w)

            row_cells = [
                (cursor_styled, 2),
                (id_trunc, id_w),
                (name_trunc, name_w),
                (agent_trunc, agent_w),
                (format_interval(job.interval_seconds), 6),
                (format_timestamp(job.last_run), 10),
                (format_timestamp(job.next_run), 10),
                (format_remaining(job, now_dt), 10),
            ]
            row_str = format_table_row(row_cells)
            content.append(row_str)

    return render_panel_frame(header_bar, content, width)


def render_approved_prs_panel(width: int, approved_prs: List[Dict[str, Any]]) -> List[str]:
    """Render approved pull requests panel."""
    inner_w = width - 4
    approved_cnt = len(approved_prs)
    panel_title = f"APPROVED PULL REQUESTS (READY TO MERGE) [{approved_cnt} Ready]"
    header_bar = render_panel_header(width, panel_title, "\033[92m\033[1m")

    if not approved_prs:
        msg_styled = "\033[2m(No approved PRs awaiting merge)\033[0m"
        return render_panel_frame(header_bar, [msg_styled], width)

    has_repo = any(bool(pr.get("repo_full_name")) for pr in approved_prs)
    cols = allocate_approved_pr_columns(inner_w, has_repo)

    content = []
    if has_repo:
        hdr_cells = [
            ("PR #", cols["pr"]),
            ("REPO", cols["repo"]),
            ("TITLE", cols["title"]),
            ("AUTHOR", cols["author"]),
            ("URL", cols["url"]),
        ]
    else:
        hdr_cells = [
            ("PR #", cols["pr"]),
            ("TITLE", cols["title"]),
            ("AUTHOR", cols["author"]),
            ("URL", cols["url"]),
        ]

    content.append(f"\033[1m{format_table_row(hdr_cells)}\033[0m")

    for pr in approved_prs:
        pr_num = pr.get("number")
        num_str = f"#{pr_num}" if pr_num is not None and pr_num != "" else ""
        title_str = pr.get("title") or ""
        author_str = pr.get("author") or ""
        url_str = pr.get("url") or ""
        if has_repo:
            repo_str = pr.get("repo_full_name", "") or "-"
            row_cells = [
                (num_str, cols["pr"]),
                (repo_str, cols["repo"]),
                (title_str, cols["title"]),
                (author_str, cols["author"]),
                (url_str, cols["url"]),
            ]
        else:
            row_cells = [
                (num_str, cols["pr"]),
                (title_str, cols["title"]),
                (author_str, cols["author"]),
                (url_str, cols["url"]),
            ]
        content.append(format_table_row(row_cells))

    return render_panel_frame(header_bar, content, width)


def render_history_tasks_panel(width: int, tasks: List[Any], stats: Dict[str, Any]) -> List[str]:
    """Render task execution history panel."""
    inner_w = max(0, width - 4)
    passed = stats.get("completed", 0)
    failed = stats.get("failed", 0)
    panel_title = f"TASK HISTORY (COMPLETED & FAILED) [Passed: {passed} | Failed: {failed}]"
    header_bar = render_panel_header(width, panel_title, "\033[95m\033[1m")

    if not tasks:
        msg_styled = "\033[2m(No task history recorded yet)\033[0m"
        return render_panel_frame(header_bar, [msg_styled], width)

    target_w = max(8, inner_w - 63)
    cols = [
        ("ID", 8),
        ("STATUS", 9),
        ("AGENT", 10),
        ("TARGET", target_w),
        ("ATTEMPT", 16),
        ("RETURN", 6),
        ("DURATION", 8),
    ]
    content = [f"\033[1m{format_table_row(cols)}\033[0m"]

    for t in tasks:
        status_color = "\033[92m" if t.status == TaskStatus.COMPLETED else "\033[91m"
        status_str = f"{status_color}{t.status}\033[0m"
        ret_val = str(t.return_code) if t.return_code is not None else "-"
        target_str = format_target_for_display(t.target_id, target_w)
        is_cached = getattr(t, "requeue_count", 0) > 0
        att_str = f"{t.attempt}/{t.max_attempts} (cached)" if is_cached else f"{t.attempt}/{t.max_attempts}"
        row_cells = [
            (t.id, 8),
            (status_str, 9),
            (t.agent, 10),
            (target_str, target_w),
            (att_str, 16),
            (ret_val, 6),
            (f"{t.elapsed_time:.1f}s", 8),
        ]
        content.append(format_table_row(row_cells))

    return render_panel_frame(header_bar, content, width)


def render_event_logs_panel(
    width: int,
    log_handler: Optional[Any] = None,
    limit: int = 15,
) -> List[str]:
    """Render recent event log records panel."""
    panel_title = "EVENT LOGS"
    header_bar = render_panel_header(width, panel_title, "\033[96m\033[1m")

    recent_logs = log_handler.get_logs(limit=limit) if log_handler else []
    if not recent_logs:
        msg_styled = "\033[2m(No event logs recorded yet)\033[0m"
        return render_panel_frame(header_bar, [msg_styled], width)

    content = []
    for log_entry in recent_logs:
        if log_entry is None:
            log_entry = ""
        elif not isinstance(log_entry, str):
            log_entry = str(log_entry)
        clean_entry = log_entry.replace("\r\n", " ").replace("\n", " ")
        content.append(f"\033[2m{clean_entry}\033[0m")

    return render_panel_frame(header_bar, content, width)
