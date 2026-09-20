#!/usr/bin/env python3
"""
Sidecar process manager for Graviton.

Manages running bin/graviton-server.py as a background sidecar service
for Antigravity plugins, IDE integrations, and CLI hooks.

Zero external dependencies (Python standard library only).
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("graviton.sidecar")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_SMEE_URL = os.environ.get("SMEE_URL", "")
DEFAULT_PID_FILE = REPO_ROOT / ".graviton_sidecar.pid"
DEFAULT_LOG_FILE = REPO_ROOT / ".graviton_sidecar.log"
SERVER_SCRIPT = REPO_ROOT / "bin" / "graviton-server.py"


def is_pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user
        return True
    except OSError:
        return False


def is_graviton_process(pid: int) -> bool:
    """
    Verify whether the given PID corresponds to a running Graviton process.
    Checks /proc/<pid>/cmdline on Linux, with fallback to 'ps' on other POSIX systems.
    """
    if pid <= 0 or not is_pid_alive(pid):
        return False

    # 1. Linux /proc/<pid>/cmdline
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    if proc_cmdline.exists():
        try:
            cmdline = proc_cmdline.read_bytes().decode("utf-8", errors="ignore").replace("\x00", " ")
            return "graviton" in cmdline.lower()
        except Exception:
            return False

    # 2. Fallback via ps command
    try:
        res = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=1.0,
        )
        if res.returncode == 0 and res.stdout:
            return "graviton" in res.stdout.lower()
    except Exception:
        pass

    return False


def read_sidecar_pid(pid_file: Optional[Path] = None) -> Optional[int]:
    """Read PID from PID file if it exists and process is alive."""
    path = Path(pid_file) if pid_file else DEFAULT_PID_FILE
    if not path.is_file():
        return None
    try:
        content = path.read_text().strip()
        pid = int(content)
        if is_pid_alive(pid):
            return pid
        else:
            # Stale PID file
            remove_sidecar_pid(path)
            return None
    except Exception as e:
        logger.debug(f"Error reading PID file '{path}': {e}")
        return None


def write_sidecar_pid(pid: int, pid_file: Optional[Path] = None) -> None:
    """Write PID to PID file atomically."""
    path = Path(pid_file) if pid_file else DEFAULT_PID_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(f"{pid}\n")
    temp_path.replace(path)


def remove_sidecar_pid(pid_file: Optional[Path] = None) -> None:
    """Remove PID file if it exists."""
    path = Path(pid_file) if pid_file else DEFAULT_PID_FILE
    try:
        if path.exists():
            path.unlink()
    except Exception as e:
        logger.debug(f"Failed to remove PID file '{path}': {e}")


def check_health(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    timeout: float = 2.0,
) -> Tuple[bool, Dict[str, Any]]:
    """
    Perform a GET request to http://{host}:{port}/health.
    
    Returns:
        (is_healthy, health_data_or_error_dict)
    """
    url = f"http://{host}:{port}/health"
    req = urllib.request.Request(url, headers={"User-Agent": "Graviton-Sidecar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8")
                try:
                    data = json.loads(raw)
                    return True, data
                except json.JSONDecodeError:
                    return True, {"status": "ok", "raw": raw}
            return False, {"status": "error", "http_code": resp.status}
    except Exception as e:
        return False, {"status": "unreachable", "error": str(e)}


def start_sidecar(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    pid_file: Optional[Path] = None,
    log_file: Optional[Path] = None,
    extra_args: Optional[List[str]] = None,
    startup_timeout: float = 10.0,
    server_script: Optional[Path] = None,
    smee_url: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Start the Graviton server in the background as a daemon sidecar.
    
    Returns:
        (success, message)
    """
    pid_path = Path(pid_file) if pid_file else DEFAULT_PID_FILE
    log_path = Path(log_file) if log_file else DEFAULT_LOG_FILE
    script = Path(server_script) if server_script else SERVER_SCRIPT

    # Check if already running and healthy
    existing_pid = read_sidecar_pid(pid_path)
    healthy, _ = check_health(host=host, port=port, timeout=1.0)
    if healthy:
        msg = f"Graviton sidecar is already healthy on http://{host}:{port}"
        if existing_pid:
            msg += f" (PID {existing_pid})"
        logger.info(msg)
        return True, msg

    if existing_pid:
        # Process is alive but not answering health check
        if is_graviton_process(existing_pid):
            logger.warning(f"Found alive PID {existing_pid} for sidecar, but health check failed. Attempting restart...")
            stop_sidecar(pid_file=pid_path, timeout=3.0)
        else:
            logger.warning(
                f"PID {existing_pid} in PID file is alive but is not a Graviton process (PID recycled). "
                "Removing stale PID file without killing process."
            )
            remove_sidecar_pid(pid_path)
            existing_pid = None

    if not script.exists():
        err = f"Graviton server script not found: {script}"
        logger.error(err)
        return False, err

    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(script),
        "--host",
        host,
        "--port",
        str(port),
    ]

    has_no_smee = extra_args and any(arg == "--no-smee" for arg in extra_args)
    has_smee = extra_args and any(arg == "--smee-url" or arg.startswith("--smee-url=") for arg in extra_args)
    if not has_smee and not has_no_smee:
        target_smee = smee_url if smee_url is not None else os.environ.get("SMEE_URL", DEFAULT_SMEE_URL)
        if target_smee:
            cmd.extend(["--smee-url", target_smee])
        else:
            cmd.append("--no-smee")

    if extra_args:
        cmd.extend(extra_args)

    logger.info(f"Launching Graviton sidecar daemon: {' '.join(cmd)}")

    try:
        with open(log_path, "a", encoding="utf-8") as out_f:
            out_f.write(f"\n--- Graviton Sidecar Started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            out_f.flush()
            proc = subprocess.Popen(
                cmd,
                stdout=out_f,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # detach process group
                cwd=str(REPO_ROOT),
            )

        write_sidecar_pid(proc.pid, pid_path)
        logger.info(f"Graviton sidecar spawned with PID {proc.pid}. Waiting for readiness...")

        # Poll health endpoint
        deadline = time.time() + startup_timeout
        while time.time() < deadline:
            # Check if process exited prematurely
            if proc.poll() is not None:
                remove_sidecar_pid(pid_path)
                return False, f"Graviton sidecar exited immediately with return code {proc.returncode}. See log: {log_path}"

            healthy, _ = check_health(host=host, port=port, timeout=0.8)
            if healthy:
                logger.info(f"Graviton sidecar ready on http://{host}:{port} (PID {proc.pid}).")
                return True, f"Graviton sidecar running (PID {proc.pid}) at http://{host}:{port}"

            time.sleep(0.3)

        return False, f"Graviton sidecar started (PID {proc.pid}) but failed readiness check within {startup_timeout}s. See log: {log_path}"

    except Exception as e:
        remove_sidecar_pid(pid_path)
        err = f"Failed to launch Graviton sidecar: {e}"
        logger.error(err)
        return False, err


def stop_sidecar(
    pid_file: Optional[Path] = None,
    timeout: float = 5.0,
    verify_process: bool = True,
) -> Tuple[bool, str]:
    """
    Stop the running Graviton sidecar process.
    
    Returns:
        (success, message)
    """
    pid_path = Path(pid_file) if pid_file else DEFAULT_PID_FILE
    pid = read_sidecar_pid(pid_path)

    if not pid:
        remove_sidecar_pid(pid_path)
        return True, "No running Graviton sidecar found."

    if verify_process and not is_graviton_process(pid):
        logger.warning(
            f"PID {pid} in PID file is alive but does not appear to be a Graviton process (PID recycled). "
            "Removing stale PID file without killing process."
        )
        remove_sidecar_pid(pid_path)
        return True, f"PID {pid} was not a Graviton process; removed stale PID file."

    logger.info(f"Stopping Graviton sidecar (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_sidecar_pid(pid_path)
        return True, f"Sidecar process (PID {pid}) was already terminated."
    except Exception as e:
        return False, f"Failed to signal sidecar PID {pid}: {e}"

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_pid_alive(pid):
            remove_sidecar_pid(pid_path)
            logger.info(f"Graviton sidecar (PID {pid}) stopped gracefully.")
            return True, f"Graviton sidecar (PID {pid}) stopped."
        time.sleep(0.2)

    # Force kill if still alive
    try:
        logger.warning(f"Graviton sidecar (PID {pid}) did not stop within {timeout}s; sending SIGKILL.")
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    except Exception:
        pass

    remove_sidecar_pid(pid_path)
    return True, f"Graviton sidecar (PID {pid}) forcibly terminated."


def get_sidecar_status(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    pid_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Query the complete live status of the Graviton sidecar.
    """
    pid_path = Path(pid_file) if pid_file else DEFAULT_PID_FILE
    pid = read_sidecar_pid(pid_path)
    healthy, health_data = check_health(host=host, port=port, timeout=1.5)

    return {
        "running": bool(pid or healthy),
        "pid": pid,
        "healthy": healthy,
        "host": host,
        "port": port,
        "endpoint": f"http://{host}:{port}",
        "details": health_data,
    }


def ensure_sidecar_running(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    pid_file: Optional[Path] = None,
    log_file: Optional[Path] = None,
    extra_args: Optional[List[str]] = None,
    startup_timeout: float = 8.0,
    smee_url: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Guarantees the Graviton sidecar is running and healthy.
    If not running or unhealthy, starts it automatically.
    """
    healthy, _ = check_health(host=host, port=port, timeout=1.0)
    if healthy:
        pid = read_sidecar_pid(pid_file)
        return True, f"Graviton sidecar is healthy on http://{host}:{port}" + (f" (PID {pid})" if pid else "")

    logger.info("Graviton sidecar is not responding; starting automatically...")
    return start_sidecar(
        host=host,
        port=port,
        pid_file=pid_file,
        log_file=log_file,
        extra_args=extra_args,
        startup_timeout=startup_timeout,
        smee_url=smee_url,
    )
