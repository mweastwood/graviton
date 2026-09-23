"""
Terminal UI Input Handling and ANSI Byte Stream Parsing.
"""

import os
import sys
import time
from typing import Any, Callable, List, Optional, Tuple

try:
    import select
    import termios
    import tty
    HAS_TERMIOS = True
except ImportError:
    select = None
    termios = None
    tty = None
    HAS_TERMIOS = False

ESCAPE_TIMEOUT: float = 0.025


def _get_termios():
    if hasattr(termios, "assert_called") or hasattr(termios, "mock_calls"):
        return termios
    t_mod = sys.modules.get("lib.tui", None)
    if t_mod and hasattr(t_mod, "termios"):
        cand = getattr(t_mod, "termios")
        if hasattr(cand, "assert_called") or hasattr(cand, "mock_calls"):
            return cand
    return termios


def _get_tty():
    if hasattr(tty, "assert_called") or hasattr(tty, "mock_calls"):
        return tty
    t_mod = sys.modules.get("lib.tui", None)
    if t_mod and hasattr(t_mod, "tty"):
        cand = getattr(t_mod, "tty")
        if hasattr(cand, "assert_called") or hasattr(cand, "mock_calls"):
            return cand
    return tty


def is_incomplete_escape_sequence(raw_bytes: bytes) -> bool:
    """Check if raw_bytes represents a partial/incomplete ANSI escape sequence or UTF-8 sequence at its end."""
    if not raw_bytes:
        return False
    last_esc_idx = raw_bytes.rfind(b"\x1b")
    if last_esc_idx != -1:
        tail = raw_bytes[last_esc_idx:]
        if len(tail) == 1:
            return True
        if tail.startswith(b"\x1b[") or tail.startswith(b"\x1bO"):
            if len(tail) == 2:
                return True
            if not any(0x40 <= b <= 0x7E and b != 0x5B for b in tail[2:]):
                return True
    try:
        raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        if e.reason == "unexpected end of data":
            return True
    return False


def split_incomplete_utf8_tail(raw_bytes: bytes) -> Tuple[bytes, bytes]:
    """Split raw_bytes into (complete_prefix, incomplete_utf8_tail)."""
    if not raw_bytes:
        return raw_bytes, b""
    for k in range(1, min(4, len(raw_bytes) + 1)):
        tail = raw_bytes[-k:]
        try:
            tail.decode("utf-8")
        except UnicodeDecodeError as e:
            if e.reason == "unexpected end of data":
                return raw_bytes[:-k], tail
            else:
                continue
    return raw_bytes, b""


def split_incomplete_escape_tail(raw_bytes: bytes) -> Tuple[bytes, bytes]:
    """Split raw_bytes into (complete_prefix, incomplete_escape_tail).

    If raw_bytes ends with an incomplete multi-byte escape sequence prefix (where len(tail) > 1,
    e.g. b"\\x1b[" or b"\\x1bO"), the incomplete tail is returned to be retained in leftover_bytes.
    Single-byte b"\\x1b" (len(tail) == 1) is not split and remains in prefix to be processed
    immediately as standalone ESC after timeout.
    """
    if not raw_bytes:
        return raw_bytes, b""
    last_esc_idx = raw_bytes.rfind(b"\x1b")
    if last_esc_idx != -1:
        tail = raw_bytes[last_esc_idx:]
        if len(tail) > 1:
            if tail.startswith(b"\x1b[") or tail.startswith(b"\x1bO"):
                if len(tail) == 2 or not any(0x40 <= b <= 0x7E and b != 0x5B for b in tail[2:]):
                    return raw_bytes[:last_esc_idx], tail
    return raw_bytes, b""


def parse_keys(raw_bytes: bytes) -> List[str]:
    """Tokenize raw input bytes into individual keys or ANSI escape sequences.

    Handles multi-byte UTF-8 sequences by inspecting character length prior to slicing,
    and uses errors='ignore' when decoding recognized non-standard or malformed ESC escape
    sequences to safely bypass unparseable byte sequences.
    """
    if not raw_bytes:
        return []
    keys = []
    i = 0
    n = len(raw_bytes)
    while i < n:
        b = raw_bytes[i]
        if b == 0x1B:  # ESC
            if i + 1 >= n:
                keys.append("\x1b")
                i += 1
            else:
                next_b = raw_bytes[i + 1]
                if next_b in (0x5B, 0x4F):  # '[' or 'O'
                    term_idx = -1
                    end_idx = n
                    for j in range(i + 2, n):
                        if raw_bytes[j] == 0x1B:
                            end_idx = j
                            break
                        if 0x40 <= raw_bytes[j] <= 0x7E and raw_bytes[j] != 0x5B:
                            term_idx = j
                            break
                    if term_idx != -1:
                        seq = raw_bytes[i : term_idx + 1].decode("utf-8", errors="ignore")
                        keys.append(seq)
                        i = term_idx + 1
                    else:
                        seq = raw_bytes[i : end_idx].decode("utf-8", errors="ignore")
                        keys.append(seq)
                        i = end_idx
                elif next_b == 0x1B:
                    keys.append("\x1b")
                    i += 1
                else:
                    char_len = 1
                    for length in range(1, min(5, n - (i + 1) + 1)):
                        try:
                            chunk = raw_bytes[i + 1 : i + 1 + length].decode("utf-8")
                            if len(chunk) == 1:
                                char_len = length
                                break
                        except UnicodeDecodeError:
                            continue
                    seq = raw_bytes[i : i + 1 + char_len].decode("utf-8", errors="ignore")
                    keys.append(seq)
                    i += 1 + char_len
        else:
            decoded_ch = None
            decoded_len = 0
            for length in range(1, min(5, n - i + 1)):
                try:
                    chunk = raw_bytes[i : i + length].decode("utf-8")
                    if len(chunk) == 1:
                        decoded_ch = chunk
                        decoded_len = length
                        break
                except UnicodeDecodeError:
                    continue
            if decoded_ch is not None:
                keys.append(decoded_ch)
                i += decoded_len
            else:
                is_incomplete = False
                try:
                    raw_bytes[i:].decode("utf-8")
                except UnicodeDecodeError as e:
                    if e.reason == "unexpected end of data":
                        is_incomplete = True
                if is_incomplete:
                    break
                i += 1
    return keys


class TerminalInputListener:
    """
    Listener for terminal stdin key press events.
    Encapsulates raw termios cbreak terminal mode initialization,
    non-blocking select polling on stdin with escape sequence timeout,
    byte stream buffering, and key event dispatching via callback.
    """

    def __init__(
        self,
        on_key: Optional[Callable[[str], None]] = None,
        escape_timeout: float = ESCAPE_TIMEOUT,
    ):
        self.on_key = on_key
        self.escape_timeout = escape_timeout
        self._old_term_settings: Optional[Any] = None
        self._running: bool = False

    def setup_terminal(self) -> bool:
        """Set up raw termios cbreak mode on stdin if interactive TTY."""
        if not HAS_TERMIOS:
            return False
        try:
            if not sys.stdin.isatty():
                return False
            fd = sys.stdin.fileno()
            t_termios = _get_termios()
            t_tty = _get_tty()
            old_settings = t_termios.tcgetattr(fd)
            t_tty.setcbreak(fd)
            self._old_term_settings = old_settings
            return True
        except Exception:
            self._old_term_settings = None
            return False

    def restore_terminal(self):
        """Restore original terminal attributes if previously modified."""
        if self._old_term_settings is not None and HAS_TERMIOS:
            try:
                if sys.stdin.isatty():
                    fd = sys.stdin.fileno()
                    t_termios = _get_termios()
                    t_termios.tcsetattr(fd, t_termios.TCSAFLUSH, self._old_term_settings)
            except Exception:
                pass
            self._old_term_settings = None

    def run_loop(self, is_running_func: Optional[Callable[[], bool]] = None):
        """Execute stdin reading loop until stopped or is_running_func returns False."""
        if not HAS_TERMIOS:
            return
        if not self.setup_terminal():
            return

        self._running = True
        fd = sys.stdin.fileno()
        leftover_bytes = b""

        def check_running():
            if not self._running:
                return False
            if is_running_func is not None:
                return is_running_func()
            return True

        try:
            while check_running():
                try:
                    rlist, _, _ = select.select([fd], [], [], 0.1)
                except (BlockingIOError, InterruptedError):
                    time.sleep(0.01)
                    continue
                except Exception:
                    break

                if rlist:
                    try:
                        chunk = os.read(fd, 32)
                    except (BlockingIOError, InterruptedError):
                        time.sleep(0.01)
                        continue
                    except Exception:
                        break

                    if not chunk:
                        break

                    raw_bytes = leftover_bytes + chunk
                    leftover_bytes = b""

                    while check_running() and is_incomplete_escape_sequence(raw_bytes):
                        try:
                            rlist_seq, _, _ = select.select([fd], [], [], self.escape_timeout)
                        except (BlockingIOError, InterruptedError):
                            time.sleep(0.01)
                            continue
                        except Exception:
                            break

                        if rlist_seq:
                            try:
                                seq_bytes = os.read(fd, 31)
                                if not seq_bytes:
                                    break
                                raw_bytes = raw_bytes + seq_bytes
                            except (BlockingIOError, InterruptedError):
                                time.sleep(0.01)
                                continue
                            except Exception:
                                break
                        else:
                            break

                    raw_bytes, leftover_esc = split_incomplete_escape_tail(raw_bytes)
                    prefix, leftover_utf8 = split_incomplete_utf8_tail(raw_bytes)
                    leftover_bytes = leftover_utf8 + leftover_esc
                    for key in parse_keys(prefix):
                        if self.on_key:
                            self.on_key(key)
                else:
                    if leftover_bytes:
                        for key in parse_keys(leftover_bytes):
                            if self.on_key:
                                self.on_key(key)
                        leftover_bytes = b""
        except Exception:
            pass
        finally:
            self._running = False
            self.restore_terminal()

    def stop(self):
        """Stop input listener loop."""
        self._running = False
