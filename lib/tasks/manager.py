"""
Thread-safe Task Queue & Execution Manager for Graviton.
"""

import collections
import json
import logging
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Collection, Dict, Iterable, Iterator, List, Optional, Set, Tuple, Union

from lib.runner import run_agent_container
from lib.quota import QuotaState, QuotaTracker, DEFAULT_GEMINI_MODELS, DEFAULT_THIRD_PARTY_MODELS, _atomic_write_json
from lib.security import is_valid_repo_name
from lib.supervisor import ContainerSupervisor, SupervisorResult, SupervisorError, clean_workspace_dir
from lib.reactions import post_emoji_reaction_async
from lib.notifications import post_task_completion_comment, post_task_start_comment

from .models import Task, TaskStatus, AUTO_CONTINUE_PATTERN, PR_URL_PATTERN
from .pool import resolve_task_pool_and_model
from .workspaces import prune_abandoned_workspaces, _PrunedTaskIds
from .persistence import (
    get_default_state_path,
    dump_queue_state as persist_dump_queue_state,
    restore_queue_state as persist_restore_queue_state,
)

logger = logging.getLogger("graviton.tasks")
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _get_effective(name, default):
    mod = sys.modules.get("lib.tasks")
    if mod and hasattr(mod, name):
        val = getattr(mod, name)
        if val is not default or hasattr(val, "mock_calls") or hasattr(val, "_mock_name"):
            return val
    return default


def _get_effective_subproc_run():
    mod = sys.modules.get("lib.tasks")
    if mod and hasattr(mod, "subprocess") and hasattr(mod.subprocess, "run"):
        val = mod.subprocess.run
        if val is not subprocess.run or hasattr(val, "mock_calls") or hasattr(val, "_mock_name"):
            return val
    return subprocess.run

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
        use_supervisor: Optional[bool] = None,
        supervisor_cls: Optional[Any] = None,
        post_completion_comment: bool = False,
        post_start_comment: bool = False,
        idle_timeout: Optional[float] = 300.0,
        max_duration: Optional[float] = 1800.0,
        skills_dir: Optional[Union[str, Path]] = None,
        agents_dir: Optional[Union[str, Path]] = None,
        on_task_init: Optional[Any] = None,
        on_task_result: Optional[Any] = None,
        on_task_thought: Optional[Any] = None,
        on_task_tool_call: Optional[Any] = None,
    ):
        self.max_workers = max_workers
        self.max_tasks = max_tasks
        self.max_pruned_tasks = max(1000, self.max_tasks * 10)
        self.script_path = script_path
        self.cwd = cwd
        self.quota_tracker = quota_tracker
        self.repos_dir = Path(repos_dir).expanduser().resolve() if repos_dir else None
        self.use_supervisor = use_supervisor
        self.supervisor_cls = supervisor_cls
        self.post_completion_comment = post_completion_comment
        self.post_start_comment = post_start_comment
        self.idle_timeout = idle_timeout
        self.max_duration = max_duration
        if skills_dir:
            self.skills_dir = Path(skills_dir).resolve()
        else:
            default_skills = REPO_ROOT / "plugin" / "skills"
            self.skills_dir = default_skills.resolve() if default_skills.is_dir() else None
        if agents_dir:
            self.agents_dir = Path(agents_dir).resolve()
        else:
            default_agents = REPO_ROOT / "plugin" / "agents"
            self.agents_dir = default_agents.resolve() if default_agents.is_dir() else None
        self.on_task_init = on_task_init
        self.on_task_result = on_task_result
        self.on_task_thought = on_task_thought
        self.on_task_tool_call = on_task_tool_call

        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._task_state_cond = threading.Condition(self._lock)
        self._clone_lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}
        self._pruned_task_ids: _PrunedTaskIds = _PrunedTaskIds(maxlen=self.max_pruned_tasks)
        self._active_processes: Dict[str, subprocess.Popen] = {}
        self._active_supervisors: Dict[str, Any] = {}
        self._task_counter = 0
        self._workers: List[threading.Thread] = []
        self._running = False
        self._draining = False
        self._paused = False
        self._stopped = False

    def _trigger_init_reaction(self, task: Task) -> None:
        """Trigger rocket emoji reaction on task init lifecycle event."""
        if getattr(task, "_init_reaction_triggered", False):
            return
        task._init_reaction_triggered = True
        try:
            if task.webhook_event_type and task.webhook_payload:
                _get_effective("post_emoji_reaction_async", post_emoji_reaction_async)(
                    task.webhook_event_type,
                    task.webhook_payload,
                    reaction="rocket",
                )
            elif task.repo_full_name and task.target_id:
                m = re.search(r"#(\d+)$", task.target_id)
                if m:
                    dummy_payload = {
                        "repository": {"full_name": task.repo_full_name},
                        "issue": {"number": int(m.group(1))},
                    }
                    _get_effective("post_emoji_reaction_async", post_emoji_reaction_async)("issues", dummy_payload, reaction="rocket")
        except Exception as e:
            logger.debug(f"Could not post init reaction for task '{task.id}': {e}")

        try:
            if getattr(self, "post_start_comment", False) and getattr(task, "remote_control_url", None):
                _get_effective("post_task_start_comment", post_task_start_comment)(task)
        except Exception as e:
            logger.debug(f"Could not post init comment for task '{task.id}': {e}")

    def _trigger_completion_comment(self, task: Task, result: Optional[Any] = None) -> None:
        """Trigger completion comment on task result lifecycle event."""
        try:
            _get_effective("post_task_completion_comment", post_task_completion_comment)(task, result)
        except Exception as e:
            logger.debug(f"Could not post completion comment for task '{task.id}': {e}")

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
            self._rebuild_queue_locked()
            logger.info("TaskManager resumed task acceptance and worker execution.")

    def toggle_pause(self) -> bool:
        """Toggle pause/resume state of task acceptance and worker execution. Returns new is_paused state."""
        with self._lock:
            self._paused = not self._paused
            if self._paused:
                logger.info("TaskManager paused task acceptance and worker execution.")
            else:
                self._rebuild_queue_locked()
                logger.info("TaskManager resumed task acceptance and worker execution.")
            return self._paused
    def rebuild_queue(self):
        """Rebuild internal task queue from active task registry."""
        with self._lock:
            self._rebuild_queue_locked()

    @property
    def is_draining(self) -> bool:
        """Return True if TaskManager is currently draining tasks."""
        with self._lock:
            return self._draining

    def _can_accept_task_locked(self, agent: Optional[str] = None, prompt: Optional[str] = None) -> bool:
        if self._paused or self._stopped:
            return False
        if self.quota_tracker is not None:
            if hasattr(self.quota_tracker, "get_pool_state"):
                g_state = self.quota_tracker.get_pool_state("gemini")
                c_state = self.quota_tracker.get_pool_state("claude_gpt")
                if g_state == QuotaState.EXHAUSTED and c_state == QuotaState.EXHAUSTED:
                    return False
            elif hasattr(self.quota_tracker, "state") and self.quota_tracker.state == QuotaState.EXHAUSTED:
                return False
        return True

    def can_accept_task(self, agent: Optional[str] = None, prompt: Optional[str] = None) -> bool:
        """
        Return False if TaskManager is paused or stopped,
        or if quota_tracker is present and quota_tracker.state == QuotaState.EXHAUSTED.
        Tasks can still be accepted and queued when behind quota pacing or draining.
        Otherwise return True.
        """
        with self._lock:
            return self._can_accept_task_locked(agent=agent, prompt=prompt)

    def start(self):
        """Start worker daemon threads and prune stale workspaces."""
        try:
            _get_effective("prune_abandoned_workspaces", prune_abandoned_workspaces)()
        except Exception as e:
            logger.warning(f"Initial workspace cleanup sweep failed: {e}")

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
            self._task_state_cond.notify_all()
            with self._queue.all_tasks_done:
                self._queue.all_tasks_done.notify_all()
            if not was_running:
                return

        # Signal workers to unblock queue.get()
        for _ in self._workers:
            self._queue.put(None)

        if wait:
            for worker in self._workers:
                worker.join(timeout=1.0)
        self._workers.clear()
        with self._lock:
            self._task_state_cond.notify_all()
            with self._queue.all_tasks_done:
                self._queue.all_tasks_done.notify_all()
        logger.info("TaskManager stopped.")

    def drain_active_tasks(self, timeout: Optional[float] = None) -> bool:
        """
        Pause worker execution of queued tasks and wait for currently running tasks to complete.

        :param timeout: Maximum seconds to wait for active tasks to complete. If None, waits indefinitely.
        :return: True if all active running tasks completed cleanly, False if timed out.
        """
        logger.info("TaskManager entering drain mode. Pausing new active task execution...")
        start_time = time.time()
        with self._task_state_cond:
            self._draining = True
            while True:
                active_tasks = [t for t in self._tasks.values() if t.status == TaskStatus.RUNNING]
                if not active_tasks:
                    queued_count = sum(
                        1
                        for t in self._tasks.values()
                        if t.status in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA)
                    )
                    logger.info(
                        f"TaskManager drain completed cleanly. No active tasks remain ({queued_count} queued task(s) preserved)."
                    )
                    return True

                if self._stopped:
                    break

                if timeout is not None:
                    remaining = timeout - (time.time() - start_time)
                    if remaining <= 0:
                        break
                    self._task_state_cond.wait(timeout=remaining)
                else:
                    self._task_state_cond.wait()

            remaining_active = len([t for t in self._tasks.values() if t.status == TaskStatus.RUNNING])
            remaining_queued = len(
                [
                    t
                    for t in self._tasks.values()
                    if t.status in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA)
                ]
            )
            logger.warning(
                f"TaskManager drain timed out after {timeout}s with "
                f"{remaining_active} running and {remaining_queued} queued task(s) remaining."
            )
            return False

    def wait_for_task(
        self,
        task_id: Union[str, Task],
        target_statuses: Optional[Collection[str]] = None,
        timeout: Optional[float] = 5.0,
    ) -> bool:
        """
        Wait until the specified task reaches one of the target statuses (or terminal statuses by default).

        :param task_id: The task ID string or Task instance.
        :param target_statuses: Collection of status strings to wait for.
                                Defaults to (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED).
        :param timeout: Maximum seconds to wait. If None, waits indefinitely.
        :return: True if task reached one of the target statuses within timeout, False otherwise.
        """
        tid = task_id.id if isinstance(task_id, Task) else str(task_id)
        if target_statuses is None:
            targets = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED}
        else:
            targets = set(target_statuses)

        start_time = time.time()
        terminal_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED}
        with self._task_state_cond:
            while True:
                task = self._tasks.get(tid)
                if task is not None:
                    if task.status in targets:
                        return True
                    if task.status in terminal_statuses:
                        return False
                elif tid in self._pruned_task_ids:
                    return bool(targets.intersection(terminal_statuses))

                if self._stopped:
                    return False

                if timeout is not None:
                    remaining = timeout - (time.time() - start_time)
                    if remaining <= 0:
                        return False
                    self._task_state_cond.wait(timeout=remaining)
                else:
                    self._task_state_cond.wait()

    def wait_for_all(
        self,
        tasks: Optional[Collection[Union[Task, str]]] = None,
        timeout: Optional[float] = 5.0,
    ) -> bool:
        """
        Wait until all specified tasks (or all known tasks if None) reach a terminal status.

        :param tasks: Collection of tasks or task IDs to wait for. If None, waits for all tasks in self._tasks.
        :param timeout: Maximum seconds to wait. If None, waits indefinitely.
        :return: True if all specified tasks reached terminal status within timeout, False otherwise.
        """
        start_time = time.time()
        terminal_statuses = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED}

        with self._task_state_cond:
            if tasks is None:
                target_ids = set(self._tasks.keys())
            else:
                target_ids = {t.id if isinstance(t, Task) else str(t) for t in tasks}

            while True:
                all_done = True
                for tid in target_ids:
                    task = self._tasks.get(tid)
                    if task is not None:
                        if task.status not in terminal_statuses:
                            all_done = False
                            break
                    elif tid not in self._pruned_task_ids:
                        all_done = False
                        break

                if all_done:
                    return True

                if self._stopped:
                    return False

                if timeout is not None:
                    remaining = timeout - (time.time() - start_time)
                    if remaining <= 0:
                        return False
                    self._task_state_cond.wait(timeout=remaining)
                else:
                    self._task_state_cond.wait()

    def join(self, timeout: Optional[float] = None) -> bool:
        """
        Wait until all tasks submitted to the task manager queue have been processed.

        Wraps queue.Queue.all_tasks_done with optional timeout support.
        :param timeout: Maximum seconds to wait. If None, waits indefinitely.
        :return: True if all queue tasks completed, False if timed out or stopped with unfinished tasks.
        """
        with self._queue.all_tasks_done:
            if timeout is None:
                while self._queue.unfinished_tasks:
                    if self._stopped:
                        return False
                    self._queue.all_tasks_done.wait()
                return not self._stopped
            else:
                endtime = time.time() + timeout
                while self._queue.unfinished_tasks:
                    if self._stopped:
                        return False
                    remaining = endtime - time.time()
                    if remaining <= 0.0:
                        return False
                    self._queue.all_tasks_done.wait(timeout=remaining)
                return not self._stopped


    def _get_default_state_path(self, filepath: Optional[Path] = None) -> Path:
        return get_default_state_path(cwd=self.cwd, filepath=filepath)

    def dump_queue_state(self, filepath: Optional[Path] = None) -> int:
        """
        Serialize pending queued and quota-paused tasks and task counter to disk JSON file.

        :param filepath: Optional Path override for destination state file.
        :return: Number of queued and quota-paused tasks serialized.
        """
        with self._lock:
            return persist_dump_queue_state(
                self._tasks,
                self._task_counter,
                cwd=self.cwd,
                filepath=filepath,
            )

    def restore_queue_state(self, filepath: Optional[Path] = None) -> int:
        """
        Restore pending queued and quota-paused tasks and task counter from disk JSON file.

        :param filepath: Optional Path override for source state file.
        :return: Number of queued and quota-paused tasks restored.
        """
        with self._lock:
            new_counter, restored_count = persist_restore_queue_state(
                self._tasks,
                self._task_counter,
                cwd=self.cwd,
                filepath=filepath,
            )
            self._task_counter = new_counter
            self._rebuild_queue_locked()
            self._prune_tasks_locked()
            self._task_state_cond.notify_all()
        return restored_count
    def _prune_tasks_locked(self):
        """
        Evict oldest finished tasks (COMPLETED, FAILED, or ABORTED) if total tasks exceed max_tasks limit.
        Must be called while holding self._lock.
        """
        if self.max_tasks <= 0 or len(self._tasks) <= self.max_tasks:
            return

        finished_tasks = [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED)
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
            self._pruned_task_ids.add(task.id)
            del self._tasks[task.id]
        if to_remove:
            self._task_state_cond.notify_all()

    def clear_completed_tasks(self) -> int:
        """
        Remove all COMPLETED, FAILED, and ABORTED tasks from memory.
        Returns the number of tasks removed.
        """
        with self._lock:
            to_remove = [
                task_id
                for task_id, t in self._tasks.items()
                if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED)
            ]
            for task_id in to_remove:
                self._pruned_task_ids.add(task_id)
                del self._tasks[task_id]
            if to_remove:
                self._task_state_cond.notify_all()
            return len(to_remove)

    def submit_task(
        self,
        agent: str,
        prompt: str,
        target_id: Optional[str] = None,
        priority: int = 0,
        max_attempts: Optional[int] = None,
        max_total_attempts: Optional[int] = None,
        attempts_per_batch: Optional[int] = None,
        cached_workspace_dir: Optional[Path] = None,
        repo_full_name: Optional[str] = None,
        repo_name: Optional[str] = None,
        clone_url: Optional[str] = None,
        repo_dir: Optional[Path] = None,
        goal_prompt: Optional[str] = None,
        use_goal: Optional[bool] = None,
        webhook_event_type: Optional[str] = None,
        webhook_payload: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
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
                priority=priority,
                enqueue_time=time.time(),
                max_attempts=initial_max_att,
                max_total_attempts=tot_att,
                attempts_per_batch=batch_att,
                goal_prompt=goal_prompt,
                use_goal=True if use_goal is None else bool(use_goal),
                webhook_event_type=webhook_event_type,
                webhook_payload=webhook_payload,
            )
            self._tasks[task_id] = task
            self._prune_tasks_locked()
            self._rebuild_queue_locked()
            self._task_state_cond.notify_all()

        logger.info(f"Task '{task_id}' submitted (agent: {agent}, target: {formatted_target_id}).")
        return task

    def _rebuild_queue_locked(self):
        """
        Rebuild self._queue with pending queued tasks sorted by highest priority first,
        then by enqueue_time. Must be called while holding self._lock.
        Preserves any queued None stop sentinels.
        """
        sentinel_count = 0
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is None:
                    sentinel_count += 1
                self._queue.task_done()
            except queue.Empty:
                break

        queued_tasks = [
            t for t in self._tasks.values()
            if t.status in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA)
        ]
        queued_tasks.sort(key=lambda t: (-t.priority, t.enqueue_time))

        for t in queued_tasks:
            self._queue.put(t)

        for _ in range(sentinel_count):
            self._queue.put(None)

    def prioritize_task(self, task_id: str, priority_bump: int = 1) -> bool:
        """
        Increment priority for specified queued task and re-order pending queued tasks.
        Returns True if task was found and prioritized, False otherwise.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.status not in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA):
                return False
            task.priority += priority_bump
            self._rebuild_queue_locked()
            logger.info(f"Prioritized task '{task_id}': new priority={task.priority}.")
            return True

    def abort_task(self, task_id: str) -> bool:
        """
        Abort an active (RUNNING) or queued (QUEUED / PAUSED_FOR_QUOTA) task.
        Returns True if task was found and aborted, False otherwise.
        """
        proc_to_kill = None
        task_to_cleanup = None

        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False

            if task.status in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA):
                task.status = TaskStatus.ABORTED
                task.finish_time = time.time()
                task.error_message = "Aborted by user"
                if task.cached_workspace_dir:
                    clean_workspace_dir(task.cached_workspace_dir)
                self._rebuild_queue_locked()
                self._prune_tasks_locked()
                self._task_state_cond.notify_all()
                logger.info(f"Queued task '{task_id}' aborted by user.")
                return True

            elif task.status == TaskStatus.RUNNING:
                task.status = TaskStatus.ABORTED
                task.finish_time = time.time()
                task.error_message = "Aborted by user"
                proc_to_kill = self._active_processes.get(task_id)
                sup_to_kill = self._active_supervisors.get(task_id)
                task_to_cleanup = task
                self._task_state_cond.notify_all()
                logger.info(f"Active task '{task_id}' marked ABORTED. Terminating subprocess/supervisor...")

            else:
                return False

        if sup_to_kill is not None:
            if hasattr(sup_to_kill, "abort") and callable(sup_to_kill.abort):
                try:
                    sup_to_kill.abort()
                except Exception as e:
                    logger.warning(f"Error aborting supervisor for task '{task_id}': {e}")
            try:
                sup_to_kill.cleanup()
            except Exception as e:
                logger.warning(f"Error cleaning up supervisor for task '{task_id}': {e}")

        if proc_to_kill is not None:
            try:
                proc_to_kill.terminate()
                try:
                    proc_to_kill.wait(timeout=1.0)
                except Exception:
                    proc_to_kill.kill()
                    try:
                        proc_to_kill.wait(timeout=0.5)
                    except Exception:
                        pass
            except (ProcessLookupError, OSError):
                pass
            except Exception as e:
                logger.warning(f"Error terminating process for task '{task_id}': {e}")

        if task_to_cleanup and task_to_cleanup.cached_workspace_dir:
            clean_workspace_dir(task_to_cleanup.cached_workspace_dir)

        return True

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def get_queued_tasks(self) -> List[Task]:
        with self._lock:
            tasks = [
                t for t in self._tasks.values() if t.status in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA)
            ]
            tasks.sort(key=lambda t: (-t.priority, t.enqueue_time))
            return tasks

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
                if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.ABORTED)
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
            aborted = sum(1 for t in self._tasks.values() if t.status == TaskStatus.ABORTED)

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
                "aborted": aborted,
                "max_workers": self.max_workers,
                "max_tasks": self.max_tasks,
                "quota_state": quota_state,
                "queue_status": queue_status,
                "status": status_str,
                "is_paused": self._paused,
                "active_remote_control_urls": {
                    t.id: t.remote_control_url
                    for t in self._tasks.values()
                    if t.status == TaskStatus.RUNNING and t.remote_control_url
                },
            }

    def _worker_loop(self, worker_id: str):
        while self._running:
            task = None
            was_exhausted = False
            with self._lock:
                draining = self._draining
                paused = self._paused

                if (draining or paused) and self._running:
                    task = None
                    was_exhausted = True
                elif not self._queue.empty():
                    try:
                        item = self._queue.get_nowait()
                        if item is None:
                            self._queue.task_done()
                            break

                        if item.status not in (TaskStatus.QUEUED, TaskStatus.PAUSED_FOR_QUOTA):
                            self._queue.task_done()
                            task = None
                        elif self.quota_tracker and hasattr(self.quota_tracker, "is_quota_ready") and not self.quota_tracker.is_quota_ready():
                            self._queue.put(item)
                            self._queue.task_done()
                            task = None
                            was_exhausted = True
                        else:
                            selected_pool, selected_model, all_exhausted = resolve_task_pool_and_model(self.quota_tracker)
                            if self._draining or self._paused or all_exhausted:
                                if all_exhausted:
                                    if item.status != TaskStatus.PAUSED_FOR_QUOTA:
                                        item.status = TaskStatus.PAUSED_FOR_QUOTA
                                        self._task_state_cond.notify_all()
                                        was_exhausted = False
                                    else:
                                        was_exhausted = True
                                else:
                                    was_exhausted = True
                                self._queue.put(item)
                                self._queue.task_done()
                                task = None
                            else:
                                item.selected_pool = selected_pool
                                item.selected_model = selected_model
                                item.status = TaskStatus.RUNNING
                                item.start_time = time.time()
                                item.worker_thread_id = worker_id
                                self._task_state_cond.notify_all()
                                task = item
                    except queue.Empty:
                        task = None

            if task is None:
                if not self._running:
                    break
                time.sleep(0.25 if was_exhausted else 0.05)
                continue
            if self.quota_tracker and self.quota_tracker.state == QuotaState.LOW_QUOTA:
                delay = self.quota_tracker.get_backoff_delay(attempt=task.attempt)
                if delay > 0:
                    logger.info(
                        f"[{worker_id}] LOW_QUOTA ({self.quota_tracker.remaining_percentage:.1f}%). "
                        f"Applying back-off delay of {delay:.2f}s..."
                    )
                    time.sleep(delay)

            logger.info(
                f"[{worker_id}] Executing task '{task.id}' ({task.agent}) via pool '{task.selected_pool}' with model '{task.selected_model}': '{task.prompt}'"
            )

            try:
                with self._lock:
                    if task.status == TaskStatus.ABORTED:
                        logger.info(f"[{worker_id}] Task '{task.id}' was ABORTED prior to process creation. Skipping execution.")
                        if task.cached_workspace_dir:
                            clean_workspace_dir(task.cached_workspace_dir)
                        self._prune_tasks_locked()
                        self._task_state_cond.notify_all()
                        continue

                # Resolve target repository checkout directory
                exec_cwd = task.repo_dir
                if not exec_cwd and task.repo_name and self.repos_dir:
                    if not _get_effective("is_valid_repo_name", is_valid_repo_name)(task.repo_name):
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
                            with self._lock:
                                if task.status == TaskStatus.ABORTED:
                                    logger.info(f"[{worker_id}] Task '{task.id}' was ABORTED prior to clone. Skipping execution.")
                                    if task.cached_workspace_dir:
                                        clean_workspace_dir(task.cached_workspace_dir)
                                    self._prune_tasks_locked()
                                    self._task_state_cond.notify_all()
                                    continue
                            logger.info(f"[{worker_id}] Repository directory '{exec_cwd}' does not exist. Auto-cloning from {task.clone_url}...")
                            try:
                                exec_cwd.parent.mkdir(parents=True, exist_ok=True)
                                _get_effective_subproc_run()(
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

                with self._lock:
                    if task.status == TaskStatus.ABORTED:
                        logger.info(f"[{worker_id}] Task '{task.id}' was ABORTED prior to process creation. Skipping execution.")
                        if task.cached_workspace_dir:
                            clean_workspace_dir(task.cached_workspace_dir)
                        self._prune_tasks_locked()
                        self._task_state_cond.notify_all()
                        continue

                def _on_process_created(proc):
                    with self._lock:
                        if task.status == TaskStatus.ABORTED:
                            try:
                                proc.terminate()
                                try:
                                    proc.wait(timeout=1.0)
                                except Exception:
                                    proc.kill()
                                    try:
                                        proc.wait(timeout=0.5)
                                    except Exception:
                                        pass
                            except (ProcessLookupError, OSError):
                                pass
                            except Exception as e:
                                logger.warning(f"[{worker_id}] Error terminating process for aborted task '{task.id}': {e}")
                        else:
                            self._active_processes[task.id] = proc

                is_sup = self.use_supervisor if self.use_supervisor is not None else (self.script_path is None)
                if is_sup and exec_cwd:
                    sup_cls = self.supervisor_cls or ContainerSupervisor
                    prompt_to_run = task.goal_prompt if (task.use_goal and task.goal_prompt) else task.prompt

                    def _on_event(event: Dict[str, Any]):
                        event_type = event.get("event")
                        if event_type == "init":
                            cid = event.get("conversation_id")
                            if cid:
                                task.conversation_id = cid
                                logger.info(f"[{worker_id}] Task '{task.id}' initialized with conversation_id: {cid}")
                            rc_url = event.get("remote_control_url") or getattr(supervisor, "remote_control_url", None)
                            if rc_url:
                                task.remote_control_url = rc_url
                                logger.info(f"[{worker_id}] Task '{task.id}' remote control URL: {rc_url}")
                            self._trigger_init_reaction(task)
                            if self.on_task_init:
                                try:
                                    self.on_task_init(task, event)
                                except Exception as cb_err:
                                    logger.debug(f"on_task_init callback error: {cb_err}")

                    def _on_thought(thought: str):
                        task.thoughts.append(thought)
                        task.append_log(f"[Thought] {thought.strip()}")
                        if self.on_task_thought:
                            try:
                                self.on_task_thought(task, thought)
                            except Exception:
                                pass

                    def _on_tool_call(tool_name: str, args: Dict[str, Any]):
                        task.tool_calls.append({"name": tool_name, "args": args})
                        args_str = json.dumps(args, ensure_ascii=False)[:200]
                        task.append_log(f"[Tool] {tool_name}({args_str})")
                        if self.on_task_tool_call:
                            try:
                                self.on_task_tool_call(task, tool_name, args)
                            except Exception:
                                pass

                    def _on_chunk(chunk: str):
                        for line in chunk.splitlines():
                            if line.strip():
                                task.append_log(line)

                    supervisor = sup_cls(
                        repo_dir=exec_cwd,
                        agent_name=task.agent,
                        model=task.selected_model,
                        run_id=task.id,
                        cache_dir=task.cached_workspace_dir,
                        skills_dir=self.skills_dir,
                        agents_dir=getattr(self, "agents_dir", None),
                        env={"ANTIGRAVITY_QUOTA_POOL": task.selected_pool} if task.selected_pool else None,
                    )

                    with self._lock:
                        if task.status == TaskStatus.ABORTED:
                            logger.info(f"[{worker_id}] Task '{task.id}' was ABORTED prior to supervisor execution.")
                            if task.cached_workspace_dir:
                                clean_workspace_dir(task.cached_workspace_dir)
                            self._prune_tasks_locked()
                            self._task_state_cond.notify_all()
                            continue
                        self._active_supervisors[task.id] = supervisor

                    try:
                        if hasattr(supervisor, "start"):
                            supervisor.start()
                        if getattr(supervisor, "session", None) and getattr(supervisor.session, "proc", None):
                            _on_process_created(supervisor.session.proc)
                        task.conversation_id = getattr(supervisor, "conversation_id", None)
                        if getattr(supervisor, "remote_control_url", None) and not task.remote_control_url:
                            task.remote_control_url = supervisor.remote_control_url
                        self._trigger_init_reaction(task)

                        if task.use_goal:
                            result = supervisor.run_goal(
                                prompt_to_run,
                                on_event=_on_event,
                                on_thought=_on_thought,
                                on_tool_call=_on_tool_call,
                                on_chunk=_on_chunk,
                                idle_timeout=self.idle_timeout,
                                max_duration=self.max_duration,
                            )
                        else:
                            result = supervisor.run_turn(
                                prompt_to_run,
                                on_event=_on_event,
                                on_thought=_on_thought,
                                on_tool_call=_on_tool_call,
                                on_chunk=_on_chunk,
                                idle_timeout=self.idle_timeout,
                                max_duration=self.max_duration,
                            )

                        task.supervisor_result = result
                        if result.conversation_id:
                            task.conversation_id = result.conversation_id
                        if getattr(result, "remote_control_url", None):
                            task.remote_control_url = result.remote_control_url
                        return_code = 0 if result.is_success else 1
                        stderr_output = result.error or ""
                        if result.response:
                            task.append_log(result.response)

                        # Deliverable validation for pr_drafter: must have produced a GitHub PR URL
                        if return_code == 0 and task.agent == "pr_drafter":
                            has_pr = bool(PR_URL_PATTERN.search(result.response or ""))
                            if not has_pr:
                                for log_line in task.logs:
                                    if PR_URL_PATTERN.search(str(log_line)):
                                        has_pr = True
                                        break
                            if not has_pr:
                                return_code = 1
                                stderr_output = "pr_drafter finished without opening a GitHub pull request"
                                if hasattr(result, "status"):
                                    result.status = "FAILED"
                                if hasattr(result, "error"):
                                    result.error = stderr_output
                                logger.warning(f"[{worker_id}] Task '{task.id}' (pr_drafter) completed turn without producing a GitHub PR URL.")

                        if self.post_completion_comment:
                            self._trigger_completion_comment(task, result)

                        if self.on_task_result:
                            try:
                                self.on_task_result(task, result)
                            except Exception as res_err:
                                logger.debug(f"on_task_result callback error: {res_err}")

                    finally:
                        if hasattr(supervisor, "cleanup"):
                            try:
                                supervisor.cleanup()
                            except Exception as c_err:
                                logger.debug(f"Supervisor cleanup error: {c_err}")
                        with self._lock:
                            self._active_supervisors.pop(task.id, None)

                elif self.script_path and exec_cwd:
                    res = _get_effective("run_agent_container", run_agent_container)(
                        task.agent,
                        task.prompt,
                        self.script_path,
                        exec_cwd,
                        on_output=task.append_log,
                        max_attempts=task.max_attempts,
                        cached_workspace_dir=task.cached_workspace_dir,
                        initial_attempt=initial_att,
                        quota_pool=task.selected_pool,
                        model=task.selected_model,
                        on_process_created=_on_process_created,
                    )
                    return_code = res.returncode
                    stderr_output = (res.stderr or "").strip()
                    if res.stdout:
                        task.update_attempt_from_output(res.stdout)
                    if res.stderr:
                        task.update_attempt_from_output(res.stderr)

                    # Deliverable validation parity for pr_drafter in non-supervisor mode
                    if return_code == 0 and task.agent == "pr_drafter":
                        has_pr = False
                        for text_src in [res.stdout or "", res.stderr or ""] + [str(l) for l in task.logs]:
                            if PR_URL_PATTERN.search(text_src):
                                has_pr = True
                                break
                        if not has_pr:
                            return_code = 1
                            stderr_output = "pr_drafter finished without opening a GitHub pull request"
                            logger.warning(f"[{worker_id}] Task '{task.id}' (pr_drafter) completed turn without producing a GitHub PR URL.")
                else:
                    return_code = 0
                    stderr_output = ""

                with self._lock:
                    self._active_processes.pop(task.id, None)
                    if task.status == TaskStatus.ABORTED:
                        logger.info(f"[{worker_id}] Task '{task.id}' was ABORTED by user.")
                        if task.cached_workspace_dir:
                            clean_workspace_dir(task.cached_workspace_dir)
                    elif return_code == 0:
                        task.finish_time = time.time()
                        task.return_code = return_code
                        task.status = TaskStatus.COMPLETED
                        logger.info(f"[{worker_id}] Task '{task.id}' COMPLETED successfully.")
                        if task.cached_workspace_dir:
                            clean_workspace_dir(task.cached_workspace_dir)
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
                                self._rebuild_queue_locked()
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
                                if task.cached_workspace_dir:
                                    clean_workspace_dir(task.cached_workspace_dir)
                        else:
                            task.finish_time = time.time()
                            task.return_code = return_code
                            task.status = TaskStatus.FAILED
                            task.error_message = stderr_output or f"Process exited with code {return_code}"
                            logger.error(f"[{worker_id}] Task '{task.id}' FAILED (exit code {return_code}).")
                            if task.cached_workspace_dir:
                                clean_workspace_dir(task.cached_workspace_dir)
                    self._prune_tasks_locked()
                    self._task_state_cond.notify_all()
            except Exception as e:
                logger.exception(f"[{worker_id}] Exception executing task '{task.id}': {e}")
                with self._lock:
                    self._active_processes.pop(task.id, None)
                    if task.status != TaskStatus.ABORTED:
                        task.finish_time = time.time()
                        task.return_code = -1
                        task.error_message = str(e)
                        task.status = TaskStatus.FAILED
                    if task.cached_workspace_dir:
                        clean_workspace_dir(task.cached_workspace_dir)
                    self._prune_tasks_locked()
                    self._task_state_cond.notify_all()
            finally:
                if self.quota_tracker:
                    try:
                        self.quota_tracker.poll_live_quota_async(
                            quota_pool=task.selected_pool if task else None,
                            force=True,
                            thread_name=f"AsyncQuotaPoll-{worker_id}",
                        )
                    except Exception as poll_err:
                        logger.warning(f"[{worker_id}] Quota fetch on task finish failed: {poll_err}")
                self._queue.task_done()

