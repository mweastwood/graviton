"""
Periodic Background Task Scheduler Engine for Graviton.

Manages recurring/periodic agent execution for automated codebase maintenance,
such as bug sweeps, performance audits, readability improvements, and refactoring sweeps.

Uses standard Python library only (threading, time, datetime, json, pathlib).
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("graviton.scheduler")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "schedules.json"

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
            "file a new issue using `gh issue create --title \"[Bug Sweep] <summary>\" --body \"<details & repro>\\n\\n<!-- antigravity-auto-reply -->\" --label \"bug\"`."
        ),
        "enabled": True,
        "last_run": None,
        "next_run": None,
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
            "`gh issue create --title \"[Quality Sweep] <scope>: <recommendation>\" --body \"<rationale & code snippet>\\n\\n<!-- antigravity-auto-reply -->\" --label \"enhancement\"`."
        ),
        "enabled": True,
        "last_run": None,
        "next_run": None,
    },
    {
        "job_id": "periodic_quota_fetch",
        "name": "Periodic Model Quota Fetch",
        "interval_seconds": 600,
        "agent": "quota_fetcher",
        "prompt": "Fetch live Antigravity model quota metrics and update QuotaTracker",
        "enabled": True,
        "last_run": None,
        "next_run": None,
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
        )


def fetch_open_issues(cwd: Optional[Path] = None) -> List[Dict[str, Any]]:
    """
    Execute `gh issue list --state open --json number,title,body,labels` to retrieve open issues.
    """
    cmd = ["gh", "issue", "list", "--state", "open", "--json", "number,title,body,labels"]
    try:
        res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, timeout=30)
        if res.returncode == 0 and res.stdout.strip():
            return json.loads(res.stdout)
        return []
    except Exception as e:
        logger.warning(f"Failed to fetch open issues via gh CLI: {e}")
        return []


def is_duplicate_issue(proposed_title: str, existing_issues: List[Dict[str, Any]]) -> bool:
    """
    Check if proposed title overlaps significantly with an existing issue title.
    """
    p_norm = proposed_title.strip().lower()
    for issue in existing_issues:
        ex_title = issue.get("title", "").strip().lower()
        if p_norm in ex_title or ex_title in p_norm:
            return True
    return False


class TaskScheduler:
    """
    Periodic task scheduler manager running in a background daemon thread.
    """

    def __init__(
        self,
        config_path: Optional[Path] = None,
        runner: Optional[Callable] = None,
        script_path: Optional[Path] = None,
        cwd: Optional[Path] = None,
        check_interval_seconds: float = 5.0,
        task_manager: Optional[Any] = None,
    ):
        self.config_path = config_path or DEFAULT_CONFIG_PATH
        self.runner = runner
        self.script_path = script_path
        self.cwd = cwd
        self.check_interval_seconds = check_interval_seconds
        self.task_manager = task_manager

        self.jobs: Dict[str, ScheduledJob] = {}
        self.job_handlers: Dict[str, Callable] = {}
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.load_config()

    def register_handler(self, key: str, handler: Callable):
        """Register a custom handler function for a specific job_id or agent."""
        self.job_handlers[key] = handler

    def update_running_states(self):
        """
        Check tasks in task_manager and update job.is_running / job.current_task_id status.
        """
        if not self.task_manager:
            return

        for job in self.jobs.values():
            target_id = f"sched:{job.job_id}"
            active_task = None
            for task in self.task_manager.get_all_tasks():
                if task.target_id == target_id and task.status in ("QUEUED", "RUNNING", "PAUSED_FOR_QUOTA"):
                    active_task = task
                    break

            if active_task:
                job.is_running = True
                job.current_task_id = active_task.id
            else:
                if job.current_task_id:
                    task = self.task_manager.get_task(job.current_task_id)
                    if task and task.status in ("QUEUED", "RUNNING", "PAUSED_FOR_QUOTA"):
                        job.is_running = True
                    else:
                        job.is_running = False
                        job.current_task_id = None
                elif not job.is_running:
                    job.is_running = False

    def load_config(self):
        """
        Load schedule job definitions from config_path or fallback to default jobs.
        """
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

    def save_config(self):
        """
        Persist current scheduled job states back to config_path.
        """
        try:
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            data = [job.to_dict() for job in self.jobs.values()]
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            logger.debug(f"Saved {len(self.jobs)} scheduled job(s) to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save schedule config to {self.config_path}: {e}")

    def add_job(self, job: ScheduledJob):
        """Add or overwrite a scheduled job."""
        self.jobs[job.job_id] = job
        self.save_config()

    def remove_job(self, job_id: str) -> bool:
        """Remove a scheduled job by job_id."""
        if job_id in self.jobs:
            del self.jobs[job_id]
            self.save_config()
            return True
        return False

    def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        """Retrieve a job by job_id."""
        return self.jobs.get(job_id)

    def trigger_job(self, job_id: str) -> bool:
        """
        Manually trigger a job immediately.
        """
        job = self.jobs.get(job_id)
        if not job:
            logger.warning(f"Cannot trigger unknown job '{job_id}'")
            return False

        logger.info(f"Manually triggering job '{job.job_id}' ({job.name})")
        self._execute_job(job)
        return True

    def _execute_job(self, job: ScheduledJob):
        """
        Hand off job prompt to runner or custom handler and update job state.
        """
        logger.info(f"Executing scheduled job '{job.job_id}' via agent '{job.agent}'")
        now_dt = datetime.now(timezone.utc)
        job.mark_executed(now_dt)
        job.is_running = True
        self.save_config()

        if job.job_id in self.job_handlers:
            try:
                self.job_handlers[job.job_id](job)
            except Exception as e:
                logger.exception(f"Error executing custom handler for job '{job.job_id}': {e}")
            finally:
                job.is_running = False
                self.save_config()
            return

        if job.agent in self.job_handlers:
            try:
                self.job_handlers[job.agent](job)
            except Exception as e:
                logger.exception(f"Error executing custom handler for agent '{job.agent}': {e}")
            finally:
                job.is_running = False
                self.save_config()
            return

        if self.task_manager:
            try:
                target_id = f"sched:{job.job_id}"
                task = self.task_manager.submit_task(
                    agent=job.agent,
                    prompt=job.prompt,
                    target_id=target_id,
                )
                job.current_task_id = task.id
                job.is_running = True
                self.save_config()
                return
            except Exception as e:
                logger.exception(f"Error submitting task for job '{job.job_id}': {e}")
                job.is_running = False
                self.save_config()
                return

        if self.runner:
            try:
                if self.script_path and self.cwd:
                    self.runner(job.agent, job.prompt, self.script_path, self.cwd)
                else:
                    self.runner(job.agent, job.prompt)
            except Exception as e:
                logger.exception(f"Error executing runner for job '{job.job_id}': {e}")
            finally:
                job.is_running = False
                self.save_config()
            return

        job.is_running = False
        self.save_config()

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
                for job in list(self.jobs.values()):
                    if self._stop_event.is_set():
                        break
                    if job.is_due(now_dt):
                        self._execute_job(job)
            except Exception as e:
                logger.exception(f"Unexpected error in scheduler loop: {e}")

            # Interruptible sleep
            self._stop_event.wait(timeout=self.check_interval_seconds)
