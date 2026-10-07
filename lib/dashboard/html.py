#!/usr/bin/env python3
"""
Dashboard HTML rendering, template loading, and table generators for Graviton.
Zero external dependencies (Python standard library only).
"""

import html
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from lib.dashboard.common import (
    REPO_ROOT,
    _build_pacing_details,
    _detect_git_repo_full_name,
    calculate_target_pacing_from_details,
    format_duration,
    format_percentage,
    get_quota_color,
    is_safe_url,
    logger,
    resolve_target_url,
)
from lib.dashboard.parser import parse_dashboard_markdown
from lib.pr_tracker import PRTracker
from lib.quota import (
    QuotaTracker,
    QuotaWindow,
    format_pacing_recovery_countdown,
    format_reset_countdown,
)
from lib.scheduler import TaskScheduler
from lib.tasks import TaskManager
from lib.updater import get_cached_git_info, get_git_info, get_hot_reload_state

_TEMPLATE_LOCK = threading.Lock()
_DASHBOARD_TEMPLATE_CACHE: Optional[str] = None


def _get_dashboard_template() -> str:
    """Thread-safe, cached loader for external dashboard HTML template."""
    global _DASHBOARD_TEMPLATE_CACHE
    if _DASHBOARD_TEMPLATE_CACHE is not None:
        return _DASHBOARD_TEMPLATE_CACHE

    with _TEMPLATE_LOCK:
        if _DASHBOARD_TEMPLATE_CACHE is not None:
            return _DASHBOARD_TEMPLATE_CACHE

        possible_paths = [
            REPO_ROOT / "templates" / "dashboard" / "dashboard.html",
            Path(__file__).resolve().parent.parent / "templates" / "dashboard" / "dashboard.html",
        ]
        for path in possible_paths:
            if path.is_file():
                try:
                    _DASHBOARD_TEMPLATE_CACHE = path.read_text(encoding="utf-8")
                    return _DASHBOARD_TEMPLATE_CACHE
                except Exception as e:
                    logger.warning(f"Failed to read dashboard template asset from {path}: {e}")

        logger.warning("Dashboard HTML template asset missing; using fallback template.")
        _DASHBOARD_TEMPLATE_CACHE = _get_fallback_dashboard_template()
        return _DASHBOARD_TEMPLATE_CACHE


def _reset_dashboard_template_cache() -> None:
    """Reset cached template (primarily for testing)."""
    global _DASHBOARD_TEMPLATE_CACHE
    with _TEMPLATE_LOCK:
        _DASHBOARD_TEMPLATE_CACHE = None


def _get_fallback_dashboard_template() -> str:
    """Minimal fallback HTML template if template asset is missing on disk."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Graviton Live Dashboard</title>
</head>
<body>
    <h1>🌌 Graviton Live Dashboard</h1>
    <p>Status: {status_icon} {status_text} | Host: {effective_host}:{effective_port} | Branch: {branch} | Commit: {commit} | Reload: {reload_state}</p>
    <p>Active Workers: {active_workers} / {max_workers}</p>
    <p>Running Tasks: {running_tasks} | Queued: {queued_tasks_count} | Completed: {completed_tasks} | Failed: {failed_tasks}</p>
    <div>{active_table_html}</div>
    <div>{queued_table_html}</div>
    <div>{approved_table_html}</div>
    <div>{history_table_html}</div>
    <pre id="content">{escaped_md}</pre>
</body>
</html>"""


def _format_reload_state_class(reload_state: Any) -> str:
    """Map hot reload lifecycle state to a CSS modifier class."""
    s = str(reload_state or "IDLE").upper()
    if "PULL" in s:
        return "pulling"
    if "REBUILD" in s:
        return "rebuilding"
    if "DRAIN" in s:
        return "draining"
    if "RELOAD" in s:
        return "reloading"
    return "idle"


def _format_target_html_cell(
    target_raw: Any,
    target_url: Optional[str] = None,
    agent: Optional[str] = None,
) -> str:
    """Format target cell for HTML table, resolving URL and unwrapping markdown link labels."""
    if not target_raw:
        return '<code>N/A</code>'
    raw_str = str(target_raw).replace("\r", " ").replace("\n", " ").strip()
    norm = raw_str.strip("`").strip()
    if not norm or norm in ("N/A", "-", "None"):
        return '<code>N/A</code>'

    label = norm
    url = target_url
    md_m = re.match(r"^\[(.*?)\]\((.*?)\)$", norm)
    if md_m:
        label = md_m.group(1).strip("` ").strip()
        if not url:
            extracted_url = md_m.group(2).strip()
            if is_safe_url(extracted_url):
                url = extracted_url
    else:
        label = norm
        if not url:
            url = resolve_target_url(label, agent=agent)

    label = label.replace(r"\|", "|").replace("\r", " ").replace("\n", " ")
    clean_label = html.escape(label.strip("` ").strip())
    if not clean_label or clean_label in ("N/A", "-", "None"):
        return '<code>N/A</code>'
    if url and is_safe_url(url):
        return f'<a href="{html.escape(url)}" target="_blank" rel="noopener" class="target-link"><code>{clean_label}</code></a>'
    return f'<code>{clean_label}</code>'


def _pacing_style_and_title(pct: Any) -> Tuple[str, str]:
    """Compute inline CSS marker style and title attribute for quota target pacing limit."""
    if pct is not None:
        try:
            pct_val = max(0.0, min(100.0, float(pct)))
            round_val = round(pct_val, 1)
            pct_str = f"{int(round_val)}%" if round_val.is_integer() else f"{round_val:.1f}%"
            return f"left: {pct_val}%;", f"Target Pacing: {pct_str} (perfect pacing limit)"
        except (ValueError, TypeError):
            pass
    return "display: none;", ""


def _safe_disp(val: Any, fallback: Any = None) -> str:
    """Clean candidate string values, ignoring null and none representations."""
    for candidate in (val, fallback):
        if candidate is not None:
            s = str(candidate).strip()
            if s and s.lower() not in ("none", "null", "none%"):
                return s
    return "N/A"


def _render_active_tasks_table(active_tasks: List[Dict[str, Any]]) -> str:
    """Render HTML table or empty state card for active tasks."""
    if not active_tasks:
        return '<div class="empty-card"><span class="empty-icon">💤</span><p class="empty-text">No container tasks currently running.</p></div>'
    rows = []
    for t in active_tasks:
        tid = html.escape(str(t.get("id", "")))
        agent = html.escape(str(t.get("agent", "")))
        model = html.escape(str(t.get("model", "") or "-"))
        target_html = _format_target_html_cell(t.get("target"), t.get("target_url"), agent=str(t.get("agent", "")))
        elapsed = html.escape(str(t.get("elapsed", "")))
        status = html.escape(str(t.get("status", "")))
        model_cell = f'<code>{model}</code>' if model != "-" else '<span class="text-muted">-</span>'
        rows.append(
            f'<tr><td><code>{tid}</code></td>'
            f'<td><span class="agent-badge">{agent}</span></td>'
            f'<td>{model_cell}</td>'
            f'<td>{target_html}</td>'
            f'<td><span class="text-muted">{elapsed}</span></td>'
            f'<td><span class="status-pill status-running"><span class="spin-icon">🔄</span> {status}</span></td></tr>'
        )
    return (
        '<div class="table-wrapper"><table class="data-table">'
        '<thead><tr><th>Task ID</th><th>Agent</th><th>Model</th><th>Target</th><th>Elapsed</th><th>Status</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _render_queued_tasks_table(queued_tasks: List[Dict[str, Any]]) -> str:
    """Render HTML table or empty state card for queued tasks."""
    if not queued_tasks:
        return '<div class="empty-card"><span class="empty-icon">📭</span><p class="empty-text">Queue is empty.</p></div>'
    rows = []
    for t in queued_tasks:
        tid = html.escape(str(t.get("id", "")))
        agent = html.escape(str(t.get("agent", "")))
        target_html = _format_target_html_cell(t.get("target"), t.get("target_url"), agent=str(t.get("agent", "")))
        prio = html.escape(str(t.get("priority", "")))
        prio_label = prio if prio.startswith("P") else f"P{prio}"
        wait_time = html.escape(str(t.get("wait_time", "")))
        rows.append(
            f'<tr><td><code>{tid}</code></td>'
            f'<td><span class="agent-badge">{agent}</span></td>'
            f'<td>{target_html}</td>'
            f'<td><span class="priority-badge">{prio_label}</span></td>'
            f'<td><span class="text-muted">{wait_time}</span></td></tr>'
        )
    return (
        '<div class="table-wrapper"><table class="data-table">'
        '<thead><tr><th>Task ID</th><th>Agent</th><th>Target</th><th>Priority</th><th>Wait Time</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _render_approved_prs_table(approved_prs: List[Dict[str, Any]]) -> str:
    """Render HTML table or empty state card for approved PRs ready to merge."""
    valid_prs = [pr for pr in approved_prs if isinstance(pr, dict)]
    if not valid_prs:
        return (
            '<div class="empty-card">'
            '<span class="empty-icon">✨</span>'
            '<p class="empty-text">No approved PRs awaiting merge.</p>'
            '</div>'
        )
    rows = []
    for pr in valid_prs:
        pr_num_raw = pr.get("number")
        raw_num = str(pr_num_raw).strip() if pr_num_raw is not None else ""
        raw_repo = str(pr.get("repo_full_name", "") or "-").strip() or "-"
        has_num = bool(raw_num and raw_num not in ("0", "-", "None"))
        num = html.escape(raw_num) if has_num else ""
        repo = html.escape(raw_repo)
        title = html.escape(str(pr.get("title") or ""))
        author_raw = pr.get("author")
        author_val = author_raw.get("login") if isinstance(author_raw, dict) else author_raw
        author_str = str(author_val or "").strip().lstrip("@")
        author = html.escape(author_str) if author_str else "-"
        url = pr.get("url", "")
        if not url and raw_repo != "-" and has_num:
            url = f"https://github.com/{raw_repo}/pull/{raw_num}"

        if url and is_safe_url(url):
            num_html = f'<a href="{html.escape(url)}" target="_blank" rel="noopener"><code>#{num}</code></a>' if has_num else '<span class="text-muted">-</span>'
            action_html = f'<a href="{html.escape(url)}" target="_blank" rel="noopener" class="btn btn-sm btn-primary">View PR ↗</a>'
        else:
            num_html = f'<code>#{num}</code>' if has_num else '<span class="text-muted">-</span>'
            action_html = '<span class="text-muted">-</span>'

        repo_html = f'<code>{repo}</code>' if repo != "-" else '<span class="text-muted">-</span>'
        author_html = f'<span class="author-badge">@{author}</span>' if author != "-" else '<span class="text-muted">-</span>'

        rows.append(
            f'<tr><td>{num_html}</td>'
            f'<td>{repo_html}</td>'
            f'<td><span class="pr-title">{title}</span></td>'
            f'<td>{author_html}</td>'
            f'<td>{action_html}</td></tr>'
        )
    return (
        '<div class="table-wrapper"><table class="data-table">'
        '<thead><tr><th>PR #</th><th>Repository</th><th>Title</th><th>Author</th><th>Action</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _render_history_tasks_table(history_tasks: List[Dict[str, Any]]) -> str:
    """Render HTML table or empty state card for execution history."""
    if not history_tasks:
        return '<div class="empty-card"><span class="empty-icon">📜</span><p class="empty-text">No completed tasks in history yet.</p></div>'
    rows = []
    for t in history_tasks:
        tid = html.escape(str(t.get("id", "")))
        agent = html.escape(str(t.get("agent", "")))
        model = html.escape(str(t.get("model", "") or "-"))
        target_html = _format_target_html_cell(t.get("target"), t.get("target_url"), agent=str(t.get("agent", "")))
        duration = html.escape(str(t.get("duration", "")))
        raw_status = str(t.get("status", "")).lower()
        is_success = "complete" in raw_status or "finish" in raw_status
        status_icon = "✅" if is_success else "❌"
        status_class = "status-completed" if is_success else "status-failed"
        status_disp = html.escape(str(t.get("status", "")))
        detail = str(t.get("details", ""))
        if detail and detail not in ("Finished", "Remote Control", "Remote Session"):
            trunc = detail[:40] + "..." if len(detail) > 40 else detail
            detail_html = f'<code class="error-snippet" title="{html.escape(detail)}">{html.escape(trunc)}</code>'
        else:
            detail_html = '<span class="text-muted">Finished</span>'
        model_cell = f'<code>{model}</code>' if model != "-" else '<span class="text-muted">-</span>'
        rows.append(
            f'<tr><td><code>{tid}</code></td>'
            f'<td><span class="agent-badge">{agent}</span></td>'
            f'<td>{model_cell}</td>'
            f'<td>{target_html}</td>'
            f'<td><span class="text-muted">{duration}</span></td>'
            f'<td><span class="status-pill {status_class}">{status_icon} {status_disp}</span></td>'
            f'<td>{detail_html}</td></tr>'
        )
    return (
        '<div class="table-wrapper"><table class="data-table">'
        '<thead><tr><th>Task ID</th><th>Agent</th><th>Model</th><th>Target</th><th>Duration</th><th>Status</th><th>Details</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _enrich_dashboard_data(
    data: Dict[str, Any],
    task_manager: Optional[TaskManager] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    pr_tracker: Optional[PRTracker] = None,
    extra_info: Optional[Dict[str, Any]] = None,
) -> None:
    """Enrich parsed dashboard data with live state from TaskManager, QuotaTracker, PRTracker, or extra_info."""
    if pr_tracker and not data.get("approved_prs"):
        try:
            pr_objs = pr_tracker.get_approved_prs()
            if pr_objs:
                data["approved_prs"] = list(pr_objs)
        except Exception as e:
            logger.debug(f"Error enriching dashboard data from pr_tracker: {e}")
    if not data.get("approved_prs") and extra_info and "approved_prs" in extra_info:
        data["approved_prs"] = list(extra_info.get("approved_prs") or [])

    if task_manager:
        try:
            stats = task_manager.get_stats()
            if stats:
                data["active_workers"] = stats.get("active_workers", data["active_workers"])
                data["max_workers"] = stats.get("max_workers", data["max_workers"])
                data["running_tasks"] = stats.get("active_tasks", data["running_tasks"])
                data["queued_tasks"] = stats.get("queued_tasks", data["queued_tasks"])
                data["completed_tasks"] = stats.get("completed_tasks", data["completed_tasks"])
                data["failed_tasks"] = stats.get("failed_tasks", data["failed_tasks"])
            if not data["active_tasks"]:
                act_objs = task_manager.get_active_tasks()
                if act_objs:
                    now_ts = time.time()
                    for t in act_objs:
                        elapsed = format_duration(now_ts - t.start_time) if t.start_time else "starting..."
                        model_val = getattr(t, "selected_model", None) or getattr(t, "selected_pool", None)
                        model_name = model_val if isinstance(model_val, str) and model_val.strip() else "-"
                        target_id = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
                        data["active_tasks"].append({
                            "id": t.id,
                            "agent": t.agent,
                            "model": model_name,
                            "target": target_id,
                            "target_url": resolve_target_url(target_id, repo=getattr(t, "repo_full_name", None), agent=t.agent),
                            "elapsed": elapsed,
                            "status": str(t.status),
                        })
            if not data["queued_tasks_list"]:
                q_objs = task_manager.get_queued_tasks()
                if q_objs:
                    now_ts = time.time()
                    for t in q_objs:
                        q_dur = format_duration(now_ts - t.enqueue_time)
                        target_id = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
                        data["queued_tasks_list"].append({
                            "id": t.id,
                            "agent": t.agent,
                            "target": target_id,
                            "target_url": resolve_target_url(target_id, repo=getattr(t, "repo_full_name", None), agent=t.agent),
                            "priority": str(t.priority),
                            "wait_time": q_dur,
                        })
            if not data["history_tasks"]:
                h_objs = task_manager.get_task_history(limit=10)
                if h_objs:
                    for t in h_objs:
                        dur = format_duration(t.finish_time - t.start_time) if (t.finish_time and t.start_time) else "N/A"
                        detail = t.error_message or "Finished"
                        model_val = getattr(t, "selected_model", None) or getattr(t, "selected_pool", None)
                        model_name = model_val if isinstance(model_val, str) and model_val.strip() else "-"
                        target_id = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
                        data["history_tasks"].append({
                            "id": t.id,
                            "agent": t.agent,
                            "model": model_name,
                            "target": target_id,
                            "target_url": resolve_target_url(target_id, repo=getattr(t, "repo_full_name", None), agent=t.agent),
                            "duration": dur,
                            "status": str(t.status),
                            "details": detail,
                        })
        except Exception as e:
            logger.debug(f"Error enriching dashboard data from task_manager: {e}")

    if quota_tracker:
        try:
            q_info = quota_tracker.get_info().to_dict()
            if q_info:
                if data["pool"] == "default":
                    data["pool"] = q_info.get("quota_pool") or q_info.get("current_pool") or data["pool"]
                if data["model"] == "default":
                    data["model"] = q_info.get("selected_model") or q_info.get("active_model") or data["model"]

            if hasattr(quota_tracker, "available_gemini_models") and quota_tracker.available_gemini_models:
                data["available_gemini_models"] = list(quota_tracker.available_gemini_models)
            if hasattr(quota_tracker, "available_third_party_models") and quota_tracker.available_third_party_models:
                data["available_third_party_models"] = list(quota_tracker.available_third_party_models)

            if hasattr(quota_tracker, "get_active_model"):
                data["active_gemini_model"] = quota_tracker.get_active_model("gemini")
                data["active_third_party_model"] = quota_tracker.get_active_model("third_party")
            else:
                if hasattr(quota_tracker, "active_gemini_model") and quota_tracker.active_gemini_model:
                    data["active_gemini_model"] = quota_tracker.active_gemini_model
                if hasattr(quota_tracker, "active_third_party_model") and quota_tracker.active_third_party_model:
                    data["active_third_party_model"] = quota_tracker.active_third_party_model
        except Exception as e:
            logger.debug(f"Error enriching dashboard data from quota_tracker: {e}")

    if quota_tracker and hasattr(quota_tracker, "get_pool_windows"):
        try:
            w5_g, w1_g = quota_tracker.get_pool_windows("gemini")
            w5_c, w1_c = quota_tracker.get_pool_windows("claude_gpt")
            if w5_g is not None:
                data["gemini_5h_pct"] = w5_g.remaining_percentage
                data["gemini_5h_rem"] = format_percentage(w5_g.remaining_percentage)
                cd = w5_g.format_reset_countdown()
                st, _ = w5_g.get_pacing_status()
                if not (w5_g.remaining_percentage is None and cd == "N/A" and (st == "OK" or not st)):
                    data["gemini_5h_details"] = _build_pacing_details(cd, st, w5_g.format_pacing_countdown())
                data["gemini_5h_target_pacing_pct"] = w5_g.get_target_pacing_percentage()
            if w1_g is not None:
                data["gemini_1w_pct"] = w1_g.remaining_percentage
                data["gemini_1w_rem"] = format_percentage(w1_g.remaining_percentage)
                cd = w1_g.format_reset_countdown()
                st, _ = w1_g.get_pacing_status()
                if not (w1_g.remaining_percentage is None and cd == "N/A" and (st == "OK" or not st)):
                    data["gemini_1w_details"] = _build_pacing_details(cd, st, w1_g.format_pacing_countdown())
                data["gemini_1w_target_pacing_pct"] = w1_g.get_target_pacing_percentage()
            if w5_c is not None:
                data["tp_5h_pct"] = w5_c.remaining_percentage
                data["tp_5h_rem"] = format_percentage(w5_c.remaining_percentage)
                cd = w5_c.format_reset_countdown()
                st, _ = w5_c.get_pacing_status()
                if not (w5_c.remaining_percentage is None and cd == "N/A" and (st == "OK" or not st)):
                    data["tp_5h_details"] = _build_pacing_details(cd, st, w5_c.format_pacing_countdown())
                data["tp_5h_target_pacing_pct"] = w5_c.get_target_pacing_percentage()
            if w1_c is not None:
                data["tp_1w_pct"] = w1_c.remaining_percentage
                data["tp_1w_rem"] = format_percentage(w1_c.remaining_percentage)
                cd = w1_c.format_reset_countdown()
                st, _ = w1_c.get_pacing_status()
                if not (w1_c.remaining_percentage is None and cd == "N/A" and (st == "OK" or not st)):
                    data["tp_1w_details"] = _build_pacing_details(cd, st, w1_c.format_pacing_countdown())
                data["tp_1w_target_pacing_pct"] = w1_c.get_target_pacing_percentage()
        except Exception as e:
            logger.debug(f"Error enriching window metrics from quota_tracker: {e}")

    if not quota_tracker and extra_info and "quota_info" in extra_info:
        q_extra = extra_info.get("quota_info") or {}
        if q_extra.get("gemini_5h_remaining_percentage") is not None:
            data["gemini_5h_pct"] = q_extra["gemini_5h_remaining_percentage"]
            data["gemini_5h_rem"] = format_percentage(q_extra["gemini_5h_remaining_percentage"])
        if q_extra.get("gemini_1w_remaining_percentage") is not None:
            data["gemini_1w_pct"] = q_extra["gemini_1w_remaining_percentage"]
            data["gemini_1w_rem"] = format_percentage(q_extra["gemini_1w_remaining_percentage"])
        if q_extra.get("third_party_5h_remaining_percentage") is not None:
            data["tp_5h_pct"] = q_extra["third_party_5h_remaining_percentage"]
            data["tp_5h_rem"] = format_percentage(q_extra["third_party_5h_remaining_percentage"])
        if q_extra.get("third_party_1w_remaining_percentage") is not None:
            data["tp_1w_pct"] = q_extra["third_party_1w_remaining_percentage"]
            data["tp_1w_rem"] = format_percentage(q_extra["third_party_1w_remaining_percentage"])
        if q_extra.get("gemini_5h_target_pacing_percentage") is not None:
            data["gemini_5h_target_pacing_pct"] = q_extra["gemini_5h_target_pacing_percentage"]
        if q_extra.get("gemini_1w_target_pacing_percentage") is not None:
            data["gemini_1w_target_pacing_pct"] = q_extra["gemini_1w_target_pacing_percentage"]
        if q_extra.get("third_party_5h_target_pacing_percentage") is not None:
            data["tp_5h_target_pacing_pct"] = q_extra["third_party_5h_target_pacing_percentage"]
        if q_extra.get("third_party_1w_target_pacing_percentage") is not None:
            data["tp_1w_target_pacing_pct"] = q_extra["third_party_1w_target_pacing_percentage"]
        for prefix, key_prefix in [
            ("gemini_5h", "gemini_5h"),
            ("gemini_1w", "gemini_1w"),
            ("tp_5h", "third_party_5h"),
            ("tp_1w", "third_party_1w"),
        ]:
            cd = q_extra.get(f"{key_prefix}_countdown")
            if cd is None and q_extra.get(f"{key_prefix}_reset_time") is not None:
                try:
                    cd = format_reset_countdown(
                        q_extra.get(f"{key_prefix}_reset_time"),
                        window_name="5H" if "5h" in key_prefix else "1W",
                    )
                except Exception:
                    cd = str(q_extra.get(f"{key_prefix}_reset_time"))
            st = q_extra.get(f"{key_prefix}_pacing_status")
            has_pct = q_extra.get(f"{key_prefix}_remaining_percentage") is not None
            has_res = q_extra.get(f"{key_prefix}_reset_time") is not None
            if cd or has_res or has_pct or (st and st != "OK"):
                rec_cd = q_extra.get(f"{key_prefix}_pacing_recovery_countdown")
                if (not rec_cd or rec_cd == "00:00:00") and q_extra.get(f"{key_prefix}_pacing_recovery_seconds") is not None:
                    try:
                        rec_sec = float(q_extra.get(f"{key_prefix}_pacing_recovery_seconds"))
                        if rec_sec > 0:
                            rec_cd = format_pacing_recovery_countdown(rec_sec)
                    except Exception:
                        pass
                if (not rec_cd or rec_cd == "00:00:00") and st == "BEHIND_PACING" and has_res and has_pct:
                    try:
                        qw = QuotaWindow(
                            name="5H" if "5h" in key_prefix else "1W",
                            duration_seconds=18000.0 if "5h" in key_prefix else 604800.0,
                            remaining_percentage=q_extra.get(f"{key_prefix}_remaining_percentage"),
                            reset_time=q_extra.get(f"{key_prefix}_reset_time"),
                        )
                        qw_rec = qw.format_pacing_countdown()
                        if qw_rec and qw_rec != "00:00:00":
                            rec_cd = qw_rec
                    except Exception:
                        pass

                details = _build_pacing_details(cd or "N/A", st, rec_cd)
                data[f"{prefix}_details"] = details
                if prefix.startswith("tp_"):
                    data[f"{key_prefix}_details"] = details

    if data.get("gemini_5h_target_pacing_pct") is None:
        data["gemini_5h_target_pacing_pct"] = calculate_target_pacing_from_details(data.get("gemini_5h_details"), 18000.0)
    if data.get("gemini_1w_target_pacing_pct") is None:
        data["gemini_1w_target_pacing_pct"] = calculate_target_pacing_from_details(data.get("gemini_1w_details"), 604800.0)
    if data.get("tp_5h_target_pacing_pct") is None:
        data["tp_5h_target_pacing_pct"] = calculate_target_pacing_from_details(data.get("tp_5h_details"), 18000.0)
    if data.get("tp_1w_target_pacing_pct") is None:
        data["tp_1w_target_pacing_pct"] = calculate_target_pacing_from_details(data.get("tp_1w_details"), 604800.0)

    g_act = data.get("active_gemini_model")
    if isinstance(g_act, str) and g_act.strip() and g_act not in data["available_gemini_models"]:
        data["available_gemini_models"].insert(0, g_act)
    tp_act = data.get("active_third_party_model")
    if isinstance(tp_act, str) and tp_act.strip() and tp_act not in data["available_third_party_models"]:
        data["available_third_party_models"].insert(0, tp_act)


def _render_header_html(data: Dict[str, Any], extra_info: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compute HTML template parameters for dashboard header, server metadata, and Git status."""
    effective_host = html.escape(data["host"])
    effective_port = data["port"]
    effective_now = html.escape(data["now_iso"])
    status_icon = data["status_icon"]
    status_text = html.escape(data["status_text"])
    status_class = html.escape(data["status_class"])

    raw_repo = _detect_git_repo_full_name() or ""
    default_repo = raw_repo if re.match(r"^[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+$", raw_repo) else ""

    commit = (extra_info.get("commit") if extra_info else None) or data.get("commit")
    branch = (extra_info.get("branch") if extra_info else None) or data.get("branch")

    if not commit or not branch or commit == "unknown" or branch == "unknown":
        try:
            c, b = get_cached_git_info(REPO_ROOT)
            commit = (commit if commit and commit != "unknown" else c) or "unknown"
            branch = (branch if branch and branch != "unknown" else b) or "unknown"
        except Exception:
            pass
    commit = commit or "unknown"
    branch = branch or "unknown"

    if extra_info is not None and "reload_state" in extra_info and extra_info["reload_state"] is not None:
        reload_state = str(extra_info["reload_state"])
    else:
        try:
            reload_state = get_hot_reload_state()
        except Exception:
            reload_state = (data.get("reload_state") if data else "IDLE") or "IDLE"
    reload_state = reload_state or "IDLE"

    reload_state_class = _format_reload_state_class(reload_state)

    return {
        "effective_host": effective_host,
        "effective_port": effective_port,
        "effective_now": effective_now,
        "status_icon": status_icon,
        "status_text": status_text,
        "status_class": status_class,
        "branch": html.escape(branch),
        "commit": html.escape(commit),
        "reload_state": html.escape(reload_state),
        "reload_state_class": html.escape(reload_state_class),
        "default_repo": default_repo,
    }


def _render_metrics_html(data: Dict[str, Any]) -> Dict[str, Any]:
    """Compute HTML template parameters for pipeline execution metrics."""
    active_workers = data["active_workers"]
    max_workers = data["max_workers"]
    workers_pct = min(100, int((active_workers / max_workers) * 100)) if max_workers > 0 else 0
    running_tasks = data["running_tasks"]
    running_pulse_style = "display: inline-block;" if running_tasks > 0 else "display: none;"
    queued_tasks_count = data["queued_tasks"]
    completed_tasks = data["completed_tasks"]
    failed_tasks = data["failed_tasks"]
    failed_class = "kpi-value text-red" if failed_tasks > 0 else "kpi-value text-muted"

    return {
        "active_workers": active_workers,
        "max_workers": max_workers,
        "workers_pct": workers_pct,
        "running_tasks": running_tasks,
        "running_pulse_style": running_pulse_style,
        "queued_tasks_count": queued_tasks_count,
        "completed_tasks": completed_tasks,
        "failed_tasks": failed_tasks,
        "failed_class": failed_class,
    }


def _render_quota_card(
    pct_val: Any,
    rem_val: Any,
    fallback_rem: Any,
    details_val: Any,
    target_pacing_pct: Any,
    default_details: str,
) -> Tuple[str, str, int, str, str, str]:
    """Compute display string, color, progress bar width, details, and pacing styles for a quota window card."""
    disp = html.escape(_safe_disp(rem_val, fallback_rem))
    color = get_quota_color(pct_val)
    try:
        bar_pct = min(100, max(0, int(float(pct_val)))) if pct_val is not None else 0
    except (ValueError, TypeError):
        bar_pct = 0
    details = html.escape(str(details_val or default_details))
    pacing_style, pacing_title = _pacing_style_and_title(target_pacing_pct)
    return disp, color, bar_pct, details, pacing_style, pacing_title


def _render_quota_html(data: Dict[str, Any]) -> Dict[str, Any]:
    """Compute HTML template parameters for quota progress meters, pacing indicators, and model selectors."""
    gemini_options_html = "\n".join([
        f'<option value="{html.escape(m)}"{ " selected" if m == data["active_gemini_model"] else ""}>{html.escape(m)}</option>'
        for m in data["available_gemini_models"]
    ])
    tp_options_html = "\n".join([
        f'<option value="{html.escape(m)}"{ " selected" if m == data["active_third_party_model"] else ""}>{html.escape(m)}</option>'
        for m in data["available_third_party_models"]
    ])

    gemini_color = get_quota_color(data["gemini_pct"])
    try:
        gemini_bar_pct = min(100, max(0, int(float(data["gemini_pct"])))) if data["gemini_pct"] is not None else 100
    except (ValueError, TypeError):
        gemini_bar_pct = 100

    tp_color = get_quota_color(data["tp_pct"])
    try:
        tp_bar_pct = min(100, max(0, int(float(data["tp_pct"])))) if data["tp_pct"] is not None else 100
    except (ValueError, TypeError):
        tp_bar_pct = 100

    g_5h_disp, g_5h_col, g_5h_bar, g_5h_det, g_5h_p_style, g_5h_p_title = _render_quota_card(
        data.get("gemini_5h_pct"),
        data.get("gemini_5h_rem"),
        data.get("gemini_rem"),
        data.get("gemini_5h_details"),
        data.get("gemini_5h_target_pacing_pct"),
        "Live Gemini burst quota",
    )
    g_1w_disp, g_1w_col, g_1w_bar, g_1w_det, g_1w_p_style, g_1w_p_title = _render_quota_card(
        data.get("gemini_1w_pct"),
        data.get("gemini_1w_rem"),
        data.get("gemini_rem"),
        data.get("gemini_1w_details"),
        data.get("gemini_1w_target_pacing_pct"),
        "Live Gemini weekly quota",
    )
    tp_5h_disp, tp_5h_col, tp_5h_bar, tp_5h_det, tp_5h_p_style, tp_5h_p_title = _render_quota_card(
        data.get("tp_5h_pct"),
        data.get("tp_5h_rem"),
        data.get("tp_rem"),
        data.get("tp_5h_details"),
        data.get("tp_5h_target_pacing_pct"),
        "Fallback burst quota",
    )
    tp_1w_disp, tp_1w_col, tp_1w_bar, tp_1w_det, tp_1w_p_style, tp_1w_p_title = _render_quota_card(
        data.get("tp_1w_pct"),
        data.get("tp_1w_rem"),
        data.get("tp_rem"),
        data.get("tp_1w_details"),
        data.get("tp_1w_target_pacing_pct"),
        "Fallback weekly quota",
    )

    return {
        "pool_str": html.escape(data["pool"]),
        "model_str": html.escape(data["model"]),
        "gemini_color": gemini_color,
        "gemini_disp": html.escape(_safe_disp(data.get("gemini_rem"))),
        "gemini_bar_pct": gemini_bar_pct,
        "gemini_options_html": gemini_options_html,
        "gemini_5h_disp": g_5h_disp,
        "gemini_5h_color": g_5h_col,
        "gemini_5h_bar_pct": g_5h_bar,
        "gemini_5h_details": g_5h_det,
        "gemini_5h_pacing_style": g_5h_p_style,
        "gemini_5h_pacing_title": html.escape(g_5h_p_title),
        "gemini_1w_disp": g_1w_disp,
        "gemini_1w_color": g_1w_col,
        "gemini_1w_bar_pct": g_1w_bar,
        "gemini_1w_details": g_1w_det,
        "gemini_1w_pacing_style": g_1w_p_style,
        "gemini_1w_pacing_title": html.escape(g_1w_p_title),
        "tp_color": tp_color,
        "tp_disp": html.escape(_safe_disp(data.get("tp_rem"))),
        "tp_bar_pct": tp_bar_pct,
        "tp_options_html": tp_options_html,
        "tp_5h_disp": tp_5h_disp,
        "tp_5h_color": tp_5h_col,
        "tp_5h_bar_pct": tp_5h_bar,
        "tp_5h_details": tp_5h_det,
        "tp_5h_pacing_style": tp_5h_p_style,
        "tp_5h_pacing_title": html.escape(tp_5h_p_title),
        "tp_1w_disp": tp_1w_disp,
        "tp_1w_color": tp_1w_col,
        "tp_1w_bar_pct": tp_1w_bar,
        "tp_1w_details": tp_1w_det,
        "tp_1w_pacing_style": tp_1w_p_style,
        "tp_1w_pacing_title": html.escape(tp_1w_p_title),
    }


def render_dashboard_html(
    markdown_content: str,
    host: str = "localhost",
    port: int = 8000,
    task_manager: Optional[TaskManager] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    scheduler: Optional[TaskScheduler] = None,
    extra_info: Optional[Dict[str, Any]] = None,
    pr_tracker: Optional[PRTracker] = None,
) -> str:
    """
    Render a rich, responsive, dark-mode web dashboard page for web browsers.
    Includes KPI metric cards, model quota capacity progress meters, interactive
    task tables with clickable target links to GitHub PRs and issues, collapsible raw markdown, and smooth
    client-side auto-refresh (3-second cadence).
    """
    escaped_md = html.escape(markdown_content)
    data = parse_dashboard_markdown(markdown_content, default_host=host, default_port=port)

    _enrich_dashboard_data(
        data,
        task_manager=task_manager,
        quota_tracker=quota_tracker,
        pr_tracker=pr_tracker,
        extra_info=extra_info,
    )

    header_params = _render_header_html(data, extra_info)
    metrics_params = _render_metrics_html(data)
    quota_params = _render_quota_html(data)

    valid_approved_prs = [pr for pr in data.get("approved_prs", []) if isinstance(pr, dict)]

    table_params = {
        "active_count": len(data["active_tasks"]),
        "active_table_html": _render_active_tasks_table(data["active_tasks"]),
        "queued_count": len(data["queued_tasks_list"]),
        "queued_table_html": _render_queued_tasks_table(data["queued_tasks_list"]),
        "approved_count": len(valid_approved_prs),
        "approved_table_html": _render_approved_prs_table(valid_approved_prs),
        "history_count": len(data["history_tasks"]),
        "history_table_html": _render_history_tasks_table(data["history_tasks"]),
        "escaped_md": escaped_md,
    }

    template = _get_dashboard_template()

    return template.format(
        **header_params,
        **metrics_params,
        **quota_params,
        **table_params,
    )
