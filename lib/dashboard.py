#!/usr/bin/env python3
"""
Graviton Live Dashboard Generator and Auto-Updater.

Formats real-time server metrics, task queues, active containers, model quota pacing,
and Remote Control links into GitHub-flavored Markdown for Antigravity side panel
artifacts, as well as standalone HTML for web browsers.

Supports automatic continuous updates to registered artifact file paths on disk.
Zero external dependencies (Python standard library only).
"""

import datetime
import html
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.tasks import TaskManager, Task, TaskStatus
from lib.quota import QuotaTracker, DEFAULT_GEMINI_MODELS, DEFAULT_THIRD_PARTY_MODELS
from lib.scheduler import TaskScheduler

logger = logging.getLogger("graviton.dashboard")


def format_duration(seconds: Optional[float]) -> str:
    """Format elapsed seconds into human readable duration string (e.g., 2m 14s)."""
    if seconds is None or seconds < 0:
        return "0s"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def format_dashboard_markdown(
    task_manager: Optional[TaskManager] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    scheduler: Optional[TaskScheduler] = None,
    host: str = "localhost",
    port: int = 8000,
    extra_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Format live server, task, and quota state as rich GitHub Flavored Markdown
    suitable for Antigravity's Auxiliary Pane Artifact viewer.
    """
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    stats = task_manager.get_stats() if task_manager else {}
    active_tasks = task_manager.get_active_tasks() if task_manager else []
    queued_tasks = task_manager.get_queued_tasks() if task_manager else []
    recent_history = task_manager.get_task_history(limit=10) if task_manager else []
    quota_info = quota_tracker.get_info().to_dict() if quota_tracker else {}

    # Status indicator
    if active_tasks:
        status_badge = "🟡 **BUSY**"
    elif task_manager and getattr(task_manager, "_draining", False):
        status_badge = "⏳ **DRAINING**"
    elif task_manager and getattr(task_manager, "_paused", False):
        status_badge = "⏸️ **PAUSED**"
    else:
        status_badge = "🟢 **ONLINE**"

    web_url = f"http://{host}:{port}/dashboard"

    lines: List[str] = [
        "# 🌌 Graviton Live Dashboard",
        "",
        "> [!NOTE]",
        f"> **Status**: {status_badge} &nbsp;|&nbsp; **Server**: `{host}:{port}` &nbsp;|&nbsp; [Open Web Dashboard 🌐]({web_url})",
        f"> *Last updated: {now_iso} (Auto-refreshed by Graviton)*",
        "",
        "---",
        "",
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
        "## 🚀 Active Container Tasks",
        "",
    ]

    if active_tasks:
        lines.extend([
            "| Task ID | Agent | Target | Elapsed | Status | Remote Control |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        now_ts = time.time()
        for t in active_tasks:
            elapsed = format_duration(now_ts - t.start_time) if t.start_time else "starting..."
            rc_link = f"[Remote Control 🌐]({t.remote_control_url})" if getattr(t, "remote_control_url", None) else "*Pending...*"
            target_disp = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
            lines.append(f"| `{t.id}` | `{t.agent}` | `{target_disp}` | {elapsed} | 🔄 `{t.status}` | {rc_link} |")
        lines.append("")
    else:
        lines.extend(["*No container tasks currently running.*", ""])

    lines.extend([
        "---",
        "",
        "## 🎯 Model Quota & Pacing",
        "",
        "| Metric | Value | Details |",
        "| :--- | :--- | :--- |",
    ])

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
    if not gemini_model:
        gemini_model = model if not any(k in p_low for k in ("claude", "gpt", "3p", "third")) and model != "default" else (DEFAULT_GEMINI_MODELS[0] if DEFAULT_GEMINI_MODELS else "default")
    if not tp_model:
        tp_model = model if any(k in p_low for k in ("claude", "gpt", "3p", "third")) and model != "default" else (DEFAULT_THIRD_PARTY_MODELS[0] if DEFAULT_THIRD_PARTY_MODELS else "default")

    gemini_rem = quota_info.get("gemini_remaining_percentage")
    if gemini_rem is None and quota_tracker and hasattr(quota_tracker, "get_pool_remaining_percentage"):
        try:
            gemini_rem = quota_tracker.get_pool_remaining_percentage("gemini")
        except Exception:
            pass
    if gemini_rem is None:
        gemini_rem = quota_info.get("remaining_percentage", "N/A")

    tp_rem = quota_info.get("third_party_remaining_percentage")
    if tp_rem is None and quota_tracker and hasattr(quota_tracker, "get_pool_remaining_percentage"):
        try:
            tp_rem = quota_tracker.get_pool_remaining_percentage("claude")
        except Exception:
            pass
    if tp_rem is None:
        tp_rem = "N/A"

    gemini_disp = f"{gemini_rem}%" if not str(gemini_rem).endswith("%") else str(gemini_rem)
    tp_disp = f"{tp_rem}%" if not str(tp_rem).endswith("%") else str(tp_rem)

    lines.extend([
        f"| **Active Pool** | `{pool}` | Configured quota bucket |",
        f"| **Active Model** | `{model}` | Active Gemini / LLM persona |",
        f"| **Active Gemini Model** | `{gemini_model}` | Active Gemini model persona |",
        f"| **Active Third-Party Model** | `{tp_model}` | Active Third-Party model persona |",
        f"| **Gemini Remaining** | `{gemini_disp}` | Live Gemini API capacity |",
        f"| **Third-Party Remaining** | `{tp_disp}` | Fallback model capacity |",
        "",
        "---",
        "",
        "## ⏳ Queued Tasks",
        "",
    ])

    if queued_tasks:
        lines.extend([
            "| Task ID | Agent | Target | Priority | Queued Duration |",
            "| :--- | :--- | :--- | :--- | :--- |",
        ])
        now_ts = time.time()
        for t in queued_tasks:
            queued_dur = format_duration(now_ts - t.enqueue_time)
            target_disp = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
            lines.append(f"| `{t.id}` | `{t.agent}` | `{target_disp}` | `{t.priority}` | {queued_dur} |")
        lines.append("")
    else:
        lines.extend(["*Queue is empty.*", ""])

    lines.extend([
        "---",
        "",
        "## 📜 Recent Task Execution History",
        "",
    ])

    if recent_history:
        lines.extend([
            "| Task ID | Agent | Target | Duration | Status | Summary / Remote Link |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ])
        for t in recent_history:
            dur = format_duration(t.finish_time - t.start_time) if (t.finish_time and t.start_time) else "N/A"
            icon = "✅" if t.status == TaskStatus.COMPLETED else "❌"
            target_disp = t.target_id or (t.repo_full_name if t.repo_full_name else "N/A")
            if getattr(t, "remote_control_url", None):
                detail = f"[Remote Control 🌐]({t.remote_control_url})"
            elif t.error_message:
                detail = f"`{t.error_message[:40]}...`" if len(t.error_message) > 40 else f"`{t.error_message}`"
            else:
                detail = "Finished"
            lines.append(f"| `{t.id}` | `{t.agent}` | `{target_disp}` | {dur} | {icon} `{t.status}` | {detail} |")
        lines.append("")
    else:
        lines.extend(["*No completed tasks in history yet.*", ""])

    lines.extend([
        "---",
        "",
        "> [!TIP]",
        "> **Agent Control**: You can submit tasks with `graviton_submit_task`, trigger PR reviews with `graviton_submit_review`, or inspect real-time logs with `graviton_get_task(task_id=\"<id>\")`.",
        "",
    ])

    return "\n".join(lines)


def is_safe_url(url: Optional[str]) -> bool:
    """Validate that a URL uses safe http or https schemes to prevent javascript: XSS."""
    if not url:
        return False
    clean = url.strip().lower()
    return clean.startswith("http://") or clean.startswith("https://")


def get_quota_color(pct: Optional[float]) -> str:
    """Return adaptive status color for quota percentage thresholds."""
    if pct is None:
        return "#58a6ff"
    if pct > 50:
        return "#3fb950"
    if pct >= 20:
        return "#d29922"
    return "#f85149"


def parse_dashboard_markdown(
    markdown_content: str,
    default_host: str = "localhost",
    default_port: int = 8000,
) -> Dict[str, Any]:
    """Parse formatted dashboard markdown into structured dictionary for HTML rendering."""
    host = default_host
    port = default_port
    server_m = re.search(r"\*\*Server\*\*:\s*`([^:`]+):(\d+)`", markdown_content)
    if server_m:
        host = server_m.group(1)
        try:
            port = int(server_m.group(2))
        except ValueError:
            pass

    status_text = "ONLINE"
    status_icon = "🟢"
    status_class = "online"
    status_m = re.search(r"\*\*Status\*\*:\s*([^\n|&]+)", markdown_content)
    status_line = status_m.group(1) if status_m else markdown_content[:500]
    if "BUSY" in status_line:
        status_text = "BUSY"
        status_icon = "🟡"
        status_class = "busy"
    elif "DRAINING" in status_line:
        status_text = "DRAINING"
        status_icon = "⏳"
        status_class = "draining"
    elif "PAUSED" in status_line:
        status_text = "PAUSED"
        status_icon = "⏸️"
        status_class = "paused"
    elif "ONLINE" in status_line:
        status_text = "ONLINE"
        status_icon = "🟢"
        status_class = "online"

    updated_m = re.search(r"\*Last updated:\s*([^(]+)", markdown_content)
    now_iso = (
        updated_m.group(1).strip()
        if updated_m
        else datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

    active_workers = 0
    max_workers = 0
    workers_m = re.search(r"\|\s*\*\*Active Workers\*\*\s*\|\s*`?(\d+)\s*/\s*(\d+)`?", markdown_content)
    if workers_m:
        active_workers = int(workers_m.group(1))
        max_workers = int(workers_m.group(2))

    def _extract_metric_int(label: str) -> int:
        m = re.search(rf"\|\s*\*\*{re.escape(label)}\*\*\s*\|\s*`?(\d+)`?", markdown_content)
        return int(m.group(1)) if m else 0

    running_tasks = _extract_metric_int("Running Tasks")
    queued_tasks_count = _extract_metric_int("Queued Tasks")
    completed_tasks = _extract_metric_int("Completed Tasks")
    failed_tasks = _extract_metric_int("Failed Tasks")

    def _extract_metric_str(label: str, default: str = "default") -> str:
        m = re.search(rf"\|\s*\*\*{re.escape(label)}\*\*\s*\|\s*`?([^`|\n]+)`?", markdown_content)
        return m.group(1).strip() if m else default

    pool = _extract_metric_str("Active Pool", "default")
    model = _extract_metric_str("Active Model", "default")
    gemini_rem = _extract_metric_str("Gemini Remaining", "N/A")
    tp_rem = _extract_metric_str("Third-Party Remaining", "N/A")

    def _parse_pct(s: str) -> Optional[float]:
        if not s or s.startswith("N/A"):
            return None
        cleaned = s.replace("%", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None

    gemini_pct = _parse_pct(gemini_rem)
    tp_pct = _parse_pct(tp_rem)

    active_tasks: List[Dict[str, Any]] = []
    queued_tasks: List[Dict[str, Any]] = []
    history_tasks: List[Dict[str, Any]] = []

    sections = re.split(r"(?m)^##\s+", markdown_content)
    for sec in sections:
        sec_lines = sec.strip().splitlines()
        if not sec_lines:
            continue
        title_line = sec_lines[0].strip()

        table_rows = []
        for line in sec_lines:
            sline = line.strip()
            if sline.startswith("|") and not sline.startswith("| Metric") and "Task ID" not in sline:
                cells = [c.strip() for c in sline.split("|")[1:-1]]
                if cells and not all(c.replace(":", "").replace("-", "") == "" for c in cells):
                    table_rows.append(cells)

        if "Active Container Tasks" in title_line:
            for cells in table_rows:
                if len(cells) >= 6:
                    tid = cells[0].strip("`")
                    agent = cells[1].strip("`")
                    target = cells[2].strip("`")
                    elapsed = cells[3]
                    status = re.sub(r"[🔄`\s]+", " ", cells[4]).strip()
                    rc_cell = cells[5]
                    rc_url = None
                    url_m = re.search(r"\[.*?\]\((.*?)\)", rc_cell)
                    if url_m:
                        cand = url_m.group(1).strip()
                        if is_safe_url(cand):
                            rc_url = cand
                    active_tasks.append({
                        "id": tid,
                        "agent": agent,
                        "target": target,
                        "elapsed": elapsed,
                        "status": status,
                        "remote_control_url": rc_url,
                    })

        elif "Queued Tasks" in title_line:
            for cells in table_rows:
                if len(cells) >= 5:
                    tid = cells[0].strip("`")
                    agent = cells[1].strip("`")
                    target = cells[2].strip("`")
                    prio = cells[3].strip("`")
                    wait_time = cells[4]
                    queued_tasks.append({
                        "id": tid,
                        "agent": agent,
                        "target": target,
                        "priority": prio,
                        "wait_time": wait_time,
                    })

        elif "Recent Task Execution History" in title_line:
            for cells in table_rows:
                if len(cells) >= 6:
                    tid = cells[0].strip("`")
                    agent = cells[1].strip("`")
                    target = cells[2].strip("`")
                    duration = cells[3]
                    status_raw = re.sub(r"[✅❌`\s]+", " ", cells[4]).strip()
                    detail_cell = cells[5]
                    rc_url = None
                    url_m = re.search(r"\[.*?\]\((.*?)\)", detail_cell)
                    if url_m:
                        cand = url_m.group(1).strip()
                        if is_safe_url(cand):
                            rc_url = cand
                            detail = "Remote Control"
                        else:
                            detail = detail_cell.strip("`").strip()
                    else:
                        detail = detail_cell.strip("`").strip()
                    history_tasks.append({
                        "id": tid,
                        "agent": agent,
                        "target": target,
                        "duration": duration,
                        "status": status_raw,
                        "details": detail,
                        "remote_control_url": rc_url,
                    })

    active_gemini_model = _extract_metric_str("Active Gemini Model", "")
    active_third_party_model = _extract_metric_str("Active Third-Party Model", "")

    available_gemini_models = DEFAULT_GEMINI_MODELS.copy()
    available_third_party_models = DEFAULT_THIRD_PARTY_MODELS.copy()
    p_low = pool.lower()

    if not active_gemini_model or not isinstance(active_gemini_model, str) or not active_gemini_model.strip():
        if not any(k in p_low for k in ("claude", "gpt", "3p", "third")):
            active_gemini_model = model if (isinstance(model, str) and model.strip() and model != "default") else (available_gemini_models[0] if available_gemini_models else "default")
        else:
            active_gemini_model = available_gemini_models[0] if available_gemini_models else "default"
    else:
        active_gemini_model = active_gemini_model.strip()

    if not active_third_party_model or not isinstance(active_third_party_model, str) or not active_third_party_model.strip():
        if any(k in p_low for k in ("claude", "gpt", "3p", "third")):
            active_third_party_model = model if (isinstance(model, str) and model.strip() and model != "default") else (available_third_party_models[0] if available_third_party_models else "default")
        else:
            active_third_party_model = available_third_party_models[0] if available_third_party_models else "default"
    else:
        active_third_party_model = active_third_party_model.strip()

    return {
        "host": host,
        "port": port,
        "status_text": status_text,
        "status_icon": status_icon,
        "status_class": status_class,
        "now_iso": now_iso,
        "active_workers": active_workers,
        "max_workers": max_workers,
        "running_tasks": running_tasks,
        "queued_tasks": queued_tasks_count,
        "completed_tasks": completed_tasks,
        "failed_tasks": failed_tasks,
        "pool": pool,
        "model": model,
        "gemini_rem": gemini_rem,
        "gemini_pct": gemini_pct,
        "tp_rem": tp_rem,
        "tp_pct": tp_pct,
        "active_tasks": active_tasks,
        "queued_tasks_list": queued_tasks,
        "history_tasks": history_tasks,
        "available_gemini_models": available_gemini_models,
        "available_third_party_models": available_third_party_models,
        "active_gemini_model": active_gemini_model,
        "active_third_party_model": active_third_party_model,
    }


def _render_active_tasks_table(active_tasks: List[Dict[str, Any]]) -> str:
    """Render HTML table or empty state card for active tasks."""
    if not active_tasks:
        return '<div class="empty-card"><span class="empty-icon">💤</span><p class="empty-text">No container tasks currently running.</p></div>'
    rows = []
    for t in active_tasks:
        tid = html.escape(str(t.get("id", "")))
        agent = html.escape(str(t.get("agent", "")))
        target = html.escape(str(t.get("target", "")))
        elapsed = html.escape(str(t.get("elapsed", "")))
        status = html.escape(str(t.get("status", "")))
        rc_url = t.get("remote_control_url")
        if rc_url and is_safe_url(rc_url):
            rc_html = f'<a href="{html.escape(rc_url)}" target="_blank" rel="noopener" class="btn btn-sm btn-primary">🌐 Remote Control</a>'
        else:
            rc_html = '<span class="text-muted">Pending...</span>'
        rows.append(
            f'<tr><td><code>{tid}</code></td>'
            f'<td><span class="agent-badge">{agent}</span></td>'
            f'<td><code>{target}</code></td>'
            f'<td><span class="text-muted">{elapsed}</span></td>'
            f'<td><span class="status-pill status-running"><span class="spin-icon">🔄</span> {status}</span></td>'
            f'<td>{rc_html}</td></tr>'
        )
    return (
        '<div class="table-wrapper"><table class="data-table">'
        '<thead><tr><th>Task ID</th><th>Agent</th><th>Target</th><th>Elapsed</th><th>Status</th><th>Remote Control</th></tr></thead>'
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
        target = html.escape(str(t.get("target", "")))
        prio = html.escape(str(t.get("priority", "")))
        prio_label = prio if prio.startswith("P") else f"P{prio}"
        wait_time = html.escape(str(t.get("wait_time", "")))
        rows.append(
            f'<tr><td><code>{tid}</code></td>'
            f'<td><span class="agent-badge">{agent}</span></td>'
            f'<td><code>{target}</code></td>'
            f'<td><span class="priority-badge">{prio_label}</span></td>'
            f'<td><span class="text-muted">{wait_time}</span></td></tr>'
        )
    return (
        '<div class="table-wrapper"><table class="data-table">'
        '<thead><tr><th>Task ID</th><th>Agent</th><th>Target</th><th>Priority</th><th>Wait Time</th></tr></thead>'
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
        target = html.escape(str(t.get("target", "")))
        duration = html.escape(str(t.get("duration", "")))
        raw_status = str(t.get("status", "")).lower()
        is_success = "complete" in raw_status or "finish" in raw_status
        status_icon = "✅" if is_success else "❌"
        status_class = "status-completed" if is_success else "status-failed"
        status_disp = html.escape(str(t.get("status", "")))
        rc_url = t.get("remote_control_url")
        detail = str(t.get("details", ""))
        if rc_url and is_safe_url(rc_url):
            detail_html = f'<a href="{html.escape(rc_url)}" target="_blank" rel="noopener" class="btn btn-sm btn-secondary">🌐 Remote Session</a>'
        elif detail and detail != "Finished":
            trunc = detail[:40] + "..." if len(detail) > 40 else detail
            detail_html = f'<code class="error-snippet" title="{html.escape(detail)}">{html.escape(trunc)}</code>'
        else:
            detail_html = '<span class="text-muted">Finished</span>'
        rows.append(
            f'<tr><td><code>{tid}</code></td>'
            f'<td><span class="agent-badge">{agent}</span></td>'
            f'<td><code>{target}</code></td>'
            f'<td><span class="text-muted">{duration}</span></td>'
            f'<td><span class="status-pill {status_class}">{status_icon} {status_disp}</span></td>'
            f'<td>{detail_html}</td></tr>'
        )
    return (
        '<div class="table-wrapper"><table class="data-table">'
        '<thead><tr><th>Task ID</th><th>Agent</th><th>Target</th><th>Duration</th><th>Status</th><th>Details</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def render_dashboard_html(
    markdown_content: str,
    host: str = "localhost",
    port: int = 8000,
    task_manager: Optional[TaskManager] = None,
    quota_tracker: Optional[QuotaTracker] = None,
    scheduler: Optional[TaskScheduler] = None,
    extra_info: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Render a rich, responsive, dark-mode web dashboard page for web browsers.
    Includes KPI metric cards, model quota capacity progress meters, interactive
    task tables with Remote Control links, collapsible raw markdown, and smooth
    client-side auto-refresh (3-second cadence).
    """
    escaped_md = html.escape(markdown_content)
    data = parse_dashboard_markdown(markdown_content, default_host=host, default_port=port)

    # Optional enrichment from live managers if provided
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
                        data["active_tasks"].append({
                            "id": t.id,
                            "agent": t.agent,
                            "target": t.target_id or (t.repo_full_name if t.repo_full_name else "N/A"),
                            "elapsed": elapsed,
                            "status": str(t.status),
                            "remote_control_url": getattr(t, "remote_control_url", None),
                        })
            if not data["queued_tasks_list"]:
                q_objs = task_manager.get_queued_tasks()
                if q_objs:
                    now_ts = time.time()
                    for t in q_objs:
                        q_dur = format_duration(now_ts - t.enqueue_time)
                        data["queued_tasks_list"].append({
                            "id": t.id,
                            "agent": t.agent,
                            "target": t.target_id or (t.repo_full_name if t.repo_full_name else "N/A"),
                            "priority": str(t.priority),
                            "wait_time": q_dur,
                        })
            if not data["history_tasks"]:
                h_objs = task_manager.get_task_history(limit=10)
                if h_objs:
                    for t in h_objs:
                        dur = format_duration(t.finish_time - t.start_time) if (t.finish_time and t.start_time) else "N/A"
                        rc_url = getattr(t, "remote_control_url", None)
                        detail = "Remote Control" if rc_url else (t.error_message or "Finished")
                        data["history_tasks"].append({
                            "id": t.id,
                            "agent": t.agent,
                            "target": t.target_id or (t.repo_full_name if t.repo_full_name else "N/A"),
                            "duration": dur,
                            "status": str(t.status),
                            "details": detail,
                            "remote_control_url": rc_url,
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

    g_act = data.get("active_gemini_model")
    if isinstance(g_act, str) and g_act.strip() and g_act not in data["available_gemini_models"]:
        data["available_gemini_models"].insert(0, g_act)
    tp_act = data.get("active_third_party_model")
    if isinstance(tp_act, str) and tp_act.strip() and tp_act not in data["available_third_party_models"]:
        data["available_third_party_models"].insert(0, tp_act)

    gemini_options_html = "\n".join([
        f'<option value="{html.escape(m)}"{ " selected" if m == data["active_gemini_model"] else ""}>{html.escape(m)}</option>'
        for m in data["available_gemini_models"]
    ])
    tp_options_html = "\n".join([
        f'<option value="{html.escape(m)}"{ " selected" if m == data["active_third_party_model"] else ""}>{html.escape(m)}</option>'
        for m in data["available_third_party_models"]
    ])

    # Calculations for presentation
    workers_pct = min(100, int((data["active_workers"] / data["max_workers"]) * 100)) if data["max_workers"] > 0 else 0
    running_pulse_style = "display: inline-block;" if data["running_tasks"] > 0 else "display: none;"
    failed_class = "kpi-value text-red" if data["failed_tasks"] > 0 else "kpi-value text-muted"

    gemini_color = get_quota_color(data["gemini_pct"])
    gemini_bar_pct = min(100, max(0, int(data["gemini_pct"]))) if data["gemini_pct"] is not None else 100

    tp_color = get_quota_color(data["tp_pct"])
    tp_bar_pct = min(100, max(0, int(data["tp_pct"]))) if data["tp_pct"] is not None else 100

    active_table_html = _render_active_tasks_table(data["active_tasks"])
    queued_table_html = _render_queued_tasks_table(data["queued_tasks_list"])
    history_table_html = _render_history_tasks_table(data["history_tasks"])

    effective_host = html.escape(data["host"])
    effective_port = data["port"]
    effective_now = html.escape(data["now_iso"])
    status_icon = data["status_icon"]
    status_text = html.escape(data["status_text"])
    status_class = html.escape(data["status_class"])
    active_workers = data["active_workers"]
    max_workers = data["max_workers"]
    running_tasks = data["running_tasks"]
    queued_tasks_count = data["queued_tasks"]
    completed_tasks = data["completed_tasks"]
    failed_tasks = data["failed_tasks"]
    pool_str = html.escape(data["pool"])
    model_str = html.escape(data["model"])
    gemini_disp = html.escape(data["gemini_rem"])
    tp_disp = html.escape(data["tp_rem"])
    active_count = len(data["active_tasks"])
    queued_count = len(data["queued_tasks_list"])
    history_count = len(data["history_tasks"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Graviton Live Dashboard</title>
    <style>
        :root {{
            --bg: #0d1117;
            --card-bg: #161b22;
            --border: #30363d;
            --border-muted: #21262d;
            --text: #c9d1d9;
            --text-muted: #8b949e;
            --text-bright: #f0f6fc;
            --accent: #58a6ff;
            --accent-hover: #79b8ff;
            --accent-green: #3fb950;
            --accent-yellow: #d29922;
            --accent-red: #f85149;
            --accent-purple: #bc8cff;
            --table-header: #21262d;
            --table-row-hover: #1c2128;
        }}
        * {{
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px 16px;
            line-height: 1.5;
            -webkit-font-smoothing: antialiased;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 16px;
            margin-bottom: 24px;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .header-title h1 {{
            color: var(--text-bright);
            margin: 0 0 4px 0;
            font-size: 1.75rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .server-meta {{
            font-size: 0.85rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .server-meta code {{
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            color: var(--accent);
            background: rgba(88, 166, 255, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
            font-size: 0.82rem;
        }}
        .meta-dot {{
            color: var(--border);
        }}
        .auto-refresh-meta {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .refresh-dot {{
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: var(--accent-green);
            display: inline-block;
        }}
        .header-badges {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 12px;
            font-size: 0.85rem;
            font-weight: 600;
            border-radius: 20px;
            border: 1px solid transparent;
        }}
        .badge-online {{
            background: rgba(63, 185, 80, 0.15);
            color: var(--accent-green);
            border-color: rgba(63, 185, 80, 0.4);
        }}
        .badge-busy {{
            background: rgba(210, 153, 34, 0.15);
            color: var(--accent-yellow);
            border-color: rgba(210, 153, 34, 0.4);
        }}
        .badge-draining {{
            background: rgba(210, 153, 34, 0.15);
            color: var(--accent-yellow);
            border-color: rgba(210, 153, 34, 0.4);
        }}
        .badge-paused {{
            background: rgba(139, 148, 158, 0.15);
            color: var(--text-muted);
            border-color: rgba(139, 148, 158, 0.4);
        }}
        .badge-counter {{
            background: rgba(88, 166, 255, 0.1);
            color: var(--accent);
            border-color: rgba(88, 166, 255, 0.3);
            font-size: 0.8rem;
            padding: 3px 8px;
        }}
        .pulse {{
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--accent-green);
            display: inline-block;
            box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.7);
            animation: pulse-ring 2s infinite;
        }}
        .mini-pulse {{
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: var(--accent-yellow);
            display: inline-block;
            box-shadow: 0 0 0 0 rgba(210, 153, 34, 0.7);
            animation: pulse-ring-yellow 2s infinite;
            margin-left: 6px;
            vertical-align: middle;
        }}
        @keyframes pulse-ring {{
            0% {{ box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.7); }}
            70% {{ box-shadow: 0 0 0 8px rgba(63, 185, 80, 0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(63, 185, 80, 0); }}
        }}
        @keyframes pulse-ring-yellow {{
            0% {{ box-shadow: 0 0 0 0 rgba(210, 153, 34, 0.7); }}
            70% {{ box-shadow: 0 0 0 8px rgba(210, 153, 34, 0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(210, 153, 34, 0); }}
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 18px 20px;
            margin-bottom: 20px;
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.12);
        }}
        .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-muted);
            padding-bottom: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .section-title h2 {{
            color: var(--text-bright);
            margin: 0;
            font-size: 1.15rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
            gap: 16px;
            margin-bottom: 20px;
        }}
        .kpi-card {{
            padding: 16px;
            margin-bottom: 0;
            display: flex;
            flex-direction: column;
            justify-content: space-between;
        }}
        .kpi-title {{
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            font-weight: 600;
            margin-bottom: 8px;
        }}
        .kpi-value {{
            font-size: 1.6rem;
            font-weight: 600;
            color: var(--text-bright);
            margin-bottom: 8px;
            display: flex;
            align-items: center;
        }}
        .kpi-desc {{
            font-size: 0.75rem;
            color: var(--text-muted);
        }}
        .gauge-track {{
            width: 100%;
            height: 6px;
            background: var(--table-header);
            border-radius: 3px;
            overflow: hidden;
            margin-bottom: 6px;
        }}
        .gauge-fill {{
            height: 100%;
            background: var(--accent);
            border-radius: 3px;
            transition: width 0.4s ease;
        }}
        .text-green {{
            color: var(--accent-green) !important;
        }}
        .text-yellow {{
            color: var(--accent-yellow) !important;
        }}
        .text-red {{
            color: var(--accent-red) !important;
        }}
        .text-muted {{
            color: var(--text-muted) !important;
        }}
        .pill-group {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .pill {{
            padding: 3px 10px;
            font-size: 0.8rem;
            border-radius: 12px;
            font-weight: 500;
        }}
        .pill-pool {{
            background: rgba(88, 166, 255, 0.15);
            color: var(--accent);
            border: 1px solid rgba(88, 166, 255, 0.3);
        }}
        .pill-model {{
            background: rgba(188, 140, 255, 0.15);
            color: var(--accent-purple);
            border: 1px solid rgba(188, 140, 255, 0.3);
        }}
        .quota-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
        }}
        .quota-meter {{
            background: rgba(22, 27, 34, 0.5);
            border: 1px solid var(--border-muted);
            border-radius: 6px;
            padding: 14px 16px;
        }}
        .meter-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }}
        .meter-name {{
            font-size: 0.88rem;
            font-weight: 600;
            color: var(--text-bright);
        }}
        .meter-pct {{
            font-size: 0.95rem;
            font-weight: 700;
        }}
        .meter-track {{
            width: 100%;
            height: 8px;
            background: var(--table-header);
            border-radius: 4px;
            overflow: hidden;
            margin-bottom: 6px;
        }}
        .meter-fill {{
            height: 100%;
            border-radius: 4px;
            transition: width 0.4s ease, background-color 0.4s ease;
        }}
        .meter-sub {{
            font-size: 0.75rem;
            color: var(--text-muted);
        }}
        .model-select-row {{
            margin-top: 10px;
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .model-select-label {{
            font-size: 0.85rem;
            color: var(--text-muted);
            white-space: nowrap;
        }}
        .model-select-dropdown {{
            background-color: var(--bg);
            color: var(--text-bright);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 4px 8px;
            font-size: 0.85rem;
            cursor: pointer;
            outline: none;
            width: 100%;
        }}
        .model-select-dropdown:hover {{
            border-color: var(--accent);
        }}
        .model-select-dropdown:focus {{
            border-color: var(--accent);
            box-shadow: 0 0 0 2px rgba(88, 166, 255, 0.2);
        }}
        .toast-container {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            z-index: 1000;
            display: flex;
            flex-direction: column;
            gap: 8px;
            max-width: 360px;
        }}
        .toast {{
            padding: 12px 16px;
            border-radius: 6px;
            font-size: 0.875rem;
            color: #ffffff;
            box-shadow: 0 4px 12px rgba(0,0,0,0.4);
            opacity: 0;
            transform: translateY(10px);
            transition: opacity 0.3s ease, transform 0.3s ease;
        }}
        .toast.show {{
            opacity: 1;
            transform: translateY(0);
        }}
        .toast-success {{
            background-color: #238636;
            border: 1px solid #2ea043;
        }}
        .toast-error {{
            background-color: #da3633;
            border: 1px solid #f85149;
        }}
        .table-wrapper {{
            overflow-x: auto;
            border-radius: 6px;
            border: 1px solid var(--border-muted);
        }}
        .data-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.88rem;
            text-align: left;
        }}
        .data-table th {{
            background: var(--table-header);
            padding: 10px 14px;
            font-size: 0.78rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border);
        }}
        .data-table td {{
            padding: 10px 14px;
            border-top: 1px solid var(--border-muted);
            vertical-align: middle;
        }}
        .data-table tr:hover td {{
            background: var(--table-row-hover);
        }}
        .data-table code {{
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.82rem;
            background: rgba(110, 118, 129, 0.2);
            padding: 2px 6px;
            border-radius: 4px;
            color: var(--text-bright);
        }}
        .agent-badge {{
            display: inline-block;
            background: rgba(88, 166, 255, 0.12);
            color: var(--accent);
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.78rem;
            font-weight: 600;
            border: 1px solid rgba(88, 166, 255, 0.25);
        }}
        .priority-badge {{
            display: inline-block;
            background: rgba(210, 153, 34, 0.15);
            color: var(--accent-yellow);
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 0.78rem;
            font-weight: 700;
            border: 1px solid rgba(210, 153, 34, 0.3);
        }}
        .status-pill {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.78rem;
            font-weight: 600;
        }}
        .status-running {{
            background: rgba(88, 166, 255, 0.15);
            color: var(--accent);
            border: 1px solid rgba(88, 166, 255, 0.3);
        }}
        .status-completed {{
            background: rgba(63, 185, 80, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(63, 185, 80, 0.3);
        }}
        .status-failed {{
            background: rgba(248, 81, 73, 0.15);
            color: var(--accent-red);
            border: 1px solid rgba(248, 81, 73, 0.3);
        }}
        .spin-icon {{
            display: inline-block;
            animation: spin 3s linear infinite;
        }}
        @keyframes spin {{
            100% {{ transform: rotate(360deg); }}
        }}
        .btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            font-weight: 500;
            text-decoration: none;
            cursor: pointer;
            transition: background 0.15s ease, border-color 0.15s ease;
        }}
        .btn-primary {{
            background: #238636;
            color: #ffffff;
            border: 1px solid rgba(240, 246, 252, 0.1);
        }}
        .btn-primary:hover {{
            background: #2ea043;
            color: #ffffff;
            text-decoration: none;
        }}
        .btn-secondary {{
            background: #21262d;
            color: var(--text);
            border: 1px solid var(--border);
        }}
        .btn-secondary:hover {{
            background: #30363d;
            color: var(--text-bright);
            text-decoration: none;
        }}
        .empty-card {{
            text-align: center;
            padding: 32px 16px;
            color: var(--text-muted);
        }}
        .empty-icon {{
            font-size: 2rem;
            display: block;
            margin-bottom: 8px;
        }}
        .empty-text {{
            margin: 0;
            font-size: 0.9rem;
            font-style: italic;
        }}
        .error-snippet {{
            color: var(--accent-red) !important;
            background: rgba(248, 81, 73, 0.1) !important;
            font-size: 0.78rem !important;
        }}
        .tips-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 12px;
            margin-bottom: 16px;
        }}
        .tip-item {{
            background: rgba(22, 27, 34, 0.5);
            border: 1px solid var(--border-muted);
            border-radius: 6px;
            padding: 10px 14px;
            display: flex;
            flex-direction: column;
            gap: 4px;
        }}
        .tip-item code {{
            color: var(--accent);
            font-size: 0.8rem;
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
        }}
        .tip-item span {{
            color: var(--text-muted);
            font-size: 0.78rem;
        }}
        .markdown-details {{
            margin-top: 16px;
            border-top: 1px solid var(--border-muted);
            padding-top: 14px;
        }}
        .markdown-summary {{
            cursor: pointer;
            font-size: 0.86rem;
            font-weight: 600;
            color: var(--text-bright);
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 12px;
            background: rgba(33, 38, 45, 0.6);
            border: 1px solid var(--border);
            border-radius: 6px;
            user-select: none;
        }}
        .markdown-summary:hover {{
            background: rgba(33, 38, 45, 0.9);
        }}
        .toggle-hint {{
            font-size: 0.75rem;
            color: var(--text-muted);
            font-weight: 400;
        }}
        .markdown-body {{
            margin-top: 10px;
        }}
        pre.markdown-view {{
            background: #090d12;
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 16px;
            overflow-x: auto;
            color: var(--text);
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.85rem;
            white-space: pre-wrap;
            word-break: break-word;
            margin: 0;
        }}
        .footer {{
            margin-top: 30px;
            font-size: 0.85rem;
            color: var(--text-muted);
            text-align: center;
        }}
        a {{
            color: var(--accent);
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-title">
                <h1>🌌 Graviton Live Dashboard <span class="pulse"></span></h1>
                <div class="server-meta">
                    <span class="meta-item">Host: <code id="meta-host">{effective_host}:{effective_port}</code></span>
                    <span class="meta-dot">&bull;</span>
                    <span class="meta-item">Updated: <span id="meta-updated">{effective_now}</span></span>
                    <span class="meta-dot">&bull;</span>
                    <span class="meta-item auto-refresh-meta"><span class="refresh-dot"></span> Auto-refreshing (3s)</span>
                </div>
            </div>
            <div class="header-badges">
                <span id="status-badge" class="badge badge-{status_class}">{status_icon} {status_text}</span>
            </div>
        </header>

        <!-- KPI Metric Cards Grid -->
        <div class="kpi-grid">
            <div class="card kpi-card">
                <div class="kpi-title">Active Workers</div>
                <div class="kpi-value" id="kpi-workers">{active_workers} / {max_workers}</div>
                <div class="gauge-track">
                    <div class="gauge-fill" id="kpi-workers-bar" style="width: {workers_pct}%;"></div>
                </div>
                <div class="kpi-desc">Capacity Utilization</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-title">Running Tasks</div>
                <div class="kpi-value">
                    <span id="kpi-running-val">{running_tasks}</span>
                    <span class="mini-pulse" id="kpi-running-pulse" style="{running_pulse_style}"></span>
                </div>
                <div class="kpi-desc">Supervisor Containers</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-title">Queued Tasks</div>
                <div class="kpi-value" id="kpi-queued">{queued_tasks_count}</div>
                <div class="kpi-desc">Awaiting Worker Slot</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-title">Completed Tasks</div>
                <div class="kpi-value text-green" id="kpi-completed">{completed_tasks}</div>
                <div class="kpi-desc">Successful Executions</div>
            </div>
            <div class="card kpi-card">
                <div class="kpi-title">Failed Tasks</div>
                <div class="{failed_class}" id="kpi-failed">{failed_tasks}</div>
                <div class="kpi-desc">Aborted / Errored</div>
            </div>
        </div>

        <!-- Model Quota & Pacing Meters Card -->
        <div class="card section-card">
            <div class="section-header">
                <div class="section-title">
                    <h2>🎯 Model Quota &amp; Pacing</h2>
                </div>
                <div class="pill-group">
                    <span class="pill pill-pool" id="quota-pool">Pool: <strong>{pool_str}</strong></span>
                    <span class="pill pill-model" id="quota-model">Model: <strong>{model_str}</strong></span>
                </div>
            </div>
            <div class="quota-grid">
                <div class="quota-meter">
                    <div class="meter-header">
                        <span class="meter-name">Gemini API Capacity</span>
                        <span class="meter-pct" id="gemini-pct-label" style="color: {gemini_color};">{gemini_disp}</span>
                    </div>
                    <div class="meter-track">
                        <div id="gemini-bar" class="meter-fill" style="width: {gemini_bar_pct}%; background-color: {gemini_color};"></div>
                    </div>
                    <div class="meter-sub">Live Gemini rate &amp; token pacing</div>
                    <div class="model-select-row">
                        <label for="gemini-model-select" class="model-select-label">Active Model:</label>
                        <select id="gemini-model-select" class="model-select-dropdown" data-pool="gemini">
{gemini_options_html}
                        </select>
                    </div>
                </div>
                <div class="quota-meter">
                    <div class="meter-header">
                        <span class="meter-name">Third-Party (Claude) Capacity</span>
                        <span class="meter-pct" id="tp-pct-label" style="color: {tp_color};">{tp_disp}</span>
                    </div>
                    <div class="meter-track">
                        <div id="tp-bar" class="meter-fill" style="width: {tp_bar_pct}%; background-color: {tp_color};"></div>
                    </div>
                    <div class="meter-sub">Fallback provider quota budget</div>
                    <div class="model-select-row">
                        <label for="tp-model-select" class="model-select-label">Active Model:</label>
                        <select id="tp-model-select" class="model-select-dropdown" data-pool="third_party">
{tp_options_html}
                        </select>
                    </div>
                </div>
            </div>
        </div>

        <div id="toast-container" class="toast-container"></div>

        <!-- Active Container Tasks Table -->
        <div class="card section-card">
            <div class="section-header">
                <div class="section-title">
                    <h2>🚀 Active Container Tasks</h2>
                </div>
                <span id="active-tasks-count" class="badge badge-counter">{active_count} Active</span>
            </div>
            <div id="active-tasks-container">
                {active_table_html}
            </div>
        </div>

        <!-- Queued Tasks Table -->
        <div class="card section-card">
            <div class="section-header">
                <div class="section-title">
                    <h2>⏳ Queued Tasks</h2>
                </div>
                <span id="queued-tasks-count" class="badge badge-counter">{queued_count} Queued</span>
            </div>
            <div id="queued-tasks-container">
                {queued_table_html}
            </div>
        </div>

        <!-- Recent Task Execution History Table -->
        <div class="card section-card">
            <div class="section-header">
                <div class="section-title">
                    <h2>📜 Recent Task Execution History</h2>
                </div>
                <span id="history-tasks-count" class="badge badge-counter">{history_count} Recorded</span>
            </div>
            <div id="history-tasks-container">
                {history_table_html}
            </div>
        </div>

        <!-- Collapsible Raw Markdown View & Quick Tips -->
        <div class="card section-card">
            <div class="section-header">
                <div class="section-title">
                    <h2>💡 Quick Tips &amp; MCP Agent Control</h2>
                </div>
            </div>
            <div class="tips-grid">
                <div class="tip-item">
                    <code>graviton_submit_task(agent, prompt, target_id)</code>
                    <span>Submit containerized autonomous tasks to worker queue</span>
                </div>
                <div class="tip-item">
                    <code>graviton_submit_review(pr_number, repo)</code>
                    <span>Trigger autonomous PR reviews with code-reviewer agent</span>
                </div>
                <div class="tip-item">
                    <code>graviton_get_task(task_id)</code>
                    <span>Inspect real-time execution logs &amp; supervisor session URLs</span>
                </div>
            </div>
            <details class="markdown-details">
                <summary class="markdown-summary">
                    <span>📄 Inspect Raw Markdown (Antigravity Artifact View)</span>
                    <span class="toggle-hint">Click to toggle</span>
                </summary>
                <div class="markdown-body">
                    <pre id="content" class="markdown-view">{escaped_md}</pre>
                </div>
            </details>
        </div>

        <div class="footer">
            Graviton Autonomous Supervisor &middot; Serving on <code>{effective_host}:{effective_port}</code> &middot; Auto-refreshes every 3 seconds
        </div>
    </div>

    <script>
        function escapeHtml(str) {{
            if (!str) return '';
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }}

        function isSafeUrl(url) {{
            if (!url) return false;
            const clean = String(url).trim().toLowerCase();
            return clean.startsWith('http://') || clean.startsWith('https://');
        }}

        function getQuotaColor(pct) {{
            if (pct === null) return '#58a6ff';
            if (pct > 50) return '#3fb950';
            if (pct >= 20) return '#d29922';
            return '#f85149';
        }}

        function updateDashboardUI(md) {{
            if (!md) return;

            // 1. Status & Meta
            let statusText = 'ONLINE';
            let statusIcon = '🟢';
            let statusClass = 'online';
            const statusMatch = md.match(/\\*\\*Status\\*\\*:\\s*([^\\n|&]+)/);
            const statusLine = statusMatch ? statusMatch[1] : md.substring(0, 500);
            if (statusLine.includes('BUSY')) {{
                statusText = 'BUSY';
                statusIcon = '🟡';
                statusClass = 'busy';
            }} else if (statusLine.includes('DRAINING')) {{
                statusText = 'DRAINING';
                statusIcon = '⏳';
                statusClass = 'draining';
            }} else if (statusLine.includes('PAUSED')) {{
                statusText = 'PAUSED';
                statusIcon = '⏸️';
                statusClass = 'paused';
            }} else if (statusLine.includes('ONLINE')) {{
                statusText = 'ONLINE';
                statusIcon = '🟢';
                statusClass = 'online';
            }}
            const statusBadge = document.getElementById('status-badge');
            if (statusBadge) {{
                statusBadge.className = `badge badge-${{statusClass}}`;
                statusBadge.textContent = `${{statusIcon}} ${{statusText}}`;
            }}

            const updatedMatch = md.match(/\\*Last updated:\\s*([^(]+)/);
            if (updatedMatch) {{
                const metaUpdated = document.getElementById('meta-updated');
                if (metaUpdated) metaUpdated.textContent = updatedMatch[1].trim();
            }}

            // 2. KPI Metrics
            const workersMatch = md.match(/\\|\\s*\\*\\*Active Workers\\*\\*\\s*\\|\\s*`?(\\d+)\\s*\\/\\s*(\\d+)`?/);
            let activeWorkers = 0, maxWorkers = 0;
            if (workersMatch) {{
                activeWorkers = parseInt(workersMatch[1], 10);
                maxWorkers = parseInt(workersMatch[2], 10);
            }}
            const kpiWorkers = document.getElementById('kpi-workers');
            if (kpiWorkers) kpiWorkers.textContent = `${{activeWorkers}} / ${{maxWorkers}}`;
            const kpiWorkersBar = document.getElementById('kpi-workers-bar');
            if (kpiWorkersBar) {{
                const workerPct = maxWorkers > 0 ? Math.min(100, Math.round((activeWorkers / maxWorkers) * 100)) : 0;
                kpiWorkersBar.style.width = `${{workerPct}}%`;
            }}

            function extractInt(label) {{
                const regex = new RegExp("\\\\|\\\\s*\\\\*\\\\*" + label + "\\\\*\\\\*\\\\s*\\\\|\\\\s*`?(\\\\d+)`?");
                const m = md.match(regex);
                return m ? parseInt(m[1], 10) : 0;
            }}

            const runningTasks = extractInt('Running Tasks');
            const queuedTasks = extractInt('Queued Tasks');
            const completedTasks = extractInt('Completed Tasks');
            const failedTasks = extractInt('Failed Tasks');

            const kpiRunning = document.getElementById('kpi-running-val');
            if (kpiRunning) kpiRunning.textContent = runningTasks;
            const kpiRunningPulse = document.getElementById('kpi-running-pulse');
            if (kpiRunningPulse) kpiRunningPulse.style.display = runningTasks > 0 ? 'inline-block' : 'none';

            const kpiQueued = document.getElementById('kpi-queued');
            if (kpiQueued) kpiQueued.textContent = queuedTasks;

            const kpiCompleted = document.getElementById('kpi-completed');
            if (kpiCompleted) kpiCompleted.textContent = completedTasks;

            const kpiFailed = document.getElementById('kpi-failed');
            if (kpiFailed) {{
                kpiFailed.textContent = failedTasks;
                kpiFailed.className = failedTasks > 0 ? 'kpi-value text-red' : 'kpi-value text-muted';
            }}

            // 3. Quota & Model
            function extractStr(label, defVal) {{
                const regex = new RegExp("\\\\|\\\\s*\\\\*\\\\*" + label + "\\\\*\\\\*\\\\s*\\\\|\\\\s*`?([^`|\\\\n]+)`?");
                const m = md.match(regex);
                return m ? m[1].trim() : defVal;
            }}
            const pool = extractStr('Active Pool', 'default');
            const model = extractStr('Active Model', 'default');
            const geminiRem = extractStr('Gemini Remaining', 'N/A');
            const geminiModel = extractStr('Active Gemini Model', '');
            const tpModel = extractStr('Active Third-Party Model', '');
            const tpRem = extractStr('Third-Party Remaining', 'N/A');

            const quotaPool = document.getElementById('quota-pool');
            if (quotaPool) quotaPool.innerHTML = `Pool: <strong>${{escapeHtml(pool)}}</strong>`;
            const quotaModel = document.getElementById('quota-model');
            if (quotaModel) quotaModel.innerHTML = `Model: <strong>${{escapeHtml(model)}}</strong>`;

            const targetGModel = (geminiModel && geminiModel !== 'default') ? geminiModel : (!pool.toLowerCase().includes('third') && !pool.toLowerCase().includes('claude') && !pool.toLowerCase().includes('3p') ? model : '');
            const gSelect = document.getElementById('gemini-model-select');
            if (gSelect && document.activeElement !== gSelect && targetGModel && targetGModel !== 'default') {{
                if (Array.from(gSelect.options).some(o => o.value === targetGModel)) {{
                    gSelect.value = targetGModel;
                    gSelect.setAttribute('data-last-val', targetGModel);
                }}
            }}
            const targetTModel = (tpModel && tpModel !== 'default') ? tpModel : ((pool.toLowerCase().includes('third') || pool.toLowerCase().includes('claude') || pool.toLowerCase().includes('3p')) ? model : '');
            const tSelect = document.getElementById('tp-model-select');
            if (tSelect && document.activeElement !== tSelect && targetTModel && targetTModel !== 'default') {{
                if (Array.from(tSelect.options).some(o => o.value === targetTModel)) {{
                    tSelect.value = targetTModel;
                    tSelect.setAttribute('data-last-val', targetTModel);
                }}
            }}

            function parsePct(str) {{
                if (!str || str.startsWith('N/A')) return null;
                const num = parseFloat(str.replace('%', ''));
                return isNaN(num) ? null : num;
            }}

            const geminiPct = parsePct(geminiRem);
            const tpPct = parsePct(tpRem);

            const geminiLabel = document.getElementById('gemini-pct-label');
            const geminiBar = document.getElementById('gemini-bar');
            if (geminiLabel) {{
                geminiLabel.textContent = geminiRem;
                geminiLabel.style.color = getQuotaColor(geminiPct);
            }}
            if (geminiBar) {{
                geminiBar.style.width = `${{geminiPct !== null ? Math.min(100, Math.max(0, geminiPct)) : 100}}%`;
                geminiBar.style.backgroundColor = getQuotaColor(geminiPct);
            }}

            const tpLabel = document.getElementById('tp-pct-label');
            const tpBar = document.getElementById('tp-bar');
            if (tpLabel) {{
                tpLabel.textContent = tpRem;
                tpLabel.style.color = getQuotaColor(tpPct);
            }}
            if (tpBar) {{
                tpBar.style.width = `${{tpPct !== null ? Math.min(100, Math.max(0, tpPct)) : 100}}%`;
                tpBar.style.backgroundColor = getQuotaColor(tpPct);
            }}

            // 4. Tables Parsing
            const sections = md.split(/^##\\s+/m);
            let activeSec = '', queuedSec = '', historySec = '';
            for (const sec of sections) {{
                if (sec.startsWith('🚀 Active Container Tasks')) activeSec = sec;
                else if (sec.startsWith('⏳ Queued Tasks')) queuedSec = sec;
                else if (sec.startsWith('📜 Recent Task Execution History')) historySec = sec;
            }}

            function parseTableRows(sec) {{
                if (!sec) return [];
                const lines = sec.split('\\n').map(l => l.trim());
                const rows = [];
                for (const line of lines) {{
                    if (line.startsWith('|') && !line.startsWith('| Metric') && !line.includes('Task ID')) {{
                        const parts = line.split('|').slice(1, -1).map(c => c.trim());
                        if (parts.length > 0 && !parts.every(p => /^:?-+:?$/.test(p))) {{
                            rows.push(parts);
                        }}
                    }}
                }}
                return rows;
            }}

            // Active Tasks Table
            const activeRows = parseTableRows(activeSec);
            const validActiveRows = activeRows.filter(r => r.length >= 6);
            const activeCount = document.getElementById('active-tasks-count');
            if (activeCount) activeCount.textContent = `${{validActiveRows.length}} Active`;
            const activeContainer = document.getElementById('active-tasks-container');
            if (activeContainer) {{
                if (validActiveRows.length === 0) {{
                    activeContainer.innerHTML = '<div class="empty-card"><span class="empty-icon">💤</span><p class="empty-text">No container tasks currently running.</p></div>';
                }} else {{
                    let htmlStr = '<div class="table-wrapper"><table class="data-table"><thead><tr><th>Task ID</th><th>Agent</th><th>Target</th><th>Elapsed</th><th>Status</th><th>Remote Control</th></tr></thead><tbody>';
                    for (const r of validActiveRows) {{
                        const tid = escapeHtml(r[0].replace(/`/g, ''));
                        const agent = escapeHtml(r[1].replace(/`/g, ''));
                        const target = escapeHtml(r[2].replace(/`/g, ''));
                        const elapsed = escapeHtml(r[3]);
                        const status = escapeHtml(r[4].replace(/[🔄`]/g, '').trim());
                        let rcHtml = '<span class="text-muted">Pending...</span>';
                        const urlMatch = r[5].match(/\\[(.*?)\\]\\((.*?)\\)/);
                        if (urlMatch && isSafeUrl(urlMatch[2])) {{
                            rcHtml = `<a href="${{escapeHtml(urlMatch[2].trim())}}" target="_blank" rel="noopener" class="btn btn-sm btn-primary">🌐 Remote Control</a>`;
                        }}
                        htmlStr += `<tr><td><code>${{tid}}</code></td><td><span class="agent-badge">${{agent}}</span></td><td><code>${{target}}</code></td><td><span class="text-muted">${{elapsed}}</span></td><td><span class="status-pill status-running"><span class="spin-icon">🔄</span> ${{status}}</span></td><td>${{rcHtml}}</td></tr>`;
                    }}
                    htmlStr += '</tbody></table></div>';
                    activeContainer.innerHTML = htmlStr;
                }}
            }}

            // Queued Tasks Table
            const queuedRows = parseTableRows(queuedSec);
            const validQueuedRows = queuedRows.filter(r => r.length >= 5);
            const queuedCount = document.getElementById('queued-tasks-count');
            if (queuedCount) queuedCount.textContent = `${{validQueuedRows.length}} Queued`;
            const queuedContainer = document.getElementById('queued-tasks-container');
            if (queuedContainer) {{
                if (validQueuedRows.length === 0) {{
                    queuedContainer.innerHTML = '<div class="empty-card"><span class="empty-icon">📭</span><p class="empty-text">Queue is empty.</p></div>';
                }} else {{
                    let htmlStr = '<div class="table-wrapper"><table class="data-table"><thead><tr><th>Task ID</th><th>Agent</th><th>Target</th><th>Priority</th><th>Wait Time</th></tr></thead><tbody>';
                    for (const r of validQueuedRows) {{
                        const tid = escapeHtml(r[0].replace(/`/g, ''));
                        const agent = escapeHtml(r[1].replace(/`/g, ''));
                        const target = escapeHtml(r[2].replace(/`/g, ''));
                        const prio = escapeHtml(r[3].replace(/`/g, ''));
                        const prioLabel = prio.startsWith('P') ? prio : `P${{prio}}`;
                        const waitTime = escapeHtml(r[4]);
                        htmlStr += `<tr><td><code>${{tid}}</code></td><td><span class="agent-badge">${{agent}}</span></td><td><code>${{target}}</code></td><td><span class="priority-badge">${{prioLabel}}</span></td><td><span class="text-muted">${{waitTime}}</span></td></tr>`;
                    }}
                    htmlStr += '</tbody></table></div>';
                    queuedContainer.innerHTML = htmlStr;
                }}
            }}

            // History Tasks Table
            const historyRows = parseTableRows(historySec);
            const validHistoryRows = historyRows.filter(r => r.length >= 6);
            const historyCount = document.getElementById('history-tasks-count');
            if (historyCount) historyCount.textContent = `${{validHistoryRows.length}} Recorded`;
            const historyContainer = document.getElementById('history-tasks-container');
            if (historyContainer) {{
                if (validHistoryRows.length === 0) {{
                    historyContainer.innerHTML = '<div class="empty-card"><span class="empty-icon">📜</span><p class="empty-text">No completed tasks in history yet.</p></div>';
                }} else {{
                    let htmlStr = '<div class="table-wrapper"><table class="data-table"><thead><tr><th>Task ID</th><th>Agent</th><th>Target</th><th>Duration</th><th>Status</th><th>Details</th></tr></thead><tbody>';
                    for (const r of validHistoryRows) {{
                        const tid = escapeHtml(r[0].replace(/`/g, ''));
                        const agent = escapeHtml(r[1].replace(/`/g, ''));
                        const target = escapeHtml(r[2].replace(/`/g, ''));
                        const dur = escapeHtml(r[3]);
                        const isSuccess = r[4].includes('✅') || r[4].toLowerCase().includes('complete');
                        const statusClean = escapeHtml(r[4].replace(/[✅❌`]/g, '').trim());
                        const statusIcon = isSuccess ? '✅' : '❌';
                        const statusPillClass = isSuccess ? 'status-completed' : 'status-failed';
                        let detailHtml = '<span class="text-muted">Finished</span>';
                        const urlMatch = r[5].match(/\\[(.*?)\\]\\((.*?)\\)/);
                        if (urlMatch && isSafeUrl(urlMatch[2])) {{
                            detailHtml = `<a href="${{escapeHtml(urlMatch[2].trim())}}" target="_blank" rel="noopener" class="btn btn-sm btn-secondary">🌐 Remote Session</a>`;
                        }} else if (r[5].trim() && r[5].trim() !== 'Finished') {{
                            const cleanDetail = escapeHtml(r[5].replace(/`/g, '').trim());
                            const trunc = cleanDetail.length > 40 ? cleanDetail.substring(0, 40) + '...' : cleanDetail;
                            detailHtml = `<code class="error-snippet" title="${{cleanDetail}}">${{trunc}}</code>`;
                        }}
                        htmlStr += `<tr><td><code>${{tid}}</code></td><td><span class="agent-badge">${{agent}}</span></td><td><code>${{target}}</code></td><td><span class="text-muted">${{dur}}</span></td><td><span class="status-pill ${{statusPillClass}}">${{statusIcon}} ${{statusClean}}</span></td><td>${{detailHtml}}</td></tr>`;
                    }}
                    htmlStr += '</tbody></table></div>';
                    historyContainer.innerHTML = htmlStr;
                }}
            }}
        }}

        function showToast(message, type) {{
            const container = document.getElementById('toast-container');
            if (!container) return;
            const toast = document.createElement('div');
            toast.className = `toast toast-${{type || 'success'}}`;
            toast.textContent = message;
            container.appendChild(toast);
            setTimeout(() => toast.classList.add('show'), 10);
            setTimeout(() => {{
                toast.classList.remove('show');
                setTimeout(() => toast.remove(), 300);
            }}, 3000);
        }}

        async function onModelSelectChange(event) {{
            const select = event.target;
            const pool = select.getAttribute('data-pool');
            const model = select.value;
            const originalVal = select.getAttribute('data-last-val') || model;
            select.disabled = true;
            try {{
                const res = await fetch('/api/model', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ pool: pool, model: model }})
                }});
                if (res.ok) {{
                    const resData = await res.json();
                    const newModel = resData.active_model || model;
                    select.setAttribute('data-last-val', newModel);
                    const poolName = pool === 'gemini' ? 'Gemini' : 'Third-Party';
                    showToast(`Updated ${{poolName}} active model to ${{newModel}}`, 'success');
                    refreshDashboard();
                }} else {{
                    let errText = 'Failed to set model';
                    try {{
                        const errData = await res.json();
                        if (errData && errData.error) errText = errData.error;
                    }} catch (e) {{}}
                    select.value = originalVal;
                    showToast(`Error: ${{errText}}`, 'error');
                }}
            }} catch (err) {{
                select.value = originalVal;
                showToast('Network error: Could not reach Graviton server', 'error');
            }} finally {{
                select.disabled = false;
            }}
        }}

        document.addEventListener('DOMContentLoaded', () => {{
            const dropdowns = document.querySelectorAll('.model-select-dropdown');
            dropdowns.forEach(d => {{
                d.setAttribute('data-last-val', d.value);
                d.addEventListener('change', onModelSelectChange);
            }});
        }});

        async function refreshDashboard() {{
            try {{
                const res = await fetch('/dashboard/content');
                if (res.ok) {{
                    const data = await res.json();
                    if (data && data.markdown) {{
                        const contentEl = document.getElementById('content');
                        if (contentEl) {{
                            contentEl.textContent = data.markdown;
                        }}
                        updateDashboardUI(data.markdown);
                    }}
                }}
            }} catch (err) {{
                console.error('Failed to auto-refresh dashboard:', err);
            }}
        }}
        setInterval(refreshDashboard, 3000);
    </script>
</body>
</html>
"""


class DashboardUpdater:
    """
    Manages continuous live dashboard updates to registered file paths on disk.

    When Antigravity opens a side panel artifact, its path is registered with
    DashboardUpdater. As tasks progress, the updater writes formatted markdown
    directly to the file on disk. Antigravity detects the disk change and live-reloads
    the side panel artifact automatically.
    """

    def __init__(
        self,
        task_manager: Optional[TaskManager] = None,
        quota_tracker: Optional[QuotaTracker] = None,
        scheduler: Optional[TaskScheduler] = None,
        host: str = "localhost",
        port: int = 8000,
        update_interval: float = 2.0,
        min_interval: float = 0.5,
    ):
        self.task_manager = task_manager
        self.quota_tracker = quota_tracker
        self.scheduler = scheduler
        self.host = host
        self.port = port
        self.update_interval = update_interval
        self.min_interval = min_interval

        self._targets: Set[Path] = set()
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_update_ts: float = 0.0

    def register_target(self, path: Union[str, Path]) -> bool:
        """Register a file path to receive live dashboard markdown updates."""
        resolved = Path(path).resolve()
        with self._lock:
            self._targets.add(resolved)
        logger.info(f"Registered live dashboard artifact target: {resolved}")
        self.trigger_update()
        return True

    def unregister_target(self, path: Union[str, Path]) -> bool:
        """Unregister a target path from live updates."""
        resolved = Path(path).resolve()
        with self._lock:
            if resolved in self._targets:
                self._targets.remove(resolved)
                logger.info(f"Unregistered live dashboard artifact target: {resolved}")
                return True
        return False

    def get_targets(self) -> List[str]:
        """Return list of currently registered target file paths."""
        with self._lock:
            return [str(p) for p in self._targets]

    def trigger_update(self) -> None:
        """Signal the updater background thread to refresh targets."""
        self._wake_event.set()

    def start(self) -> None:
        """Start the background updater loop."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="GravitonDashboardUpdater",
            )
            self._thread.start()
            logger.info("Graviton DashboardUpdater background loop started.")

    def stop(self) -> None:
        """Stop the background updater loop and join thread."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._wake_event.set()
        if self._thread and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=3.0)
        logger.info("Graviton DashboardUpdater stopped.")

    def get_markdown(self) -> str:
        """Generate and return current markdown content without writing to targets."""
        return format_dashboard_markdown(
            task_manager=self.task_manager,
            quota_tracker=self.quota_tracker,
            scheduler=self.scheduler,
            host=self.host,
            port=self.port,
        )

    def update_now(self) -> Optional[str]:
        """Generate current markdown content and write to all registered targets."""
        content = self.get_markdown()

        with self._lock:
            targets = list(self._targets)

        for target in targets:
            self._write_to_target(target, content)

        self._last_update_ts = time.time()
        return content

    def _write_to_target(self, target: Path, content: str) -> bool:
        """Atomically write markdown content to a target file path."""
        tmp_path: Optional[Path] = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target.with_suffix(f"{target.suffix}.tmp.{os.getpid()}.{threading.get_ident()}")
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(target)
            return True
        except Exception as e:
            logger.warning(f"Failed writing live dashboard to {target}: {e}")
            if tmp_path is not None:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
            return False

    def _run_loop(self) -> None:
        """Background updater loop handling timed heartbeats and triggered events."""
        while self._running:
            # Wait for wake event or periodic timeout
            woken = self._wake_event.wait(timeout=self.update_interval)
            if not self._running:
                break
            self._wake_event.clear()

            # Throttle if triggered too quickly
            now = time.time()
            elapsed_since_last = now - self._last_update_ts
            if elapsed_since_last < self.min_interval:
                time.sleep(self.min_interval - elapsed_since_last)
                if not self._running:
                    break

            try:
                self.update_now()
            except Exception as e:
                logger.warning(f"Error during dashboard update cycle: {e}")
