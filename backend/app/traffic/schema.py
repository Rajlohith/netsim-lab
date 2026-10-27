"""Raw-data schema (version 1). The same definitions are mirrored in /schemas/*.json and docs."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone

SCHEMA_VERSION = 1

SENT_COLUMNS = ("seq", "scheduled_mono_ns", "send_mono_ns", "send_wall_ns", "size_bytes", "send_errno")
ECHO_COLUMNS = ("seq", "recv_mono_ns", "recv_wall_ns", "size_bytes")
RECEIVED_COLUMNS = ("seq", "session_id", "recv_mono_ns", "recv_wall_ns", "hdr_send_mono_ns",
                    "hdr_send_wall_ns", "size_bytes", "src_addr", "src_port")
EVENT_COLUMNS = ("event_id", "kind", "mono_ns", "wall_ns", "detail")

INT_COLUMNS = {
    "sent.csv": {"seq", "scheduled_mono_ns", "send_mono_ns", "send_wall_ns", "size_bytes", "send_errno"},
    "echoes.csv": {"seq", "recv_mono_ns", "recv_wall_ns", "size_bytes"},
    "received.csv": {"seq", "session_id", "recv_mono_ns", "recv_wall_ns", "hdr_send_mono_ns",
                     "hdr_send_wall_ns", "size_bytes", "src_port"},
    "events.csv": {"mono_ns", "wall_ns"},
}
COLUMNS = {"sent.csv": SENT_COLUMNS, "echoes.csv": ECHO_COLUMNS,
           "received.csv": RECEIVED_COLUMNS, "events.csv": EVENT_COLUMNS}

EVENT_KINDS = ("scenario_start", "scenario_end", "handoff_start", "handoff_end", "note")
STATUSES = ("running", "completed", "interrupted", "failed")
ROLES = ("sender", "receiver")

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_id(value: str, what: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value) or ".." in value:
        raise ValueError(f"invalid {what} {value!r}: use [A-Za-z0-9._-], 1-64 chars, starting alphanumeric")
    return value


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


_META_REQUIRED = {
    "schema_version": int, "tool": str, "role": str, "scenario_id": str, "run_id": str,
    "session_id": int, "status": str, "start_wall_iso": str, "start_mono_ns": int,
    "params": dict, "counters": dict, "clock": dict, "files": dict,
}


def validate_meta(meta: dict) -> list[str]:
    """Returns a list of problems (empty = valid)."""
    errs = []
    for k, t in _META_REQUIRED.items():
        if k not in meta:
            errs.append(f"missing '{k}'")
        elif not isinstance(meta[k], t) or isinstance(meta[k], bool):
            errs.append(f"'{k}' should be {t.__name__}")
    if meta.get("schema_version") != SCHEMA_VERSION:
        errs.append("unsupported schema_version")
    if meta.get("role") not in ROLES:
        errs.append("bad role")
    if meta.get("status") not in STATUSES:
        errs.append("bad status")
    if meta.get("status") in ("completed", "interrupted", "failed") and meta.get("end_mono_ns") is None:
        errs.append("finished run without end_mono_ns")
    return errs
