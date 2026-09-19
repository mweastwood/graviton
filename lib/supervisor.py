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
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

logger = logging.getLogger("graviton.supervisor")


@dataclass
class SupervisorResult:
    """Represents the structured result of an agent turn."""

    conversation_id: Optional[str] = None
    status: str = "UNKNOWN"
    response: str = ""
    duration_seconds: float = 0.0
    num_turns: int = 0
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def is_success(self) -> bool:
        return self.status == "SUCCESS" and not self.error


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
    ):
        self.agent_name = agent_name
        self.model = model
        self.cwd = Path(cwd).resolve() if cwd else None
        self.remote_control = remote_control
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.extra_args = list(extra_args) if extra_args else []
        self.agy_binary = agy_binary or shutil.which("agy") or "agy"
        self.env = dict(env) if env is not None else dict(os.environ)

        self.proc: Optional[subprocess.Popen] = None
        self.conversation_id: Optional[str] = None
        self.init_data: Dict[str, Any] = {}
        self._is_closed = False

    def is_alive(self) -> bool:
        """Return True if the underlying process is currently running."""
        return self.proc is not None and self.proc.poll() is None

    def start(self, timeout: float = 30.0) -> str:
        """
        Spawn the agy process, establish NDJSON pipes, and await the 'init' event.

        :param timeout: Maximum seconds to wait for initial handshake.
        :return: Initialized conversation_id string.
        :raises SupervisorError: If initialization fails or process exits unexpectedly.
        """
        if self.is_alive():
            return self.conversation_id or ""

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

        # Read the first line from stdout which must be the 'init' event
        start_time = time.time()
        init_line = None

        while time.time() - start_time < timeout:
            if self.proc.poll() is not None:
                stderr_output = self.proc.stderr.read() if self.proc.stderr else ""
                raise SupervisorError(
                    f"agy process exited prematurely with code {self.proc.returncode}. stderr: {stderr_output.strip()}"
                )

            line = self.proc.stdout.readline() if self.proc.stdout else ""
            if line.strip():
                init_line = line
                break
            time.sleep(0.05)

        if not init_line:
            self.close()
            raise SupervisorTimeoutError(f"Timed out waiting for 'init' handshake after {timeout}s.")

        try:
            event = json.loads(init_line.strip())
        except json.JSONDecodeError as e:
            self.close()
            raise SupervisorError(f"Malformed JSON in init handshake: {init_line.strip()}") from e

        if event.get("event") != "init":
            self.close()
            raise SupervisorError(f"Expected 'init' event, got: {event.get('event')}")

        self.conversation_id = event.get("conversation_id")
        self.init_data = event.get("init", {})
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

    def receive_turn(
        self,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> SupervisorResult:
        """
        Stream events from stdout until a 'result' event signals turn completion.

        :param timeout: Optional maximum seconds to wait for turn completion.
        :param on_event: Optional callback invoked for every parsed NDJSON event.
        :return: SupervisorResult summarizing the turn.
        :raises SupervisorTimeoutError: If timeout elapses before 'result'.
        :raises SupervisorError: If process terminates unexpectedly or emits malformed data.
        """
        if not self.is_alive() or self.proc is None or self.proc.stdout is None:
            raise SupervisorError("Cannot receive turn: StreamSession process is not running.")

        events: List[Dict[str, Any]] = []
        start_time = time.time()

        while True:
            if timeout is not None and (time.time() - start_time) > timeout:
                raise SupervisorTimeoutError(f"Turn execution exceeded timeout of {timeout}s.")

            if self.proc.poll() is not None:
                # Process exited; drain remaining lines
                rem = self.proc.stdout.read()
                for line in rem.splitlines():
                    if line.strip():
                        try:
                            ev = json.loads(line.strip())
                            events.append(ev)
                            if on_event:
                                on_event(ev)
                            if ev.get("event") == "result":
                                res_data = ev.get("result", {})
                                return SupervisorResult(
                                    conversation_id=self.conversation_id,
                                    status=res_data.get("status", "SUCCESS"),
                                    response=res_data.get("response", ""),
                                    duration_seconds=res_data.get("duration_seconds", time.time() - start_time),
                                    num_turns=res_data.get("num_turns", 1),
                                    usage=res_data.get("usage", {}),
                                    error=res_data.get("error"),
                                    events=events,
                                )
                        except json.JSONDecodeError:
                            continue
                stderr_output = self.proc.stderr.read() if self.proc.stderr else ""
                raise SupervisorError(
                    f"agy process exited prematurely with code {self.proc.returncode}. stderr: {stderr_output.strip()}"
                )

            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.02)
                continue

            stripped = line.strip()
            if not stripped:
                continue

            try:
                event = json.loads(stripped)
            except json.JSONDecodeError as e:
                logger.warning(f"Unparseable stream-json output line: {stripped} ({e})")
                continue

            events.append(event)
            if on_event:
                try:
                    on_event(event)
                except Exception as cb_err:
                    logger.warning(f"Error in on_event callback: {cb_err}")

            if event.get("event") == "result":
                res_data = event.get("result", {})
                return SupervisorResult(
                    conversation_id=self.conversation_id,
                    status=res_data.get("status", "SUCCESS"),
                    response=res_data.get("response", ""),
                    duration_seconds=res_data.get("duration_seconds", time.time() - start_time),
                    num_turns=res_data.get("num_turns", 1),
                    usage=res_data.get("usage", {}),
                    error=res_data.get("error"),
                    events=events,
                )

    def run_turn(
        self,
        prompt: str,
        timeout: Optional[float] = None,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> SupervisorResult:
        """
        High-level helper: starts session if not already running, sends prompt,
        and returns the turn result.
        """
        if not self.is_alive():
            self.start()
        self.send_prompt(prompt)
        return self.receive_turn(timeout=timeout, on_event=on_event)

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
    timeout: Optional[float] = None,
    on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    extra_args: Optional[List[str]] = None,
) -> SupervisorResult:
    """
    Convenience function to execute a single turn using StreamSession with full lifecycle management.
    """
    with StreamSession(
        agent_name=agent_name,
        model=model,
        cwd=cwd,
        remote_control=remote_control,
        extra_args=extra_args,
    ) as session:
        return session.run_turn(prompt, timeout=timeout, on_event=on_event)
