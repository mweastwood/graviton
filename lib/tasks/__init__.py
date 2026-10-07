"""
Thread-safe Task Queue & Execution Manager package for Graviton.

Provides:
- models: Task and TaskStatus data models
- pool: resolve_task_pool_and_model quota-aware scheduler resolver
- workspaces: prune_abandoned_workspaces and _PrunedTaskIds garbage collector
- persistence: dump_queue_state, restore_queue_state, and state path helpers
- manager: TaskManager execution engine
"""

import subprocess
from pathlib import Path

from lib.runner import run_agent_container
from lib.quota import _atomic_write_json
from lib.security import is_valid_repo_name
from lib.supervisor import clean_workspace_dir
from lib.reactions import post_emoji_reaction_async
from lib.notifications import post_task_completion_comment, post_task_start_comment

from .models import (
    AUTO_CONTINUE_PATTERN,
    PR_URL_PATTERN,
    Task,
    TaskStatus,
)
from .pool import resolve_task_pool_and_model
from .workspaces import (
    _PrunedTaskIds,
    prune_abandoned_workspaces,
)
from .persistence import (
    dump_queue_state,
    get_default_state_path,
    restore_queue_state,
)
from .manager import (
    REPO_ROOT,
    TaskManager,
    logger,
)

__all__ = [
    "TaskStatus",
    "Task",
    "AUTO_CONTINUE_PATTERN",
    "PR_URL_PATTERN",
    "resolve_task_pool_and_model",
    "prune_abandoned_workspaces",
    "_PrunedTaskIds",
    "get_default_state_path",
    "dump_queue_state",
    "restore_queue_state",
    "TaskManager",
    "logger",
    "REPO_ROOT",
    "run_agent_container",
    "post_emoji_reaction_async",
    "post_task_start_comment",
    "post_task_completion_comment",
    "is_valid_repo_name",
    "clean_workspace_dir",
    "_atomic_write_json",
    "subprocess",
]
