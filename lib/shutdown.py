"""
Graceful shutdown execution utilities for Graviton server.
"""

import logging
import time
from typing import Any, Optional

from lib.updater import set_hot_reload_state


def run_graceful_shutdown(
    task_manager: Optional[Any] = None,
    scheduler: Optional[Any] = None,
    httpd: Optional[Any] = None,
    quota_tracker: Optional[Any] = None,
    grace_period: float = 3.0,
    timeout: Optional[float] = None,
    on_quit: Optional[Any] = None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """
    Execute 4-step graceful shutdown sequence:
    1. Drain Active Tasks (task_manager.drain_active_tasks)
    2. Webhook Grace Buffer (sleep grace_period seconds)
    3. Shutdown HTTP Listener (httpd.shutdown) & Persist Task Queue and Model Selection (task_manager.dump_queue_state, quota_tracker.dump_model_selection)
    4. Clean Abort & Termination Teardown (scheduler, on_quit, httpd.server_close, task_manager)
    """
    log = logger or logging.getLogger("graviton")
    try:
        # Step 1: Drain Active Tasks
        set_hot_reload_state("SHUTDOWN: DRAINING_TASKS")
        log.info("Graceful shutdown Step 1/4: Draining active tasks...")
        if task_manager:
            try:
                task_manager.drain_active_tasks(timeout=timeout)
            except Exception as e:
                log.warning(f"Error draining active tasks during shutdown: {e}")

        # Step 2: Webhook Grace Buffer
        if grace_period > 0:
            set_hot_reload_state("SHUTDOWN: WAITING_WEBHOOKS")
            log.info(f"Graceful shutdown Step 2/4: Waiting {grace_period:.1f}s webhook grace buffer...")
            time.sleep(grace_period)

        # Step 3: Shutdown HTTP Listener & Persist Task Queue and Model Selection
        set_hot_reload_state("SHUTDOWN: PERSISTING_QUEUE")
        log.info("Graceful shutdown Step 3/4: Closing HTTP listener and persisting state...")
        if httpd:
            try:
                httpd.shutdown()
            except Exception as e:
                log.warning(f"Error shutting down HTTP server: {e}")

        if task_manager:
            try:
                task_manager.dump_queue_state()
            except Exception as e:
                log.warning(f"Error dumping task queue state: {e}")

        qt = quota_tracker or (getattr(task_manager, "quota_tracker", None) if task_manager else None)
        if qt is not None and hasattr(qt, "dump_model_selection"):
            try:
                qt.dump_model_selection()
            except Exception as e:
                log.warning(f"Error dumping model selection state: {e}")

    finally:
        # Step 4: Clean Abort & Termination Teardown
        log.info("Graceful shutdown Step 4/4: Clean abort & termination...")
        qt = quota_tracker or (getattr(task_manager, "quota_tracker", None) if task_manager else None)
        if qt is not None and hasattr(qt, "stop_background_polling"):
            try:
                qt.stop_background_polling()
            except Exception as e:
                log.warning(f"Error stopping quota background polling during shutdown: {e}")

        if scheduler:
            try:
                scheduler.stop()
            except Exception as e:
                log.warning(f"Error stopping scheduler during shutdown: {e}")

        if on_quit:
            try:
                on_quit()
            except Exception as e:
                log.warning(f"Error in on_quit callback during shutdown: {e}")

        if httpd:
            try:
                httpd.server_close()
            except Exception as e:
                log.warning(f"Error closing HTTP server socket during shutdown: {e}")

        if task_manager:
            try:
                task_manager.stop()
            except Exception as e:
                log.warning(f"Error stopping task_manager during shutdown: {e}")

        set_hot_reload_state("IDLE")
