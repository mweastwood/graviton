"""
Thread-safe Task Queue & Execution Manager for Graviton.
"""

import logging
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from lib.runner import run_agent_container
from lib.quota import QuotaState, QuotaTracker
from lib.security import is_valid_repo_name

logger = logging.getLogger("graviton.tasks")


class TaskStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED_FOR_QUOTA = "PAUSED_FOR_QUOTA"


@dataclass
class Task:
    id: str
    agent: str
    prompt: str
    target_id: Optional[str] = None
    repo_full_name: Optional[str] = None
    repo_name: Optional[str] = None
    clone_url: Optional[str] = None
    repo_dir: Optional[Path] = None
    cached_workspace_dir: Optional[Path] = None
    status: str = TaskStatus.QUEUED
    enqueue_time: float = field(default_factory=time.time)
    start_time: Optional[float] = None
    finish_time: Optional[float] = None
    worker_thread_id: Optional[str] = None
    return_code: Optional[int] = None
    error_message: Optional[str] = None
    attempt: int = 1
    max_attempts: int = 3
    max_total_attempts: int = 6
    attempts_per_batch: int = 3
    requeue_count: int = 0

    @property
    def elapsed_time(self) -> float:
        if self.start_time is None:
            return 0.0
        if self.finish_time is not None:
            return self.finish_time - self.start_time
        return time.time() - self.start_time

    @property
    def wait_time(self) -> float:
        if self.start_time is not None:
            return self.start_time - self.enqueue_time
        return time.time() - self.enqueue_time

    def update_attempt_from_line(self, line: str) -> bool:
        """Parse retry log line and update attempt / max_attempts if present."""
        match = re.search(r"(?i)Auto-continuing conversation \(Attempt\s+(\d+)(?:/(\d+))?\)", line)
        if match:
            self.attempt = int(match.group(1))
            if match.group(2):
                self.max_attempts = int(match.group(2))
            return True
        return False

    def update_attempt_from_output(self, output: str):
        """Parse all lines of output string and update attempt / max_attempts."""
        if not output:
            return
        for line in output.splitlines():
            self.update_attempt_from_line(line)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "prompt": self.prompt,
            "target_id": self.target_id,
            "repo_full_name": self.repo_full_name,
            "repo_name": self.repo_name,
            "clone_url": self.clone_url,
            "repo_dir": str(self.repo_dir) if self.repo_dir else None,
            "cached_workspace_dir": str(self.cached_workspace_dir) if self.cached_workspace_dir else None,
            "status": self.status,
            "enqueue_time": self.enqueue_time,
            "start_time": self.start_time,
            "finish_time": self.finish_time,
            "worker_thread_id": self.worker_thread_id,
            "return_code": self.return_code,
            "elapsed_time": round(self.elapsed_time, 2),
            "wait_time": round(self.wait_time, 2),
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "max_total_attempts": self.max_total_attempts,
            "attempts_per_batch": self.attempts_per_batch,
            "requeue_count": self.requeue_count,
        }


class TaskManager:
    """
    Thread-safe Task Manager managing pending queued tasks, active workers,
    and task execution history with QuotaTracker back-off support and multi-repo support.
    """

    def __init__(
        self,
        max_workers: int = 2,
        max_tasks: int = 1000,
        script_path: Optional[Path] = None,
        cwd: Optional[Path] = None,
        quota_tracker: Optional[QuotaTracker] = None,
        repos_dir: Optional[Path] = None,
    ):
        self.max_workers = max_workers
        self.max_tasks = max_tasks
        self.script_path = script_path
        self.cwd = cwd
        self.quota_tracker = quota_tracker
        self.repos_dir = Path(repos_dir).expanduser().resolve() if repos_dir else None

        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._clone_lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}
        self._task_counter = 0
        self._workers: List[threading.Thread] = []
        self._running = False
        self._draining = False
        self._paused = False
        self._stopped = False

    @property
    def is_paused(self) -> bool:
        """Return True if TaskManager is currently paused and not accepting new tasks or executing queued tasks."""
        with self._lock:
            return self._paused

    def pause(self):
        """Pause acceptance of new tasks and worker execution of queued tasks."""
        with self._lock:
            self._paused = True
            logger.info("TaskManager paused task acceptance and worker execution.")

    def resume(self):
        """Resume acceptance of new tasks and worker execution of queued tasks."""
        with self._lock:
            self._paused = False
            logger.info("TaskManager resumed task acceptance and worker execution.")

    def toggle_pause(self) -> bool:
        """Toggle pause/resume state of task acceptance and worker execution. Returns new is_paused state."""
        with self._lock:
            self._paused = not self._paused
            if self._paused:
                logger.info("TaskManager paused task acceptance and worker execution.")
            else:
                logger.info("TaskManager resumed task acceptance and worker execution.")
            return self._paused

    @property
    def is_draining(self) -> bool:
        """Return True if TaskManager is currently draining tasks."""
        with self._lock:
            return self._draining

    def _can_accept_task_locked(self, agent: Optional[str] = None, prompt: Optional[str] = None) -> bool:
        if self._paused or self._stopped or self._draining:
            return False
        if self.quota_tracker is not None:
            if hasattr(self.quota_tracker, "state") and self.quota_tracker.state == QuotaState.EXHAUSTED:
                return False
        return True

    def can_accept_task(self, agent: Optional[str] = None, prompt: Optional[str] = None) -> bool:
        """
        Return False if TaskManager is paused, draining, or stopped,
        or if quota_tracker is present and quota_tracker.state == QuotaState.EXHAUSTED.
        Tasks can still be accepted and queued when behind quota pacing.
        Otherwise return True.
        """
        with self._lock:
            return self._can_accept_task_locked(agent=agent, prompt=prompt)

    def start(self):
        """Start worker daemon threads."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._stopped = False
            self._workers = []
            for i in range(self.max_workers):
                worker_id = f"Worker-{i+1}"
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(worker_id,),
                    daemon=True,
                    name=worker_id,
                )
                t.start()
                self._workers.append(t)
            logger.info(f"TaskManager started with {self.max_workers} worker threads.")

    def stop(self, wait: bool = True):
        """Stop worker threads cleanly."""
        with self._lock:
            was_running = self._running
            self._running = False
            self._stopped = True
            if not was_running:
                return

        # Signal workers to unblock queue.get()
        for _ in self._workers:
            self._queue.put(None)

        if wait:
            for worker in self._workers:
                worker.join(timeout=1.0)
        self._workers.clear()
        logger.info("TaskManager stopped.")

    def drain_active_tasks(self, timeout: Optional[float] = None) -> bool:
        """
        Pause worker execution of queued tasks and wait for currently running tasks to complete.

        :param timeout: Maximum seconds to wait for active tasks to complete. If None, waits indefinitely.
        :return: True if all active running tasks completed cleanly, False if timed out.
        """
        with self._lock:
            self._draining = True

        logger.info("TaskManager entering drain mode. Pausing new active task execution...")
        start_time = time.time()
        while timeout is None or (time.time() - start_time < timeout):
            if not self.get_active_tasks():
                queued_count = len(self.get_queued_tasks())
                logger.info(
                    f"TaskManager drain completed cleanly. No active tasks remain ({queued_count} queued task(s) preserved)."
                )
                return True
            time.sleep(0.1)

        remaining_active = len(self.get_active_tasks())
        remaining_queued = len(self.get_queued_tasks())
        if remaining_active > 0:
            logger.warning(
                f"TaskManager drain timed out after {timeout}s with "
                f"{remaining_active} running and {remaining_queued} queued task(s) remaining."
            )
            return False
        return True

    def _get_default_state_path(self, filepath: Optional[Path] = None) -> Path:
        if filepath is not None:
            return Path(filepath)
        if self.cwd:
            return self.cwd / ".graviton_queue_state.json"
        return Path(".graviton_queue_state.json")

    def dump_queue_state(self, filepath: Optional[Path] = None) -> int:
        """
        Serialize pending queued and quota-paused tasks and task counter to disk JSON file.

        :param filepath: Optional Path override for destination state file.
        :return: Number of queued and quota-paused tasks serialized.
        """
        path = self._get_default_state_path(filepath)
        with self._lock:
            queued_tasks = [
                t
                for t in self._tasks.values()
                if t.status in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA)
            ]
            if not queued_tasks:
                if path.exists():
                    try:
                        path.unlink()
                    except Exception as e:
                        logger.warning(f"Could not remove stale state file '{path}': {e}")
                return 0

            state = {
                "task_counter": self._task_counter,
                "queued_tasks": [t.to_dict() for t in queued_tasks],
            }

        try:
            import json
            path.write_text(json.dumps(state, indent=2), encoding="utf-8")
            logger.info(f"Dumped {len(queued_tasks)} queued/quota-paused task(s) state to {path}.")
            return len(queued_tasks)
        except Exception as e:
            logger.exception(f"Failed to dump queue state to '{path}': {e}")
            return 0

    def restore_queue_state(self, filepath: Optional[Path] = None) -> int:
        """
        Restore pending queued and quota-paused tasks and task counter from disk JSON file.

        :param filepath: Optional Path override for source state file.
        :return: Number of queued and quota-paused tasks restored.
        """
        path = self._get_default_state_path(filepath)
        if not path.exists():
            return 0

        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
            try:
                path.unlink()
            except Exception as e:
                logger.warning(f"Could not remove state file '{path}' after reading: {e}")
        except Exception as e:
            logger.exception(f"Failed to read queue state file '{path}': {e}")
            return 0

        if not isinstance(data, dict):
            logger.warning(f"Invalid queue state format in '{path}': expected JSON object.")
            return 0

        queued_data = data.get("queued_tasks", [])
        if not isinstance(queued_data, list):
            logger.warning(f"Invalid queued_tasks format in '{path}': expected list.")
            return 0

        saved_counter = data.get("task_counter", 0)

        with self._lock:
            if isinstance(saved_counter, int):
                self._task_counter = max(self._task_counter, saved_counter)
            restored_count = 0
            for td in queued_data:
                if not isinstance(td, dict):
                    logger.warning(f"Skipping non-dict item in queued_tasks state: {td}")
                    continue

                try:
                    task_id = td.get("id")
                    agent = td.get("agent")
                    prompt = td.get("prompt")
                    if not task_id or not agent or prompt is None:
                        logger.warning(
                            f"Skipping queue state item missing required fields ('id', 'agent', 'prompt'): {td}"
                        )
                        continue

                    if isinstance(task_id, str) and task_id.startswith("task-"):
                        try:
                            num = int(task_id.split("-")[1])
                            self._task_counter = max(self._task_counter, num)
                        except (IndexError, ValueError):
                            pass

                    restored_status = td.get("status", TaskStatus.QUEUED)
                    if not isinstance(restored_status, str) or restored_status not in (
                        TaskStatus.QUEUED,
                        TaskStatus.PAUSED_FOR_QUOTA,
                    ):
                        restored_status = TaskStatus.QUEUED
                    repo_dir_val = Path(td["repo_dir"]) if td.get("repo_dir") else None

                    cached_workspace_dir_val = (
                        Path(td["cached_workspace_dir"]) if td.get("cached_workspace_dir") else None
                    )
                    task = Task(
                        id=str(task_id),
                        agent=str(agent),
                        prompt=str(prompt),
                        target_id=str(td["target_id"]) if td.get("target_id") is not None else None,
                        repo_full_name=td.get("repo_full_name"),
                        repo_name=td.get("repo_name"),
                        clone_url=td.get("clone_url"),
                        repo_dir=repo_dir_val,
                        cached_workspace_dir=cached_workspace_dir_val,
                        status=restored_status,
                        enqueue_time=float(td.get("enqueue_time", time.time())),
                        attempt=int(td.get("attempt", 1)),
                        max_attempts=int(td.get("max_attempts", 3)),
                        max_total_attempts=int(td.get("max_total_attempts", 6)),
                        attempts_per_batch=int(td.get("attempts_per_batch", 3)),
                        requeue_count=int(td.get("requeue_count", 0)),
                    )
                    self._tasks[task.id] = task
                    self._queue.put(task)
                    restored_count += 1
                except Exception as e:
                    logger.warning(f"Failed to restore queued task item {td}: {e}")
                    continue

        logger.info(f"Restored {restored_count} queued/quota-paused task(s) state from {path}.")
        return restored_count

    def _prune_tasks_locked(self):
        """
        Evict oldest finished tasks (COMPLETED or FAILED) if total tasks exceed max_tasks limit.
        Must be called while holding self._lock.
        """
        if self.max_tasks <= 0 or len(self._tasks) <= self.max_tasks:
            return

        finished_tasks = [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
        ]
        if not finished_tasks:
            return

        finished_tasks.sort(
            key=lambda t: (
                t.enqueue_time,
                t.finish_time if t.finish_time is not None else t.enqueue_time,
            )
        )

        excess = len(self._tasks) - self.max_tasks
        to_remove = finished_tasks[:excess]
        for task in to_remove:
            del self._tasks[task.id]

    def clear_completed_tasks(self) -> int:
        """
        Remove all COMPLETED and FAILED tasks from memory.
        Returns the number of tasks removed.
        """
        with self._lock:
            to_remove = [
                task_id
                for task_id, t in self._tasks.items()
                if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            ]
            for task_id in to_remove:
                del self._tasks[task_id]
            return len(to_remove)

    def submit_task(
        self,
        agent: str,
        prompt: str,
        target_id: Optional[str] = None,
        max_attempts: Optional[int] = None,
        max_total_attempts: Optional[int] = None,
        attempts_per_batch: Optional[int] = None,
        cached_workspace_dir: Optional[Path] = None,
        repo_full_name: Optional[str] = None,
        repo_name: Optional[str] = None,
        clone_url: Optional[str] = None,
        repo_dir: Optional[Path] = None,
    ) -> Task:
        """Submit a new task to the queue."""
        with self._lock:
            if repo_full_name and target_id:
                if not target_id.startswith(f"{repo_full_name}#"):
                    target_num_str = target_id.split("#")[-1]
                    formatted_target_id = f"{repo_full_name}#{target_num_str}"
                else:
                    formatted_target_id = target_id
            else:
                formatted_target_id = target_id

            if formatted_target_id is not None:
                for existing_task in self._tasks.values():
                    if (
                        existing_task.agent == agent
                        and existing_task.target_id == formatted_target_id
                        and existing_task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED_FOR_QUOTA)
                    ):
                        logger.info(
                            f"Skipping duplicate task submission for agent '{agent}' target '{formatted_target_id}'. "
                            f"Existing task '{existing_task.id}' is {existing_task.status}."
                        )
                        return existing_task

            if not self._can_accept_task_locked(agent=agent, prompt=prompt):
                if self._paused:
                    raise RuntimeError("Cannot accept new task: task acceptance is paused")
                if self._draining:
                    raise RuntimeError("Server is draining tasks for update")
                if self._stopped:
                    raise RuntimeError("Cannot accept new task: task manager is stopped")
                if self.quota_tracker is not None and hasattr(self.quota_tracker, "state") and self.quota_tracker.state == QuotaState.EXHAUSTED:
                    raise RuntimeError("Cannot accept new task: quota is exhausted")
                raise RuntimeError("Cannot accept new task: task admission suspended")

            self._task_counter += 1
            task_id = f"task-{self._task_counter}"
            tot_att = max_total_attempts if max_total_attempts is not None else 6
            batch_att = attempts_per_batch if attempts_per_batch is not None else 3
            initial_max_att = min(max_attempts if max_attempts is not None else batch_att, tot_att)

            task = Task(
                id=task_id,
                agent=agent,
                prompt=prompt,
                target_id=formatted_target_id,
                repo_full_name=repo_full_name,
                repo_name=repo_name,
                clone_url=clone_url,
                repo_dir=Path(repo_dir) if repo_dir else None,
                cached_workspace_dir=Path(cached_workspace_dir) if cached_workspace_dir else None,
                status=TaskStatus.QUEUED,
                enqueue_time=time.time(),
                max_attempts=initial_max_att,
                max_total_attempts=tot_att,
                attempts_per_batch=batch_att,
            )
            self._tasks[task_id] = task
            self._prune_tasks_locked()

        self._queue.put(task)
        logger.info(f"Task '{task_id}' submitted (agent: {agent}, target: {formatted_target_id}).")
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def get_queued_tasks(self) -> List[Task]:
        with self._lock:
            return [
                t for t in self._tasks.values() if t.status in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA)
            ]

    def get_active_tasks(self) -> List[Task]:
        with self._lock:
            return [
                t for t in self._tasks.values() if t.status == TaskStatus.RUNNING
            ]

    def get_task_history(self, limit: int = 20) -> List[Task]:
        with self._lock:
            finished = [
                t
                for t in self._tasks.values()
                if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            ]
            finished.sort(
                key=lambda x: x.finish_time if x.finish_time is not None else 0,
                reverse=True,
            )
            return finished[:limit]

    def get_all_tasks(self) -> List[Task]:
        with self._lock:
            return list(self._tasks.values())

    def get_stats(self) -> dict:
        with self._lock:
            total = len(self._tasks)
            queued = sum(1 for t in self._tasks.values() if t.status == TaskStatus.QUEUED)
            running = sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)
            completed = sum(1 for t in self._tasks.values() if t.status == TaskStatus.COMPLETED)
            failed = sum(1 for t in self._tasks.values() if t.status == TaskStatus.FAILED)
            paused = sum(1 for t in self._tasks.values() if t.status == TaskStatus.PAUSED_FOR_QUOTA)

            quota_state = self.quota_tracker.state if self.quota_tracker else QuotaState.NORMAL
            is_behind = (self.quota_tracker.is_behind_pacing() is True) if self.quota_tracker else False

            if self._paused:
                queue_status = "PAUSED"
                status_str = "PAUSED"
            elif quota_state == QuotaState.EXHAUSTED:
                queue_status = "PAUSED_FOR_QUOTA"
                status_str = "PAUSED_FOR_QUOTA"
            elif is_behind:
                queue_status = "PAUSED_FOR_PACING"
                status_str = "BEHIND_PACING"
            elif quota_state == QuotaState.LOW_QUOTA:
                queue_status = "BACKING_OFF"
                status_str = "RUNNING"
            else:
                queue_status = "ACTIVE"
                status_str = "RUNNING"

            return {
                "total": total,
                "queued": queued,
                "running": running,
                "completed": completed,
                "failed": failed,
                "paused": paused,
                "max_workers": self.max_workers,
                "max_tasks": self.max_tasks,
                "quota_state": quota_state,
                "queue_status": queue_status,
                "status": status_str,
                "is_paused": self._paused,
            }

    def _worker_loop(self, worker_id: str):
        while self._running:
            with self._lock:
                draining = self._draining
                paused = self._paused

            if (draining or paused) and self._running:
                time.sleep(0.1)
                continue

            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if task is None or not self._running:
                self._queue.task_done()
                break

            if self.quota_tracker:
                state = self.quota_tracker.state
                is_behind = hasattr(self.quota_tracker, "is_behind_pacing") and self.quota_tracker.is_behind_pacing() is True
                if state == QuotaState.EXHAUSTED or is_behind:
                    with self._lock:
                        task.status = TaskStatus.PAUSED_FOR_QUOTA
                    self._queue.put(task)
                    self._queue.task_done()
                    time.sleep(0.1)
                    continue
                elif state == QuotaState.LOW_QUOTA:
                    delay = self.quota_tracker.get_backoff_delay(attempt=task.attempt)
                    if delay > 0:
                        logger.info(
                            f"[{worker_id}] LOW_QUOTA ({self.quota_tracker.remaining_percentage:.1f}%). "
                            f"Applying back-off delay of {delay:.2f}s..."
                        )
                        time.sleep(delay)

            # Double-check draining and pause state under lock before transitioning task to RUNNING
            with self._lock:
                is_behind = hasattr(self.quota_tracker, "is_behind_pacing") and self.quota_tracker.is_behind_pacing() is True if self.quota_tracker else False
                is_exhausted = (self.quota_tracker and self.quota_tracker.state == QuotaState.EXHAUSTED)
                if self._draining or self._paused or is_behind or is_exhausted:
                    if is_behind or is_exhausted:
                        task.status = TaskStatus.PAUSED_FOR_QUOTA
                    self._queue.put(task)
                    self._queue.task_done()
                    time.sleep(0.1)
                    continue
                task.status = TaskStatus.RUNNING
                task.start_time = time.time()
                task.worker_thread_id = worker_id

            logger.info(f"[{worker_id}] Executing task '{task.id}' ({task.agent}): '{task.prompt}'")

            try:
                # Resolve target repository checkout directory
                exec_cwd = task.repo_dir
                if not exec_cwd and task.repo_name and self.repos_dir:
                    if not is_valid_repo_name(task.repo_name):
                        logger.warning(f"[{worker_id}] Unsafe or invalid repo_name '{task.repo_name}' attempting path traversal out of {self.repos_dir}")
                        raise RuntimeError(f"Unsafe or invalid repo_name '{task.repo_name}' attempting path traversal out of {self.repos_dir}")
                    candidate_cwd = (self.repos_dir / task.repo_name).resolve()
                    repos_dir_resolved = self.repos_dir.resolve()
                    if candidate_cwd != repos_dir_resolved and repos_dir_resolved in candidate_cwd.parents:
                        exec_cwd = candidate_cwd
                    else:
                        logger.warning(f"[{worker_id}] Unsafe or invalid repo_name '{task.repo_name}' attempting path traversal out of {self.repos_dir}")
                        raise RuntimeError(f"Unsafe or invalid repo_name '{task.repo_name}' attempting path traversal out of {self.repos_dir}")

                if not exec_cwd:
                    exec_cwd = self.cwd

                if exec_cwd and task.clone_url:
                    with self._clone_lock:
                        if not exec_cwd.exists():
                            logger.info(f"[{worker_id}] Repository directory '{exec_cwd}' does not exist. Auto-cloning from {task.clone_url}...")
                            try:
                                import subprocess
                                exec_cwd.parent.mkdir(parents=True, exist_ok=True)
                                subprocess.run(
                                    ["git", "clone", "--", task.clone_url, str(exec_cwd)],
                                    check=True,
                                    capture_output=True,
                                    text=True,
                                )
                                logger.info(f"[{worker_id}] Successfully auto-cloned repository to '{exec_cwd}'.")
                            except Exception as clone_err:
                                logger.error(f"[{worker_id}] Failed to auto-clone repository '{task.clone_url}' into '{exec_cwd}': {clone_err}")
                                raise RuntimeError(f"Failed to auto-clone repository '{task.clone_url}' into '{exec_cwd}': {clone_err}") from clone_err

                if exec_cwd and not exec_cwd.exists():
                    logger.error(f"[{worker_id}] Target repository directory '{exec_cwd}' does not exist.")
                    raise RuntimeError(f"Target repository directory '{exec_cwd}' does not exist.")

                if not task.cached_workspace_dir:
                    task.cached_workspace_dir = Path(f"/tmp/graviton-workspaces/cache/{task.id}")

                initial_att = task.attempt + 1 if task.requeue_count > 0 else 1

                if self.script_path and exec_cwd:
                    res = run_agent_container(
                        task.agent,
                        task.prompt,
                        self.script_path,
                        exec_cwd,
                        on_output=task.update_attempt_from_line,
                        max_attempts=task.max_attempts,
                        cached_workspace_dir=task.cached_workspace_dir,
                        initial_attempt=initial_att,
                    )
                    return_code = res.returncode
                    stderr_output = (res.stderr or "").strip()
                    if res.stdout:
                        task.update_attempt_from_output(res.stdout)
                    if res.stderr:
                        task.update_attempt_from_output(res.stderr)
                else:
                    return_code = 0
                    stderr_output = ""

                with self._lock:
                    if return_code == 0:
                        task.finish_time = time.time()
                        task.return_code = return_code
                        task.status = TaskStatus.COMPLETED
                        logger.info(f"[{worker_id}] Task '{task.id}' COMPLETED successfully.")
                        if task.cached_workspace_dir and task.cached_workspace_dir.exists():
                            import shutil
                            shutil.rmtree(task.cached_workspace_dir, ignore_errors=True)
                    else:
                        if task.attempt >= task.max_attempts:
                            if task.attempt < task.max_total_attempts:
                                old_max = task.max_attempts
                                task.max_attempts = min(task.attempt + task.attempts_per_batch, task.max_total_attempts)
                                task.requeue_count += 1
                                task.status = TaskStatus.QUEUED
                                task.finish_time = None
                                task.return_code = return_code
                                task.error_message = stderr_output or f"Process exited with code {return_code}"
                                self._queue.put(task)
                                logger.info(
                                    f"[{worker_id}] Task '{task.id}' hit {task.attempt}/{old_max} attempts. "
                                    f"Cached workspace and re-queued for attempts {task.attempt + 1}..{task.max_attempts}."
                                )
                            else:
                                task.finish_time = time.time()
                                task.return_code = return_code
                                task.status = TaskStatus.FAILED
                                task.error_message = stderr_output or f"Process exited with code {return_code}"
                                logger.error(
                                    f"[{worker_id}] Task '{task.id}' FAILED after reaching max_total_attempts "
                                    f"({task.attempt}/{task.max_total_attempts})."
                                )
                                if task.cached_workspace_dir and task.cached_workspace_dir.exists():
                                    import shutil
                                    shutil.rmtree(task.cached_workspace_dir, ignore_errors=True)
                        else:
                            task.finish_time = time.time()
                            task.return_code = return_code
                            task.status = TaskStatus.FAILED
                            task.error_message = stderr_output or f"Process exited with code {return_code}"
                            logger.error(f"[{worker_id}] Task '{task.id}' FAILED (exit code {return_code}).")
                            if task.cached_workspace_dir and task.cached_workspace_dir.exists():
                                import shutil
                                shutil.rmtree(task.cached_workspace_dir, ignore_errors=True)
                    self._prune_tasks_locked()
            except Exception as e:
                logger.exception(f"[{worker_id}] Exception executing task '{task.id}': {e}")
                with self._lock:
                    task.finish_time = time.time()
                    task.return_code = -1
                    task.error_message = str(e)
                    task.status = TaskStatus.FAILED
                    self._prune_tasks_locked()
            finally:
                if self.quota_tracker:
                    try:
                        self.quota_tracker.poll_live_quota_async(
                            force=True,
                            thread_name=f"AsyncQuotaPoll-{worker_id}",
                        )
                    except Exception as poll_err:
                        logger.warning(f"[{worker_id}] Quota fetch on task finish failed: {poll_err}")
                self._queue.task_done()

