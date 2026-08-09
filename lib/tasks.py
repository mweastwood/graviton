"""
Thread-safe Task Queue & Execution Manager for Graviton.
"""

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from lib.runner import run_agent_container

logger = logging.getLogger("graviton.tasks")


class TaskStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass
class Task:
    id: str
    agent: str
    prompt: str
    target_id: Optional[str] = None
    status: str = TaskStatus.QUEUED
    enqueue_time: float = field(default_factory=time.time)
    start_time: Optional[float] = None
    finish_time: Optional[float] = None
    worker_thread_id: Optional[str] = None
    return_code: Optional[int] = None
    error_message: Optional[str] = None

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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "prompt": self.prompt,
            "target_id": self.target_id,
            "status": self.status,
            "enqueue_time": self.enqueue_time,
            "start_time": self.start_time,
            "finish_time": self.finish_time,
            "worker_thread_id": self.worker_thread_id,
            "return_code": self.return_code,
            "elapsed_time": round(self.elapsed_time, 2),
            "wait_time": round(self.wait_time, 2),
        }


class TaskManager:
    """
    Thread-safe Task Manager managing pending queued tasks, active workers,
    and task execution history.
    """

    def __init__(
        self,
        max_workers: int = 2,
        script_path: Optional[Path] = None,
        cwd: Optional[Path] = None,
    ):
        self.max_workers = max_workers
        self.script_path = script_path
        self.cwd = cwd

        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._tasks: Dict[str, Task] = {}
        self._task_counter = 0
        self._workers: List[threading.Thread] = []
        self._running = False
        self._draining = False

    @property
    def is_draining(self) -> bool:
        """Return True if TaskManager is currently draining tasks."""
        with self._lock:
            return self._draining

    def start(self):
        """Start worker daemon threads."""
        with self._lock:
            if self._running:
                return
            self._running = True
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
            if not self._running:
                return
            self._running = False

        # Signal workers to unblock queue.get()
        for _ in self._workers:
            self._queue.put(None)

        if wait:
            for worker in self._workers:
                worker.join(timeout=1.0)
        self._workers.clear()
        logger.info("TaskManager stopped.")

    def drain_active_tasks(self, timeout: float = 300.0) -> bool:
        """
        Block new incoming tasks and wait for active (running and queued) tasks to complete.

        :param timeout: Maximum seconds to wait for active tasks to complete.
        :return: True if all active tasks completed within timeout, False if timed out.
        """
        with self._lock:
            self._draining = True

        logger.info("TaskManager entering drain mode. Blocking new task submissions...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            if not self.get_active_tasks() and not self.get_queued_tasks():
                logger.info("TaskManager drain completed cleanly. No active or queued tasks remain.")
                return True
            time.sleep(0.1)

        remaining_active = len(self.get_active_tasks())
        remaining_queued = len(self.get_queued_tasks())
        if remaining_active > 0 or remaining_queued > 0:
            logger.warning(
                f"TaskManager drain timed out after {timeout}s with "
                f"{remaining_active} running and {remaining_queued} queued task(s) remaining."
            )
            return False
        return True

    def submit_task(
        self, agent: str, prompt: str, target_id: Optional[str] = None
    ) -> Task:
        """Submit a new task to the queue."""
        with self._lock:
            if self._draining:
                raise RuntimeError("TaskManager is draining tasks")
            self._task_counter += 1
            task_id = f"task-{self._task_counter}"
            task = Task(
                id=task_id,
                agent=agent,
                prompt=prompt,
                target_id=target_id,
                status=TaskStatus.QUEUED,
                enqueue_time=time.time(),
            )
            self._tasks[task_id] = task

        self._queue.put(task)
        logger.info(f"Task '{task_id}' submitted (agent: {agent}, target: {target_id}).")
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        with self._lock:
            return self._tasks.get(task_id)

    def get_queued_tasks(self) -> List[Task]:
        with self._lock:
            return [
                t for t in self._tasks.values() if t.status == TaskStatus.QUEUED
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
            return {
                "total": total,
                "queued": queued,
                "running": running,
                "completed": completed,
                "failed": failed,
                "max_workers": self.max_workers,
            }

    def _worker_loop(self, worker_id: str):
        while self._running:
            try:
                task = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if task is None or not self._running:
                self._queue.task_done()
                break

            # Update task to RUNNING
            with self._lock:
                task.status = TaskStatus.RUNNING
                task.start_time = time.time()
                task.worker_thread_id = worker_id

            logger.info(f"[{worker_id}] Executing task '{task.id}' ({task.agent}): '{task.prompt}'")

            try:
                if self.script_path and self.cwd:
                    res = run_agent_container(
                        task.agent, task.prompt, self.script_path, self.cwd
                    )
                    return_code = res.returncode
                    stderr_output = (res.stderr or "").strip()
                else:
                    return_code = 0
                    stderr_output = ""

                with self._lock:
                    task.finish_time = time.time()
                    task.return_code = return_code
                    if return_code == 0:
                        task.status = TaskStatus.COMPLETED
                        logger.info(f"[{worker_id}] Task '{task.id}' COMPLETED successfully.")
                    else:
                        task.status = TaskStatus.FAILED
                        task.error_message = stderr_output or f"Process exited with code {return_code}"
                        logger.error(f"[{worker_id}] Task '{task.id}' FAILED (exit code {return_code}).")
            except Exception as e:
                logger.exception(f"[{worker_id}] Exception executing task '{task.id}': {e}")
                with self._lock:
                    task.finish_time = time.time()
                    task.return_code = -1
                    task.error_message = str(e)
                    task.status = TaskStatus.FAILED
            finally:
                self._queue.task_done()
