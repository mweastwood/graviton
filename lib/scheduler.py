"""
Periodic Background Task Scheduler Engine for Graviton.

Manages recurring/periodic agent execution for automated codebase maintenance,
such as bug sweeps, performance audits, readability improvements, and refactoring sweeps.

Uses standard Python library only (threading, time, datetime, json, pathlib).
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from lib.quota import QuotaState

logger = logging.getLogger("graviton.scheduler")


def _atomic_write_json(target_path: Path, data: Any, indent: int = 2):
    """
    Atomically write JSON data to target_path using a temporary file in the target directory.
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(target_path.parent),
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as f:
            tmp_path = Path(f.name)
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        if tmp_path and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        raise


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "schedules.json"
DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / ".graviton_scheduler_state.json"

DEFAULT_JOBS = [
    {
        "job_id": "periodic_bug_sweep",
        "name": "Periodic Automated Bug Sweep",
        "interval_seconds": 86400,
        "agent": "codebase_auditor",
        "prompt": (
            "Perform periodic bug sweep: fetch open issues using `gh issue list --state open --json number,title,body,labels`, "
            "scan the codebase for previously unknown bugs (unhandled exceptions, resource leaks, broken error paths, "
            "type inconsistencies, race conditions). Deduplicate findings against open issues. For any new unknown bug, "
            "file a new issue using `gh issue create --title \"[Bug Sweep] <summary>\" --body \"<details & repro>\\n\\n<!-- antigravity-auto-reply -->\\n<!-- graviton:codebase_auditor -->\" --label \"bug\"`."
        ),
        "enabled": True,
        "last_run": None,
        "next_run": None,
        "repo_cycling": True,
    },
    {
        "job_id": "periodic_quality_sweep",
        "name": "Periodic Performance, Readability & Modularization Sweep",
        "interval_seconds": 86400,
        "agent": "codebase_auditor",
        "prompt": (
            "Perform periodic quality sweep: fetch open issues using `gh issue list --state open --json number,title,body,labels`, "
            "scan the codebase for performance bottlenecks, readability concerns, or modularization issues. "
            "Deduplicate findings against open issues. For any new quality finding, file a new issue using "
            "`gh issue create --title \"[Quality Sweep] <scope>: <recommendation>\" --body \"<rationale & code snippet>\\n\\n<!-- antigravity-auto-reply -->\\n<!-- graviton:codebase_auditor -->\" --label \"enhancement\"`."
        ),
        "enabled": True,
        "last_run": None,
        "next_run": None,
        "repo_cycling": True,
    },
]


def parse_iso_timestamp(ts_str: Optional[str], context: str = "") -> Optional[datetime]:
    """
    Parse an ISO format timestamp string into a UTC-aware datetime object.
    Logs a diagnostic warning if parsing fails due to invalid format or type errors.
    """
    if not ts_str:
        return None
    try:
        if not isinstance(ts_str, str):
            raise TypeError(f"Expected str, got {type(ts_str).__name__}")
        if len(ts_str.strip()) < 4:
            raise ValueError(f"Invalid isoformat string: '{ts_str}'")
        if ts_str.endswith("Z") or ts_str.endswith("z"):
            clean_ts = ts_str[:-1] + "+00:00"
        else:
            clean_ts = ts_str
        dt = datetime.fromisoformat(clean_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError) as e:
        ctx_msg = f" for {context}" if context else ""
        logger.warning(f"Failed to parse ISO timestamp '{ts_str}'{ctx_msg}: {e}")
        return None


class ScheduledJob:
    """
    Data structure representing a periodic scheduled task.
    """

    def __init__(
        self,
        job_id: str,
        name: str,
        interval_seconds: int,
        agent: str,
        prompt: str,
        enabled: bool = True,
        last_run: Optional[str] = None,
        next_run: Optional[str] = None,
        is_running: bool = False,
        current_task_id: Optional[str] = None,
        repo_cycling: bool = False,
        current_repo_index: int = 0,
    ):
        self.job_id = job_id
        self.name = name
        self.interval_seconds = interval_seconds
        self.agent = agent
        self.prompt = prompt
        self.enabled = enabled
        self.last_run = last_run
        self.next_run = next_run
        self.is_running = is_running
        self.current_task_id = current_task_id
        self.repo_cycling = repo_cycling
        self.current_repo_index = current_repo_index

    def is_due(self, now_dt: Optional[datetime] = None) -> bool:
        """
        Check if the job is enabled and due to run.
        """
        if not self.enabled or self.is_running:
            return False

        if now_dt is None:
            now_dt = datetime.now(timezone.utc)

        if self.next_run:
            next_dt = parse_iso_timestamp(self.next_run, context=f"job '{self.job_id}' next_run")
            if next_dt is not None:
                return now_dt >= next_dt

        if self.last_run:
            last_dt = parse_iso_timestamp(self.last_run, context=f"job '{self.job_id}' last_run")
            if last_dt is not None:
                elapsed = (now_dt - last_dt).total_seconds()
                return elapsed >= self.interval_seconds

        # If no last_run or next_run, it's due immediately
        return True

    def mark_executed(self, now_dt: Optional[datetime] = None):
        """
        Update last_run and next_run timestamps after job execution.
        """
        if now_dt is None:
            now_dt = datetime.now(timezone.utc)
        self.last_run = now_dt.isoformat()
        next_dt = datetime.fromtimestamp(now_dt.timestamp() + self.interval_seconds, tz=timezone.utc)
        self.next_run = next_dt.isoformat()

    def to_config_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "agent": self.agent,
            "prompt": self.prompt,
            "enabled": self.enabled,
            "repo_cycling": self.repo_cycling,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "interval_seconds": self.interval_seconds,
            "agent": self.agent,
            "prompt": self.prompt,
            "enabled": self.enabled,
            "last_run": self.last_run,
            "next_run": self.next_run,
            "is_running": self.is_running,
            "current_task_id": self.current_task_id,
            "repo_cycling": self.repo_cycling,
            "current_repo_index": self.current_repo_index,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduledJob":
        return cls(
            job_id=data["job_id"],
            name=data.get("name", data["job_id"]),
            interval_seconds=int(data.get("interval_seconds", 86400)),
            agent=data.get("agent", "codebase_auditor"),
            prompt=data.get("prompt", ""),
            enabled=bool(data.get("enabled", True)),
            last_run=data.get("last_run"),
            next_run=data.get("next_run"),
            is_running=bool(data.get("is_running", False)),
            current_task_id=data.get("current_task_id"),
            repo_cycling=bool(data.get("repo_cycling", False)),
            current_repo_index=int(data.get("current_repo_index", 0)),
        )


_issues_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_issues_cache_lock = threading.Lock()


def fetch_open_issues(
    cwd: Optional[Path] = None, ttl: float = 60.0, force: bool = False, timeout: float = 30.0
) -> List[Dict[str, Any]]:
    """
    Execute `gh issue list --state open --json number,title,body,labels` to retrieve open issues.
    Caches results for `ttl` seconds to avoid redundant gh CLI subprocess execution.
    """
    cwd_key = str(Path(cwd).resolve()) if cwd else ""
    now = time.time()

    if not force:
        with _issues_cache_lock:
            if cwd_key in _issues_cache:
                cached_time, cached_data = _issues_cache[cwd_key]
                if (now - cached_time) < ttl:
                    return cached_data

    cmd = ["gh", "issue", "list", "--state", "open", "--json", "number,title,body,labels"]
    try:
        res = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=timeout
        )
        if res.returncode == 0:
            stdout_str = res.stdout.strip()
            data = json.loads(stdout_str) if stdout_str else []
            if isinstance(data, list):
                with _issues_cache_lock:
                    _issues_cache[cwd_key] = (now, data)
                return data
        logger.warning(
            f"gh issue list returned exit code {res.returncode}: {res.stderr.strip() if res.stderr else ''}"
        )
        with _issues_cache_lock:
            if cwd_key in _issues_cache:
                return _issues_cache[cwd_key][1]
        return []
    except Exception as e:
        logger.warning(f"Failed to fetch open issues via gh CLI: {e}")
        with _issues_cache_lock:
            if cwd_key in _issues_cache:
                return _issues_cache[cwd_key][1]
        return []


def _normalize_title(title: str) -> str:
    """Normalize title by stripping bracketed prefix tags, lowercasing, and stripping whitespace."""
    if not title or not isinstance(title, str):
        return ""
    cleaned = re.sub(r"^(?:\s*\[[^\]]+\]\s*)+", "", title.strip().lower())
    return cleaned.strip()


def tokenize_title(title: str) -> Set[str]:
    """Extract token set from issue title after normalizing bracketed tags."""
    if not title or not isinstance(title, str):
        return set()
    clean = _normalize_title(title)
    if not clean:
        return set()
    return set(re.findall(r"\w+", clean))


def pretokenize_issues(existing_issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Pre-tokenize a list of existing issues in place by attaching pre-computed token sets
    and normalized title strings to reduce redundant computations during duplicate detection.
    """
    if not existing_issues or not isinstance(existing_issues, list):
        return existing_issues if isinstance(existing_issues, list) else []

    for issue in existing_issues:
        if not isinstance(issue, dict):
            continue
        ex_raw = issue.get("title", "")
        if not isinstance(ex_raw, str) or not ex_raw:
            continue
        if "_norm_title" not in issue or not isinstance(issue["_norm_title"], str):
            issue["_norm_title"] = ex_raw.strip().lower()
        if "_clean_title" not in issue or not isinstance(issue["_clean_title"], str):
            issue["_clean_title"] = _normalize_title(ex_raw)

        tokens_val = issue.get("_tokens")
        if tokens_val is None:
            tokens_val = issue.get("tokens")

        if isinstance(tokens_val, set):
            issue["_tokens"] = tokens_val
        elif isinstance(tokens_val, (list, tuple)):
            try:
                issue["_tokens"] = set(tokens_val)
            except (TypeError, ValueError):
                issue["_tokens"] = tokenize_title(ex_raw)
        else:
            issue["_tokens"] = tokenize_title(ex_raw)

    return existing_issues


def is_duplicate_issue(
    proposed_title: str, existing_issues: List[Dict[str, Any]], similarity_threshold: float = 0.7
) -> bool:
    """
    Check if proposed title overlaps significantly with an existing issue title
    using normalized exact matching and token set similarity to prevent false positives
    from short issue titles.
    """
    if not proposed_title or not isinstance(proposed_title, str):
        return False

    p_norm = proposed_title.strip().lower()
    if not p_norm:
        return False

    if not existing_issues or not isinstance(existing_issues, list):
        return False

    p_clean = _normalize_title(proposed_title)
    p_tokens = tokenize_title(proposed_title)

    for issue in existing_issues:
        if not isinstance(issue, dict):
            continue
        ex_raw = issue.get("title", "")
        if not isinstance(ex_raw, str) or not ex_raw:
            continue

        ex_norm = issue.get("_norm_title")
        if not isinstance(ex_norm, str):
            ex_norm = ex_raw.strip().lower()

        ex_clean = issue.get("_clean_title")
        if not isinstance(ex_clean, str):
            ex_clean = _normalize_title(ex_raw)

        # Exact match (raw or normalized without bracket tags)
        if p_norm == ex_norm or (p_clean and p_clean == ex_clean):
            return True

        # Token set similarity (Jaccard similarity)
        if p_tokens:
            ex_tokens = issue.get("_tokens")
            if ex_tokens is None:
                ex_tokens = issue.get("tokens")

            if isinstance(ex_tokens, set):
                pass
            elif isinstance(ex_tokens, (list, tuple)):
                try:
                    ex_tokens = set(ex_tokens)
                except (TypeError, ValueError):
                    ex_tokens = tokenize_title(ex_raw)
            else:
                ex_tokens = tokenize_title(ex_raw)

            if ex_tokens:
                intersection = p_tokens & ex_tokens
                union = p_tokens | ex_tokens
                jaccard = len(intersection) / len(union)
                if jaccard >= similarity_threshold:
                    return True

    return False


class TaskScheduler:
    """
    Periodic task scheduler manager running in a background daemon thread.
    """
    _all_save_threads: Set[threading.Thread] = set()
    _all_save_threads_lock: threading.Lock = threading.Lock()

    @classmethod
    def wait_all_saves(cls, timeout: float = 5.0) -> None:
        """Wait for all active background save operations across all scheduler instances to complete."""
        with cls._all_save_threads_lock:
            threads = list(cls._all_save_threads)
        for t in threads:
            t.join(timeout=timeout)

    def __init__(
        self,
        config_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
        runner: Optional[Callable] = None,
        script_path: Optional[Path] = None,
        cwd: Optional[Path] = None,
        check_interval_seconds: float = 5.0,
        task_manager: Optional[Any] = None,
        quota_tracker: Optional[Any] = None,
        repos_dir: Optional[Path] = None,
    ):
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._state_version: int = 0
        self._latest_written_state_version: int = 0
        self._config_version: int = 0
        self._latest_written_config_version: int = 0
        self._active_save_threads: Set[threading.Thread] = set()

        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.state_path = state_path or DEFAULT_STATE_PATH
        self.runner = runner
        self.script_path = script_path
        self.cwd = cwd
        self.check_interval_seconds = check_interval_seconds
        self.task_manager = task_manager
        self.quota_tracker = quota_tracker
        if repos_dir is not None:
            self.repos_dir = Path(repos_dir)
        elif task_manager is not None and getattr(task_manager, "repos_dir", None) is not None:
            self.repos_dir = Path(task_manager.repos_dir)
        else:
            self.repos_dir = None

        self.jobs: Dict[str, ScheduledJob] = {}
        self.job_handlers: Dict[str, Callable] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.load_config()
        self.load_state()

    def wait_for_saves(self, timeout: float = 5.0) -> None:
        """Wait for all active background save operations for this scheduler instance to complete."""
        with self._lock:
            threads = list(self._active_save_threads)
        for t in threads:
            t.join(timeout=timeout)

    def register_handler(self, key: str, handler: Callable):
        """Register a custom handler function for a specific job_id or agent."""
        with self._lock:
            self.job_handlers[key] = handler

    def update_running_states(self):
        """
        Check tasks in task_manager and update job.is_running / job.current_task_id status.
        Only updates jobs that are managed by TaskManager.
        """
        if not self.task_manager:
            return

        state_changed = False
        with self._lock:
            for job in list(self.jobs.values()):
                # Skip custom handler jobs as their is_running state is managed within _execute_job
                if job.job_id in self.job_handlers or job.agent in self.job_handlers:
                    continue

                active_task = None
                for task in self.task_manager.get_all_tasks():
                    if task.status in ("QUEUED", "RUNNING", "PAUSED_FOR_QUOTA"):
                        t_id = getattr(task, "target_id", None) or ""
                        is_match = (
                            t_id == f"sched:{job.job_id}"
                            or t_id.endswith(f"#sched:{job.job_id}")
                            or t_id.endswith(f":sched:{job.job_id}")
                            or t_id.endswith(f"#{job.job_id}")
                            or (job.current_task_id and task.id == job.current_task_id)
                        )
                        if is_match:
                            active_task = task
                            break

                if active_task:
                    if not job.is_running or job.current_task_id != active_task.id:
                        job.is_running = True
                        job.current_task_id = active_task.id
                        state_changed = True
                elif job.current_task_id:
                    task = self.task_manager.get_task(job.current_task_id)
                    if task and task.status in ("QUEUED", "RUNNING", "PAUSED_FOR_QUOTA"):
                        if not job.is_running:
                            job.is_running = True
                            state_changed = True
                    else:
                        if job.is_running or job.current_task_id is not None:
                            job.is_running = False
                            job.current_task_id = None
                            state_changed = True
                elif job.is_running:
                    job.is_running = False
                    state_changed = True

        if state_changed:
            self.save_state(async_save=True)

    def load_config(self):
        """
        Load schedule job definitions from config_path or fallback to default jobs.
        """
        with self._lock:
            if self.config_path.exists():
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        raw_jobs = json.load(f)
                    self.jobs = {item["job_id"]: ScheduledJob.from_dict(item) for item in raw_jobs}
                    logger.info(f"Loaded {len(self.jobs)} scheduled job(s) from {self.config_path}")
                    return
                except Exception as e:
                    logger.error(f"Failed to load schedule config from {self.config_path}: {e}")

            # Fallback to default jobs and create file
            self.jobs = {item["job_id"]: ScheduledJob.from_dict(item) for item in DEFAULT_JOBS}
        self.save_config()

    def load_state(self):
        """
        Load runtime execution state (last_run, next_run, enabled) from state_path if present.
        Resets transient running states (is_running = False, current_task_id = None) on startup
        to recover from process crashes mid-execution, unless reconciled by an active task manager.
        State snapshot and in-memory updates are modified under lock context, while
        update_running_states and disk writes (save_state) execute outside the lock context.
        """
        need_save_fallback = False
        loaded_successfully = False

        if self.state_path.exists():
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    state_data = json.load(f)

                with self._lock:
                    # Reset transient runtime execution flags for all jobs on load to recover from process crashes
                    for job in self.jobs.values():
                        job.is_running = False
                        job.current_task_id = None

                    if isinstance(state_data, dict):
                        for job_id, s_info in state_data.items():
                            if job_id in self.jobs and isinstance(s_info, dict):
                                if "last_run" in s_info:
                                    self.jobs[job_id].last_run = s_info["last_run"]
                                if "next_run" in s_info:
                                    self.jobs[job_id].next_run = s_info["next_run"]
                                if "enabled" in s_info:
                                    self.jobs[job_id].enabled = bool(s_info["enabled"])
                                if "current_repo_index" in s_info:
                                    try:
                                        self.jobs[job_id].current_repo_index = int(s_info["current_repo_index"])
                                    except (ValueError, TypeError):
                                        pass
                        loaded_successfully = True
                    elif isinstance(state_data, list):
                        for item in state_data:
                            if isinstance(item, dict) and "job_id" in item:
                                job_id = item["job_id"]
                                if job_id in self.jobs:
                                    if "last_run" in item:
                                        self.jobs[job_id].last_run = item["last_run"]
                                    if "next_run" in item:
                                        self.jobs[job_id].next_run = item["next_run"]
                                    if "enabled" in item:
                                        self.jobs[job_id].enabled = bool(item["enabled"])
                                    if "current_repo_index" in item:
                                        try:
                                            self.jobs[job_id].current_repo_index = int(item["current_repo_index"])
                                        except (ValueError, TypeError):
                                            pass
                        loaded_successfully = True
                    else:
                        need_save_fallback = True
            except Exception as e:
                logger.error(f"Failed to load schedule state from {self.state_path}: {e}")
                need_save_fallback = True
        else:
            need_save_fallback = True

        if loaded_successfully:
            with self._lock:
                job_count = len(self.jobs)
            logger.info(f"Loaded schedule state for {job_count} job(s) from {self.state_path}")
            if self.task_manager:
                self.update_running_states()
        else:
            with self._lock:
                for job in self.jobs.values():
                    job.is_running = False
                    job.current_task_id = None
            if need_save_fallback:
                # Fallback / Migration: Save initial execution state to state_path
                self.save_state()

    def save_state(self, async_save: bool = False):
        """
        Persist job execution state (last_run, next_run, enabled, is_running, current_task_id, current_repo_index) to state_path.
        Uses atomic file replacement to prevent file corruption during unexpected crashes.
        State snapshot is captured under self._lock context. If async_save is True, file writing
        and fsync execute asynchronously on a background thread to prevent blocking critical scheduler threads.
        """
        with self._lock:
            self._state_version += 1
            version = self._state_version
            data = {
                job_id: {
                    "last_run": job.last_run,
                    "next_run": job.next_run,
                    "enabled": job.enabled,
                    "is_running": job.is_running,
                    "current_task_id": job.current_task_id,
                    "current_repo_index": job.current_repo_index,
                }
                for job_id, job in self.jobs.items()
            }

        def _do_write():
            with self._save_lock:
                if version < self._latest_written_state_version:
                    return
                try:
                    _atomic_write_json(self.state_path, data, indent=2)
                    self._latest_written_state_version = version
                    logger.debug(f"Saved schedule state for {len(data)} job(s) to {self.state_path}")
                except Exception as e:
                    logger.error(f"Failed to save schedule state to {self.state_path}: {e}")

        if async_save:
            def _worker():
                try:
                    _do_write()
                finally:
                    with self._lock:
                        self._active_save_threads.discard(t)
                    with TaskScheduler._all_save_threads_lock:
                        TaskScheduler._all_save_threads.discard(t)

            t = threading.Thread(target=_worker, daemon=True, name="SchedulerStateSave")
            with self._lock:
                self._active_save_threads.add(t)
            with TaskScheduler._all_save_threads_lock:
                TaskScheduler._all_save_threads.add(t)
            t.start()
        else:
            _do_write()

    def save_config(self, async_save: bool = False):
        """
        Persist current scheduled job definitions back to config_path.
        Excludes dynamic runtime state attributes (last_run, next_run, is_running, current_task_id).
        Uses atomic file replacement to prevent file corruption during unexpected crashes.
        State snapshot is captured under self._lock context. If async_save is True, file writing
        and fsync execute asynchronously on a background thread.
        """
        with self._lock:
            self._config_version += 1
            version = self._config_version
            data = [job.to_config_dict() for job in self.jobs.values()]

        def _do_write():
            with self._save_lock:
                if version < self._latest_written_config_version:
                    return
                try:
                    _atomic_write_json(self.config_path, data, indent=2)
                    self._latest_written_config_version = version
                    logger.debug(f"Saved {len(data)} scheduled job(s) to {self.config_path}")
                except Exception as e:
                    logger.error(f"Failed to save schedule config to {self.config_path}: {e}")

        if async_save:
            def _worker():
                try:
                    _do_write()
                finally:
                    with self._lock:
                        self._active_save_threads.discard(t)
                    with TaskScheduler._all_save_threads_lock:
                        TaskScheduler._all_save_threads.discard(t)

            t = threading.Thread(target=_worker, daemon=True, name="SchedulerConfigSave")
            with self._lock:
                self._active_save_threads.add(t)
            with TaskScheduler._all_save_threads_lock:
                TaskScheduler._all_save_threads.add(t)
            t.start()
        else:
            _do_write()

    def add_job(self, job: ScheduledJob):
        """Add or overwrite a scheduled job."""
        with self._lock:
            self.jobs[job.job_id] = job
        self.save_config()
        self.save_state()

    def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job by job_id."""
        with self._lock:
            removed = job_id in self.jobs
            if removed:
                del self.jobs[job_id]
        if removed:
            self.save_config()
            self.save_state()
        return removed

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        """Retrieve a job by job_id."""
        with self._lock:
            return self.jobs.get(job_id)

    def trigger_job(self, job_id: str) -> bool:
        """
        Manually trigger a job immediately.
        """
        with self._lock:
            job = self.jobs.get(job_id)
        if not job:
            logger.warning(f"Cannot trigger unknown job '{job_id}'")
            return False

        logger.info(f"Manually triggering job '{job.job_id}' ({job.name})")
        self._execute_job(job)
        return True

    def _pick_next_repo(self, job: ScheduledJob) -> Optional[Path]:
        """
        If job.repo_cycling is True and self.repos_dir exists, return the next
        repo directory in round-robin order and advance job.current_repo_index.
        Returns None if cycling is disabled or no repos are available.
        """
        if not job.repo_cycling or not self.repos_dir or not self.repos_dir.is_dir():
            return None
        try:
            repos = sorted(
                p for p in self.repos_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except Exception as e:
            logger.warning(f"Failed to iterate repos_dir '{self.repos_dir}': {e}")
            return None
        if not repos:
            return None
        idx = job.current_repo_index % len(repos)
        chosen = repos[idx]
        job.current_repo_index = (idx + 1) % len(repos)
        return chosen

    def _execute_job(self, job: ScheduledJob):
        """
        Hand off job prompt to runner or custom handler and update job state.
        """
        logger.info(f"Executing scheduled job '{job.job_id}' via agent '{job.agent}'")
        now_dt = datetime.now(timezone.utc)

        with self._lock:
            handler = self.job_handlers.get(job.job_id) or self.job_handlers.get(job.agent)
            chosen_repo = self._pick_next_repo(job)

        active_prompt = job.prompt
        repo_name = None
        repo_dir = None
        if chosen_repo:
            repo_name = chosen_repo.name
            repo_dir = chosen_repo
            active_prompt = f"Target repository: {repo_name} (at {repo_dir}).\n{job.prompt}"
        elif self.cwd:
            repo_dir = Path(self.cwd)
            repo_name = repo_dir.name
        elif self.task_manager and getattr(self.task_manager, "cwd", None):
            repo_dir = Path(self.task_manager.cwd)
            repo_name = repo_dir.name

        if handler:
            with self._lock:
                job.is_running = True
            self.save_state()
            try:
                with self._lock:
                    job.mark_executed(now_dt)
                handler(job)
            except Exception as e:
                logger.exception(f"Error executing custom handler for job '{job.job_id}': {e}")
            finally:
                with self._lock:
                    job.is_running = False
                self.save_state()
            return

        if self.task_manager:
            if hasattr(self.task_manager, "can_accept_task") and not self.task_manager.can_accept_task(job.agent, active_prompt):
                logger.warning(f"Task acceptance suspended (quota pacing or manager state). Deferring job execution '{job.job_id}'.")
                with self._lock:
                    job.mark_executed(now_dt)
                self.save_state()
                return

            with self._lock:
                job.is_running = True
            self.save_state()

            try:
                target_id = f"{repo_name}#sched:{job.job_id}" if repo_name else f"sched:{job.job_id}"
                task = self.task_manager.submit_task(
                    agent=job.agent,
                    prompt=active_prompt,
                    target_id=target_id,
                    repo_name=repo_name,
                    repo_dir=repo_dir,
                )
                with self._lock:
                    job.mark_executed(now_dt)
                    job.current_task_id = task.id
                    job.is_running = True
                self.save_state()
                return
            except Exception as e:
                logger.exception(f"Error submitting task for job '{job.job_id}': {e}")
                with self._lock:
                    job.is_running = False
                self.save_state()
                return

        if self.runner:
            qt = getattr(self, "quota_tracker", None)
            if qt:
                is_behind = False
                if hasattr(qt, "is_behind_pacing") and callable(getattr(qt, "is_behind_pacing", None)):
                    res = qt.is_behind_pacing()
                    if res is True:
                        is_behind = True
                is_exhausted = (getattr(qt, "state", None) == QuotaState.EXHAUSTED)
                if is_behind or is_exhausted:
                    logger.warning(f"Task acceptance suspended (quota pacing or manager state). Deferring job execution '{job.job_id}'.")
                    with self._lock:
                        job.mark_executed(now_dt)
                    self.save_state()
                    return

            with self._lock:
                job.is_running = True
            self.save_state()

            try:
                with self._lock:
                    job.mark_executed(now_dt)
                exec_cwd = repo_dir if repo_dir else self.cwd
                if self.script_path and exec_cwd:
                    self.runner(job.agent, active_prompt, self.script_path, exec_cwd)
                else:
                    self.runner(job.agent, active_prompt)
            except Exception as e:
                logger.exception(f"Error executing runner for job '{job.job_id}': {e}")
            finally:
                with self._lock:
                    job.is_running = False
                self.save_state()
            return

        with self._lock:
            job.is_running = False
        self.save_state()

    def start(self):
        """
        Start the background scheduler thread.
        """
        if self._thread and self._thread.is_alive():
            logger.warning("Scheduler is already running.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._thread.start()
        logger.info(f"TaskScheduler started with check interval {self.check_interval_seconds}s.")

    def stop(self, timeout: float = 2.0):
        """
        Stop the background scheduler thread gracefully.
        """
        logger.info("Stopping TaskScheduler...")
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self.wait_for_saves(timeout=timeout)
        logger.info("TaskScheduler stopped.")

    def is_running(self) -> bool:
        """
        Return True if the background scheduler thread is running.
        """
        return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def _scheduler_loop(self):
        """
        Main loop for checking and executing due jobs.
        """
        while not self._stop_event.is_set():
            try:
                self.update_running_states()
                now_dt = datetime.now(timezone.utc)
                with self._lock:
                    jobs_to_check = list(self.jobs.values())
                for job in jobs_to_check:
                    if self._stop_event.is_set():
                        break
                    with self._lock:
                        due = job.is_due(now_dt)
                    if due:
                        self._execute_job(job)
            except Exception as e:
                logger.exception(f"Unexpected error in scheduler loop: {e}")

            # Interruptible sleep
            self._stop_event.wait(timeout=self.check_interval_seconds)

