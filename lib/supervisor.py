"""
Programmatic Stream-JSON Supervisor for Antigravity CLI (agy).

Provides a robust, multi-turn NDJSON stream interface to drive `agy` processes
programmatically using `--input-format stream-json --output-format stream-json`.
Eliminates one-shot print timeouts, step limits, and fragile transcript file scraping.

Standard library only (zero external dependencies).
"""

import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import struct
import subprocess
import threading
import time
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

logger = logging.getLogger("graviton.supervisor")

DEFAULT_PROJECT_NAME = "Graviton Workers"

__all__ = [
    "ContainerSupervisor",
    "DEFAULT_PROJECT_NAME",
    "StreamSession",
    "SupervisorError",
    "SupervisorResult",
    "SupervisorTimeoutError",
    "clean_workspace_dir",
    "ensure_default_project",
    "ensure_workspace_trusted",
    "extract_remote_control_url",
    "find_project_for_repo",
    "get_remote_control_instance_name",
    "has_ssh_credentials",
    "run_container_goal",
    "run_container_turn",
    "run_goal_turn",
    "run_stream_turn",
    "sync_conversation_to_agyhub",
    "to_ssh_url",
]

_UNSET: Any = object()
_CACHED_INSTANCE_NAME: Any = _UNSET


def get_remote_control_instance_name() -> Optional[str]:
    """Retrieve and cache the Antigravity instance name if available."""
    global _CACHED_INSTANCE_NAME
    if _CACHED_INSTANCE_NAME is not _UNSET:
        return _CACHED_INSTANCE_NAME
    env_instance = os.environ.get("ANTIGRAVITY_INSTANCE_NAME")
    if env_instance:
        _CACHED_INSTANCE_NAME = env_instance.strip()
        return _CACHED_INSTANCE_NAME
    agy_bin = shutil.which("agy") or "agy"
    try:
        res = subprocess.run(
            [agy_bin, "remote-control", "status"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if res.returncode == 0 and res.stdout:
            m = re.search(r"Instance name:\s*([^\s(]+)", res.stdout, re.IGNORECASE)
            if m:
                _CACHED_INSTANCE_NAME = m.group(1).strip()
                return _CACHED_INSTANCE_NAME
    except Exception:
        pass
    _CACHED_INSTANCE_NAME = None
    return None


def extract_remote_control_url(
    event: Optional[Dict[str, Any]] = None,
    stderr_lines: Optional[Union[str, Iterable[str]]] = None,
    conversation_id: Optional[str] = None,
    remote_control_enabled: bool = True,
    instance_name: Optional[str] = None,
) -> Optional[str]:
    """
    Extract or synthesize a clickable Antigravity Remote Control URL.

    Checks:
    1. Direct fields in event dict (e.g. 'remote_control_url', 'remote_control.url', 'init.remote_control_url').
    2. URL patterns in stderr or captured terminal output.
    3. Canonical conversation URL fallback (https://antigravity.google.com?instance=<instance>) if remote control is enabled.
    """
    if event and isinstance(event, dict):
        if event.get("remote_control_url"):
            return str(event["remote_control_url"]).strip()
        rc_obj = event.get("remote_control")
        if isinstance(rc_obj, dict) and rc_obj.get("url"):
            return str(rc_obj["url"]).strip()
        init_obj = event.get("init")
        if isinstance(init_obj, dict):
            if init_obj.get("remote_control_url"):
                return str(init_obj["remote_control_url"]).strip()
            if init_obj.get("url"):
                return str(init_obj["url"]).strip()
            if init_obj.get("session_url"):
                return str(init_obj["session_url"]).strip()

    if stderr_lines:
        if isinstance(stderr_lines, str):
            lines = stderr_lines.splitlines()
        else:
            lines = stderr_lines
        url_regex = re.compile(r"https://antigravity\.google\.com/[a-zA-Z0-9_\-/?&=#]+")
        for line in lines:
            if not line:
                continue
            match = url_regex.search(line)
            if match:
                return match.group(0).rstrip(".,;")

    if remote_control_enabled and conversation_id and conversation_id.strip():
        base_url = os.environ.get("ANTIGRAVITY_REMOTE_CONTROL_BASE_URL", "https://antigravity.google.com").rstrip("/")
        inst = instance_name if instance_name is not None else get_remote_control_instance_name()
        query_suffix = f"?instance={inst.strip()}" if inst and inst.strip() else ""
        return f"{base_url}/c/{conversation_id.strip()}{query_suffix}"

    return None


@dataclass
class SupervisorResult:
    """Represents the structured result of an agent turn."""

    conversation_id: Optional[str] = None
    remote_control_url: Optional[str] = None
    status: str = "UNKNOWN"
    response: str = ""
    duration_seconds: float = 0.0
    num_turns: int = 0
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    thoughts: List[str] = field(default_factory=list)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.status == "SUCCESS" and not self.error

    @property
    def is_goal_complete(self) -> bool:
        resp = self.response or ""
        return (
            "<!-- GOAL_COMPLETE -->" in resp
            or (self.is_success and "GOAL_COMPLETE" in resp)
            or self.is_success
        )

    def to_dict(self) -> Dict[str, Any]:
        from dataclasses import asdict
        return asdict(self)


class SupervisorError(Exception):
    """Base exception for StreamSession supervisor failures."""
    pass


class SupervisorTimeoutError(SupervisorError):
    """Raised when an agent turn exceeds the configured timeout."""
    pass


class StreamSession:
    """
    Manages a persistent, multi-turn agy process via bidirectional NDJSON streaming.
    """

    def __init__(
        self,
        agent_name: Optional[str] = None,
        model: Optional[str] = None,
        cwd: Optional[Union[str, Path]] = None,
        remote_control: bool = False,
        dangerously_skip_permissions: bool = True,
        extra_args: Optional[List[str]] = None,
        agy_binary: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        custom_command: Optional[List[str]] = None,
    ):
        self.agent_name = agent_name
        self.model = model
        self.cwd = Path(cwd).resolve() if cwd else None
        self.remote_control = remote_control
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.extra_args = list(extra_args) if extra_args else []
        self.agy_binary = agy_binary or shutil.which("agy") or "agy"
        self.env = dict(env) if env is not None else dict(os.environ)
        self.env.setdefault("GIT_TERMINAL_PROMPT", "0")
        self.custom_command = list(custom_command) if custom_command is not None else None

        self.proc: Optional[subprocess.Popen] = None
        self.conversation_id: Optional[str] = None
        self.remote_control_url: Optional[str] = None
        self.init_data: Dict[str, Any] = {}
        self._is_closed = False
        self._stdout_queue: queue.Queue = queue.Queue(maxsize=0)
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_lines: deque = deque(maxlen=2000)
        self._stderr_thread: Optional[threading.Thread] = None

    def is_alive(self) -> bool:
        """Return True if the underlying process is currently running."""
        return self.proc is not None and self.proc.poll() is None

    def _drain_stdout(self) -> None:
        """Continuously read stdout lines and enqueue them to avoid blocking."""
        if not self.proc or not self.proc.stdout:
            return
        try:
            while not self._is_closed:
                line = self.proc.stdout.readline()
                if not line:
                    break
                if not isinstance(line, str):
                    break
                while not self._is_closed:
                    try:
                        self._stdout_queue.put(line, timeout=0.2)
                        break
                    except queue.Full:
                        continue
        except (ValueError, OSError, StopIteration):
            pass
        except Exception as e:
            logger.debug(f"Exception reading agy stdout: {e}")
        finally:
            try:
                self._stdout_queue.put(None, timeout=0.5)
            except Exception:
                pass

    def _drain_stderr(self) -> None:
        """Continuously drain stderr in the background to prevent pipe buffer exhaustion."""
        if not self.proc or not self.proc.stderr:
            return
        try:
            while not self._is_closed:
                line = self.proc.stderr.readline()
                if not line:
                    break
                if not isinstance(line, str):
                    break
                self._stderr_lines.append(line)
                logger.debug(f"[agy stderr] {line.rstrip()}")
        except (ValueError, OSError, StopIteration):
            pass
        except Exception as e:
            logger.debug(f"Exception reading agy stderr: {e}")

    def get_stderr(self) -> str:
        """Return accumulated stderr lines from the subprocess."""
        if self.proc and self.proc.poll() is not None and self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.1)

        stderr_text = "".join(self._stderr_lines)
        if not stderr_text and self.proc and self.proc.stderr:
            # Guard fallback read: only read directly if background drain thread is not running
            # and process has terminated, to prevent racing or blocking indefinitely while alive.
            thread_stopped = self._stderr_thread is None or not self._stderr_thread.is_alive()
            if thread_stopped and self.proc.poll() is not None:
                try:
                    if hasattr(self.proc.stderr, "read") and callable(self.proc.stderr.read):
                        res = self.proc.stderr.read()
                        if isinstance(res, str):
                            stderr_text = res
                except Exception:
                    pass
        return stderr_text

    def _start_reader_threads(self) -> None:
        """Ensure stdout and stderr background reader threads are running."""
        if self._stdout_thread is None or not self._stdout_thread.is_alive():
            if self.proc and self.proc.stdout:
                self._stdout_thread = threading.Thread(
                    target=self._drain_stdout,
                    name="StreamSession-stdout-reader",
                    daemon=True,
                )
                self._stdout_thread.start()

        if self._stderr_thread is None or not self._stderr_thread.is_alive():
            if self.proc and self.proc.stderr:
                self._stderr_thread = threading.Thread(
                    target=self._drain_stderr,
                    name="StreamSession-stderr-reader",
                    daemon=True,
                )
                self._stderr_thread.start()

    def start(self, timeout: float = 30.0) -> str:
        """
        Spawn the agy process, establish NDJSON pipes, and await the 'init' event.

        :param timeout: Maximum seconds to wait for initial handshake.
        :return: Initialized conversation_id string.
        :raises SupervisorError: If initialization fails or process exits unexpectedly.
        """
        if self.is_alive():
            return self.conversation_id or ""

        self._is_closed = False
        self._stdout_queue = queue.Queue(maxsize=0)
        self._stderr_lines.clear()
        self._stdout_thread = None
        self._stderr_thread = None

        if self.custom_command:
            cmd = list(self.custom_command)
        else:
            cmd = [
                self.agy_binary,
                "--input-format", "stream-json",
                "--output-format", "stream-json",
            ]

            if self.dangerously_skip_permissions:
                cmd.append("--dangerously-skip-permissions")

            if self.remote_control:
                cmd.append("--remote-control")

            if self.agent_name:
                cmd.extend(["--agent", self.agent_name])

            if self.model:
                cmd.extend(["--model", self.model])

            if self.extra_args:
                cmd.extend(self.extra_args)

        logger.debug(f"Starting StreamSession with command: {' '.join(cmd)}")

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd) if self.cwd else None,
                env=self.env,
                text=True,
                bufsize=1,
            )
        except Exception as e:
            raise SupervisorError(f"Failed to spawn agy process: {e}") from e

        self._start_reader_threads()

        # Read the first line from stdout which must be the 'init' event
        start_time = time.time()
        init_line = None

        while True:
            remaining = timeout - (time.time() - start_time)
            if remaining <= 0:
                break

            if self.proc.poll() is not None and self._stdout_queue.empty():
                stderr_output = self.get_stderr()
                self.close()
                raise SupervisorError(
                    f"agy process exited prematurely with code {self.proc.returncode}. stderr: {stderr_output.strip()}"
                )

            try:
                line = self._stdout_queue.get(timeout=min(remaining, 0.1))
            except queue.Empty:
                if self.proc.poll() is not None and self._stdout_queue.empty():
                    stderr_output = self.get_stderr()
                    self.close()
                    raise SupervisorError(
                        f"agy process exited prematurely with code {self.proc.returncode}. stderr: {stderr_output.strip()}"
                    )
                continue

            if line is None:
                self.proc.poll()
                stderr_output = self.get_stderr()
                self.close()
                raise SupervisorError(
                    f"agy process exited prematurely with code {self.proc.returncode}. stderr: {stderr_output.strip()}"
                )

            stripped = line.strip()
            if stripped:
                init_line = stripped
                break

        if not init_line:
            self.close()
            raise SupervisorTimeoutError(f"Timed out waiting for 'init' handshake after {timeout}s.")

        try:
            event = json.loads(init_line)
        except json.JSONDecodeError as e:
            self.close()
            raise SupervisorError(f"Malformed JSON in init handshake: {init_line}") from e

        if event.get("event") != "init":
            self.close()
            raise SupervisorError(f"Expected 'init' event, got: {event.get('event')}")

        self.conversation_id = event.get("conversation_id")
        self.init_data = event.get("init", {})
        self.remote_control_url = extract_remote_control_url(
            event=event,
            stderr_lines=self._stderr_lines,
            conversation_id=self.conversation_id,
            remote_control_enabled=self.remote_control,
        )
        if self.remote_control_url:
            logger.info(
                f"StreamSession established successfully (conversation_id={self.conversation_id}, remote_control_url={self.remote_control_url})"
            )
        else:
            logger.info(f"StreamSession established successfully (conversation_id={self.conversation_id})")
        return self.conversation_id or ""

    def send_prompt(self, prompt: str) -> None:
        """
        Send a user prompt event to the active session over stdin.

        :param prompt: User message or goal instruction.
        :raises SupervisorError: If process is not alive or stdin fails.
        """
        if not self.is_alive() or self.proc is None or self.proc.stdin is None:
            raise SupervisorError("Cannot send prompt: StreamSession process is not running.")

        payload = {
            "event": "user",
            "message": {
                "content": [
                    {"type": "text", "text": prompt}
                ]
            },
        }

        try:
            line = json.dumps(payload) + "\n"
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
            logger.debug(f"Dispatched prompt to conversation {self.conversation_id}: {prompt[:80]}...")
        except Exception as e:
            raise SupervisorError(f"Failed writing prompt to agy stdin: {e}") from e

    def _build_result(
        self,
        res_data: Dict[str, Any],
        elapsed: float,
        events: List[Dict[str, Any]],
        thoughts: Optional[List[str]] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> SupervisorResult:
        """Construct SupervisorResult with explicit fallbacks for null or missing metrics."""
        status = res_data.get("status")
        if status is None:
            status = "SUCCESS"

        response = res_data.get("response")
        if response is None:
            response = ""

        duration = res_data.get("duration_seconds")
        if duration is None:
            duration = elapsed

        turns = res_data.get("num_turns")
        if turns is None:
            turns = 1

        usage = res_data.get("usage")
        if usage is None:
            usage = {}

        return SupervisorResult(
            conversation_id=self.conversation_id,
            remote_control_url=self.remote_control_url,
            status=status,
            response=response,
            duration_seconds=float(duration),
            num_turns=int(turns),
            usage=usage,
            error=res_data.get("error"),
            events=events,
            thoughts=list(thoughts) if thoughts else [],
            tool_calls=list(tool_calls) if tool_calls else [],
        )

    def receive_turn(
        self,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_thought: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        idle_timeout: Optional[float] = None,
        max_duration: Optional[float] = 1800.0,
    ) -> SupervisorResult:
        """
        Stream events from stdout until a 'result' event signals turn completion.

        :param timeout: Optional maximum seconds to wait for turn completion (overrides max_duration if smaller).
        :param on_event: Optional callback invoked for every parsed NDJSON event.
        :param on_thought: Optional callback invoked when the model emits thinking/thought content.
        :param on_tool_call: Optional callback invoked when a tool call occurs.
        :param on_chunk: Optional callback invoked when streaming text tokens arrive.
        :param on_step: Optional callback invoked for every step_update payload.
        :param idle_timeout: Optional watchdog timeout triggering if no events arrive for X seconds.
        :param max_duration: Maximum execution ceiling (default: 1800.0s / 30m) preventing indefinite loops.
        :return: SupervisorResult summarizing the turn with captured thoughts and tool calls.
        :raises SupervisorTimeoutError: If execution ceiling or idle watchdog expires before 'result'.
        :raises SupervisorError: If process terminates unexpectedly or emits malformed data.
        """
        if not self.is_alive() or self.proc is None or self.proc.stdout is None:
            raise SupervisorError("Cannot receive turn: StreamSession process is not running.")

        self._start_reader_threads()

        events: List[Dict[str, Any]] = []
        thoughts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []

        start_time = time.time()
        last_event_time = time.time()

        effective_limit = timeout
        if effective_limit is None:
            effective_limit = max_duration
        elif max_duration is not None:
            effective_limit = min(effective_limit, max_duration)

        while True:
            now = time.time()
            if effective_limit is not None and (now - start_time) > effective_limit:
                raise SupervisorTimeoutError(f"Turn execution exceeded timeout ceiling of {effective_limit}s.")

            if idle_timeout is not None and (now - last_event_time) > idle_timeout:
                raise SupervisorTimeoutError(f"Watchdog timeout: agent idle for {idle_timeout}s without emitting events.")

            wait_timeout = 0.1
            if effective_limit is not None:
                remaining = effective_limit - (now - start_time)
                if remaining <= 0:
                    raise SupervisorTimeoutError(f"Turn execution exceeded timeout ceiling of {effective_limit}s.")
                wait_timeout = min(wait_timeout, remaining)

            if idle_timeout is not None:
                idle_remaining = idle_timeout - (now - last_event_time)
                if idle_remaining <= 0:
                    raise SupervisorTimeoutError(f"Watchdog timeout: agent idle for {idle_timeout}s without emitting events.")
                wait_timeout = min(wait_timeout, idle_remaining)

            wait_timeout = max(0.0, wait_timeout)

            try:
                line = self._stdout_queue.get(timeout=wait_timeout)
            except queue.Empty:
                now = time.time()
                if effective_limit is not None and (now - start_time) > effective_limit:
                    raise SupervisorTimeoutError(f"Turn execution exceeded timeout ceiling of {effective_limit}s.")
                if idle_timeout is not None and (now - last_event_time) > idle_timeout:
                    raise SupervisorTimeoutError(f"Watchdog timeout: agent idle for {idle_timeout}s without emitting events.")
                if self.proc.poll() is not None and self._stdout_queue.empty():
                    stderr_output = self.get_stderr()
                    self.close()
                    raise SupervisorError(
                        f"agy process exited prematurely with code {self.proc.returncode}. stderr: {stderr_output.strip()}"
                    )
                continue

            if line is None:
                self.proc.poll()
                stderr_output = self.get_stderr()
                self.close()
                raise SupervisorError(
                    f"agy process exited prematurely with code {self.proc.returncode}. stderr: {stderr_output.strip()}"
                )

            stripped = line.strip()
            if not stripped:
                continue

            last_event_time = time.time()

            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as e:
                logger.warning(f"Unparseable stream-json output line: {stripped} ({e})")
                continue

            if event.get("event") == "init":
                if self.remote_control_url and not event.get("remote_control_url"):
                    event["remote_control_url"] = self.remote_control_url

            events.append(event)
            if on_event:
                try:
                    on_event(event)
                except Exception as cb_err:
                    logger.warning(f"Error in on_event callback: {cb_err}")

            if event.get("event") == "step_update":
                su = event.get("step_update") or event.get("step") or {}
                if on_step:
                    try:
                        on_step(su)
                    except Exception as s_err:
                        logger.warning(f"Error in on_step callback: {s_err}")

                step_type = su.get("step_type")
                if step_type == "tool":
                    tool_id = su.get("tool_call_id") or su.get("call_id") or su.get("id")
                    tool_name = su.get("tool_name") or ""
                    tool_info = su.get("tool_info") or {}
                    state = su.get("state")
                    duration = su.get("duration_seconds")

                    entry = {
                        "name": tool_name,
                        "info": tool_info,
                        "state": state,
                        "duration_seconds": duration,
                    }
                    if tool_id is not None:
                        entry["id"] = tool_id

                    existing = None
                    if tool_id is not None:
                        for item in tool_calls:
                            if item.get("id") == tool_id:
                                existing = item
                                break

                    if existing is not None:
                        if tool_name:
                            existing["name"] = tool_name
                        if tool_info:
                            existing["info"] = tool_info
                        if state is not None:
                            existing["state"] = state
                        if duration is not None:
                            existing["duration_seconds"] = duration
                    else:
                        tool_calls.append(entry)
                    if on_tool_call:
                        try:
                            on_tool_call(tool_name, tool_info)
                        except Exception as tc_err:
                            logger.warning(f"Error in on_tool_call callback: {tc_err}")

                elif step_type == "agent_response":
                    text_delta = su.get("text_delta")
                    if text_delta and on_chunk:
                        try:
                            on_chunk(text_delta)
                        except Exception as c_err:
                            logger.warning(f"Error in on_chunk callback: {c_err}")

                    thought = su.get("thought") or su.get("thinking")
                    if thought:
                        thoughts.append(str(thought))
                        if on_thought:
                            try:
                                on_thought(str(thought))
                            except Exception as th_err:
                                logger.warning(f"Error in on_thought callback: {th_err}")

            if event.get("event") == "result":
                res_data = event.get("result") or {}
                elapsed = time.time() - start_time
                return self._build_result(res_data, elapsed, events, thoughts=thoughts, tool_calls=tool_calls)

    def run_turn(
        self,
        prompt: str,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_thought: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        idle_timeout: Optional[float] = None,
        max_duration: Any = _UNSET,
        **kwargs: Any,
    ) -> SupervisorResult:
        """
        High-level helper: starts session if not already running, sends prompt,
        and returns the turn result.
        """
        if not self.is_alive():
            self.start()
        self.send_prompt(prompt)
        extra = dict(kwargs)
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        if max_duration is not _UNSET:
            extra["max_duration"] = max_duration
        return self.receive_turn(timeout=timeout, on_event=on_event, **extra)

    def run_goal(
        self,
        goal: str,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_thought: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        idle_timeout: Optional[float] = None,
        max_duration: Optional[float] = 1800.0,
    ) -> SupervisorResult:
        """
        Execute an autonomous multi-turn goal. Automatically prepends '/goal ' if needed.
        """
        clean_goal = goal.strip()
        prompt = clean_goal if clean_goal.startswith("/goal") else f"/goal {clean_goal}"
        extra = {}
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        extra["max_duration"] = max_duration
        return self.run_turn(
            prompt,
            timeout=timeout,
            on_event=on_event,
            **extra,
        )

    def close(self, timeout: float = 5.0) -> None:
        """
        Gracefully close stdin and terminate the process if needed.
        """
        if self._is_closed:
            return
        self._is_closed = True

        if self.proc is not None:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
            except Exception:
                pass

            try:
                if self.proc.stdout:
                    self.proc.stdout.close()
            except Exception:
                pass

            try:
                if self.proc.stderr:
                    self.proc.stderr.close()
            except Exception:
                pass

            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                logger.warning("StreamSession process did not exit gracefully; terminating...")
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=1.0)
                except Exception:
                    pass
            except Exception:
                pass

        if self._stdout_thread and self._stdout_thread.is_alive():
            self._stdout_thread.join(timeout=0.2)
        if self._stderr_thread and self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=0.2)

    def __enter__(self) -> "StreamSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def run_stream_turn(
    prompt: str,
    agent_name: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[Union[str, Path]] = None,
    remote_control: bool = False,
    dangerously_skip_permissions: bool = True,
    timeout: Optional[float] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_thought: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
    idle_timeout: Optional[float] = None,
    max_duration: Any = _UNSET,
    extra_args: Optional[List[str]] = None,
    agy_binary: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
    **kwargs: Any,
) -> SupervisorResult:
    """
    Convenience function to execute a single turn using StreamSession with full lifecycle management.
    """
    with StreamSession(
        agent_name=agent_name,
        model=model,
        cwd=cwd,
        remote_control=remote_control,
        dangerously_skip_permissions=dangerously_skip_permissions,
        extra_args=extra_args,
        agy_binary=agy_binary,
        env=env,
    ) as session:
        extra = dict(kwargs)
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        if max_duration is not _UNSET:
            extra["max_duration"] = max_duration
        return session.run_turn(prompt, timeout=timeout, on_event=on_event, **extra)


def run_goal_turn(
    goal: str,
    agent_name: Optional[str] = None,
    model: Optional[str] = None,
    cwd: Optional[Union[str, Path]] = None,
    remote_control: bool = False,
    dangerously_skip_permissions: bool = True,
    timeout: Optional[float] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_thought: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
    idle_timeout: Optional[float] = None,
    max_duration: Optional[float] = 1800.0,
    extra_args: Optional[List[str]] = None,
    agy_binary: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> SupervisorResult:
    """
    Convenience function to execute an autonomous /goal using StreamSession.
    """
    with StreamSession(
        agent_name=agent_name,
        model=model,
        cwd=cwd,
        remote_control=remote_control,
        dangerously_skip_permissions=dangerously_skip_permissions,
        extra_args=extra_args,
        agy_binary=agy_binary,
        env=env,
    ) as session:
        extra = {}
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        extra["max_duration"] = max_duration
        return session.run_goal(
            goal,
            timeout=timeout,
            on_event=on_event,
            **extra,
        )


def clean_workspace_dir(path: Optional[Union[Path, str]], docker_binary: str = "docker") -> bool:
    """
    Robustly removes a workspace directory, handling root-owned files or permission errors
    by adjusting file permissions and falling back to a docker rm helper if needed.
    """
    if path is None:
        return True
    p = Path(path)
    if not p.exists():
        return True

    # Ensure all directories and files in tree are writable before deletion
    try:
        os.chmod(str(p), 0o777)
        for root, dirs, files in os.walk(str(p)):
            for d in dirs:
                try:
                    os.chmod(os.path.join(root, d), 0o777)
                except Exception:
                    pass
            for f in files:
                try:
                    os.chmod(os.path.join(root, f), 0o777)
                except Exception:
                    pass
    except Exception:
        pass

    def _handle_remove_readonly(func, target_path, exc_info):
        try:
            parent = Path(target_path).parent
            if parent.exists():
                try:
                    os.chmod(parent, 0o777)
                except Exception:
                    pass
            os.chmod(target_path, 0o777)
            if func in (os.unlink, os.rmdir, os.remove):
                func(target_path)
        except Exception:
            pass

    try:
        shutil.rmtree(p, onerror=_handle_remove_readonly)
    except Exception:
        pass

    if not p.exists():
        return True

    # Fallback to helper container if files were created by root inside docker
    try:
        subprocess.run(
            [
                docker_binary,
                "run",
                "--rm",
                "-v",
                f"{p.resolve()}:/target",
                "alpine",
                "sh",
                "-c",
                "rm -rf /target/* /target/.[!.]* 2>/dev/null || rm -rf /target/* /target/.* 2>/dev/null || true",
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass

    return not p.exists()


def _extract_uri(res_item: Any) -> Optional[str]:
    """Safely extract folderUri from a resource item dict or nested gitFolder dict."""
    if not isinstance(res_item, dict):
        return None
    if isinstance(res_item.get("folderUri"), str):
        return res_item["folderUri"]
    gf = res_item.get("gitFolder")
    if isinstance(gf, dict) and isinstance(gf.get("folderUri"), str):
        return gf["folderUri"]
    return None


def ensure_default_project(
    projects_dir: Path,
    repo_path: Optional[Path] = None,
    name: str = DEFAULT_PROJECT_NAME,
) -> Tuple[str, str]:
    """
    Ensures a project definition exists for the given name in projects_dir,
    creating it if not present. Returns (project_id, project_name).
    """
    if not projects_dir.exists():
        try:
            projects_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    for p in projects_dir.glob("*.json"):
        if p.name in ("outside-of-project.json", "default-cli-project.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            p_name = str(data.get("name") or "")
            p_id = str(data.get("id") or p.stem)
            if p_name.strip().lower() == name.lower() or p_id == name:
                proj_res = data.get("projectResources")
                if not isinstance(proj_res, dict):
                    proj_res = {}
                    data["projectResources"] = proj_res
                res_list = proj_res.get("resources")
                if not isinstance(res_list, list):
                    res_list = []
                    proj_res["resources"] = res_list
                updated = False
                if repo_path:
                    target_uri = f"file://{repo_path.resolve()}"
                    has_uri = any(_extract_uri(r) == target_uri for r in res_list)
                    if not has_uri:
                        res_list.append({
                            "gitFolder": {
                                "folderUri": target_uri,
                                "defaultBranch": "main",
                            }
                        })
                        updated = True

                # Always ensure container workspace is registered for sandbox execution
                workspace_uri = "file:///workspace"
                has_workspace = any(_extract_uri(r) == workspace_uri for r in res_list)
                if not has_workspace:
                    res_list.append({
                        "gitFolder": {
                            "folderUri": workspace_uri,
                            "defaultBranch": "main",
                        }
                    })
                    updated = True

                if updated:
                    try:
                        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                return (p_id, p_name)
        except Exception:
            continue

    # Deterministic UUID for the project
    proj_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"graviton-{name.lower().replace(' ', '-')}"))
    proj_file = projects_dir / f"{proj_id}.json"
    resources = [
        {
            "gitFolder": {
                "folderUri": "file:///workspace",
                "defaultBranch": "main",
            }
        }
    ]
    if repo_path:
        target_uri = f"file://{repo_path.resolve()}"
        if target_uri != "file:///workspace":
            resources.append({
                "gitFolder": {
                    "folderUri": target_uri,
                    "defaultBranch": "main",
                }
            })
    proj_data = {
        "id": proj_id,
        "name": name,
        "projectResources": {"resources": resources},
        "settings": {},
        "isWorkspaceOnly": False,
    }
    try:
        proj_file.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")
        logger.info(f"Auto-created default project '{name}' ({proj_id}) at {proj_file}")
    except Exception as e:
        logger.debug(f"Could not auto-create project file {proj_file}: {e}")
    return (proj_id, name)


def ensure_workspace_trusted(cli_dir: Optional[Union[str, Path]] = None) -> None:
    """Ensure /workspace is explicitly in trustedWorkspaces in settings.json atomically."""
    base = Path(cli_dir) if cli_dir else (Path.home() / ".gemini" / "antigravity-cli")
    settings_file = base / "settings.json"
    try:
        base.mkdir(parents=True, exist_ok=True)
        if settings_file.is_file():
            try:
                data = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        tw = data.setdefault("trustedWorkspaces", [])
        if not isinstance(tw, list):
            tw = []
            data["trustedWorkspaces"] = tw
        if "/workspace" not in tw:
            tw.append("/workspace")
            tmp_file = settings_file.with_name(f".{settings_file.name}.tmp_{uuid.uuid4().hex}")
            tmp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_file.replace(settings_file)
    except Exception as e:
        logger.debug(f"Failed to ensure /workspace in settings.json: {e}")


def find_project_for_repo(
    repo_dir: Union[str, Path],
    config_dir: Optional[Union[str, Path]] = None,
    preferred_name_or_id: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    Looks in ~/.gemini/config/projects/ to find a project JSON file.
    Prefers:
    1. Explicit preferred_name_or_id if provided.
    2. ANTIGRAVITY_PROJECT or GRAVITON_PROJECT_ID environment variable if set.
    3. Project named DEFAULT_PROJECT_NAME ("Graviton Workers") if it exists, or auto-created.
    4. Project matching repo_dir.
    Returns (project_id, project_name) or None.
    """
    repo_path = Path(repo_dir).resolve()
    projects_dir = Path(config_dir) if config_dir else (Path.home() / ".gemini" / "config" / "projects")
    default_sys_dir = (Path.home() / ".gemini" / "config" / "projects").resolve()
    if not projects_dir.is_dir():
        if projects_dir.resolve() == default_sys_dir:
            return ensure_default_project(projects_dir, repo_path, DEFAULT_PROJECT_NAME)
        return None

    explicit_pref = (
        preferred_name_or_id
        or os.environ.get("ANTIGRAVITY_PROJECT")
        or os.environ.get("GRAVITON_PROJECT_ID")
    )
    if explicit_pref:
        explicit_pref = explicit_pref.strip()

    match_by_repo = None
    match_default_worker = None

    for p in projects_dir.glob("*.json"):
        if p.name in ("outside-of-project.json", "default-cli-project.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            project_id = str(data.get("id") or p.stem)
            project_name = str(data.get("name") or project_id)

            if explicit_pref and (explicit_pref == project_id or explicit_pref.lower() == project_name.lower()):
                return (project_id, project_name)

            if project_name.strip().lower() == DEFAULT_PROJECT_NAME.lower():
                match_default_worker = (project_id, project_name)

            proj_res = data.get("projectResources")
            resources = proj_res.get("resources", []) if isinstance(proj_res, dict) and isinstance(proj_res.get("resources"), list) else []
            for r in resources:
                folder_uri = _extract_uri(r)
                if folder_uri:
                    parsed_path = urllib.parse.unquote(urllib.parse.urlparse(folder_uri).path)
                    if parsed_path:
                        folder_path = Path(parsed_path).resolve()
                        if folder_path == repo_path or repo_path.is_relative_to(folder_path):
                            if not match_by_repo:
                                match_by_repo = (project_id, project_name)
        except Exception:
            continue

    if match_by_repo:
        return ensure_default_project(projects_dir, repo_path, match_by_repo[1])

    if match_default_worker:
        return ensure_default_project(projects_dir, repo_path, match_default_worker[1])

    # If config_dir is not custom (i.e. default system config dir), auto-ensure DEFAULT_PROJECT_NAME
    default_sys_dir = (Path.home() / ".gemini" / "config" / "projects").resolve()
    if projects_dir.resolve() == default_sys_dir:
        return ensure_default_project(projects_dir, repo_path, DEFAULT_PROJECT_NAME)

    return None


def _parse_git_repo_info(repo_path: Path) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (repo_name, git_url, current_branch) for a git repository directory.
    """
    repo_name, git_url, branch = None, None, None
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            git_url = res.stdout.strip()
            m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", git_url)
            if m:
                repo_name = m.group(1)
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "branch", "--show-current"],
            capture_output=True, text=True, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            branch = res.stdout.strip()
    except Exception:
        pass

    return repo_name, git_url, branch


def _encode_varint(val: int) -> bytes:
    if val < 0:
        val &= 0xffffffffffffffff
    out = []
    while True:
        b = val & 0x7f
        val >>= 7
        if val:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _decode_varint(data: bytes, pos: int) -> Tuple[int, int]:
    res = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        res |= (b & 0x7f) << shift
        if not (b & 0x80):
            break
        shift += 7
    return res, pos


def _encode_field(field_num: int, wire_type: int, data: Union[int, bytes, str]) -> bytes:
    tag = (field_num << 3) | wire_type
    if wire_type == 0:
        return _encode_varint(tag) + _encode_varint(int(data))
    elif wire_type == 2:
        b_data = data.encode("utf-8") if isinstance(data, str) else data
        return _encode_varint(tag) + _encode_varint(len(b_data)) + b_data
    elif wire_type == 1:
        if isinstance(data, int):
            b_data = struct.pack("<q", data)
        else:
            b_data = data.encode("utf-8") if isinstance(data, str) else data
        return _encode_varint(tag) + b_data
    elif wire_type == 5:
        if isinstance(data, int):
            b_data = struct.pack("<i", data)
        else:
            b_data = data.encode("utf-8") if isinstance(data, str) else data
        return _encode_varint(tag) + b_data
    raise ValueError(f"Unsupported wire type {wire_type}")


def _parse_fields(data: bytes) -> List[Tuple[int, int, Any]]:
    fields = []
    pos = 0
    length = len(data)
    while pos < length:
        try:
            tag, pos = _decode_varint(data, pos)
        except IndexError:
            break
        field_num = tag >> 3
        wire_type = tag & 7
        if wire_type == 0:
            val, pos = _decode_varint(data, pos)
            fields.append((field_num, wire_type, val))
        elif wire_type == 1:
            if pos + 8 > length:
                break
            val = data[pos:pos+8]
            pos += 8
            fields.append((field_num, wire_type, val))
        elif wire_type == 2:
            f_len, pos = _decode_varint(data, pos)
            if pos + f_len > length:
                break
            val = data[pos:pos+f_len]
            pos += f_len
            fields.append((field_num, wire_type, val))
        elif wire_type == 5:
            if pos + 4 > length:
                break
            val = data[pos:pos+4]
            pos += 4
            fields.append((field_num, wire_type, val))
        else:
            break
    return fields


def _build_workspace_proto(
    workspace_uri: str,
    repo_name: Optional[str] = None,
    git_url: Optional[str] = None,
    branch: Optional[str] = "main",
) -> bytes:
    out = _encode_field(1, 2, workspace_uri.encode("utf-8"))
    out += _encode_field(2, 2, workspace_uri.encode("utf-8"))
    if repo_name or git_url:
        git_info = b""
        if repo_name:
            git_info += _encode_field(1, 2, repo_name.encode("utf-8"))
        if git_url:
            git_info += _encode_field(2, 2, git_url.encode("utf-8"))
        out += _encode_field(3, 2, git_info)
    if branch:
        out += _encode_field(4, 2, branch.encode("utf-8"))
    return out


def _read_agyhub_entries(pb_data: bytes) -> List[Tuple[str, bytes]]:
    entries = []
    pos = 0
    while pos < len(pb_data):
        try:
            tag, pos = _decode_varint(pb_data, pos)
        except IndexError:
            break
        field_num = tag >> 3
        wire_type = tag & 7
        if field_num != 1 or wire_type != 2:
            break
        length, pos = _decode_varint(pb_data, pos)
        entry_bytes = pb_data[pos:pos+length]
        pos += length

        conv_id = None
        raw_summary = None
        for s_num, s_type, s_val in _parse_fields(entry_bytes):
            if s_num == 1 and isinstance(s_val, bytes):
                conv_id = s_val.decode("utf-8", errors="ignore")
            elif s_num == 2 and isinstance(s_val, bytes):
                raw_summary = s_val
        if conv_id and raw_summary:
            entries.append((conv_id, raw_summary))
    return entries


def _write_agyhub_entries(entries: List[Tuple[str, bytes]]) -> bytes:
    out = b""
    for cid, summary in entries:
        inner = _encode_field(1, 2, cid.encode("utf-8")) + _encode_field(2, 2, summary)
        out += _encode_field(1, 2, inner)
    return out


def sync_conversation_to_agyhub(
    conversation_id: str,
    repo_dir: Optional[Union[str, Path]] = None,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
    workspace_uri: Optional[str] = None,
    cli_dir: Optional[Union[str, Path]] = None,
    config_dir: Optional[Union[str, Path]] = None,
) -> bool:
    """
    Ensures a conversation is registered under the proper project in conversation_summaries.db
    and indexed in agyhub_summaries_proto.pb so that it appears in Antigravity Remote Control.
    """
    if not conversation_id or not conversation_id.strip():
        return False

    cid = conversation_id.strip()
    c_dir = Path(cli_dir) if cli_dir else (Path.home() / ".gemini" / "antigravity-cli")
    cfg_dir = Path(config_dir) if config_dir else (Path.home() / ".gemini" / "config")

    repo_path = Path(repo_dir).resolve() if repo_dir else None
    if repo_path and not workspace_uri:
        workspace_uri = f"file://{repo_path}"

    if repo_path and not project_id:
        resolved = find_project_for_repo(repo_path, config_dir=cfg_dir / "projects" if cfg_dir else None)
        if resolved:
            project_id = resolved[0]

    repo_name, git_url, curr_branch = _parse_git_repo_info(repo_path) if repo_path else (None, None, branch or "main")
    branch = branch or curr_branch or "main"

    ws_proto_bytes = _build_workspace_proto(workspace_uri, repo_name, git_url, branch) if workspace_uri else None

    db_path = c_dir / "conversation_summaries.db"
    if not db_path.exists():
        return False

    try:
        with sqlite3.connect(str(db_path), timeout=30.0) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT raw_summary, project_id, workspace_uris FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                return False

            raw_summary = row[0]
            curr_pid = row[1]
            curr_ws = row[2]

            target_pid = project_id or curr_pid
            target_ws = json.dumps([workspace_uri]) if workspace_uri else (curr_ws or json.dumps([]))

            # Parse and modify raw_summary proto
            fields = _parse_fields(raw_summary)
            new_f17 = b""
            for f_num, w_type, val in fields:
                if f_num == 17 and isinstance(val, bytes):
                    sub_fields = _parse_fields(val)
                    if ws_proto_bytes:
                        new_f17 += _encode_field(1, 2, ws_proto_bytes)
                    for s_num, s_type, s_val in sub_fields:
                        if s_num == 1 and ws_proto_bytes:
                            continue  # Replaced with ws_proto_bytes above
                        elif s_num == 18 and target_pid:
                            new_f17 += _encode_field(18, 2, target_pid.encode("utf-8"))
                        elif s_num == 7 and workspace_uri:
                            new_f17 += _encode_field(7, 2, workspace_uri.encode("utf-8"))
                        else:
                            new_f17 += _encode_field(s_num, s_type, s_val)
                    if target_pid and not any(s_num == 18 for s_num, _, _ in sub_fields) and not (18 in [s[0] for s in _parse_fields(new_f17)]):
                        new_f17 += _encode_field(18, 2, target_pid.encode("utf-8"))
                    if workspace_uri and not any(s_num == 7 for s_num, _, _ in sub_fields) and not (7 in [s[0] for s in _parse_fields(new_f17)]):
                        new_f17 += _encode_field(7, 2, workspace_uri.encode("utf-8"))
                    break

            if not new_f17:
                if ws_proto_bytes:
                    new_f17 += _encode_field(1, 2, ws_proto_bytes)
                new_f17 += _encode_field(6, 2, cid.encode("utf-8"))
                if workspace_uri:
                    new_f17 += _encode_field(7, 2, workspace_uri.encode("utf-8"))
                if target_pid:
                    new_f17 += _encode_field(18, 2, target_pid.encode("utf-8"))

            new_summary_bytes = b""
            for f_num, w_type, val in fields:
                if f_num == 17:
                    new_summary_bytes += _encode_field(17, 2, new_f17)
                elif f_num == 9 and ws_proto_bytes:
                    new_summary_bytes += _encode_field(9, 2, ws_proto_bytes)
                else:
                    new_summary_bytes += _encode_field(f_num, w_type, val)

            if ws_proto_bytes and not any(f[0] == 9 for f in fields):
                new_summary_bytes += _encode_field(9, 2, ws_proto_bytes)

            if not any(f[0] == 17 for f in fields):
                new_summary_bytes += _encode_field(17, 2, new_f17)

            cursor.execute(
                "UPDATE conversation_summaries SET project_id = ?, workspace_uris = ?, raw_summary = ? WHERE conversation_id = ?",
                (target_pid, target_ws, new_summary_bytes, cid),
            )
            conn.commit()
            try:
                db_path.chmod(0o644)
            except Exception:
                pass

        # Update conversation db trajectory_metadata_blob if present
        conv_db_path = c_dir / "conversations" / f"{cid}.db"
        if conv_db_path.exists():
            try:
                try:
                    conv_db_path.chmod(0o644)
                except Exception:
                    pass
                with sqlite3.connect(str(conv_db_path), timeout=30.0) as c_conn:
                    c_cur = c_conn.cursor()
                    c_cur.execute(
                        "UPDATE trajectory_metadata_blob SET data = ? WHERE id = 'main'",
                        (new_f17,),
                    )
                    c_conn.commit()
            except Exception as e:
                logger.debug(f"Failed to update trajectory_metadata_blob for {cid}: {e}")

        # Update agyhub_summaries_proto.pb and jetbox_summaries_proto.pb
        for proto_name in ("agyhub_summaries_proto.pb", "jetbox_summaries_proto.pb"):
            pb_path = c_dir / proto_name
            existing_entries = []
            if pb_path.exists():
                try:
                    existing_entries = _read_agyhub_entries(pb_path.read_bytes())
                except Exception as e:
                    logger.debug(f"Failed to read existing {proto_name}: {e}")

            filtered = [(entry_id, entry_sum) for entry_id, entry_sum in existing_entries if entry_id != cid]
            updated_entries = [(cid, new_summary_bytes)] + filtered
            encoded_hub_data = _write_agyhub_entries(updated_entries)

            tmp_path = pb_path.with_name(f".{pb_path.name}.tmp_{uuid.uuid4().hex}")
            tmp_path.write_bytes(encoded_hub_data)
            try:
                tmp_path.chmod(0o644)
            except Exception:
                pass
            tmp_path.replace(pb_path)
        logger.debug(f"Successfully synced conversation {cid} to agyhub.")
        return True
    except Exception as e:
        logger.warning(f"Error during sync_conversation_to_agyhub({cid}): {e}")
        return False


def to_ssh_url(url: str) -> str:
    """
    Convert an HTTPS GitHub URL to an SSH clone URL if applicable.
    Leaves SSH URLs or non-GitHub URLs untouched.

    Examples:
        https://github.com/owner/repo.git -> git@github.com:owner/repo.git
        https://github.com/owner/repo -> git@github.com:owner/repo.git
        git@github.com:owner/repo.git -> git@github.com:owner/repo.git
    """
    if not url:
        return url
    trimmed = url.strip()
    clean = trimmed.rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    m = re.match(r"^https://(?:[^@/]+@)?github\.com/([^/]+)/([^/]+)$", clean)
    if m:
        owner, repo = m.group(1), m.group(2)
        return f"git@github.com:{owner}/{repo}.git"
    return trimmed


def has_ssh_credentials(ssh_dir: Optional[Union[str, Path]] = None) -> bool:
    """
    Check if the host/environment has usable SSH credentials in ~/.ssh (or specified directory).
    Detects standard private key files or non-empty SSH config file.
    """
    path = Path(ssh_dir) if ssh_dir else (Path.home() / ".ssh")
    if not path.is_dir():
        return False
    try:
        for item in path.iterdir():
            if item.is_file() and item.stat().st_size > 0:
                name = item.name
                if name == "config":
                    return True
                if name.startswith("id_") and not name.endswith(".pub"):
                    return True
    except OSError:
        pass
    return False



class ContainerSupervisor:
    """
    Executes Antigravity StreamSession inside an isolated Docker container.

    Enforces strict security isolation:
    - Ephemeral workspace cloned locally under /tmp/graviton-workspaces/run-*
    - Container runs with --security-opt=no-new-privileges
    - Host ~/.gemini/antigravity-cli mounted to persist conversations and trajectories,
      with strict read-only overlays for host binaries, built-in skills, and credentials
    - Host SSH and GitHub CLI configs mounted read-only (:ro)
    - Automatically cleans up container and ephemeral directory on exit
    """

    def __init__(
        self,
        repo_dir: Union[str, Path],
        agent_name: Optional[str] = "code_reviewer",
        model: Optional[str] = None,
        image_name: Optional[str] = None,
        remote_control: bool = True,
        dangerously_skip_permissions: bool = True,
        extra_args: Optional[List[str]] = None,
        base_workspaces_dir: Union[str, Path] = "/tmp/graviton-workspaces",
        run_id: Optional[str] = None,
        default_branch: Optional[str] = None,
        git_user_name: Optional[str] = None,
        git_user_email: Optional[str] = None,
        github_token: Optional[str] = None,
        skills_dir: Optional[Union[str, Path]] = None,
        agents_dir: Optional[Union[str, Path]] = None,
        env: Optional[Dict[str, str]] = None,
        docker_binary: Optional[str] = None,
        agy_binary: Optional[str] = None,
        cache_dir: Optional[Union[str, Path]] = None,
        project_id: Optional[str] = None,
        user: Optional[str] = None,
        container_home: Optional[str] = None,
        cli_dir: Optional[Union[str, Path]] = None,
        ssh_dir: Optional[Union[str, Path]] = None,
    ):
        self.repo_dir = Path(repo_dir).resolve()
        self.agent_name = agent_name
        self.model = model
        self.image_name = image_name or os.environ.get("ANTIGRAVITY_IMAGE", "antigravity-agent:latest")
        self.remote_control = remote_control
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.extra_args = list(extra_args) if extra_args else []
        self.base_workspaces_dir = Path(base_workspaces_dir)
        self.run_id = run_id or f"{int(time.time())}_{os.urandom(4).hex()}"
        self.default_branch = default_branch
        self.temp_workspace = self.base_workspaces_dir / f"run-{self.run_id}"
        self.container_name = f"graviton-stream-run-{self.run_id}"
        self.git_user_name = git_user_name
        self.git_user_email = git_user_email
        self.github_token = github_token
        self.ssh_dir = Path(ssh_dir).resolve() if ssh_dir else (Path.home() / ".ssh")
        self.skills_dir = Path(skills_dir).resolve() if skills_dir else None
        self.agents_dir = Path(agents_dir).resolve() if agents_dir else None
        self.env = dict(env) if env is not None else {}
        self.docker_binary = docker_binary or shutil.which("docker") or "docker"
        self.agy_binary = agy_binary
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None

        if user is not None:
            self.user: Optional[str] = user
        else:
            host_uid = os.getuid() if hasattr(os, "getuid") else 0
            host_gid = os.getgid() if hasattr(os, "getgid") else 0
            self.user = f"{host_uid}:{host_gid}" if host_uid != 0 else None

        if container_home is not None:
            self.container_home: str = container_home
        elif self.user and self.user.split(":")[0] not in ("0", "root"):
            self.container_home = "/home/ubuntu"
        else:
            self.container_home = "/root"

        raw_project_id = (
            project_id
            or os.environ.get("ANTIGRAVITY_PROJECT")
            or os.environ.get("GRAVITON_PROJECT_ID")
        )
        resolved = find_project_for_repo(self.repo_dir, preferred_name_or_id=raw_project_id)
        if resolved:
            self.project_id = resolved[0]
        else:
            self.project_id = raw_project_id or DEFAULT_PROJECT_NAME

        self.cli_dir = Path(cli_dir).resolve() if cli_dir else None
        ensure_workspace_trusted(self.cli_dir)

        self.session: Optional[StreamSession] = None
        self.conversation_id: Optional[str] = None
        self.remote_control_url: Optional[str] = None
        self._workspace_prepared = False
        self._is_cleaned_up = False

    def is_alive(self) -> bool:
        """Return True if the underlying container session is currently active."""
        return self.session is not None and self.session.is_alive()

    def get_stderr(self) -> str:
        """Return accumulated stderr from the container session."""
        return self.session.get_stderr() if self.session else ""

    def _resolve_github_token(self) -> Optional[str]:
        """Resolve GitHub token from supervisor property, environment, or gh auth token."""
        if self.github_token:
            return self.github_token
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            return token
        try:
            res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
        return None

    def prepare_workspace(self, branch: Optional[str] = None) -> Path:
        """
        Create isolated ephemeral workspace directory and populate it from cache or local clone.
        """
        if self._workspace_prepared and self.temp_workspace.exists():
            return self.temp_workspace

        clean_workspace_dir(self.temp_workspace, docker_binary=self.docker_binary)
        self.temp_workspace.parent.mkdir(parents=True, exist_ok=True)

        if self.cache_dir and self.cache_dir.is_dir():
            shutil.copytree(self.cache_dir, self.temp_workspace, dirs_exist_ok=True)
            # Ensure origin URL uses SSH if SSH credentials are available
            origin_res = subprocess.run(
                ["git", "-C", str(self.temp_workspace), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            origin_url = origin_res.stdout.strip()
            if origin_url and has_ssh_credentials(self.ssh_dir):
                ssh_url = to_ssh_url(origin_url)
                if ssh_url != origin_url:
                    subprocess.run(
                        ["git", "-C", str(self.temp_workspace), "remote", "set-url", "origin", ssh_url],
                        capture_output=True,
                        check=False,
                    )
        else:
            # Fast local git clone to ensure isolated .git index and working copy
            clone_cmd = ["git", "clone", "--local", str(self.repo_dir), str(self.temp_workspace)]
            res = subprocess.run(clone_cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
                clean_workspace_dir(self.temp_workspace, docker_binary=self.docker_binary)
                shutil.copytree(self.repo_dir, self.temp_workspace, dirs_exist_ok=True)

            # Restore original remote origin URL
            origin_res = subprocess.run(
                ["git", "-C", str(self.repo_dir), "remote", "get-url", "origin"],
                capture_output=True,
                text=True,
                check=False,
            )
            origin_url = origin_res.stdout.strip()
            if origin_url:
                if has_ssh_credentials(self.ssh_dir):
                    origin_url = to_ssh_url(origin_url)
                subprocess.run(
                    ["git", "-C", str(self.temp_workspace), "remote", "set-url", "origin", origin_url],
                    capture_output=True,
                    check=False,
                )
                subprocess.run(
                    ["git", "-C", str(self.temp_workspace), "fetch", "origin"],
                    capture_output=True,
                    check=False,
                )

            # Determine target branch
            target_branch = branch or self.default_branch
            if not target_branch:
                branch_res = subprocess.run(
                    ["git", "-C", str(self.temp_workspace), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                cur = branch_res.stdout.strip()
                target_branch = cur if cur and cur != "HEAD" else "main"

            subprocess.run(
                ["git", "-C", str(self.temp_workspace), "checkout", target_branch],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["git", "-C", str(self.temp_workspace), "reset", "--hard", f"origin/{target_branch}"],
                capture_output=True,
                check=False,
            )

        # Configure fallback authentication with GitHub token if available
        token = self._resolve_github_token()
        if token:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.temp_workspace),
                    "config",
                    f"url.https://x-access-token:{token}@github.com/.insteadOf",
                    "https://github.com/",
                ],
                capture_output=True,
                check=False,
            )

        # Configure pre-commit hook if present
        hook_path = self.temp_workspace / ".githooks" / "pre-commit"
        if hook_path.is_file():
            try:
                hook_path.chmod(hook_path.stat().st_mode | 0o111)
                subprocess.run(
                    ["git", "-C", str(self.temp_workspace), "config", "core.hooksPath", ".githooks"],
                    capture_output=True,
                    check=False,
                )
            except Exception as e:
                logger.debug(f"Failed to configure githooks: {e}")

        self._workspace_prepared = True
        return self.temp_workspace

    def build_docker_command(self, agy_args: Optional[List[str]] = None) -> List[str]:
        """
        Construct the docker run command with full security sandboxing, volume mounts,
        and stream-json agy flags.
        """
        cmd = [
            self.docker_binary,
            "run",
            "-i",
            "--name",
            self.container_name,
            "--security-opt=no-new-privileges",
        ]
        if self.user:
            cmd.extend(["--user", self.user])
        cmd.extend(["-e", f"HOME={self.container_home}"])

        # Ephemeral workspace mount
        cmd.extend(["-v", f"{self.temp_workspace.resolve()}:/workspace", "-w", "/workspace"])

        # Host agy binary mount
        host_agy = self.agy_binary or shutil.which("agy")
        if host_agy and Path(host_agy).exists():
            cmd.extend(["-v", f"{Path(host_agy).resolve()}:/usr/local/bin/agy:ro"])

        # Config directory tmpfs overlay
        cmd.extend(["--tmpfs", f"{self.container_home}/.gemini/config:rw,exec"])

        # Skills directory mount
        skills_path = self.skills_dir
        if not skills_path:
            plugin_skills = self.repo_dir / "plugin" / "skills"
            if plugin_skills.is_dir():
                skills_path = plugin_skills
            else:
                repo_skills = self.repo_dir / "skills"
                if repo_skills.is_dir():
                    skills_path = repo_skills
        if skills_path and skills_path.is_dir():
            cmd.extend(["-v", f"{skills_path.resolve()}:{self.container_home}/.gemini/config/skills:ro"])

        # Agents directory mount
        agents_path = self.agents_dir
        if not agents_path:
            plugin_agents = self.repo_dir / "plugin" / "agents"
            if plugin_agents.is_dir():
                agents_path = plugin_agents
            else:
                repo_agents = self.repo_dir / "agents"
                if repo_agents.is_dir():
                    agents_path = repo_agents
        if agents_path and agents_path.is_dir():
            cmd.extend(["-v", f"{agents_path.resolve()}:{self.container_home}/.gemini/config/agents:ro"])

        # SSH credentials mount
        ssh_dir = self.ssh_dir if self.ssh_dir else (Path.home() / ".ssh")
        if ssh_dir.is_dir():
            cmd.extend(["-v", f"{ssh_dir.resolve()}:{self.container_home}/.ssh:ro"])

        # GitHub CLI config mount
        gh_config = Path.home() / ".config" / "gh"
        if gh_config.is_dir():
            cmd.extend(["-v", f"{gh_config.resolve()}:{self.container_home}/.config/gh:ro"])

        # Mount Antigravity CLI directory to persist conversations, summaries, and brain
        # while keeping credential files and builtins read-only, and scratch isolated.
        # Note: 'bin' must remain writable because agy dynamically writes its agentapi
        # execution helper into ~/.gemini/antigravity-cli/bin/agentapi.
        cli_dir = self.cli_dir if self.cli_dir else (Path.home() / ".gemini" / "antigravity-cli")
        if cli_dir.is_dir():
            cmd.extend([
                "-v", f"{cli_dir.resolve()}:{self.container_home}/.gemini/antigravity-cli",
                "--tmpfs", f"{self.container_home}/.gemini/antigravity-cli/scratch:rw,exec",
            ])
            for ro_sub in ["builtin", "updater"]:
                sub_target = cli_dir / ro_sub
                if sub_target.is_dir():
                    cmd.extend(["-v", f"{sub_target.resolve()}:{self.container_home}/.gemini/antigravity-cli/{ro_sub}:ro"])
            for cred_file in [
                "antigravity-oauth-token",
                "token.json",
                "settings.json",
                "antigravity_state.pbtxt",
                "jetski_state.pbtxt",
                "installation_id",
            ]:
                target = cli_dir / cred_file
                if target.is_file():
                    cmd.extend(["-v", f"{target.resolve()}:{self.container_home}/.gemini/antigravity-cli/{cred_file}:ro"])

        # Mount host config.json for Antigravity Remote Control identification
        gemini_config_file = Path.home() / ".gemini" / "config" / "config.json"
        if gemini_config_file.is_file():
            cmd.extend(["-v", f"{gemini_config_file.resolve()}:{self.container_home}/.gemini/config/config.json:ro"])

        # Mount host projects directory for project definitions and workspace scoping
        gemini_projects_dir = Path.home() / ".gemini" / "config" / "projects"
        if gemini_projects_dir.is_dir():
            cmd.extend(["-v", f"{gemini_projects_dir.resolve()}:{self.container_home}/.gemini/config/projects:ro"])

        # Environment variables
        github_token = self._resolve_github_token()
        if github_token:
            cmd.extend(["-e", f"GITHUB_TOKEN={github_token}"])

        git_user = self.git_user_name or os.environ.get("GIT_AUTHOR_NAME")
        if not git_user:
            try:
                res = subprocess.run(
                    ["git", "-C", str(self.repo_dir), "config", "user.name"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                git_user = res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else "Graviton Bot"
            except Exception:
                git_user = "Graviton Bot"

        git_email = self.git_user_email or os.environ.get("GIT_AUTHOR_EMAIL")
        if not git_email:
            try:
                res = subprocess.run(
                    ["git", "-C", str(self.repo_dir), "config", "user.email"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                git_email = res.stdout.strip() if res.returncode == 0 and res.stdout.strip() else "graviton-bot@users.noreply.github.com"
            except Exception:
                git_email = "graviton-bot@users.noreply.github.com"

        cmd.extend([
            "-e", f"GIT_AUTHOR_NAME={git_user}",
            "-e", f"GIT_AUTHOR_EMAIL={git_email}",
            "-e", f"GIT_COMMITTER_NAME={git_user}",
            "-e", f"GIT_COMMITTER_EMAIL={git_email}",
            "-e", "GIT_TERMINAL_PROMPT=0",
        ])

        target_model = self.model or os.environ.get("ANTIGRAVITY_MODEL") or os.environ.get("MODEL_NAME")
        if target_model:
            cmd.extend([
                "-e", f"ANTIGRAVITY_MODEL={target_model}",
                "-e", f"MODEL_NAME={target_model}",
            ])

        quota_pool = os.environ.get("ANTIGRAVITY_QUOTA_POOL")
        if quota_pool:
            cmd.extend(["-e", f"ANTIGRAVITY_QUOTA_POOL={quota_pool}"])

        instance_name = os.environ.get("ANTIGRAVITY_INSTANCE_NAME") or get_remote_control_instance_name()
        if instance_name:
            cmd.extend(["-e", f"ANTIGRAVITY_INSTANCE_NAME={instance_name}"])

        for k, v in self.env.items():
            cmd.extend(["-e", f"{k}={v}"])

        # Container Image
        cmd.append(self.image_name)

        # Container Inner Command
        if agy_args is not None:
            cmd.extend(agy_args)
        else:
            inner_cmd = [
                "agy",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
            ]
            if self.dangerously_skip_permissions:
                inner_cmd.append("--dangerously-skip-permissions")
            if self.remote_control:
                inner_cmd.append("--remote-control")
            # Note: Do not pass '--agent' in container mode. Passing '--agent' causes agy to enter
            # restricted subagent mode, which strips execution and file modification tools (run_command,
            # write_to_file, etc.). Persona and task instructions are provided via the goal/prompt and mounted skills.
            if target_model:
                inner_cmd.extend(["--model", target_model])
            if self.project_id and not any(arg == "--project" or arg.startswith("--project=") for arg in (self.extra_args or [])):
                inner_cmd.extend(["--project", self.project_id])
            if self.extra_args:
                inner_cmd.extend(self.extra_args)
            cmd.extend(inner_cmd)

        return cmd

    def start(self, timeout: float = 120.0, branch: Optional[str] = None) -> str:
        """
        Prepare the workspace, build the docker command, and start StreamSession.
        """
        if self.is_alive():
            return self.conversation_id or ""

        if not self._workspace_prepared:
            self.prepare_workspace(branch=branch or self.default_branch)

        # Remove any existing container sharing the name to avoid conflict errors
        try:
            subprocess.run(
                [self.docker_binary, "rm", "-f", self.container_name],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

        docker_cmd = self.build_docker_command()
        self.session = StreamSession(
            custom_command=docker_cmd,
            cwd=self.temp_workspace,
            env=dict(os.environ),
            remote_control=self.remote_control,
        )
        self.conversation_id = self.session.start(timeout=timeout)
        self.remote_control_url = getattr(self.session, "remote_control_url", None)
        if self.conversation_id:
            self.sync_agyhub()
        return self.conversation_id or ""

    def sync_agyhub(self) -> bool:
        """Syncs active conversation to agyhub_summaries_proto.pb and conversation_summaries.db."""
        if not self.conversation_id:
            return False
        try:
            return sync_conversation_to_agyhub(
                conversation_id=self.conversation_id,
                repo_dir=self.repo_dir,
                branch=self.default_branch,
                project_id=self.project_id,
                cli_dir=self.cli_dir,
            )
        except Exception as e:
            logger.debug(f"Failed to sync conversation to agyhub: {e}")
            return False

    def send_prompt(self, prompt: str) -> None:
        """Send prompt to the container session."""
        if not self.is_alive() or self.session is None:
            raise SupervisorError("Cannot send prompt: ContainerSupervisor session is not running.")
        self.session.send_prompt(prompt)

    def receive_turn(
        self,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_thought: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        idle_timeout: Optional[float] = None,
        max_duration: Any = _UNSET,
        **kwargs: Any,
    ) -> SupervisorResult:
        """Receive a turn response from the container session."""
        if not self.is_alive() or self.session is None:
            raise SupervisorError("Cannot receive turn: ContainerSupervisor session is not running.")
        extra = dict(kwargs)
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        if max_duration is not _UNSET:
            extra["max_duration"] = max_duration

        has_synced_stream = False

        def _intercept_event(evt: Dict[str, Any]) -> None:
            nonlocal has_synced_stream
            if not has_synced_stream and evt.get("event") == "step_update":
                try:
                    if self.sync_agyhub():
                        has_synced_stream = True
                except Exception as sync_err:
                    logger.debug(f"Failed initial streaming agyhub sync: {sync_err}")
            if on_event:
                on_event(evt)

        res = self.session.receive_turn(timeout=timeout, on_event=_intercept_event, **extra)
        if not getattr(res, "remote_control_url", None) and self.remote_control_url:
            res.remote_control_url = self.remote_control_url
        if self.conversation_id:
            self.sync_agyhub()
        return res

    def run_turn(
        self,
        prompt: str,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_thought: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        idle_timeout: Optional[float] = None,
        max_duration: Any = _UNSET,
        **kwargs: Any,
    ) -> SupervisorResult:
        """Execute a turn, starting the container session if not already running."""
        if not self.is_alive():
            self.start()
        self.send_prompt(prompt)
        extra = dict(kwargs)
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        if max_duration is not _UNSET:
            extra["max_duration"] = max_duration
        return self.receive_turn(timeout=timeout, on_event=on_event, **extra)

    def run_goal(
        self,
        goal: str,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_thought: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_chunk: Optional[Callable[[str], None]] = None,
        on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
        idle_timeout: Optional[float] = None,
        max_duration: Optional[float] = 1800.0,
    ) -> SupervisorResult:
        """Execute an autonomous /goal in the container session."""
        clean_goal = goal.strip()
        prompt = clean_goal if clean_goal.startswith("/goal") else f"/goal {clean_goal}"
        extra = {}
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        extra["max_duration"] = max_duration
        return self.run_turn(
            prompt,
            timeout=timeout,
            on_event=on_event,
            **extra,
        )

    def cleanup(self) -> None:
        """Terminate the StreamSession, remove the docker container, and wipe the workspace."""
        if self._is_cleaned_up:
            return
        self._is_cleaned_up = True

        if self.conversation_id:
            self.sync_agyhub()

        if self.session is not None:
            try:
                self.session.close()
            except Exception as e:
                logger.debug(f"Error closing StreamSession: {e}")
            self.session = None

        if self.container_name:
            try:
                subprocess.run(
                    [self.docker_binary, "rm", "-f", self.container_name],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            except Exception as e:
                logger.debug(f"Error removing container {self.container_name}: {e}")

        if self.temp_workspace and self.temp_workspace.exists():
            clean_workspace_dir(self.temp_workspace, docker_binary=self.docker_binary)

    def close(self) -> None:
        """Synonym for cleanup."""
        self.cleanup()

    def abort(self) -> None:
        """Abort execution and cleanup resources."""
        self.cleanup()

    def __enter__(self) -> "ContainerSupervisor":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()


def run_container_turn(
    prompt: str,
    repo_dir: Union[str, Path],
    agent_name: Optional[str] = "code_reviewer",
    model: Optional[str] = None,
    image_name: Optional[str] = None,
    remote_control: bool = True,
    dangerously_skip_permissions: bool = True,
    timeout: Optional[float] = None,
    branch: Optional[str] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_thought: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
    idle_timeout: Optional[float] = None,
    max_duration: Any = _UNSET,
    extra_args: Optional[List[str]] = None,
    base_workspaces_dir: Union[str, Path] = "/tmp/graviton-workspaces",
    run_id: Optional[str] = None,
    git_user_name: Optional[str] = None,
    git_user_email: Optional[str] = None,
    github_token: Optional[str] = None,
    skills_dir: Optional[Union[str, Path]] = None,
    agents_dir: Optional[Union[str, Path]] = None,
    env: Optional[Dict[str, str]] = None,
    docker_binary: Optional[str] = None,
    agy_binary: Optional[str] = None,
    cache_dir: Optional[Union[str, Path]] = None,
    **kwargs: Any,
) -> SupervisorResult:
    """
    Convenience function to execute a turn in an isolated container with full lifecycle management.
    """
    with ContainerSupervisor(
        repo_dir=repo_dir,
        agent_name=agent_name,
        model=model,
        image_name=image_name,
        remote_control=remote_control,
        dangerously_skip_permissions=dangerously_skip_permissions,
        extra_args=extra_args,
        base_workspaces_dir=base_workspaces_dir,
        run_id=run_id,
        default_branch=branch,
        git_user_name=git_user_name,
        git_user_email=git_user_email,
        github_token=github_token,
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        env=env,
        docker_binary=docker_binary,
        agy_binary=agy_binary,
        cache_dir=cache_dir,
    ) as supervisor:
        extra = dict(kwargs)
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        if max_duration is not _UNSET:
            extra["max_duration"] = max_duration
        return supervisor.run_turn(prompt, timeout=timeout, on_event=on_event, **extra)


def run_container_goal(
    goal: str,
    repo_dir: Union[str, Path],
    agent_name: Optional[str] = "code_reviewer",
    model: Optional[str] = None,
    image_name: Optional[str] = None,
    remote_control: bool = True,
    dangerously_skip_permissions: bool = True,
    timeout: Optional[float] = None,
    branch: Optional[str] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_thought: Optional[Callable[[str], None]] = None,
    on_tool_call: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    on_chunk: Optional[Callable[[str], None]] = None,
    on_step: Optional[Callable[[Dict[str, Any]], None]] = None,
    idle_timeout: Optional[float] = None,
    max_duration: Optional[float] = 1800.0,
    extra_args: Optional[List[str]] = None,
    base_workspaces_dir: Union[str, Path] = "/tmp/graviton-workspaces",
    run_id: Optional[str] = None,
    git_user_name: Optional[str] = None,
    git_user_email: Optional[str] = None,
    github_token: Optional[str] = None,
    skills_dir: Optional[Union[str, Path]] = None,
    agents_dir: Optional[Union[str, Path]] = None,
    env: Optional[Dict[str, str]] = None,
    docker_binary: Optional[str] = None,
    agy_binary: Optional[str] = None,
    cache_dir: Optional[Union[str, Path]] = None,
) -> SupervisorResult:
    """
    Convenience function to execute an autonomous /goal in an isolated container.
    """
    with ContainerSupervisor(
        repo_dir=repo_dir,
        agent_name=agent_name,
        model=model,
        image_name=image_name,
        remote_control=remote_control,
        dangerously_skip_permissions=dangerously_skip_permissions,
        extra_args=extra_args,
        base_workspaces_dir=base_workspaces_dir,
        run_id=run_id,
        default_branch=branch,
        git_user_name=git_user_name,
        git_user_email=git_user_email,
        github_token=github_token,
        skills_dir=skills_dir,
        agents_dir=agents_dir,
        env=env,
        docker_binary=docker_binary,
        agy_binary=agy_binary,
        cache_dir=cache_dir,
    ) as supervisor:
        extra = {}
        if on_thought is not None:
            extra["on_thought"] = on_thought
        if on_tool_call is not None:
            extra["on_tool_call"] = on_tool_call
        if on_chunk is not None:
            extra["on_chunk"] = on_chunk
        if on_step is not None:
            extra["on_step"] = on_step
        if idle_timeout is not None:
            extra["idle_timeout"] = idle_timeout
        extra["max_duration"] = max_duration
        return supervisor.run_goal(
            goal,
            timeout=timeout,
            on_event=on_event,
            **extra,
        )

