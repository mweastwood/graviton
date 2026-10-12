"""Unit tests for lib/agyhub.py (Protobuf codec and agyhub sync helpers)."""

import json
import os
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.agyhub import (
    _build_workspace_proto,
    _decode_varint,
    _encode_field,
    _encode_varint,
    _parse_fields,
    _parse_git_repo_info,
    _read_agyhub_entries,
    _write_agyhub_entries,
    sync_conversation_to_agyhub,
)


class TestAgyhubCodec(unittest.TestCase):
    """Test low-level Protobuf wire protocol serialization and deserialization in lib/agyhub."""

    def test_varint_encode_decode(self):
        # Boundary cases: 0, 127, 128, and multi-byte values
        test_values = [
            0,
            1,
            127,
            128,
            255,
            256,
            16383,
            16384,
            2097151,
            2097152,
            268435455,
            268435456,
            536870911,
            0x7FFFFFFF,
            0xFFFFFFFF,
            0x7FFFFFFFFFFFFFFF,
        ]
        for val in test_values:
            encoded = _encode_varint(val)
            decoded, pos = _decode_varint(encoded, 0)
            self.assertEqual(decoded, val, f"Mismatch for value {val}")
            self.assertEqual(pos, len(encoded))

        # Negative numbers (two's complement 64-bit mask)
        for neg_val in [-1, -42, -128, -2147483648]:
            encoded = _encode_varint(neg_val)
            decoded, pos = _decode_varint(encoded, 0)
            self.assertEqual(decoded, neg_val & 0xFFFFFFFFFFFFFFFF)
            self.assertEqual(pos, len(encoded))

    def test_encode_field_wire_type_0(self):
        # Varint wire type 0: ints, bools
        f_int = _encode_field(1, 0, 42)
        parsed = _parse_fields(f_int)
        self.assertEqual(parsed, [(1, 0, 42)])

        f_zero = _encode_field(2, 0, 0)
        self.assertEqual(_parse_fields(f_zero), [(2, 0, 0)])

        f_bool_true = _encode_field(3, 0, True)
        self.assertEqual(_parse_fields(f_bool_true), [(3, 0, 1)])

        f_bool_false = _encode_field(4, 0, False)
        self.assertEqual(_parse_fields(f_bool_false), [(4, 0, 0)])

        with self.assertRaises(TypeError):
            _encode_field(1, 0, "not an int")

    def test_encode_field_wire_type_1(self):
        # 64-bit fixed wire type 1: int, float, 8 raw bytes / 8 char string
        f_int = _encode_field(1, 1, 1234567890123456)
        parsed = _parse_fields(f_int)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0][0], 1)
        self.assertEqual(parsed[0][1], 1)
        self.assertEqual(struct.unpack("<q", parsed[0][2])[0], 1234567890123456)

        f_float = _encode_field(2, 1, 3.141592653589793)
        parsed = _parse_fields(f_float)
        self.assertAlmostEqual(struct.unpack("<d", parsed[0][2])[0], 3.141592653589793)

        f_bytes = _encode_field(3, 1, b"12345678")
        parsed = _parse_fields(f_bytes)
        self.assertEqual(parsed, [(3, 1, b"12345678")])

        f_str = _encode_field(4, 1, "ABCDEFGH")
        parsed = _parse_fields(f_str)
        self.assertEqual(parsed, [(4, 1, b"ABCDEFGH")])

        # Invalid length raises
        with self.assertRaises(ValueError):
            _encode_field(5, 1, b"too_short")
        with self.assertRaises(ValueError):
            _encode_field(5, 1, "too_long_value")

    def test_encode_field_wire_type_2(self):
        # Length-delimited wire type 2: str, bytes, bytearray
        f_str = _encode_field(1, 2, "hello world")
        parsed = _parse_fields(f_str)
        self.assertEqual(parsed, [(1, 2, b"hello world")])

        f_bytes = _encode_field(2, 2, b"\x00\x01\x02\x03")
        parsed = _parse_fields(f_bytes)
        self.assertEqual(parsed, [(2, 2, b"\x00\x01\x02\x03")])

        f_empty = _encode_field(3, 2, "")
        parsed = _parse_fields(f_empty)
        self.assertEqual(parsed, [(3, 2, b"")])

        with self.assertRaises(TypeError):
            _encode_field(4, 2, 12345)

    def test_encode_field_wire_type_5(self):
        # 32-bit fixed wire type 5: int, float, 4 raw bytes / 4 char string
        f_int = _encode_field(1, 5, 987654)
        parsed = _parse_fields(f_int)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0][0], 1)
        self.assertEqual(parsed[0][1], 5)
        self.assertEqual(struct.unpack("<i", parsed[0][2])[0], 987654)

        f_float = _encode_field(2, 5, 1.25)
        parsed = _parse_fields(f_float)
        self.assertAlmostEqual(struct.unpack("<f", parsed[0][2])[0], 1.25)

        f_bytes = _encode_field(3, 5, b"test")
        parsed = _parse_fields(f_bytes)
        self.assertEqual(parsed, [(3, 5, b"test")])

        f_str = _encode_field(4, 5, "byte")
        parsed = _parse_fields(f_str)
        self.assertEqual(parsed, [(4, 5, b"byte")])

        # Invalid length raises
        with self.assertRaises(ValueError):
            _encode_field(5, 5, b"123")
        with self.assertRaises(ValueError):
            _encode_field(5, 5, "12345")

    def test_encode_field_validation(self):
        # Invalid field numbers
        for invalid_fn in [0, -1, 536870912, 19000, 19500, 19999]:
            with self.assertRaises(ValueError):
                _encode_field(invalid_fn, 0, 100)

        with self.assertRaises(TypeError):
            _encode_field("1", 0, 100)

        # Unsupported wire type
        for invalid_wt in [3, 4, 6, 7, -1]:
            with self.assertRaises(ValueError):
                _encode_field(1, invalid_wt, 100)

        with self.assertRaises(TypeError):
            _encode_field(1, "0", 100)

    def test_parse_fields_truncation_and_termination(self):
        # Empty payload
        self.assertEqual(_parse_fields(b""), [])

        # Field number 0 terminates parsing
        self.assertEqual(_parse_fields(b"\x00\x00"), [])

        valid = _encode_field(1, 0, 100)
        self.assertEqual(_parse_fields(valid + b"\x00"), [(1, 0, 100)])

        # Truncated varint tag
        self.assertEqual(_parse_fields(valid + b"\x80"), [(1, 0, 100)])

    def test_agyhub_entries_roundtrip(self):
        # Empty entries
        self.assertEqual(_write_agyhub_entries([]), b"")
        self.assertEqual(_read_agyhub_entries(b""), [])

        # Multiple entries roundtrip
        entries = [
            ("conv-001", b"Summary data for conversation 1"),
            ("conv-002", b"Another summary with \x00 binary \xff bytes"),
            ("conv-003", _encode_field(1, 2, "Title") + _encode_field(2, 0, 42)),
        ]
        serialized = _write_agyhub_entries(entries)
        deserialized = _read_agyhub_entries(serialized)
        self.assertEqual(deserialized, entries)

    def test_build_workspace_proto(self):
        proto = _build_workspace_proto(
            workspace_uri="file:///test/repo",
            repo_name="owner/repo",
            git_url="https://github.com/owner/repo.git",
            branch="feature",
        )
        fields = _parse_fields(proto)
        field_dict = {num: val for num, wt, val in fields}
        self.assertEqual(field_dict[1], b"file:///test/repo")
        self.assertEqual(field_dict[2], b"file:///test/repo")
        self.assertEqual(field_dict[4], b"feature")

        git_info_fields = _parse_fields(field_dict[3])
        git_dict = {num: val for num, wt, val in git_info_fields}
        self.assertEqual(git_dict[1], b"owner/repo")
        self.assertEqual(git_dict[2], b"https://github.com/owner/repo.git")


class TestAgyhubSync(unittest.TestCase):
    """Test sync_conversation_to_agyhub in lib/agyhub."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)
        self.cli_dir = self.base / ".gemini" / "antigravity-cli"
        self.cli_dir.mkdir(parents=True, exist_ok=True)
        self.conv_dir = self.cli_dir / "conversations"
        self.conv_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir = self.base / ".gemini" / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.repo_dir = self.base / "test_repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_sync_conversation_validation_and_missing_db(self):
        # Empty or whitespace conversation_id returns False
        self.assertFalse(sync_conversation_to_agyhub(""))
        self.assertFalse(sync_conversation_to_agyhub("   "))

        # Database doesn't exist returns False
        self.assertFalse(
            sync_conversation_to_agyhub(
                "conv-test",
                cli_dir=self.cli_dir,
            )
        )

    def test_sync_conversation_full_flow(self):
        cid = "conv-abc-123"
        db_path = self.cli_dir / "conversation_summaries.db"
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE conversation_summaries (
                    conversation_id TEXT PRIMARY KEY,
                    title TEXT,
                    project_id TEXT,
                    workspace_uris TEXT,
                    raw_summary BLOB
                )
            """)
            initial_f17 = _encode_field(6, 2, cid.encode("utf-8"))
            raw_summary = _encode_field(1, 2, b"My Topic") + _encode_field(17, 2, initial_f17)
            cur.execute(
                "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?)",
                (cid, "My Topic", "old-project", "", raw_summary),
            )
            conn.commit()

        # Conversation db
        conv_db_path = self.conv_dir / f"{cid}.db"
        with sqlite3.connect(str(conv_db_path)) as c_conn:
            c_cur = c_conn.cursor()
            c_cur.execute("CREATE TABLE trajectory_metadata_blob (id TEXT PRIMARY KEY, data BLOB)")
            c_cur.execute("INSERT INTO trajectory_metadata_blob VALUES ('main', ?)", (initial_f17,))
            c_conn.commit()

        ok = sync_conversation_to_agyhub(
            conversation_id=cid,
            repo_dir=self.repo_dir,
            branch="main",
            project_id="new-project-id",
            workspace_uri="file:///custom/workspace",
            cli_dir=self.cli_dir,
            config_dir=self.config_dir,
        )
        self.assertTrue(ok)

        # Verify DB updated
        with sqlite3.connect(str(db_path)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT project_id, workspace_uris, raw_summary FROM conversation_summaries WHERE conversation_id = ?", (cid,))
            row = cur.fetchone()
            self.assertEqual(row[0], "new-project-id")
            self.assertIn("file:///custom/workspace", row[1])

        # Verify agyhub_summaries_proto.pb created and contains entry
        hub_pb = self.cli_dir / "agyhub_summaries_proto.pb"
        self.assertTrue(hub_pb.exists())
        entries = _read_agyhub_entries(hub_pb.read_bytes())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], cid)

    def test_parse_git_repo_info_non_git(self):
        repo_name, git_url, branch = _parse_git_repo_info(self.repo_dir)
        self.assertIsNone(repo_name)
        self.assertIsNone(git_url)
        self.assertIsNone(branch)

    def test_parse_git_repo_info_git_repo(self):
        git_dir = self.base / "git_test_repo"
        git_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", str(git_dir)], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(git_dir), "config", "remote.origin.url", "https://github.com/myorg/custom-repo.git"],
            check=True,
            capture_output=True,
        )
        subprocess.run(["git", "-C", str(git_dir), "checkout", "-b", "feature/my-branch"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(git_dir), "config", "user.name", "Test User"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(git_dir), "config", "user.email", "test@example.com"], check=True, capture_output=True)
        (git_dir / "README.md").write_text("test")
        subprocess.run(["git", "-C", str(git_dir), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(git_dir), "commit", "-m", "init"], check=True, capture_output=True)

        # Test passing Path object
        repo_name, git_url, branch = _parse_git_repo_info(git_dir)
        self.assertEqual(repo_name, "myorg/custom-repo")
        self.assertEqual(git_url, "https://github.com/myorg/custom-repo.git")
        self.assertEqual(branch, "feature/my-branch")

        # Test passing string path
        repo_name_str, git_url_str, branch_str = _parse_git_repo_info(str(git_dir))
        self.assertEqual(repo_name_str, "myorg/custom-repo")
        self.assertEqual(git_url_str, "https://github.com/myorg/custom-repo.git")
        self.assertEqual(branch_str, "feature/my-branch")


if __name__ == "__main__":
    unittest.main()
