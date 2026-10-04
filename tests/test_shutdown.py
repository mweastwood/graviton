"""
Unit tests for lib/shutdown.py (run_graceful_shutdown).
"""

import unittest
from unittest.mock import MagicMock

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


if __name__ == "__main__":
    unittest.main()
