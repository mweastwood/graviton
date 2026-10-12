"""
Protobuf encoding/decoding and agyhub summary synchronization for Antigravity Remote Control.
"""

import json
import logging
import re
import sqlite3
import struct
import subprocess
import uuid
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

from lib.projects import find_project_for_repo

logger = logging.getLogger("graviton.agyhub")

__all__ = [
    "_build_workspace_proto",
    "_decode_varint",
    "_encode_field",
    "_encode_varint",
    "_parse_fields",
    "_parse_git_repo_info",
    "_read_agyhub_entries",
    "_write_agyhub_entries",
    "sync_conversation_to_agyhub",
]


def _parse_git_repo_info(repo_path: Union[str, Path]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Returns (repo_name, git_url, current_branch) for a git repository directory.
    """
    repo_name, git_url, branch = None, None, None
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=False, timeout=5.0,
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
            capture_output=True, text=True, check=False, timeout=5.0,
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


def _encode_field(field_num: int, wire_type: int, data: Union[int, float, bytes, bytearray, str, bool]) -> bytes:
    """Encode a single Protocol Buffer field with tag (field number and wire type) and payload.

    Supported wire types:
        - 0 (Varint): int (int32, int64, uint32, uint64, sint32, sint64, bool, enum).
        - 1 (64-bit fixed): int, float (fixed64, sfixed64, double), 8-byte str/bytes/bytearray.
        - 2 (Length-delimited): str (UTF-8 encoded), bytes, bytearray (string, bytes, embedded messages).
        - 5 (32-bit fixed): int, float (fixed32, sfixed32, float), 4-byte str/bytes/bytearray.

    Wire structure:
        Tag: varint-encoded ((field_num << 3) | wire_type)
        Followed by:
            - Wire type 0: varint-encoded integer value
            - Wire type 1: 8 raw bytes (little-endian fixed64 or IEEE 754 double)
            - Wire type 2: varint length prefix + raw bytes payload
            - Wire type 5: 4 raw bytes (little-endian fixed32 or IEEE 754 single-precision float)
    """
    if not isinstance(field_num, int) or isinstance(field_num, bool):
        raise TypeError(f"Invalid field number type {type(field_num).__name__}: must be an int")
    if field_num <= 0 or field_num > 536870911:
        raise ValueError(f"Invalid field number {field_num}: must be between 1 and 536870911")
    if 19000 <= field_num <= 19999:
        raise ValueError(f"Invalid field number {field_num}: field numbers 19000-19999 are reserved")

    if not isinstance(wire_type, int) or isinstance(wire_type, bool):
        raise TypeError(f"Invalid wire type {type(wire_type).__name__}: must be an int")
    if wire_type not in (0, 1, 2, 5):
        raise ValueError(f"Unsupported wire type {wire_type}")

    tag = (field_num << 3) | wire_type
    if wire_type == 0:
        if isinstance(data, bool):
            int_val = 1 if data else 0
        elif isinstance(data, int):
            int_val = data
        else:
            raise TypeError(f"Unsupported data type for wire type 0: {type(data).__name__}")
        if not (-0x8000000000000000 <= int_val <= 0xffffffffffffffff):
            raise ValueError(f"Integer value {int_val} out of range for varint field")
        return _encode_varint(tag) + _encode_varint(int_val)
    elif wire_type == 2:
        if isinstance(data, str):
            b_data = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray)):
            b_data = bytes(data)
        else:
            raise TypeError(f"Unsupported data type for wire type 2: {type(data).__name__}")
        return _encode_varint(tag) + _encode_varint(len(b_data)) + b_data
    elif wire_type == 1:
        if isinstance(data, int) and not isinstance(data, bool):
            if not (-0x8000000000000000 <= data <= 0xffffffffffffffff):
                raise ValueError(f"Integer value {data} out of range for 64-bit fixed field")
            b_data = struct.pack("<Q", data & 0xffffffffffffffff)
        elif isinstance(data, float):
            b_data = struct.pack("<d", data)
        elif isinstance(data, str):
            b_data = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray)):
            b_data = bytes(data)
        else:
            raise TypeError(f"Unsupported data type for wire type 1: {type(data).__name__}")
        if len(b_data) != 8:
            raise ValueError(f"Wire type 1 requires 8 bytes, got {len(b_data)}")
        return _encode_varint(tag) + b_data
    elif wire_type == 5:
        if isinstance(data, int) and not isinstance(data, bool):
            if not (-0x80000000 <= data <= 0xffffffff):
                raise ValueError(f"Integer value {data} out of range for 32-bit fixed field")
            b_data = struct.pack("<I", data & 0xffffffff)
        elif isinstance(data, float):
            try:
                b_data = struct.pack("<f", data)
            except (OverflowError, struct.error):
                raise ValueError(f"Float value {data} out of range for 32-bit fixed field")
        elif isinstance(data, str):
            b_data = data.encode("utf-8")
        elif isinstance(data, (bytes, bytearray)):
            b_data = bytes(data)
        else:
            raise TypeError(f"Unsupported data type for wire type 5: {type(data).__name__}")
        if len(b_data) != 4:
            raise ValueError(f"Wire type 5 requires 4 bytes, got {len(b_data)}")
        return _encode_varint(tag) + b_data


def _parse_fields(data: bytes) -> List[Tuple[int, int, Any]]:
    """Parse raw protobuf message bytes into a list of (field_num, wire_type, value) tuples.

    Iterates through serialized protobuf wire data and extracts fields according to their wire type:
        - 0 (Varint): decodes varint integer value.
        - 1 (64-bit fixed): extracts 8 raw payload bytes.
        - 2 (Length-delimited): decodes varint length prefix and extracts raw payload bytes.
        - 5 (32-bit fixed): extracts 4 raw payload bytes.

    Parsing stops cleanly if unknown wire types, field number 0, or truncated data are encountered.

    Returns:
        List of tuples: (field_number: int, wire_type: int, value: Union[int, bytes])
    """
    fields = []
    pos = 0
    length = len(data)
    while pos < length:
        try:
            tag, pos = _decode_varint(data, pos)
            field_num = tag >> 3
            wire_type = tag & 7
            if field_num == 0:
                break
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
        except IndexError:
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
            field_num = tag >> 3
            wire_type = tag & 7
            if field_num != 1 or wire_type != 2:
                break
            length, pos = _decode_varint(pb_data, pos)
            if pos + length > len(pb_data):
                break
            entry_bytes = pb_data[pos:pos+length]
            pos += length

            conv_id = None
            raw_summary = None
            for s_num, s_type, s_val in _parse_fields(entry_bytes):
                if s_num == 1 and isinstance(s_val, bytes):
                    conv_id = s_val.decode("utf-8", errors="ignore")
                elif s_num == 2 and isinstance(s_val, bytes):
                    raw_summary = s_val
            if conv_id and raw_summary is not None:
                entries.append((conv_id, raw_summary))
        except IndexError:
            break
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
