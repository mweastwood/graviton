#!/usr/bin/env python3
"""
Markdown dashboard formatting and section generators for Graviton.
Zero external dependencies (Python standard library only).
"""

import datetime
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from lib.dashboard.common import (
    _build_pacing_details,
    format_duration,
    format_percentage,
    is_safe_url,
    logger,
    resolve_target_url,
)
from lib.pr_tracker import PRTracker
from lib.quota import (
    DEFAULT_GEMINI_MODELS,
    DEFAULT_THIRD_PARTY_MODELS,
    QuotaTracker,
    QuotaWindow,
    format_pacing_recovery_countdown,
    format_reset_countdown,
)
from lib.scheduler import TaskScheduler
from lib.tasks import Task, TaskManager, TaskStatus


def _format_target_markdown_cell(target_disp: Any, target_url: Optional[str]) -> str:
    """Format target cell for markdown table, stripping existing formatting to prevent double-backticks or nested links."""
    clean_disp = str(target_disp or "N/A").replace("\r", " ").replace("\n", " ").strip().strip("`").strip()
    md_m = re.match(r"^\[(.*?)\]\((.*?)\)$", clean_disp)
    if md_m:
        clean_disp = md_m.group(1).strip("` ").strip()
    if not clean_disp:
        clean_disp = "N/A"
    clean_disp = clean_disp.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    if target_url and is_safe_url(target_url):
        safe_url = target_url.strip().replace(")", "%29").replace("(", "%28").replace("|", "")
        return f"[`{clean_disp}`]({safe_url})"
    return f"`{clean_disp}`"


def _fmt_window_val_and_details(w: Any, fallback_pct: Any = None, default_name: str = "5H") -> Tuple[str, str]:
    """Format a quota window object or dictionary into display percentage and pacing details string."""
    if w is None:
        if fallback_pct is not None and fallback_pct != "N/A":
            disp = format_percentage(fallback_pct)
            return disp, "Live quota capacity"
        return "N/A", "N/A"
    if isinstance(w, dict):
        win_name = w.get("name") or default_name
        pct = w.get("remaining_percentage")
        pct_disp = format_percentage(pct) if pct is not None else "N/A"
        cd = w.get("reset_countdown")
        if cd is None and w.get("reset_time") is not None:
            try:
                cd = format_reset_countdown(w.get("reset_time"), window_name=win_name)
            except Exception:
                cd = str(w.get("reset_time"))
        if not cd:
            cd = "N/A"
        status = w.get("pacing_status", "OK")
        if pct is None and cd == "N/A" and (status == "OK" or not status):
            return "N/A", "N/A"
        rec_cd = w.get("pacing_recovery_countdown")
        if (not rec_cd or rec_cd == "00:00:00") and w.get("pacing_recovery_seconds") is not None:
            try:
                rec_sec = float(w.get("pacing_recovery_seconds"))
                if rec_sec > 0:
                    rec_cd = format_pacing_recovery_countdown(rec_sec)
            except Exception:
                pass
        if (not rec_cd or rec_cd == "00:00:00") and status == "BEHIND_PACING" and w.get("reset_time") is not None and pct is not None:
            try:
                dur = w.get("duration_seconds")
                if dur is None:
                    dur = 18000.0 if str(win_name).upper() == "5H" else 604800.0
                qw = QuotaWindow(
                    name=win_name,
                    duration_seconds=dur,
                    remaining_percentage=pct,
                    reset_time=w.get("reset_time"),
                )
                qw_rec = qw.format_pacing_countdown()
                if qw_rec and qw_rec != "00:00:00":
                    rec_cd = qw_rec
            except Exception:
                pass
        return pct_disp, _build_pacing_details(cd, status, rec_cd)

    # QuotaWindow object
    pct = w.remaining_percentage
    pct_disp = format_percentage(pct)
    cd = w.format_reset_countdown()
    status, _ = w.get_pacing_status()
    if pct is None and cd == "N/A" and (status == "OK" or not status):
        return "N/A", "N/A"
    return pct_disp, _build_pacing_details(cd, status, w.format_pacing_countdown())


def _gather_markdown_context(
    task_manager: Optional[TaskManager] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    scheduler: Optional[TaskScheduler] = None,
    host: str = "localhost",
    port: int = 8000,
    extra_info: Optional[Dict[str, Any]] = None,
    pr_tracker: Optional[PRTracker] = None,
) -> Dict[str, Any]:
    """Gather server, manager, and quota data into a normalized context dictionary for markdown generation."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stats = task_manager.get_stats() if task_manager else {}
    active_tasks = task_manager.get_active_tasks() if task_manager else []
    queued_tasks = task_manager.get_queued_tasks() if task_manager else []
    recent_history = task_manager.get_task_history(limit=10) if task_manager else []
    quota_info = quota_tracker.get_info().to_dict() if quota_tracker else {}
    if not quota_info and extra_info and "quota_info" in extra_info:
        quota_info = extra_info.get("quota_info") or {}
    approved_prs: List[Dict[str, Any]] = []
    if pr_tracker and hasattr(pr_tracker, "get_approved_prs"):
        try:
            approved_prs = pr_tracker.get_approved_prs() or []
        except Exception as e:
            logger.debug(f"Error fetching approved PRs from pr_tracker: {e}")
    if not approved_prs and extra_info and "approved_prs" in extra_info:
        approved_prs = extra_info.get("approved_prs") or []

    if active_tasks:
        status_badge = "🟡 **BUSY**"
    elif task_manager and getattr(task_manager, "_draining", False):
        status_badge = "⏳ **DRAINING**"
    elif task_manager and getattr(task_manager, "_paused", False):
        status_badge = "⏸️ **PAUSED**"
    else:
        status_badge = "🟢 **ONLINE**"

    web_url = f"http://{host}:{port}/dashboard"

    return {
        "now_iso": now_iso,
        "stats": stats,
        "active_tasks": active_tasks,
        "queued_tasks": queued_tasks,
        "recent_history": recent_history,
        "quota_info": quota_info,
        "approved_prs": approved_prs,
        "status_badge": status_badge,
        "web_url": web_url,
        "host": host,
        "port": port,
    }


def _md_header(ctx: Dict[str, Any]) -> List[str]:
    """Render the dashboard top title, status alert box, and server URL."""
    return [
        "# 🌌 Graviton Live Dashboard",
        "",
        "> [!NOTE]",
        f"> **Status**: {ctx['status_badge']} &nbsp;|&nbsp; **Server**: `{ctx['host']}:{ctx['port']}` &nbsp;|&nbsp; [Open Web Dashboard 🌐]({ctx['web_url']})",
        f"> *Last updated: {ctx['now_iso']} (Auto-refreshed by Graviton)*",
        "",
        "---",
        "",
    ]


def _md_pipeline(ctx: Dict[str, Any]) -> List[str]:
    """Render task pipeline execution summary table."""
    stats = ctx["stats"]
    return [
        "## 📊 Task Pipeline",
        "",
        "| Metric | Count | Description |",
        "| :--- | :--- | :--- |",
        f"| **Active Workers** | `{stats.get('active_workers', 0)} / {stats.get('max_workers', 0)}` | Worker threads currently processing tasks |",
        f"| **Running Tasks** | `{stats.get('active_tasks', 0)}` | Active containerized supervisor sessions |",
        f"| **Queued Tasks** | `{stats.get('queued_tasks', 0)}` | Tasks awaiting available worker slot |",
        f"| **Completed Tasks** | `{stats.get('completed_tasks', 0)}` | Successfully finished tasks |",
        f"| **Failed Tasks** | `{stats.get('failed_tasks', 0)}` | Failed or aborted executions |",
        "",
        "---",
        "",
    ]


def _md_active_tasks(ctx: Dict[str, Any]) -> List[str]:
    """Render active container tasks table or empty state."""
    active_tasks = ctx["active_tasks"]
    lines = [
        "## 🚀 Active Container Tasks",
        "",
    ]
    if active_tasks:
        lines.extend([
            "| Task ID | Agent | Model | Target | Elapsed | Status |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        now_ts = time.time()
        for t in active_tasks:
            elapsed = format_duration(now_ts - t.start_time) if t.start_time else "starting..."
            target_disp = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
            model_val = getattr(t, "selected_model", None) or getattr(t, "selected_pool", None)
            model_name = model_val if isinstance(model_val, str) and model_val.strip() else "-"
            model_disp = f"`{model_name}`" if model_name != "-" else "-"
            target_url = resolve_target_url(target_disp, repo=getattr(t, "repo_full_name", None), agent=getattr(t, "agent", None))
            target_cell = _format_target_markdown_cell(target_disp, target_url)
            lines.append(f"| `{t.id}` | `{t.agent}` | {model_disp} | {target_cell} | {elapsed} | 🔄 `{t.status}` |")
        lines.append("")
    else:
        lines.extend(["*No container tasks currently running.*", ""])
    lines.extend(["---", ""])
    return lines


def _md_quota_gemini_rows(
    pool: str,
    gemini_model: str,
    g_5h_val: str,
    g_5h_details: str,
    g_1w_val: str,
    g_1w_details: str,
    gemini_disp: str,
) -> List[str]:
    """Render markdown table rows specific to Gemini quota windows and capacity."""
    return [
        f"| **Active Gemini Model** | `{gemini_model}` | Active Gemini model persona |",
        f"| **Gemini (5H)** | `{g_5h_val}` | {g_5h_details} |",
        f"| **Gemini (1W)** | `{g_1w_val}` | {g_1w_details} |",
        f"| **Gemini Remaining** | `{gemini_disp}` | Live Gemini API capacity |",
    ]


def _md_quota_third_party_rows(
    tp_model: str,
    c_5h_val: str,
    c_5h_details: str,
    c_1w_val: str,
    c_1w_details: str,
    tp_disp: str,
) -> List[str]:
    """Render markdown table rows specific to Third-Party quota windows and capacity."""
    return [
        f"| **Active Third-Party Model** | `{tp_model}` | Active Third-Party model persona |",
        f"| **Third-Party (5H)** | `{c_5h_val}` | {c_5h_details} |",
        f"| **Third-Party (1W)** | `{c_1w_val}` | {c_1w_details} |",
        f"| **Third-Party Remaining** | `{tp_disp}` | Fallback model capacity |",
    ]


def _md_quota(ctx: Dict[str, Any], quota_tracker: Optional[QuotaTracker] = None) -> List[str]:
    """Render model quota pacing table for both Gemini and third-party models."""
    quota_info = ctx["quota_info"]

    pool = (
        quota_info.get("quota_pool")
        or quota_info.get("current_pool")
        or (getattr(quota_tracker, "quota_pool", None) if quota_tracker else None)
        or "default"
    )

    model = quota_info.get("selected_model") or quota_info.get("active_model")
    if not model and quota_tracker:
        if hasattr(quota_tracker, "get_active_model"):
            try:
                model = quota_tracker.get_active_model(pool)
            except Exception:
                try:
                    model = quota_tracker.get_active_model()
                except Exception:
                    pass
        elif hasattr(quota_tracker, "get_selected_model"):
            try:
                model = quota_tracker.get_selected_model()
            except Exception:
                pass
    if not model:
        model = "default"

    gemini_model = None
    tp_model = None
    if quota_tracker and hasattr(quota_tracker, "get_active_model"):
        try:
            gemini_model = quota_tracker.get_active_model("gemini")
        except Exception:
            pass
        try:
            tp_model = quota_tracker.get_active_model("third_party")
        except Exception:
            pass
    if not gemini_model and quota_info:
        gemini_model = quota_info.get("active_gemini_model")
    if not tp_model and quota_info:
        tp_model = quota_info.get("active_third_party_model")

    p_low = str(pool).lower()
    p_info = str((quota_info.get("quota_pool") if quota_info else None) or p_low).lower()
    is_tp = "claude" in p_info or "gpt" in p_info or "3p" in p_info or "third" in p_info

    if not gemini_model:
        gemini_model = model if not is_tp and model != "default" else (DEFAULT_GEMINI_MODELS[0] if DEFAULT_GEMINI_MODELS else "default")
    if not tp_model:
        tp_model = model if is_tp and model != "default" else (DEFAULT_THIRD_PARTY_MODELS[0] if DEFAULT_THIRD_PARTY_MODELS else "default")

    gemini_rem = quota_info.get("gemini_remaining_percentage")
    if gemini_rem is None and quota_tracker and hasattr(quota_tracker, "get_pool_remaining_percentage"):
        try:
            gemini_rem = quota_tracker.get_pool_remaining_percentage("gemini")
        except Exception:
            pass
    if gemini_rem is None and not is_tp:
        gemini_rem = quota_info.get("remaining_percentage")
    if gemini_rem is None or str(gemini_rem).strip().lower() in ("none", "null", "n/a", "unknown", "none%"):
        gemini_rem = "N/A"
        gemini_disp = "N/A"
    else:
        gemini_disp = f"{gemini_rem}%" if not str(gemini_rem).endswith("%") else str(gemini_rem)

    tp_rem = quota_info.get("third_party_remaining_percentage")
    if tp_rem is None and quota_tracker and hasattr(quota_tracker, "get_pool_remaining_percentage"):
        try:
            tp_rem = quota_tracker.get_pool_remaining_percentage("claude")
        except Exception:
            pass
    if tp_rem is None and is_tp:
        tp_rem = quota_info.get("remaining_percentage")
    if tp_rem is None or str(tp_rem).strip().lower() in ("none", "null", "n/a", "unknown", "none%"):
        tp_rem = "N/A"
        tp_disp = "N/A"
    else:
        tp_disp = f"{tp_rem}%" if not str(tp_rem).endswith("%") else str(tp_rem)

    w5_g, w1_g = (None, None)
    w5_c, w1_c = (None, None)
    if quota_tracker and hasattr(quota_tracker, "get_pool_windows"):
        try:
            w5_g, w1_g = quota_tracker.get_pool_windows("gemini")
            w5_c, w1_c = quota_tracker.get_pool_windows("claude_gpt")
        except Exception:
            pass
    elif quota_tracker:
        p_tr = str(getattr(quota_tracker, "quota_pool", pool) or "").lower()
        is_tp_tr = "claude" in p_tr or "gpt" in p_tr or "3p" in p_tr or "third" in p_tr
        w5_g = getattr(quota_tracker, "gemini_window_5h", None) or (None if is_tp_tr else getattr(quota_tracker, "window_5h", None))
        w1_g = getattr(quota_tracker, "gemini_window_1w", None) or (None if is_tp_tr else getattr(quota_tracker, "window_1w", None))
        w5_c = getattr(quota_tracker, "claude_window_5h", None) or (getattr(quota_tracker, "window_5h", None) if is_tp_tr else None)
        w1_c = getattr(quota_tracker, "claude_window_1w", None) or (getattr(quota_tracker, "window_1w", None) if is_tp_tr else None)

    if quota_info:
        if w5_g is None:
            w5_g = quota_info.get("gemini_window_5h") or (None if is_tp else quota_info.get("window_5h"))
        if w1_g is None:
            w1_g = quota_info.get("gemini_window_1w") or (None if is_tp else quota_info.get("window_1w"))
        if w5_c is None:
            w5_c = quota_info.get("claude_window_5h") or (quota_info.get("window_5h") if is_tp else None)
        if w1_c is None:
            w1_c = quota_info.get("claude_window_1w") or (quota_info.get("window_1w") if is_tp else None)

        if w5_g is None and quota_info.get("gemini_5h_remaining_percentage") is not None:
            w5_g = {
                "name": "5H",
                "remaining_percentage": quota_info.get("gemini_5h_remaining_percentage"),
                "reset_countdown": quota_info.get("gemini_5h_countdown"),
                "reset_time": quota_info.get("gemini_5h_reset_time"),
                "pacing_status": quota_info.get("gemini_5h_pacing_status", "OK"),
                "pacing_recovery_countdown": quota_info.get("gemini_5h_pacing_recovery_countdown"),
                "pacing_recovery_seconds": quota_info.get("gemini_5h_pacing_recovery_seconds"),
            }
        if w1_g is None and quota_info.get("gemini_1w_remaining_percentage") is not None:
            w1_g = {
                "name": "1W",
                "remaining_percentage": quota_info.get("gemini_1w_remaining_percentage"),
                "reset_countdown": quota_info.get("gemini_1w_countdown"),
                "reset_time": quota_info.get("gemini_1w_reset_time"),
                "pacing_status": quota_info.get("gemini_1w_pacing_status", "OK"),
                "pacing_recovery_countdown": quota_info.get("gemini_1w_pacing_recovery_countdown"),
                "pacing_recovery_seconds": quota_info.get("gemini_1w_pacing_recovery_seconds"),
            }
        if w5_c is None and quota_info.get("third_party_5h_remaining_percentage") is not None:
            w5_c = {
                "name": "5H",
                "remaining_percentage": quota_info.get("third_party_5h_remaining_percentage"),
                "reset_countdown": quota_info.get("third_party_5h_countdown"),
                "reset_time": quota_info.get("third_party_5h_reset_time"),
                "pacing_status": quota_info.get("third_party_5h_pacing_status", "OK"),
                "pacing_recovery_countdown": quota_info.get("third_party_5h_pacing_recovery_countdown"),
                "pacing_recovery_seconds": quota_info.get("third_party_5h_pacing_recovery_seconds"),
            }
        if w1_c is None and quota_info.get("third_party_1w_remaining_percentage") is not None:
            w1_c = {
                "name": "1W",
                "remaining_percentage": quota_info.get("third_party_1w_remaining_percentage"),
                "reset_countdown": quota_info.get("third_party_1w_countdown"),
                "reset_time": quota_info.get("third_party_1w_reset_time"),
                "pacing_status": quota_info.get("third_party_1w_pacing_status", "OK"),
                "pacing_recovery_countdown": quota_info.get("third_party_1w_pacing_recovery_countdown"),
                "pacing_recovery_seconds": quota_info.get("third_party_1w_pacing_recovery_seconds"),
            }

    g_5h_val, g_5h_details = _fmt_window_val_and_details(w5_g, fallback_pct=gemini_rem, default_name="5H")
    g_1w_val, g_1w_details = _fmt_window_val_and_details(w1_g, fallback_pct=gemini_rem, default_name="1W")
    c_5h_val, c_5h_details = _fmt_window_val_and_details(w5_c, fallback_pct=tp_rem, default_name="5H")
    c_1w_val, c_1w_details = _fmt_window_val_and_details(w1_c, fallback_pct=tp_rem, default_name="1W")

    lines = [
        "## 🎯 Model Quota & Pacing",
        "",
        "| Metric | Value | Details |",
        "| :--- | :--- | :--- |",
        f"| **Active Pool** | `{pool}` | Configured quota bucket |",
        f"| **Active Model** | `{model}` | Active Gemini / LLM persona |",
    ]
    # Gemini section rows
    lines.append(f"| **Active Gemini Model** | `{gemini_model}` | Active Gemini model persona |")
    lines.append(f"| **Active Third-Party Model** | `{tp_model}` | Active Third-Party model persona |")
    lines.append(f"| **Gemini (5H)** | `{g_5h_val}` | {g_5h_details} |")
    lines.append(f"| **Gemini (1W)** | `{g_1w_val}` | {g_1w_details} |")
    lines.append(f"| **Third-Party (5H)** | `{c_5h_val}` | {c_5h_details} |")
    lines.append(f"| **Third-Party (1W)** | `{c_1w_val}` | {c_1w_details} |")
    lines.append(f"| **Gemini Remaining** | `{gemini_disp}` | Live Gemini API capacity |")
    lines.append(f"| **Third-Party Remaining** | `{tp_disp}` | Fallback model capacity |")
    lines.extend(["", "---", ""])
    return lines


def _md_queued(ctx: Dict[str, Any]) -> List[str]:
    """Render queued tasks table or empty state."""
    queued_tasks = ctx["queued_tasks"]
    lines = [
        "## ⏳ Queued Tasks",
        "",
    ]
    if queued_tasks:
        lines.extend([
            "| Task ID | Agent | Target | Priority | Queued Duration |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        now_ts = time.time()
        for t in queued_tasks:
            queued_dur = format_duration(now_ts - t.enqueue_time)
            target_disp = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
            target_url = resolve_target_url(target_disp, repo=getattr(t, "repo_full_name", None), agent=getattr(t, "agent", None))
            target_cell = _format_target_markdown_cell(target_disp, target_url)
            lines.append(f"| `{t.id}` | `{t.agent}` | {target_cell} | `{t.priority}` | {queued_dur} |")
        lines.append("")
    else:
        lines.extend(["*Queue is empty.*", ""])
    lines.extend(["---", ""])
    return lines


def _md_approved_prs(ctx: Dict[str, Any]) -> List[str]:
    """Render approved pull requests table or empty state."""
    approved_prs = ctx["approved_prs"]
    lines = [
        "## 🔀 Approved Pull Requests (Ready to Merge)",
        "",
    ]
    valid_prs = [pr for pr in approved_prs if isinstance(pr, dict)]
    if valid_prs:
        lines.extend([
            "| PR # | Repository | Title | Author | URL |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        for pr in valid_prs:
            pr_num_raw = pr.get("number")
            raw_num = str(pr_num_raw).replace("\r", "").replace("\n", "").replace("|", "-").replace("`", "").strip() if pr_num_raw is not None else ""
            has_num = bool(raw_num and raw_num not in ("0", "-", "None"))
            num = raw_num if has_num else ""
            repo_name = (str(pr.get("repo_full_name", "") or "-")).replace("\r", " ").replace("\n", " ").replace("|", "-").replace("`", "").strip() or "-"
            title_text = (str(pr.get("title") or "")).replace("\r", " ").replace("\n", " ").replace("|", "-").strip()
            author_raw = pr.get("author")
            author_val = author_raw.get("login") if isinstance(author_raw, dict) else author_raw
            author_str = str(author_val or "").strip().lstrip("@")
            author_text = author_str.replace("\r", " ").replace("\n", " ").replace("|", "-").replace("`", "").strip() if author_str else "-"
            if not author_text:
                author_text = "-"
            url_raw = pr.get("url")
            url_val = str(url_raw or "").replace("|", "").replace("\r", "").replace("\n", "").replace("`", "").replace(")", "%29").replace("(", "%28").strip()
            if not url_val and repo_name != "-" and has_num:
                url_val = f"https://github.com/{repo_name}/pull/{num}"
            if has_num:
                pr_cell = f"[`#{num}`]({url_val})" if url_val and is_safe_url(url_val) else f"`#{num}`"
            else:
                pr_cell = "-"
            url_cell = f"[View PR ↗]({url_val})" if url_val and is_safe_url(url_val) else "-"
            author_cell = f"`@{author_text}`" if author_text != "-" else "-"
            repo_cell = f"`{repo_name}`" if repo_name != "-" else "-"
            lines.append(f"| {pr_cell} | {repo_cell} | {title_text} | {author_cell} | {url_cell} |")
        lines.append("")
    else:
        lines.extend(["*(No approved PRs awaiting merge)*", ""])
    lines.extend(["---", ""])
    return lines


def _md_history(ctx: Dict[str, Any]) -> List[str]:
    """Render recent task execution history table or empty state."""
    recent_history = ctx["recent_history"]
    lines = [
        "## 📜 Recent Task Execution History",
        "",
    ]
    if recent_history:
        lines.extend([
            "| Task ID | Agent | Model | Target | Duration | Status | Details |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for t in recent_history:
            dur = format_duration(t.finish_time - t.start_time) if (t.finish_time and t.start_time) else "N/A"
            icon = "✅" if t.status == TaskStatus.COMPLETED else "❌"
            target_disp = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
            model_val = getattr(t, "selected_model", None) or getattr(t, "selected_pool", None)
            model_name = model_val if isinstance(model_val, str) and model_val.strip() else "-"
            model_disp = f"`{model_name}`" if model_name != "-" else "-"
            target_url = resolve_target_url(target_disp, repo=getattr(t, "repo_full_name", None), agent=getattr(t, "agent", None))
            target_cell = _format_target_markdown_cell(target_disp, target_url)
            if t.error_message:
                clean_err = str(t.error_message).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
                detail = f"`{clean_err[:40]}...`" if len(clean_err) > 40 else f"`{clean_err}`"
            else:
                detail = "Finished"
            lines.append(f"| `{t.id}` | `{t.agent}` | {model_disp} | {target_cell} | {dur} | {icon} `{t.status}` | {detail} |")
        lines.append("")
    else:
        lines.extend(["*No completed tasks in history yet.*", ""])
    lines.extend(["---", ""])
    return lines


def _md_footer() -> List[str]:
    """Render the dashboard tip and agent control callout footer."""
    return [
        "> [!TIP]",
        "> **Agent Control**: You can submit tasks with `graviton_submit_task`, trigger PR reviews with `graviton_submit_review`, or inspect real-time logs with `graviton_get_task(task_id=\"<id>\")`.",
        "",
    ]


def format_dashboard_markdown(
    task_manager: Optional[TaskManager] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    scheduler: Optional[TaskScheduler] = None,
    host: str = "localhost",
    port: int = 8000,
    extra_info: Optional[Dict[str, Any]] = None,
    pr_tracker: Optional[PRTracker] = None,
) -> str:
    """
    Format live server, task, and quota state as rich GitHub Flavored Markdown
    suitable for Antigravity's Auxiliary Pane Artifact viewer.
    """
    ctx = _gather_markdown_context(
        task_manager=task_manager,
        quota_tracker=quota_tracker,
        scheduler=scheduler,
        host=host,
        port=port,
        extra_info=extra_info,
        pr_tracker=pr_tracker,
    )

    lines: List[str] = []
    lines.extend(_md_header(ctx))
    lines.extend(_md_pipeline(ctx))
    lines.extend(_md_active_tasks(ctx))
    lines.extend(_md_quota(ctx, quota_tracker=quota_tracker))
    lines.extend(_md_queued(ctx))
    lines.extend(_md_approved_prs(ctx))
    lines.extend(_md_history(ctx))
    lines.extend(_md_footer())

    return "\n".join(lines)
