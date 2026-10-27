"""mip_events.csv: protocol-level event log (one row per event), timestamps from the injected clock."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from ..traffic.recorder import CsvRecorder

MIP_EVENT_COLUMNS = ("seq", "mono_ns", "wall_ns", "node", "kind", "home_address", "care_of_address",
                     "identification", "code", "lifetime_s", "detail")
MIP_INT_COLUMNS = {"seq", "mono_ns", "wall_ns"}
MIP_OPT_INT_COLUMNS = {"identification", "code", "lifetime_s"}     # empty string = not applicable

KINDS = (
    "link_change", "attach", "detach", "reg_request_sent", "reg_request_received", "reg_reply_sent", "reg_reply_received",
    "reg_retransmit", "reg_timeout", "reg_failed", "reg_rejected", "reregister", "resync_clock",
    "binding_created", "binding_updated", "binding_removed", "binding_expired",
    "auth_failure", "replay_rejected", "reply_auth_invalid", "request_malformed",
    "dataplane_error", "tunnel_encap", "tunnel_decap", "tunnel_drop", "tunnel_stats",
    "ro_signal_sent", "ro_signal_received", "ro_binding_active", "ro_binding_removed", "ro_rejected", "ro_failed",
)


class NullEvents:
    def emit(self, node, kind, **kw):
        pass


class MipEventLog:
    """Writes events as they happen; `clock` supplies mono_ns()/wall_ns() (real or simulated)."""

    def __init__(self, path: Path, clock, flush_interval_s: float = 0.0):
        self._rec = CsvRecorder(path, MIP_EVENT_COLUMNS, flush_interval_s)
        self.clock = clock
        self._seq = 0

    def emit(self, node: str, kind: str, home_address="", care_of_address="", identification=None,
             code=None, lifetime_s=None, detail=""):
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        self._seq += 1
        mono, wall = self.clock.mono_ns(), self.clock.wall_ns()
        self._rec.write(self._seq, mono, wall, node, kind, home_address, care_of_address,
                        "" if identification is None else identification, "" if code is None else code,
                        "" if lifetime_s is None else lifetime_s, detail)

    def close(self):
        self._rec.close()


def read_events(path: Path):
    """Reads mip_events*.csv from a run directory (or one file) and merges them by monotonic time.
    Returns (rows, notes) or (None, notes) if nothing exists. Unterminated last lines / malformed rows are
    dropped and reported in notes."""
    path = Path(path)
    files = sorted(path.glob("mip_events*.csv")) if path.is_dir() else [path]
    files = [f for f in files if f.exists()]
    if not files:
        return None, ["no mip_events*.csv found"]
    rows, notes = [], []
    for i, f in enumerate(files):
        r, n = _read_one(f)
        rows += [dict(x, _order=(i, x["seq"])) for x in r]
        notes += n
    rows.sort(key=lambda x: (x["mono_ns"], x["_order"]))
    return rows, notes


def _read_one(path: Path):
    text, notes = path.read_text(errors="replace"), []
    if text and not text.endswith("\n"):
        text = text[: text.rfind("\n") + 1]
        notes.append(f"{path.name}: unterminated last line dropped")
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if header is None or tuple(header) != MIP_EVENT_COLUMNS:
        raise ValueError(f"{path}: unexpected header {header!r}")
    rows, bad = [], 0
    for r in reader:
        if len(r) != len(MIP_EVENT_COLUMNS):
            bad += 1
            continue
        try:
            d = dict(zip(MIP_EVENT_COLUMNS, r))
            for c in MIP_INT_COLUMNS:
                d[c] = int(d[c])
            for c in MIP_OPT_INT_COLUMNS:
                d[c] = None if d[c] == "" else int(d[c])
            rows.append(d)
        except ValueError:
            bad += 1
    if bad:
        notes.append(f"{path.name}: {bad} malformed row(s) skipped")
    return rows, notes
