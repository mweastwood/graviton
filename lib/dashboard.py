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

# Module-level pre-compiled regex patterns
SERVER_PATTERN = re.compile(r"\*\*Server\*\*:\s*`([^:`]+):(\d+)`")
STATUS_PATTERN = re.compile(r"\*\*Status\*\*:\s*([^\n|&]+)")
UPDATED_PATTERN = re.compile(r"\*Last updated:\s*([^(]+)")
WORKERS_PATTERN = re.compile(r"\|\s*\*\*Active Workers\*\*\s*\|\s*`?(\d+)\s*/\s*(\d+)`?")
METRIC_INT_PATTERNS = {
    "Running Tasks": re.compile(r"\|\s*\*\*Running Tasks\*\*\s*\|\s*`?(\d+)`?"),
    "Queued Tasks": re.compile(r"\|\s*\*\*Queued Tasks\*\*\s*\|\s*`?(\d+)`?"),
    "Completed Tasks": re.compile(r"\|\s*\*\*Completed Tasks\*\*\s*\|\s*`?(\d+)`?"),
    "Failed Tasks": re.compile(r"\|\s*\*\*Failed Tasks\*\*\s*\|\s*`?(\d+)`?"),
}
METRIC_STR_PATTERNS = {
    "Active Pool": re.compile(r"\|\s*\*\*Active Pool\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Active Model": re.compile(r"\|\s*\*\*Active Model\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Gemini Remaining": re.compile(r"\|\s*\*\*Gemini Remaining\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third-Party Remaining": re.compile(r"\|\s*\*\*Third-Party Remaining\*\*\s*\|\s*`?([^`|\n]+)`?"),
}
SECTION_SPLIT_PATTERN = re.compile(r"(?m)^##\s+")
STATUS_CLEANUP_PATTERN = re.compile(r"[🔄`\s]+")
STATUS_HISTORY_CLEANUP_PATTERN = re.compile(r"[✅❌`\s]+")
MD_LINK_PATTERN = re.compile(r"\[.*?\]\((.*?)\)")

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
            Path(__file__).resolve().parent / "templates" / "dashboard" / "dashboard.html",
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
    <p>Status: {status_icon} {status_text} | Host: {effective_host}:{effective_port}</p>
    <p>Active Workers: {active_workers} / {max_workers}</p>
    <p>Running Tasks: {running_tasks} | Queued: {queued_tasks_count} | Completed: {completed_tasks} | Failed: {failed_tasks}</p>
    <div>{active_table_html}</div>
    <div>{queued_table_html}</div>
    <div>{history_table_html}</div>
    <pre id="content">{escaped_md}</pre>
</body>
</html>"""



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

    template = _get_dashboard_template()

    return template.format(
        effective_host=effective_host,
        effective_port=effective_port,
        effective_now=effective_now,
        status_icon=status_icon,
        status_text=status_text,
        status_class=status_class,
        active_workers=active_workers,
        max_workers=max_workers,
        workers_pct=workers_pct,
        running_tasks=running_tasks,
        running_pulse_style=running_pulse_style,
        queued_tasks_count=queued_tasks_count,
        completed_tasks=completed_tasks,
        failed_tasks=failed_tasks,
        failed_class=failed_class,
        pool_str=pool_str,
        model_str=model_str,
        gemini_color=gemini_color,
        gemini_disp=gemini_disp,
        gemini_bar_pct=gemini_bar_pct,
        gemini_options_html=gemini_options_html,
        tp_color=tp_color,
        tp_disp=tp_disp,
        tp_bar_pct=tp_bar_pct,
        tp_options_html=tp_options_html,
        active_count=active_count,
        active_table_html=active_table_html,
        queued_count=queued_count,
        queued_table_html=queued_table_html,
        history_count=history_count,
        history_table_html=history_table_html,
        escaped_md=escaped_md,
    )


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
