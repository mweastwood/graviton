"""
Unit tests for lib/tui_input.py (Terminal UI Input & ANSI Byte Parser).
"""

import io
import os
import pty
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from lib.tui import TerminalDashboard
from lib.tui_input import (
    ESCAPE_TIMEOUT,
    HAS_TERMIOS,
    TerminalInputListener,
    _get_termios,
    _get_tty,
    is_incomplete_escape_sequence,
    parse_keys,
    split_incomplete_escape_tail,
    split_incomplete_utf8_tail,
)


class TestTUIInput(unittest.TestCase):

    def test_is_incomplete_escape_sequence(self):
        # Empty or non-escape sequence
        self.assertFalse(is_incomplete_escape_sequence(b""))
        self.assertFalse(is_incomplete_escape_sequence(b"a"))
        self.assertFalse(is_incomplete_escape_sequence(b"hello"))

        # Incomplete escape sequences
        self.assertTrue(is_incomplete_escape_sequence(b"\x1b"))
        self.assertTrue(is_incomplete_escape_sequence(b"\x1b["))
        self.assertTrue(is_incomplete_escape_sequence(b"\x1b[["))
        self.assertTrue(is_incomplete_escape_sequence(b"\x1b[1"))
        self.assertTrue(is_incomplete_escape_sequence(b"\x1b[15"))
        self.assertTrue(is_incomplete_escape_sequence(b"\x1bO"))
        self.assertTrue(is_incomplete_escape_sequence(b"j\x1b["))
        self.assertTrue(is_incomplete_escape_sequence(b"\x1b[A\x1b["))
        self.assertTrue(is_incomplete_escape_sequence(b"j\xc3"))
        self.assertTrue(is_incomplete_escape_sequence(b"\xe2\x82"))

        # Complete escape sequences
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b[A"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b[B"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b[15~"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1bOA"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1bOB"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b1"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1bM"))
        self.assertFalse(is_incomplete_escape_sequence(b"\xc3\xa1"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b[A1"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b[A "))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b[A\n"))
        self.assertFalse(is_incomplete_escape_sequence(b"\x1b[A\xc3\xa1"))

    def test_split_incomplete_utf8_tail(self):
        self.assertEqual(split_incomplete_utf8_tail(b""), (b"", b""))
        self.assertEqual(split_incomplete_utf8_tail(b"hello"), (b"hello", b""))
        self.assertEqual(split_incomplete_utf8_tail(b"j\xc3"), (b"j", b"\xc3"))
        self.assertEqual(split_incomplete_utf8_tail(b"\xe2\x82"), (b"", b"\xe2\x82"))
        self.assertEqual(split_incomplete_utf8_tail(b"\xc3\xa1"), (b"\xc3\xa1", b""))

    def test_split_incomplete_escape_tail(self):
        self.assertEqual(split_incomplete_escape_tail(b""), (b"", b""))
        self.assertEqual(split_incomplete_escape_tail(b"hello"), (b"hello", b""))
        self.assertEqual(split_incomplete_escape_tail(b"\x1b"), (b"\x1b", b""))
        self.assertEqual(split_incomplete_escape_tail(b"\x1b["), (b"", b"\x1b["))
        self.assertEqual(split_incomplete_escape_tail(b"\x1bO"), (b"", b"\x1bO"))
        self.assertEqual(split_incomplete_escape_tail(b"\x1b[1;"), (b"", b"\x1b[1;"))
        self.assertEqual(split_incomplete_escape_tail(b"\x1b[A"), (b"\x1b[A", b""))
        self.assertEqual(split_incomplete_escape_tail(b"j\x1b["), (b"j", b"\x1b["))

    def test_parse_keys(self):
        self.assertEqual(parse_keys(b""), [])
        self.assertEqual(parse_keys(b"j"), ["j"])
        self.assertEqual(parse_keys(b"jj"), ["j", "j"])
        self.assertEqual(parse_keys(b"kk"), ["k", "k"])
        self.assertEqual(parse_keys(b"\x1b[B"), ["\x1b[B"])
        self.assertEqual(parse_keys(b"\x1b[B\x1b[B"), ["\x1b[B", "\x1b[B"])
        self.assertEqual(parse_keys(b"j\x1b[A"), ["j", "\x1b[A"])
        self.assertEqual(parse_keys(b"\x1bOA"), ["\x1bOA"])
        self.assertEqual(parse_keys(b"\x1bOB"), ["\x1bOB"])
        self.assertEqual(parse_keys(b"\x1bOB\x1bOA"), ["\x1bOB", "\x1bOA"])
        self.assertEqual(parse_keys(b"\x1b1"), ["\x1b1"])
        self.assertEqual(parse_keys(b"\x1b"), ["\x1b"])
        self.assertEqual(parse_keys(b"\x80\x61\x62"), ["a", "b"])
        self.assertEqual(parse_keys(b"\x1b[\x1b[A"), ["\x1b[", "\x1b[A"])
        self.assertEqual(parse_keys(b"\x1b[["), ["\x1b[["])
        self.assertEqual(parse_keys(b"\x1b[1\x1b[B"), ["\x1b[1", "\x1b[B"])
        self.assertEqual(parse_keys(b"\x1b\x1b[A"), ["\x1b", "\x1b[A"])
        self.assertEqual(parse_keys(b"j\xc3"), ["j"])
        self.assertEqual(parse_keys(b"\xc3\xa1"), ["á"])
        self.assertEqual(parse_keys(b"\x1b\xc3\xa1"), ["\x1bá"])
        self.assertEqual(parse_keys(b"\x1b\xe2\x82\xac"), ["\x1b€"])

    @staticmethod
    def _wait_for_condition(condition, timeout=5.0, interval=0.01):
        start = time.time()
        while time.time() - start < timeout:
            if condition():
                return True
            time.sleep(interval)
        return bool(condition())

    def test_terminal_input_listener_event_dispatch(self):
        if not HAS_TERMIOS:
            self.skipTest("termios not available on this platform")

        master, slave = pty.openpty()

        try:
            handled_keys = []
            listener = TerminalInputListener(on_key=lambda k: handled_keys.append(k))

            class MockStdin:
                def fileno(self):
                    return slave
                def isatty(self):
                    return True

            mock_stdin = MockStdin()

            with patch("sys.stdin", mock_stdin):
                thread = threading.Thread(target=listener.run_loop, daemon=True)
                thread.start()

                self.assertTrue(self._wait_for_condition(lambda: listener._old_term_settings is not None))

                try:
                    # Write 'j' key
                    os.write(master, b"j")
                    self.assertTrue(self._wait_for_condition(lambda: "j" in handled_keys))

                    # Write arrow down sequence
                    handled_keys.clear()
                    os.write(master, b"\x1b[B")
                    self.assertTrue(self._wait_for_condition(lambda: "\x1b[B" in handled_keys))

                    # Write UTF-8 characters across chunks
                    handled_keys.clear()
                    os.write(master, b"\xc3")
                    time.sleep(0.01)
                    os.write(master, b"\xa1")
                    self.assertTrue(self._wait_for_condition(lambda: "á" in handled_keys))

                finally:
                    listener.stop()
                    thread.join(timeout=3.0)
        finally:
            os.close(master)
            os.close(slave)

    def test_terminal_input_listener_restore_terminal(self):
        listener = TerminalInputListener()
        listener._old_term_settings = [1, 2, 3, 4]

        with patch("lib.tui_input.termios") as mock_termios, \
             patch("lib.tui_input.sys.stdin.isatty", return_value=True):
            mock_termios.TCSAFLUSH = 2
            listener.restore_terminal()
            mock_termios.tcsetattr.assert_called_once_with(sys.stdin.fileno(), 2, [1, 2, 3, 4])
            self.assertIsNone(listener._old_term_settings)

    def test_terminal_dashboard_termios_sync(self):
        manager = MagicMock()
        dashboard = TerminalDashboard(task_manager=manager)
        dashboard._old_term_settings = [1, 2, 3, 4]
        self.assertEqual(dashboard._stored_old_term_settings, [1, 2, 3, 4])
        self.assertEqual(dashboard._input_listener._old_term_settings, [1, 2, 3, 4])

        with patch("lib.tui_input.termios") as mock_termios, \
             patch("sys.stdin.isatty", return_value=True):
            mock_termios.TCSAFLUSH = 2
            dashboard._restore_termios()

        self.assertIsNone(dashboard._stored_old_term_settings)
        self.assertIsNone(dashboard._input_listener._old_term_settings)
        self.assertIsNone(dashboard._old_term_settings)

    def test_terminal_dashboard_leftover_bytes_sync(self):
        manager = MagicMock()
        dashboard = TerminalDashboard(task_manager=manager)
        # Test initial defaults
        self.assertEqual(dashboard._stored_leftover_bytes, b"")
        self.assertEqual(dashboard._stored_idle_flush_count, 0)
        self.assertEqual(dashboard._leftover_bytes, b"")
        self.assertEqual(dashboard._idle_flush_count, 0)

        # Test sync with _input_listener
        dashboard._leftover_bytes = b"\x1b["
        self.assertEqual(dashboard._stored_leftover_bytes, b"\x1b[")
        self.assertEqual(dashboard._input_listener._leftover_bytes, b"\x1b[")
        self.assertEqual(dashboard._leftover_bytes, b"\x1b[")

        dashboard._idle_flush_count = 3
        self.assertEqual(dashboard._stored_idle_flush_count, 3)
        self.assertEqual(dashboard._input_listener._idle_flush_count, 3)
        self.assertEqual(dashboard._idle_flush_count, 3)

        # Test fallback when _input_listener is None
        dashboard._input_listener = None
        self.assertEqual(dashboard._leftover_bytes, b"\x1b[")
        self.assertEqual(dashboard._idle_flush_count, 3)
        dashboard._leftover_bytes = b"\x1b[A"
        dashboard._idle_flush_count = 4
        self.assertEqual(dashboard._stored_leftover_bytes, b"\x1b[A")
        self.assertEqual(dashboard._stored_idle_flush_count, 4)
        self.assertEqual(dashboard._leftover_bytes, b"\x1b[A")
        self.assertEqual(dashboard._idle_flush_count, 4)

    def test_terminal_input_listener_idle_timeout_flushes_leftover_bytes(self):
        if not HAS_TERMIOS:
            self.skipTest("termios not available on this platform")

        master, slave = pty.openpty()
        try:
            handled_keys = []
            listener = TerminalInputListener(on_key=lambda k: handled_keys.append(k))

            class MockStdin:
                def fileno(self):
                    return slave
                def isatty(self):
                    return True

            mock_stdin = MockStdin()
            with patch("sys.stdin", mock_stdin):
                thread = threading.Thread(target=listener.run_loop, daemon=True)
                thread.start()

                self.assertTrue(self._wait_for_condition(lambda: listener._old_term_settings is not None))

                try:
                    os.write(master, b"\x1b[")
                    self.assertTrue(
                        self._wait_for_condition(
                            lambda: "\x1b[" in handled_keys and len(listener.leftover_bytes) == 0 and listener.idle_flush_count > 0,
                            timeout=2.0,
                        )
                    )
                    self.assertIn("\x1b[", handled_keys)
                    self.assertEqual(listener.leftover_bytes, b"")
                    self.assertGreaterEqual(listener.idle_flush_count, 1)

                    os.write(master, b"a")
                    self.assertTrue(self._wait_for_condition(lambda: "a" in handled_keys))
                finally:
                    listener.stop()
                    thread.join(timeout=3.0)
        finally:
            os.close(master)
            os.close(slave)

    def test_get_termios_and_tty_mock_lookup(self):
        mock_termios = MagicMock()
        mock_tty = MagicMock()

        class FakeModule:
            termios = "not_a_mock"
            tty = "not_a_mock"

        with patch.dict("sys.modules", {"lib.tui": FakeModule()}):
            with patch("lib.tui_input.termios", "real_termios"), \
                 patch("lib.tui_input.tty", "real_tty"):
                self.assertEqual(_get_termios(), "real_termios")
                self.assertEqual(_get_tty(), "real_tty")

        class MockModule:
            termios = mock_termios
            tty = mock_tty

        with patch.dict("sys.modules", {"lib.tui": MockModule()}):
            with patch("lib.tui_input.termios", "real_termios"), \
                 patch("lib.tui_input.tty", "real_tty"):
                self.assertEqual(_get_termios(), mock_termios)
                self.assertEqual(_get_tty(), mock_tty)
