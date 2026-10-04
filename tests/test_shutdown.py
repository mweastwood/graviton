"""
Unit tests for lib/shutdown.py (run_graceful_shutdown).
"""

import unittest
from unittest.mock import MagicMock, patch

from lib.shutdown import run_graceful_shutdown


class TestShutdown(unittest.TestCase):
    def test_run_graceful_shutdown_httpd_shutdown_before_dump_queue_state(self):
        mock_task_manager = MagicMock()
        mock_httpd = MagicMock()
        call_order = []

        def record_httpd_shutdown():
            call_order.append("httpd.shutdown")

        def record_dump_queue():
            call_order.append("dump_queue_state")

        mock_httpd.shutdown.side_effect = record_httpd_shutdown
        mock_task_manager.dump_queue_state.side_effect = record_dump_queue

        run_graceful_shutdown(
            task_manager=mock_task_manager,
            httpd=mock_httpd,
            grace_period=0.01,
        )

        self.assertEqual(call_order, ["httpd.shutdown", "dump_queue_state"])

    def test_run_graceful_shutdown_exception_resilience(self):
        mock_task_manager = MagicMock()
        mock_scheduler = MagicMock()
        mock_httpd = MagicMock()
        mock_on_quit = MagicMock()

        mock_task_manager.drain_active_tasks.side_effect = RuntimeError("Drain error")

        run_graceful_shutdown(
            task_manager=mock_task_manager,
            scheduler=mock_scheduler,
            httpd=mock_httpd,
            grace_period=0.01,
            on_quit=mock_on_quit,
        )

        mock_httpd.shutdown.assert_called_once()
        mock_task_manager.dump_queue_state.assert_called_once()
        mock_scheduler.stop.assert_called_once()
        mock_on_quit.assert_called_once()
        mock_httpd.server_close.assert_called_once()
        mock_task_manager.stop.assert_called_once()


    def test_drain_active_tasks_called_with_timeout(self):
        tm = MagicMock()
        run_graceful_shutdown(task_manager=tm, grace_period=0, timeout=12.5)
        tm.drain_active_tasks.assert_called_once_with(timeout=12.5)

    def test_quota_tracker_persisted_and_polling_stopped(self):
        qt = MagicMock()
        run_graceful_shutdown(quota_tracker=qt, grace_period=0)
        qt.dump_model_selection.assert_called_once()
        qt.stop_background_polling.assert_called_once()

    def test_quota_tracker_falls_back_to_task_manager(self):
        tm = MagicMock()
        run_graceful_shutdown(task_manager=tm, grace_period=0)
        tm.quota_tracker.dump_model_selection.assert_called_once()
        tm.quota_tracker.stop_background_polling.assert_called_once()

    @patch("lib.shutdown.set_hot_reload_state")
    def test_hot_reload_state_progression_ends_idle(self, mock_set_state):
        run_graceful_shutdown(grace_period=0.01)
        states = [c.args[0] for c in mock_set_state.call_args_list]
        self.assertEqual(
            states,
            [
                "SHUTDOWN: DRAINING_TASKS",
                "SHUTDOWN: WAITING_WEBHOOKS",
                "SHUTDOWN: PERSISTING_QUEUE",
                "IDLE",
            ],
        )

    @patch("lib.shutdown.set_hot_reload_state")
    def test_hot_reload_state_idle_even_on_failure(self, mock_set_state):
        tm = MagicMock()
        tm.dump_queue_state.side_effect = RuntimeError("boom")
        run_graceful_shutdown(task_manager=tm, grace_period=0)
        self.assertEqual(mock_set_state.call_args_list[-1].args[0], "IDLE")

    def test_all_optional_arguments_none(self):
        run_graceful_shutdown(grace_period=0)
        run_graceful_shutdown(task_manager=None, httpd=None, quota_tracker=None, scheduler=None, grace_period=0.01)


if __name__ == "__main__":
    unittest.main()
