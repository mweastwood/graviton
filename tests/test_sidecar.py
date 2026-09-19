#!/usr/bin/env python3
"""
Unit tests for lib/sidecar.py
"""

import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parent.parent
from lib.sidecar import (
    is_pid_alive,
    read_sidecar_pid,
    write_sidecar_pid,
    remove_sidecar_pid,
    check_health,
    start_sidecar,
    stop_sidecar,
    get_sidecar_status,
    ensure_sidecar_running,
)


class TestSidecarManager(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.pid_file = self.tmp_path / ".graviton.pid"
        self.log_file = self.tmp_path / ".graviton.log"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_is_pid_alive(self):
        # Current process is alive
        self.assertTrue(is_pid_alive(os.getpid()))
        # Non-positive PID is not alive
        self.assertFalse(is_pid_alive(0))
        self.assertFalse(is_pid_alive(-1))
        # Non-existent PID
        self.assertFalse(is_pid_alive(99999999))

    def test_pid_file_lifecycle(self):
        self.assertIsNone(read_sidecar_pid(self.pid_file))

        # Write current PID
        curr_pid = os.getpid()
        write_sidecar_pid(curr_pid, self.pid_file)
        self.assertTrue(self.pid_file.exists())
        self.assertEqual(read_sidecar_pid(self.pid_file), curr_pid)

        # Remove PID file
        remove_sidecar_pid(self.pid_file)
        self.assertFalse(self.pid_file.exists())
        self.assertIsNone(read_sidecar_pid(self.pid_file))

    def test_read_stale_pid_file(self):
        # Write dead PID
        self.pid_file.write_text("99999999\n")
        pid = read_sidecar_pid(self.pid_file)
        self.assertIsNone(pid)
        # Stale file should have been cleaned up
        self.assertFalse(self.pid_file.exists())

    @patch("lib.sidecar.urllib.request.urlopen")
    def test_check_health_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"status": "ok", "service": "graviton-server"}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        healthy, data = check_health(host="127.0.0.1", port=8000)
        self.assertTrue(healthy)
        self.assertEqual(data.get("service"), "graviton-server")

    @patch("lib.sidecar.urllib.request.urlopen")
    def test_check_health_unreachable(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Connection refused")

        healthy, data = check_health(host="127.0.0.1", port=8000)
        self.assertFalse(healthy)
        self.assertEqual(data.get("status"), "unreachable")

    @patch("lib.sidecar.check_health")
    def test_start_sidecar_already_healthy(self, mock_health):
        mock_health.return_value = (True, {"status": "ok"})
        success, msg = start_sidecar(
            host="127.0.0.1",
            port=8000,
            pid_file=self.pid_file,
            log_file=self.log_file,
        )
        self.assertTrue(success)
        self.assertIn("already healthy", msg)

    @patch("lib.sidecar.is_pid_alive", return_value=True)
    @patch("lib.sidecar.subprocess.Popen")
    @patch("lib.sidecar.check_health")
    def test_start_sidecar_success(self, mock_health, mock_popen, mock_alive):
        # Initially not healthy, then becomes healthy
        mock_health.side_effect = [(False, {}), (True, {"status": "ok"})]
        mock_proc = MagicMock()
        mock_proc.pid = 4321
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        fake_script = self.tmp_path / "server.py"
        fake_script.write_text("#!/usr/bin/env python3\n")

        success, msg = start_sidecar(
            host="127.0.0.1",
            port=8000,
            pid_file=self.pid_file,
            log_file=self.log_file,
            server_script=fake_script,
            startup_timeout=3.0,
        )
        self.assertTrue(success)
        self.assertIn("running", msg)
        self.assertEqual(read_sidecar_pid(self.pid_file), 4321)

    @patch("lib.sidecar.is_pid_alive")
    @patch("lib.sidecar.os.kill")
    def test_stop_sidecar_running(self, mock_kill, mock_alive):
        self.pid_file.write_text("5555\n")
        mock_alive.side_effect = [True, False]  # alive when reading, then dead after kill

        success, msg = stop_sidecar(pid_file=self.pid_file, timeout=2.0)
        self.assertTrue(success)
        mock_kill.assert_called_with(5555, signal.SIGTERM)
        self.assertFalse(self.pid_file.exists())

    def test_stop_sidecar_not_running(self):
        success, msg = stop_sidecar(pid_file=self.pid_file)
        self.assertTrue(success)
        self.assertIn("No running Graviton sidecar found", msg)

    @patch("lib.sidecar.check_health")
    def test_get_sidecar_status(self, mock_health):
        mock_health.return_value = (True, {"service": "graviton-server", "tasks": {"active_tasks": 2}})
        write_sidecar_pid(os.getpid(), self.pid_file)

        status = get_sidecar_status(host="127.0.0.1", port=8000, pid_file=self.pid_file)
        self.assertTrue(status["running"])
        self.assertTrue(status["healthy"])
        self.assertEqual(status["pid"], os.getpid())
        self.assertEqual(status["port"], 8000)

    @patch("lib.sidecar.start_sidecar")
    @patch("lib.sidecar.check_health")
    def test_ensure_sidecar_running(self, mock_health, mock_start):
        # Already healthy
        mock_health.return_value = (True, {"status": "ok"})
        success, _ = ensure_sidecar_running(pid_file=self.pid_file)
        self.assertTrue(success)
        mock_start.assert_not_called()

        # Not healthy -> calls start_sidecar
        mock_health.return_value = (False, {})
        mock_start.return_value = (True, "started")
        success, msg = ensure_sidecar_running(pid_file=self.pid_file)
        self.assertTrue(success)
        mock_start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
