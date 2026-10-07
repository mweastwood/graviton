"""
Persistence helpers for dumping and restoring task queue state.
"""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

from lib.quota import _atomic_write_json
from .models import Task, TaskStatus

logger = logging.getLogger("graviton.tasks")


def _get_effective_atomic_write():
    mod = sys.modules.get("lib.tasks")
    if mod and hasattr(mod, "_atomic_write_json"):
        val = getattr(mod, "_atomic_write_json")
        if hasattr(val, "mock_calls") or hasattr(val, "_mock_name") or val is not _atomic_write_json:
            return val
    return _atomic_write_json


def get_default_state_path(cwd: Optional[Path] = None, filepath: Optional[Path] = None) -> Path:
    """Return destination state file path given optional cwd and explicit filepath override."""
    if filepath is not None:
        return Path(filepath)
    if cwd:
        return Path(cwd) / ".graviton_queue_state.json"
    return Path(".graviton_queue_state.json")


def dump_queue_state(
    tasks: Dict[str, Task],
    task_counter: int,
    cwd: Optional[Path] = None,
    filepath: Optional[Path] = None,
) -> int:
    """
    Serialize pending queued and quota-paused tasks and task counter to disk JSON file.

    :param tasks: Dictionary of tasks.
    :param task_counter: Current highest task integer counter.
    :param cwd: Optional working directory of task manager.
    :param filepath: Optional Path override for destination state file.
    :return: Number of queued and quota-paused tasks serialized.
    """
    path = get_default_state_path(cwd=cwd, filepath=filepath)
    queued_tasks = [
        t
        for t in tasks.values()
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
        "task_counter": task_counter,
        "queued_tasks": [t.to_dict() for t in queued_tasks],
    }

    try:
        write_fn = _get_effective_atomic_write()
        write_fn(path, state, indent=2)
        logger.info(f"Dumped {len(queued_tasks)} queued/quota-paused task(s) state to {path}.")
        return len(queued_tasks)
    except Exception as e:
        logger.exception(f"Failed to dump queue state to '{path}': {e}")
        return 0


def restore_queue_state(
    tasks: Dict[str, Task],
    current_task_counter: int,
    cwd: Optional[Path] = None,
    filepath: Optional[Path] = None,
) -> Tuple[int, int]:
    """
    Restore pending queued and quota-paused tasks and task counter from disk JSON file.

    :param tasks: Dictionary to populate with restored tasks.
    :param current_task_counter: Current task counter value.
    :param cwd: Optional working directory of task manager.
    :param filepath: Optional Path override for source state file.
    :return: Tuple of (new_task_counter, number_of_restored_tasks).
    """
    path = get_default_state_path(cwd=cwd, filepath=filepath)
    if not path.exists():
        return current_task_counter, 0

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            path.unlink()
        except Exception as e:
            logger.warning(f"Could not remove state file '{path}' after reading: {e}")
    except Exception as e:
        logger.exception(f"Failed to read queue state file '{path}': {e}")
        return current_task_counter, 0

    if not isinstance(data, dict):
        logger.warning(f"Invalid queue state format in '{path}': expected JSON object.")
        return current_task_counter, 0

    queued_data = data.get("queued_tasks", [])
    if not isinstance(queued_data, list):
        logger.warning(f"Invalid queued_tasks format in '{path}': expected list.")
        return current_task_counter, 0

    saved_counter = data.get("task_counter", 0)
    task_counter = current_task_counter
    if isinstance(saved_counter, int):
        task_counter = max(task_counter, saved_counter)

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
                    task_counter = max(task_counter, num)
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
                priority=int(td.get("priority", 0)),
                attempt=int(td.get("attempt", 1)),
                max_attempts=int(td.get("max_attempts", 3)),
                max_total_attempts=int(td.get("max_total_attempts", 6)),
                attempts_per_batch=int(td.get("attempts_per_batch", 3)),
                requeue_count=int(td.get("requeue_count", 0)),
                goal_prompt=td.get("goal_prompt"),
                use_goal=bool(td.get("use_goal", True)),
                conversation_id=td.get("conversation_id"),
                remote_control_url=td.get("remote_control_url"),
                thoughts=list(td.get("thoughts", [])),
                tool_calls=list(td.get("tool_calls", [])),
                supervisor_result=td.get("supervisor_result"),
                webhook_event_type=td.get("webhook_event_type"),
                webhook_payload=td.get("webhook_payload"),
            )
            tasks[task.id] = task
            restored_count += 1
        except Exception as e:
            logger.warning(f"Failed to restore queued task item {td}: {e}")
            continue

    logger.info(f"Restored {restored_count} queued/quota-paused task(s) state from {path}.")
    return task_counter, restored_count
