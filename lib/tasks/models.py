"""
Task and TaskStatus data models for Graviton task queue.
"""

import collections
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

AUTO_CONTINUE_PATTERN = re.compile(
    r"Auto-continuing conversation \(Attempt\s+(\d+)(?:/(\d+))?\)",
    re.IGNORECASE,
)
PR_URL_PATTERN = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+", re.IGNORECASE)




class TaskStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED_FOR_QUOTA = "PAUSED_FOR_QUOTA"
    ABORTED = "ABORTED"


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
    priority: int = 0
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
    selected_pool: Optional[str] = None
    selected_model: Optional[str] = None
    goal_prompt: Optional[str] = None
    use_goal: bool = True
    conversation_id: Optional[str] = None
    remote_control_url: Optional[str] = None
    thoughts: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    supervisor_result: Optional[Any] = None
    webhook_event_type: Optional[str] = None
    webhook_payload: Optional[Dict[str, Any]] = None
    logs: collections.deque = field(default_factory=lambda: collections.deque(maxlen=1000))

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

    def append_log(self, line: str) -> None:
        """Append log line to buffered logs and update attempt metadata."""
        if line is None:
            return
        if not isinstance(line, str):
            line = str(line)
        self.logs.append(line)
        self.update_attempt_from_line(line)

    def get_logs(self, limit: Optional[int] = None) -> List[str]:
        """Return buffered log lines safely as a list."""
        logs_list = list(self.logs)
        if limit is not None and limit > 0:
            return logs_list[-limit:]
        return logs_list

    def update_attempt_from_line(self, line: str) -> bool:
        """Parse retry log line and update attempt / max_attempts if present."""
        if not line or ("Auto-continuing conversation" not in line and "auto-continuing conversation" not in line):
            return False
        import sys
        pat = AUTO_CONTINUE_PATTERN
        mod = sys.modules.get("lib.tasks")
        if mod and hasattr(mod, "AUTO_CONTINUE_PATTERN"):
            val = getattr(mod, "AUTO_CONTINUE_PATTERN")
            if val is not AUTO_CONTINUE_PATTERN or hasattr(val, "mock_calls") or hasattr(val, "_mock_name"):
                pat = val
        match = pat.search(line)
        if match:
            self.attempt = int(match.group(1))
            if match.group(2):
                self.max_attempts = max(self.max_attempts, int(match.group(2)))
            return True
        return False

    def update_attempt_from_output(self, output: str):
        """Parse all lines of output string and update attempt / max_attempts."""
        if not output:
            return
        for line in output.splitlines():
            self.update_attempt_from_line(line)

    def to_dict(self) -> dict:
        sup_result = None
        if self.supervisor_result is not None:
            if hasattr(self.supervisor_result, "to_dict") and callable(self.supervisor_result.to_dict):
                sup_result = self.supervisor_result.to_dict()
            elif hasattr(self.supervisor_result, "__dataclass_fields__"):
                sup_result = asdict(self.supervisor_result)
            elif isinstance(self.supervisor_result, dict):
                sup_result = self.supervisor_result
            else:
                sup_result = str(self.supervisor_result)

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
            "priority": self.priority,
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
            "selected_pool": self.selected_pool,
            "selected_model": self.selected_model,
            "goal_prompt": self.goal_prompt,
            "use_goal": self.use_goal,
            "conversation_id": self.conversation_id,
            "remote_control_url": self.remote_control_url,
            "thoughts": list(self.thoughts),
            "tool_calls": list(self.tool_calls),
            "supervisor_result": sup_result,
            "webhook_event_type": self.webhook_event_type,
            "webhook_payload": self.webhook_payload,
        }



