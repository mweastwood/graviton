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
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.tasks import TaskManager, Task, TaskStatus
from lib.quota import QuotaTracker
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

    pool = quota_info.get("current_pool", "default")
    model = quota_info.get("selected_model") or (quota_tracker.get_selected_model() if quota_tracker else "default")
    gemini_rem = quota_info.get("gemini_remaining_percentage", "N/A")
    tp_rem = quota_info.get("third_party_remaining_percentage", "N/A")
    lines.extend([
        f"| **Active Pool** | `{pool}` | Configured quota bucket |",
        f"| **Active Model** | `{model}` | Active Gemini / LLM persona |",
        f"| **Gemini Remaining** | `{gemini_rem}%` | Live Gemini API capacity |",
        f"| **Third-Party Remaining** | `{tp_rem}%` | Fallback model capacity |",
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


def render_dashboard_html(markdown_content: str, host: str = "localhost", port: int = 8000) -> str:
    """
    Render a self-contained, reactive HTML dashboard page for web browsers.
    Includes auto-refresh and clean dark-mode presentation.
    """
    escaped_md = html.escape(markdown_content)
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
            --text: #c9d1d9;
            --text-bright: #f0f6fc;
            --accent: #58a6ff;
            --accent-green: #3fb950;
            --accent-yellow: #d29922;
            --accent-red: #f85149;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
            line-height: 1.6;
        }}
        .container {{
            max-width: 1100px;
            margin: 0 auto;
        }}
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border);
            padding-bottom: 16px;
            margin-bottom: 24px;
        }}
        h1 {{
            color: var(--text-bright);
            margin: 0;
            font-size: 1.8rem;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .badge {{
            display: inline-block;
            padding: 3px 8px;
            font-size: 0.85rem;
            font-weight: 600;
            border-radius: 12px;
            background: rgba(63, 185, 80, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(63, 185, 80, 0.4);
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }}
        pre.markdown-view {{
            background: var(--card-bg);
            border: 1px solid var(--border);
            border-radius: 6px;
            padding: 16px;
            overflow-x: auto;
            color: var(--text);
            font-family: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
            font-size: 0.92rem;
            white-space: pre-wrap;
            word-break: break-word;
        }}
        .footer {{
            margin-top: 30px;
            font-size: 0.85rem;
            color: #8b949e;
            text-align: center;
        }}
        a {{
            color: var(--accent);
            text-decoration: none;
        }}
        a:hover {{
            text-decoration: underline;
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
        @keyframes pulse-ring {{
            0% {{ box-shadow: 0 0 0 0 rgba(63, 185, 80, 0.7); }}
            70% {{ box-shadow: 0 0 0 10px rgba(63, 185, 80, 0); }}
            100% {{ box-shadow: 0 0 0 0 rgba(63, 185, 80, 0); }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🌌 Graviton Live Dashboard <span class="pulse"></span></h1>
            <div>
                <span class="badge">Live Auto-Update (3s)</span>
            </div>
        </header>

        <div class="card">
            <pre id="content" class="markdown-view">{escaped_md}</pre>
        </div>

        <div class="footer">
            Graviton Autonomous Supervisor &middot; Serving on <code>{host}:{port}</code> &middot; Auto-refreshes every 3 seconds
        </div>
    </div>

    <script>
        async function refreshDashboard() {{
            try {{
                const res = await fetch('/dashboard/content');
                if (res.ok) {{
                    const data = await res.json();
                    if (data && data.markdown) {{
                        document.getElementById('content').textContent = data.markdown;
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
