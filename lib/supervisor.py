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
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

logger = logging.getLogger("graviton.supervisor")

__all__ = [
    "ContainerSupervisor",
    "StreamSession",
    "SupervisorError",
    "SupervisorResult",
    "SupervisorTimeoutError",
    "clean_workspace_dir",
    "extract_remote_control_url",
    "get_remote_control_instance_name",
    "run_container_goal",
    "run_container_turn",
    "run_goal_turn",
    "run_stream_turn",
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
    3. Canonical conversation URL fallback (https://antigravity.google.com/c/<conversation_id>) if remote control is enabled.
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

    def _handle_remove_readonly(func, target_path, exc_info):
        try:
            os.chmod(target_path, 0o777)
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


class ContainerSupervisor:
    """
    Executes Antigravity StreamSession inside an isolated Docker container.

    Enforces strict security isolation:
    - Ephemeral workspace cloned locally under /tmp/graviton-workspaces/run-*
    - Container runs with --security-opt=no-new-privileges
    - Host ~/.gemini/antigravity-cli mounted with --tmpfs and read-only credential files
      to prevent accidental exposure or corruption of host conversations/brain cache
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
        self.skills_dir = Path(skills_dir).resolve() if skills_dir else None
        self.agents_dir = Path(agents_dir).resolve() if agents_dir else None
        self.env = dict(env) if env is not None else {}
        self.docker_binary = docker_binary or shutil.which("docker") or "docker"
        self.agy_binary = agy_binary
        self.cache_dir = Path(cache_dir).resolve() if cache_dir else None

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

    def prepare_workspace(self, branch: Optional[str] = None) -> Path:
        """
        Create isolated ephemeral workspace directory and populate it from cache or local clone.
        """
        if self._workspace_prepared and self.temp_workspace.exists():
            return self.temp_workspace

        self.temp_workspace.mkdir(parents=True, exist_ok=True)

        if self.cache_dir and self.cache_dir.is_dir():
            shutil.copytree(self.cache_dir, self.temp_workspace, dirs_exist_ok=True)
        else:
            # Fast local git clone to ensure isolated .git index and working copy
            clone_cmd = ["git", "clone", "--local", str(self.repo_dir), str(self.temp_workspace)]
            res = subprocess.run(clone_cmd, capture_output=True, text=True, check=False)
            if res.returncode != 0:
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

        # Ephemeral workspace mount
        cmd.extend(["-v", f"{self.temp_workspace.resolve()}:/workspace", "-w", "/workspace"])

        # Host agy binary mount
        host_agy = self.agy_binary or shutil.which("agy")
        if host_agy and Path(host_agy).exists():
            cmd.extend(["-v", f"{Path(host_agy).resolve()}:/usr/local/bin/agy:ro"])

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
            cmd.extend(["-v", f"{skills_path.resolve()}:/root/.gemini/config/skills:ro"])

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
            cmd.extend(["-v", f"{agents_path.resolve()}:/root/.gemini/config/agents:ro"])

        # SSH credentials mount
        ssh_dir = Path.home() / ".ssh"
        if ssh_dir.is_dir():
            cmd.extend(["-v", f"{ssh_dir.resolve()}:/root/.ssh:ro"])

        # GitHub CLI config mount
        gh_config = Path.home() / ".config" / "gh"
        if gh_config.is_dir():
            cmd.extend(["-v", f"{gh_config.resolve()}:/root/.config/gh:ro"])

        # Secure Antigravity CLI mount: tmpfs overlay for cache/conversations/brain/db
        # and read-only mounts for credentials
        cli_dir = Path.home() / ".gemini" / "antigravity-cli"
        if cli_dir.is_dir():
            cmd.extend([
                "--tmpfs", "/root/.gemini/antigravity-cli:rw,exec",
                "--tmpfs", "/root/.gemini/config:rw,exec",
            ])
            for cred_file in [
                "antigravity-oauth-token",
                "settings.json",
                "antigravity_state.pbtxt",
                "jetski_state.pbtxt",
                "installation_id",
            ]:
                target = cli_dir / cred_file
                if target.is_file():
                    cmd.extend(["-v", f"{target.resolve()}:/root/.gemini/antigravity-cli/{cred_file}:ro"])
        else:
            cmd.extend(["--tmpfs", "/root/.gemini/config:rw,exec"])

        # Environment variables
        github_token = self.github_token or os.environ.get("GITHUB_TOKEN")
        if not github_token:
            try:
                res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
                if res.returncode == 0 and res.stdout.strip():
                    github_token = res.stdout.strip()
            except Exception:
                pass
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
            if self.agent_name:
                inner_cmd.extend(["--agent", self.agent_name])
            if target_model:
                inner_cmd.extend(["--model", target_model])
            if self.extra_args:
                inner_cmd.extend(self.extra_args)
            cmd.extend(inner_cmd)

        return cmd

    def start(self, timeout: float = 60.0, branch: Optional[str] = None) -> str:
        """
        Prepare the workspace, build the docker command, and start StreamSession.
        """
        if self.is_alive():
            return self.conversation_id or ""

        if not self._workspace_prepared:
            self.prepare_workspace(branch=branch or self.default_branch)

        docker_cmd = self.build_docker_command()
        self.session = StreamSession(
            custom_command=docker_cmd,
            cwd=self.temp_workspace,
            env=dict(os.environ),
            remote_control=self.remote_control,
        )
        self.conversation_id = self.session.start(timeout=timeout)
        self.remote_control_url = getattr(self.session, "remote_control_url", None)
        return self.conversation_id or ""

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

        res = self.session.receive_turn(timeout=timeout, on_event=on_event, **extra)
        if not getattr(res, "remote_control_url", None) and self.remote_control_url:
            res.remote_control_url = self.remote_control_url
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

