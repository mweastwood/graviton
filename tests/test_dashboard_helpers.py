#!/usr/bin/env python3
"""
Unit tests for isolated dashboard section helpers (markdown, parser, html).
"""

import unittest

from lib.dashboard.html import (
    _render_header_html,
    _render_metrics_html,
    _render_quota_card,
)
from lib.dashboard.markdown import (
    _md_header,
    _md_pipeline,
    _md_quota_gemini_rows,
    _md_quota_third_party_rows,
)
from lib.dashboard.parser import (
    _parse_active,
    _parse_header,
    _parse_metrics,
    _parse_queued,
)


class TestDashboardSectionHelpers(unittest.TestCase):
    def test_markdown_helpers_in_isolation(self):
        ctx = {
            "status_badge": "🟢 **ONLINE**",
            "host": "127.0.0.1",
            "port": 9000,
            "web_url": "http://127.0.0.1:9000/dashboard",
            "now_iso": "2026-10-07 12:00:00 UTC",
            "stats": {
                "active_workers": 1,
                "max_workers": 4,
                "active_tasks": 1,
                "queued_tasks": 2,
                "completed_tasks": 5,
                "failed_tasks": 0,
            },
        }

        header_lines = _md_header(ctx)
        self.assertIn("# 🌌 Graviton Live Dashboard", header_lines)
        self.assertTrue(any("🟢 **ONLINE**" in line for line in header_lines))

        pipeline_lines = _md_pipeline(ctx)
        self.assertIn("## 📊 Task Pipeline", pipeline_lines)
        self.assertTrue(any("`1 / 4`" in line for line in pipeline_lines))

        gemini_rows = _md_quota_gemini_rows("gemini", "gemini-3.8-flash", "80%", "Reset: 02:00", "90%", "Reset: 4d", "80%")
        self.assertEqual(len(gemini_rows), 4)
        self.assertIn("gemini-3.8-flash", gemini_rows[0])

        tp_rows = _md_quota_third_party_rows("claude-sonnet", "50%", "Reset: 01:00", "60%", "Reset: 2d", "50%")
        self.assertEqual(len(tp_rows), 4)
        self.assertIn("claude-sonnet", tp_rows[0])

    def test_parser_helpers_in_isolation(self):
        sample_md = """# Dashboard
> **Status**: 🟢 **ONLINE** | **Server**: `localhost:8000`
*Last updated: 2026-10-07 12:00:00 UTC*
| **Active Workers** | `2 / 4` |
| **Running Tasks** | `1` |
| **Queued Tasks** | `0` |
| **Completed Tasks** | `10` |
| **Failed Tasks** | `0` |
"""
        hdr = _parse_header(sample_md, default_host="localhost", default_port=8000)
        self.assertEqual(hdr["status_text"], "ONLINE")
        self.assertEqual(hdr["host"], "localhost")
        self.assertEqual(hdr["port"], 8000)

        metrics = _parse_metrics(sample_md)
        self.assertEqual(metrics["active_workers"], 2)
        self.assertEqual(metrics["max_workers"], 4)
        self.assertEqual(metrics["running_tasks"], 1)
        self.assertEqual(metrics["completed_tasks"], 10)

        active_sec = """## 🚀 Active Container Tasks
| Task ID | Agent | Model | Target | Elapsed | Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `task-1` | `code_reviewer` | `gemini-3.8` | `mweastwood/graviton#10` | 15s | 🔄 `RUNNING` |
"""
        tasks = _parse_active(active_sec)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["id"], "task-1")
        self.assertEqual(tasks[0]["agent"], "code_reviewer")
        self.assertEqual(tasks[0]["model"], "gemini-3.8")

        queued_sec = """## ⏳ Queued Tasks
| Task ID | Agent | Target | Priority | Queued Duration |
| :--- | :--- | :--- | :--- | :--- |
| `task-2` | `pr_drafter` | `mweastwood/graviton#11` | `1` | 5s |
"""
        queued = _parse_queued(queued_sec)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0]["id"], "task-2")
        self.assertEqual(queued[0]["priority"], "1")

    def test_html_helpers_in_isolation(self):
        data = {
            "host": "localhost",
            "port": 8000,
            "now_iso": "2026-10-07 12:00:00 UTC",
            "status_icon": "🟢",
            "status_text": "ONLINE",
            "status_class": "online",
            "commit": "abc1234",
            "branch": "main",
            "active_workers": 2,
            "max_workers": 4,
            "running_tasks": 1,
            "queued_tasks": 0,
            "completed_tasks": 10,
            "failed_tasks": 0,
        }
        header_params = _render_header_html(data, extra_info={"reload_state": "IDLE"})
        self.assertEqual(header_params["effective_host"], "localhost")
        self.assertEqual(header_params["reload_state"], "IDLE")
        self.assertEqual(header_params["reload_state_class"], "idle")

        metrics_params = _render_metrics_html(data)
        self.assertEqual(metrics_params["workers_pct"], 50)
        self.assertEqual(metrics_params["running_tasks"], 1)
        self.assertIn("inline-block", metrics_params["running_pulse_style"])

        disp, color, bar_pct, details, pacing_style, pacing_title = _render_quota_card(
            85.0, "85%", None, "Reset: 02:00 | Pacing: OK", 50.0, "Default burst"
        )
        self.assertEqual(disp, "85%")
        self.assertEqual(color, "#3fb950")
        self.assertEqual(bar_pct, 85)
        self.assertIn("left: 50.0%;", pacing_style)


if __name__ == "__main__":
    unittest.main()
