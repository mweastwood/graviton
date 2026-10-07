#!/usr/bin/env python3
"""
Dashboard markdown parsing and section extractors for Graviton.
Zero external dependencies (Python standard library only).
"""

import datetime
import re
from typing import Any, Dict, List, Optional, Tuple

from lib.dashboard.common import (
    _extract_countdown,
    calculate_target_pacing_from_details,
    is_safe_url,
    resolve_target_url,
)
from lib.quota import DEFAULT_GEMINI_MODELS, DEFAULT_THIRD_PARTY_MODELS

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
    "Active Gemini Model": re.compile(r"\|\s*\*\*Active Gemini Model\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Active Third-Party Model": re.compile(r"\|\s*\*\*Active Third-Party Model\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Gemini Remaining": re.compile(r"\|\s*\*\*Gemini Remaining\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third-Party Remaining": re.compile(r"\|\s*\*\*Third-Party Remaining\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third Party Remaining": re.compile(r"\|\s*\*\*Third Party Remaining\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Gemini (5H)": re.compile(r"\|\s*\*\*Gemini \(5H\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Gemini Quota (5H)": re.compile(r"\|\s*\*\*Gemini Quota \(5H\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Gemini (1W)": re.compile(r"\|\s*\*\*Gemini \(1W\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Gemini Quota (1W)": re.compile(r"\|\s*\*\*Gemini Quota \(1W\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third-Party (5H)": re.compile(r"\|\s*\*\*Third-Party \(5H\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third Party (5H)": re.compile(r"\|\s*\*\*Third Party \(5H\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third-Party Quota (5H)": re.compile(r"\|\s*\*\*Third-Party Quota \(5H\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third-Party (1W)": re.compile(r"\|\s*\*\*Third-Party \(1W\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third Party (1W)": re.compile(r"\|\s*\*\*Third Party \(1W\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
    "Third-Party Quota (1W)": re.compile(r"\|\s*\*\*Third-Party Quota \(1W\)\*\*\s*\|\s*`?([^`|\n]+)`?"),
}
SECTION_SPLIT_PATTERN = re.compile(r"(?m)^##\s+")
STATUS_CLEANUP_PATTERN = re.compile(r"[🔄`\s]+")
STATUS_HISTORY_CLEANUP_PATTERN = re.compile(r"[✅❌`\s]+")
MD_LINK_PATTERN = re.compile(r"\[.*?\]\((.*?)\)")


def _parse_target_cell(cell: str, agent: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Parse a target table cell, returning (display_text, resolved_url)."""
    cell_str = cell.strip()
    m = re.search(r"\[(.*?)\]\((.*?)\)", cell_str)
    if m:
        disp = m.group(1).strip("` ")
        url = m.group(2).strip()
        safe_url = url if is_safe_url(url) else None
        return disp, safe_url
    cleaned = cell_str.strip("` ")
    return cleaned, resolve_target_url(cleaned, agent=agent)


def _extract_table_rows(sec_lines: List[str]) -> List[List[str]]:
    """Extract and unescape table rows from a list of markdown lines."""
    table_rows = []
    for line in sec_lines:
        sline = line.strip()
        if sline.startswith("|"):
            raw_cells = re.split(r"(?<!\\)\|", sline)[1:-1]
            cells = [c.strip().replace(r"\|", "|") for c in raw_cells]
            if cells and not all(c.replace(":", "").replace("-", "") == "" for c in cells):
                if cells[0] not in ("Metric", "Task ID", "PR #"):
                    table_rows.append(cells)
    return table_rows


def _parse_pct(s: str) -> Optional[float]:
    """Parse percentage string into float value or None."""
    if not s or s.startswith("N/A"):
        return None
    cleaned = s.replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_header(markdown_content: str, default_host: str = "localhost", default_port: int = 8000) -> Dict[str, Any]:
    """Parse server address, online status badge, and update timestamp from markdown content."""
    host = default_host
    port = default_port
    server_m = SERVER_PATTERN.search(markdown_content)
    if server_m:
        host = server_m.group(1)
        try:
            port = int(server_m.group(2))
        except ValueError:
            pass

    status_text = "ONLINE"
    status_icon = "🟢"
    status_class = "online"
    status_m = STATUS_PATTERN.search(markdown_content)
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

    updated_m = UPDATED_PATTERN.search(markdown_content)
    now_iso = (
        updated_m.group(1).strip()
        if updated_m
        else datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )

    return {
        "host": host,
        "port": port,
        "status_text": status_text,
        "status_icon": status_icon,
        "status_class": status_class,
        "now_iso": now_iso,
    }


def _parse_metrics(markdown_content: str) -> Dict[str, int]:
    """Parse worker counts and pipeline task status metrics."""
    active_workers = 0
    max_workers = 0
    workers_m = WORKERS_PATTERN.search(markdown_content)
    if workers_m:
        active_workers = int(workers_m.group(1))
        max_workers = int(workers_m.group(2))

    def _extract_metric_int(label: str) -> int:
        pat = METRIC_INT_PATTERNS.get(label)
        m = pat.search(markdown_content) if pat else re.search(rf"\|\s*\*\*{re.escape(label)}\*\*\s*\|\s*`?(\d+)`?", markdown_content)
        return int(m.group(1)) if m else 0

    return {
        "active_workers": active_workers,
        "max_workers": max_workers,
        "running_tasks": _extract_metric_int("Running Tasks"),
        "queued_tasks": _extract_metric_int("Queued Tasks"),
        "completed_tasks": _extract_metric_int("Completed Tasks"),
        "failed_tasks": _extract_metric_int("Failed Tasks"),
    }


def _parse_quota(markdown_content: str) -> Dict[str, Any]:
    """Parse model persona, quota window capacities, countdowns, and pacing thresholds."""
    def _extract_metric_str(label_or_patterns, default: str = "default") -> str:
        patterns = [label_or_patterns] if isinstance(label_or_patterns, str) else label_or_patterns
        for p in patterns:
            pat = METRIC_STR_PATTERNS.get(p)
            m = pat.search(markdown_content) if pat else re.search(rf"\|\s*\*\*{re.escape(p)}\*\*\s*\|\s*`?([^`|\n]+)`?", markdown_content)
            if m:
                return m.group(1).strip()
        return default

    pool = _extract_metric_str("Active Pool", "default")
    model = _extract_metric_str("Active Model", "default")
    gemini_rem = _extract_metric_str("Gemini Remaining", "N/A")
    tp_rem = _extract_metric_str(["Third-Party Remaining", "Third Party Remaining"], "N/A")

    gemini_5h_rem = _extract_metric_str(["Gemini (5H)", "Gemini Quota (5H)"], gemini_rem)
    gemini_1w_rem = _extract_metric_str(["Gemini (1W)", "Gemini Quota (1W)"], gemini_rem)
    tp_5h_rem = _extract_metric_str(["Third-Party (5H)", "Third Party (5H)", "Third-Party Quota (5H)"], tp_rem)
    tp_1w_rem = _extract_metric_str(["Third-Party (1W)", "Third Party (1W)", "Third-Party Quota (1W)"], tp_rem)

    def _extract_metric_details(label_or_patterns, default: str = "") -> str:
        patterns = [label_or_patterns] if isinstance(label_or_patterns, str) else label_or_patterns
        for p in patterns:
            m = re.search(rf"\|\s*\*\*{re.escape(p)}\*\*\s*\|\s*`?[^`|\n]+`?\s*\|\s*(.*?)\s*\|(?:\s*$)", markdown_content, re.MULTILINE)
            if not m:
                m = re.search(rf"\|\s*\*\*{re.escape(p)}\*\*\s*\|\s*`?[^`|\n]+`?\s*\|\s*([^|\n]+)\|", markdown_content)
            if m:
                return m.group(1).strip()
        return default

    gemini_5h_details = _extract_metric_details(["Gemini (5H)", "Gemini Quota (5H)"], "Live Gemini burst quota")
    gemini_1w_details = _extract_metric_details(["Gemini (1W)", "Gemini Quota (1W)"], "Live Gemini weekly quota")
    tp_5h_details = _extract_metric_details(["Third-Party (5H)", "Third Party (5H)", "Third-Party Quota (5H)"], "Fallback burst quota")
    tp_1w_details = _extract_metric_details(["Third-Party (1W)", "Third Party (1W)", "Third-Party Quota (1W)"], "Fallback weekly quota")

    gemini_5h_countdown = _extract_countdown(gemini_5h_details)
    gemini_1w_countdown = _extract_countdown(gemini_1w_details)
    tp_5h_countdown = _extract_countdown(tp_5h_details)
    tp_1w_countdown = _extract_countdown(tp_1w_details)

    gemini_pct = _parse_pct(gemini_rem)
    tp_pct = _parse_pct(tp_rem)
    gemini_5h_pct = _parse_pct(gemini_5h_rem)
    gemini_1w_pct = _parse_pct(gemini_1w_rem)
    tp_5h_pct = _parse_pct(tp_5h_rem)
    tp_1w_pct = _parse_pct(tp_1w_rem)

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

    gemini_5h_target_pacing_pct = calculate_target_pacing_from_details(gemini_5h_details, 18000.0)
    gemini_1w_target_pacing_pct = calculate_target_pacing_from_details(gemini_1w_details, 604800.0)
    tp_5h_target_pacing_pct = calculate_target_pacing_from_details(tp_5h_details, 18000.0)
    tp_1w_target_pacing_pct = calculate_target_pacing_from_details(tp_1w_details, 604800.0)

    return {
        "pool": pool,
        "model": model,
        "gemini_rem": gemini_rem,
        "gemini_pct": gemini_pct,
        "gemini_5h_rem": gemini_5h_rem,
        "gemini_5h_pct": gemini_5h_pct,
        "gemini_5h_details": gemini_5h_details,
        "gemini_5h_countdown": gemini_5h_countdown,
        "gemini_5h_target_pacing_pct": gemini_5h_target_pacing_pct,
        "gemini_1w_rem": gemini_1w_rem,
        "gemini_1w_pct": gemini_1w_pct,
        "gemini_1w_details": gemini_1w_details,
        "gemini_1w_countdown": gemini_1w_countdown,
        "gemini_1w_target_pacing_pct": gemini_1w_target_pacing_pct,
        "tp_rem": tp_rem,
        "tp_pct": tp_pct,
        "tp_5h_rem": tp_5h_rem,
        "tp_5h_pct": tp_5h_pct,
        "tp_5h_details": tp_5h_details,
        "tp_5h_countdown": tp_5h_countdown,
        "tp_5h_target_pacing_pct": tp_5h_target_pacing_pct,
        "tp_1w_rem": tp_1w_rem,
        "tp_1w_pct": tp_1w_pct,
        "tp_1w_details": tp_1w_details,
        "tp_1w_countdown": tp_1w_countdown,
        "tp_1w_target_pacing_pct": tp_1w_target_pacing_pct,
        "third_party_5h_rem": tp_5h_rem,
        "third_party_5h_pct": tp_5h_pct,
        "third_party_5h_details": tp_5h_details,
        "third_party_5h_countdown": tp_5h_countdown,
        "third_party_5h_target_pacing_pct": tp_5h_target_pacing_pct,
        "third_party_1w_rem": tp_1w_rem,
        "third_party_1w_pct": tp_1w_pct,
        "third_party_1w_details": tp_1w_details,
        "third_party_1w_countdown": tp_1w_countdown,
        "third_party_1w_target_pacing_pct": tp_1w_target_pacing_pct,
        "available_gemini_models": available_gemini_models,
        "available_third_party_models": available_third_party_models,
        "active_gemini_model": active_gemini_model,
        "active_third_party_model": active_third_party_model,
    }


def _parse_active(sec_str: str) -> List[Dict[str, Any]]:
    """Parse active container tasks from the active section string."""
    sec_lines = sec_str.strip().splitlines()
    table_rows = _extract_table_rows(sec_lines)
    active_tasks: List[Dict[str, Any]] = []
    has_model_hdr = any("Model" in line for line in sec_lines if line.strip().startswith("|") and "Task ID" in line)

    for cells in table_rows:
        if len(cells) >= 7:
            tid = cells[0].strip("`")
            agent = cells[1].strip("`")
            model = cells[2].strip("`")
            target, target_url = _parse_target_cell(cells[3], agent=agent)
            elapsed = cells[4]
            status = STATUS_CLEANUP_PATTERN.sub(" ", cells[5]).strip()
            rc_cell = cells[6]
            rc_url = None
            url_m = MD_LINK_PATTERN.search(rc_cell)
            if url_m:
                cand = url_m.group(1).strip()
                if is_safe_url(cand):
                    rc_url = cand
            active_tasks.append({
                "id": tid,
                "agent": agent,
                "model": model,
                "target": target,
                "target_url": target_url,
                "elapsed": elapsed,
                "status": status,
                "remote_control_url": rc_url,
            })
        elif len(cells) >= 6:
            tid = cells[0].strip("`")
            agent = cells[1].strip("`")
            is_rc_last = bool(MD_LINK_PATTERN.search(cells[5])) or "Remote Control" in cells[5]
            if has_model_hdr or (not is_rc_last and not any(p in cells[2] for p in ("#", "/", "issues", "pull"))):
                model = cells[2].strip("`")
                target, target_url = _parse_target_cell(cells[3], agent=agent)
                elapsed = cells[4]
                status = STATUS_CLEANUP_PATTERN.sub(" ", cells[5]).strip()
                rc_url = None
            else:
                model = "-"
                target, target_url = _parse_target_cell(cells[2], agent=agent)
                elapsed = cells[3]
                status = STATUS_CLEANUP_PATTERN.sub(" ", cells[4]).strip()
                rc_cell = cells[5]
                rc_url = None
                url_m = MD_LINK_PATTERN.search(rc_cell)
                if url_m:
                    cand = url_m.group(1).strip()
                    if is_safe_url(cand):
                        rc_url = cand
            active_tasks.append({
                "id": tid,
                "agent": agent,
                "model": model,
                "target": target,
                "target_url": target_url,
                "elapsed": elapsed,
                "status": status,
                "remote_control_url": rc_url,
            })
        elif len(cells) >= 5:
            tid = cells[0].strip("`")
            agent = cells[1].strip("`")
            target, target_url = _parse_target_cell(cells[2], agent=agent)
            elapsed = cells[3]
            status = STATUS_CLEANUP_PATTERN.sub(" ", cells[4]).strip()
            active_tasks.append({
                "id": tid,
                "agent": agent,
                "model": "-",
                "target": target,
                "target_url": target_url,
                "elapsed": elapsed,
                "status": status,
                "remote_control_url": None,
            })
    return active_tasks


def _parse_queued(sec_str: str) -> List[Dict[str, Any]]:
    """Parse queued tasks from the queued section string."""
    sec_lines = sec_str.strip().splitlines()
    table_rows = _extract_table_rows(sec_lines)
    queued_tasks: List[Dict[str, Any]] = []

    for cells in table_rows:
        if len(cells) >= 5:
            tid = cells[0].strip("`")
            agent = cells[1].strip("`")
            target, target_url = _parse_target_cell(cells[2], agent=agent)
            prio = cells[3].strip("`")
            wait_time = cells[4]
            queued_tasks.append({
                "id": tid,
                "agent": agent,
                "target": target,
                "target_url": target_url,
                "priority": prio,
                "wait_time": wait_time,
            })
    return queued_tasks


def _parse_prs(sec_str: str) -> List[Dict[str, Any]]:
    """Parse approved pull requests from the PR section string."""
    sec_lines = sec_str.strip().splitlines()
    table_rows = _extract_table_rows(sec_lines)
    approved_prs_list: List[Dict[str, Any]] = []

    for cells in table_rows:
        if len(cells) >= 4 and not cells[0].startswith("(") and "PR #" not in cells[0]:
            pr_num: Optional[int] = None
            pr_url = ""
            m_num = re.search(r"\[.*?#?(\d+).*?\]\((.*?)\)", cells[0])
            if m_num:
                pr_num = int(m_num.group(1))
                cand_url = m_num.group(2).strip()
                if is_safe_url(cand_url):
                    pr_url = cand_url
            else:
                num_digits = re.search(r"\d+", cells[0].strip("`#"))
                pr_num = int(num_digits.group(0)) if num_digits else None
            repo_cell = cells[1].strip("`") if len(cells) > 1 else ""
            title_cell = cells[2] if len(cells) > 2 else ""
            author_cell = cells[3].strip("`@") if len(cells) > 3 else ""
            if len(cells) >= 5 and not pr_url:
                url_m = re.search(r"\[.*?\]\((.*?)\)", cells[4])
                if url_m:
                    cand_url = url_m.group(1).strip()
                    if is_safe_url(cand_url):
                        pr_url = cand_url
                elif is_safe_url(cells[4].strip()):
                    pr_url = cells[4].strip()
            if not pr_url and repo_cell and repo_cell != "-" and pr_num:
                pr_url = f"https://github.com/{repo_cell}/pull/{pr_num}"
            approved_prs_list.append({
                "number": pr_num,
                "repo_full_name": repo_cell,
                "title": title_cell,
                "author": author_cell,
                "url": pr_url,
            })
    return approved_prs_list


def _parse_history(sec_str: str) -> List[Dict[str, Any]]:
    """Parse recent execution history from the history section string."""
    sec_lines = sec_str.strip().splitlines()
    table_rows = _extract_table_rows(sec_lines)
    history_tasks: List[Dict[str, Any]] = []
    has_model_hdr = any("Model" in line for line in sec_lines if line.strip().startswith("|") and "Task ID" in line)

    for cells in table_rows:
        if len(cells) >= 7:
            tid = cells[0].strip("`")
            agent = cells[1].strip("`")
            model = cells[2].strip("`")
            target, target_url = _parse_target_cell(cells[3], agent=agent)
            duration = cells[4]
            status_raw = STATUS_HISTORY_CLEANUP_PATTERN.sub(" ", cells[5]).strip()
            detail_cell = cells[6]
            rc_url = None
            url_m = MD_LINK_PATTERN.search(detail_cell)
            if url_m:
                cand = url_m.group(1).strip()
                if is_safe_url(cand):
                    rc_url = cand
                    detail = "Remote Control"
                else:
                    detail = detail_cell.strip("`").strip()
            else:
                detail = detail_cell.strip("`").strip()
            if detail in ("Remote Control", "Remote Session"):
                detail = "Finished"
            history_tasks.append({
                "id": tid,
                "agent": agent,
                "model": model,
                "target": target,
                "target_url": target_url,
                "duration": duration,
                "status": status_raw,
                "details": detail,
                "remote_control_url": rc_url,
            })
        elif len(cells) >= 6:
            tid = cells[0].strip("`")
            agent = cells[1].strip("`")
            if has_model_hdr:
                model = cells[2].strip("`")
                target, target_url = _parse_target_cell(cells[3], agent=agent)
                duration = cells[4]
                status_raw = STATUS_HISTORY_CLEANUP_PATTERN.sub(" ", cells[5]).strip()
                detail = "Finished"
                rc_url = None
            else:
                model = "-"
                target, target_url = _parse_target_cell(cells[2], agent=agent)
                duration = cells[3]
                status_raw = STATUS_HISTORY_CLEANUP_PATTERN.sub(" ", cells[4]).strip()
                detail_cell = cells[5]
                rc_url = None
                url_m = MD_LINK_PATTERN.search(detail_cell)
                if url_m:
                    cand = url_m.group(1).strip()
                    if is_safe_url(cand):
                        rc_url = cand
                        detail = "Finished"
                    else:
                        detail = detail_cell.strip("`").strip()
                else:
                    detail = detail_cell.strip("`").strip()
                if detail in ("Remote Control", "Remote Session"):
                    detail = "Finished"
            history_tasks.append({
                "id": tid,
                "agent": agent,
                "model": model,
                "target": target,
                "target_url": target_url,
                "duration": duration,
                "status": status_raw,
                "details": detail,
                "remote_control_url": rc_url,
            })
        elif len(cells) >= 5:
            tid = cells[0].strip("`")
            agent = cells[1].strip("`")
            target, target_url = _parse_target_cell(cells[2], agent=agent)
            duration = cells[3]
            status_raw = STATUS_HISTORY_CLEANUP_PATTERN.sub(" ", cells[4]).strip()
            history_tasks.append({
                "id": tid,
                "agent": agent,
                "model": "-",
                "target": target,
                "target_url": target_url,
                "duration": duration,
                "status": status_raw,
                "details": "Finished",
                "remote_control_url": None,
            })
    return history_tasks


def parse_dashboard_markdown(
    markdown_content: str,
    default_host: str = "localhost",
    default_port: int = 8000,
) -> Dict[str, Any]:
    """Parse formatted dashboard markdown into structured dictionary for HTML rendering."""
    header_data = _parse_header(markdown_content, default_host=default_host, default_port=default_port)
    metrics_data = _parse_metrics(markdown_content)
    quota_data = _parse_quota(markdown_content)

    active_tasks: List[Dict[str, Any]] = []
    queued_tasks: List[Dict[str, Any]] = []
    history_tasks: List[Dict[str, Any]] = []
    approved_prs_list: List[Dict[str, Any]] = []

    sections = SECTION_SPLIT_PATTERN.split(markdown_content)
    for sec in sections:
        sec_lines = sec.strip().splitlines()
        if not sec_lines:
            continue
        title_line = sec_lines[0].strip()

        if "Active Container Tasks" in title_line:
            active_tasks = _parse_active(sec)
        elif "Queued Tasks" in title_line:
            queued_tasks = _parse_queued(sec)
        elif "Approved Pull Requests" in title_line or "Ready to Merge" in title_line or "Mergeable" in title_line:
            approved_prs_list = _parse_prs(sec)
        elif "Recent Task Execution History" in title_line:
            history_tasks = _parse_history(sec)

    result = {
        **header_data,
        "commit": "unknown",
        "branch": "unknown",
        "reload_state": "IDLE",
        **metrics_data,
        **quota_data,
        "active_tasks": active_tasks,
        "queued_tasks_list": queued_tasks,
        "history_tasks": history_tasks,
        "approved_prs": approved_prs_list,
    }
    return result
