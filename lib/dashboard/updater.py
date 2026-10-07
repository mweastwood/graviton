#!/usr/bin/env python3
"""
Continuous background dashboard updater for Graviton.
Zero external dependencies (Python standard library only).
"""

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Union

from lib.dashboard.common import logger
from lib.pr_tracker import PRTracker
from lib.quota import QuotaTracker
from lib.scheduler import TaskScheduler
from lib.tasks import TaskManager


class DashboardUpdater:
    """
    Manages continuous live dashboard updates to registered file paths on disk.

    When Antigravity opens a side panel artifact, its path is registered with
    DashboardUpdater. As tasks progress, the updater writes formatted markdown
    directly to the file on disk. Antigravity detects the disk change and live-reloads
    the side panel artifact automatically.
    """

    def __init__(
        self,
        task_manager: Optional[TaskManager] = None,
        quota_tracker: Optional[QuotaTracker] = None,
        scheduler: Optional[TaskScheduler] = None,
        pr_tracker: Optional[PRTracker] = None,
        host: str = "localhost",
        port: int = 8000,
        update_interval: float = 2.0,
        min_interval: float = 0.5,
    ):
        self.task_manager = task_manager
        self.quota_tracker = quota_tracker
        self.scheduler = scheduler
        self.pr_tracker = pr_tracker
        self.host = host
        self.port = port
        self.update_interval = update_interval
        self.min_interval = min_interval

        self._targets: Set[Path] = set()
        self._lock = threading.Lock()
        self._wake_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._last_update_ts: float = 0.0

    def register_target(self, path: Union[str, Path]) -> bool:
        """Register a file path to receive live dashboard markdown updates."""
        resolved = Path(path).resolve()
        with self._lock:
            self._targets.add(resolved)
        logger.info(f"Registered live dashboard artifact target: {resolved}")
        self.trigger_update()
        return True

    def unregister_target(self, path: Union[str, Path]) -> bool:
        """Unregister a target path from live updates."""
        resolved = Path(path).resolve()
        with self._lock:
            if resolved in self._targets:
                self._targets.remove(resolved)
                logger.info(f"Unregistered live dashboard artifact target: {resolved}")
                return True
        return False

    def get_targets(self) -> List[str]:
        """Return list of currently registered target file paths."""
        with self._lock:
            return [str(p) for p in self._targets]

    def trigger_update(self) -> None:
        """Signal the updater background thread to refresh targets."""
        self._wake_event.set()

    def start(self) -> None:
        """Start the background updater loop."""
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="GravitonDashboardUpdater",
            )
            self._thread.start()
            logger.info("Graviton DashboardUpdater background loop started.")

    def stop(self) -> None:
        """Stop the background updater loop and join thread."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._wake_event.set()
        if self._thread and self._thread.is_alive() and self._thread != threading.current_thread():
            self._thread.join(timeout=3.0)
        logger.info("Graviton DashboardUpdater stopped.")

    def get_markdown(self) -> str:
        """Generate and return current markdown content without writing to targets."""
        from lib.dashboard.markdown import format_dashboard_markdown

        return format_dashboard_markdown(
            task_manager=self.task_manager,
            quota_tracker=self.quota_tracker,
            scheduler=self.scheduler,
            pr_tracker=self.pr_tracker,
            host=self.host,
            port=self.port,
        )

    def update_now(self) -> Optional[str]:
        """Generate current markdown content and write to all registered targets."""
        content = self.get_markdown()

        with self._lock:
            targets = list(self._targets)

        for target in targets:
            self._write_to_target(target, content)

        self._last_update_ts = time.time()
        return content

    def _write_to_target(self, target: Path, content: str) -> bool:
        """Atomically write markdown content to a target file path."""
        tmp_path: Optional[Path] = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = target.with_suffix(f"{target.suffix}.tmp.{os.getpid()}.{threading.get_ident()}")
            tmp_path.write_text(content, encoding="utf-8")
            tmp_path.replace(target)
            return True
        except Exception as e:
            logger.warning(f"Failed writing live dashboard to {target}: {e}")
            if tmp_path is not None:
                try:
                    if tmp_path.exists():
                        tmp_path.unlink()
                except Exception:
                    pass
            return False

    def _run_loop(self) -> None:
        """Background updater loop handling timed heartbeats and triggered events."""
        while self._running:
            # Wait for wake event or periodic timeout
            woken = self._wake_event.wait(timeout=self.update_interval)
            if not self._running:
                break
            self._wake_event.clear()

            # Throttle if triggered too quickly
            now = time.time()
            elapsed_since_last = now - self._last_update_ts
            if elapsed_since_last < self.min_interval:
                time.sleep(self.min_interval - elapsed_since_last)
                if not self._running:
                    break

            try:
                self.update_now()
            except Exception as e:
                logger.warning(f"Error during dashboard update cycle: {e}")
