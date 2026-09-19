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


from importlib.machinery import SourceFileLoader
import io

sidecar_cli = SourceFileLoader("graviton_sidecar_cli", str(REPO_ROOT / "bin" / "graviton-sidecar")).load_module()


class TestGravitonSidecarCLI(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmpdir.name)
        self.pid_file = self.tmp_path / ".graviton.pid"
        self.log_file = self.tmp_path / ".graviton.log"

    def tearDown(self):
        self.tmpdir.cleanup()

    @patch.object(sidecar_cli, "ensure_sidecar_running")
    def test_cli_ensure_success_default(self, mock_ensure):
        mock_ensure.return_value = (True, "Graviton sidecar running (PID 1234)")
        with patch("sys.argv", ["graviton-sidecar", "--pid-file", str(self.pid_file), "ensure"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Graviton sidecar running", mock_out.getvalue())

    @patch.object(sidecar_cli, "ensure_sidecar_running")
    def test_cli_ensure_success_json(self, mock_ensure):
        mock_ensure.return_value = (True, "Graviton sidecar running (PID 1234)")
        with patch("sys.argv", ["graviton-sidecar", "--pid-file", str(self.pid_file), "ensure", "--json"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertEqual(json.loads(mock_out.getvalue().strip()), {})

    @patch.object(sidecar_cli, "ensure_sidecar_running")
    def test_cli_ensure_success_quiet(self, mock_ensure):
        mock_ensure.return_value = (True, "Graviton sidecar running (PID 1234)")
        with patch("sys.argv", ["graviton-sidecar", "--pid-file", str(self.pid_file), "ensure", "--quiet"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 0)
            self.assertEqual(mock_out.getvalue().strip(), "")

    @patch.object(sidecar_cli, "ensure_sidecar_running")
    def test_cli_ensure_failure(self, mock_ensure):
        mock_ensure.return_value = (False, "Process failed to bind port")
        with patch("sys.argv", ["graviton-sidecar", "--pid-file", str(self.pid_file), "ensure"]), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Process failed to bind port", mock_err.getvalue())

    @patch.object(sidecar_cli, "ensure_sidecar_running")
    def test_cli_ensure_failure_json(self, mock_ensure):
        mock_ensure.return_value = (False, "Process failed to bind port")
        with patch("sys.argv", ["graviton-sidecar", "--pid-file", str(self.pid_file), "ensure", "--json"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 1)
            out_json = json.loads(mock_out.getvalue().strip())
            self.assertEqual(out_json["error"], "Process failed to bind port")

    @patch.object(sidecar_cli, "get_sidecar_status")
    def test_cli_status_text(self, mock_status):
        mock_status.return_value = {
            "running": True,
            "healthy": True,
            "pid": 5678,
            "endpoint": "http://127.0.0.1:8000",
            "details": {
                "tasks": {"active_tasks": 2, "queued_tasks": 1},
                "quota": {"current_pool": "gemini"},
            },
        }
        with patch("sys.argv", ["graviton-sidecar", "status"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            sidecar_cli.main()
            output = mock_out.getvalue()
            self.assertIn("RUNNING (HEALTHY)", output)
            self.assertIn("5678", output)
            self.assertIn("2 active, 1 queued", output)

    @patch.object(sidecar_cli, "get_sidecar_status")
    def test_cli_status_json(self, mock_status):
        mock_status.return_value = {
            "running": True,
            "healthy": True,
            "pid": 5678,
            "endpoint": "http://127.0.0.1:8000",
            "details": {},
        }
        with patch("sys.argv", ["graviton-sidecar", "status", "--json"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            sidecar_cli.main()
            data = json.loads(mock_out.getvalue())
            self.assertEqual(data["pid"], 5678)
            self.assertTrue(data["healthy"])

    @patch.object(sidecar_cli, "start_sidecar")
    def test_cli_start(self, mock_start):
        mock_start.return_value = (True, "Graviton sidecar started (PID 9999)")
        with patch("sys.argv", [
            "graviton-sidecar",
            "--pid-file", str(self.pid_file),
            "--log-file", str(self.log_file),
            "start",
            "--smee-url", "https://smee.io/test1234",
            "--no-supervisor",
        ]), patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 0)
            mock_start.assert_called_once()
            _, kwargs = mock_start.call_args
            self.assertIn("--smee-url", kwargs["extra_args"])
            self.assertIn("--no-supervisor", kwargs["extra_args"])

    @patch.object(sidecar_cli, "stop_sidecar")
    def test_cli_stop(self, mock_stop):
        mock_stop.return_value = (True, "Graviton sidecar stopped")
        with patch("sys.argv", ["graviton-sidecar", "--pid-file", str(self.pid_file), "stop"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 0)
            mock_stop.assert_called_once()

    def test_cli_logs_with_deque(self):
        # Write 20 lines to log file
        log_lines = [f"Log line {i}\n" for i in range(20)]
        self.log_file.write_text("".join(log_lines))

        with patch("sys.argv", ["graviton-sidecar", "--log-file", str(self.log_file), "logs", "-n", "5"]), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            sidecar_cli.main()
            output = mock_out.getvalue().strip().splitlines()
            self.assertEqual(len(output), 5)
            self.assertEqual(output[0], "Log line 15")
            self.assertEqual(output[-1], "Log line 19")

    def test_cli_logs_file_not_found(self):
        missing_log = self.tmp_path / "nonexistent.log"
        with patch("sys.argv", ["graviton-sidecar", "--log-file", str(missing_log), "logs"]), \
             patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            with self.assertRaises(SystemExit) as cm:
                sidecar_cli.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Log file not found", mock_err.getvalue())


if __name__ == "__main__":
    unittest.main()
