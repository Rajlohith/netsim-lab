"""approach_events.csv: the common, approach-independent event log used for the comparison metrics."""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from ..traffic.recorder import CsvRecorder

COLUMNS = ("seq", "mono_ns", "wall_ns", "approach", "node", "kind", "bytes", "detail")
KINDS = ("link_change",      # the physical movement (old link lost / new link available)
         "attach",           # the mobile node can use its new address (after movement detection)
         "signal_tx", "signal_rx",   # mobility/transport signalling the mobile node sent / received (bytes = UDP/TCP payload)
         "recovered",        # approach-defined recovery point (see docs/MOBILITY_COMPARISON.md)
         "conn_opened", "conn_closed", "path_validated", "retransmit", "failure", "note")


class ApproachEventLog:
    def __init__(self, path: Path, clock, approach: str):
        self._rec = CsvRecorder(path, COLUMNS, flush_interval_s=1e9)
        self.clock, self.approach, self._seq = clock, approach, 0

    def emit(self, node: str, kind: str, nbytes: int = 0, detail: str = ""):
        if kind not in KINDS:
            raise ValueError(f"unknown approach event kind {kind!r}")
        self._seq += 1
        self._rec.write(self._seq, self.clock.mono_ns(), self.clock.wall_ns(), self.approach, node, kind, nbytes, detail)

    def close(self):
        self._rec.close()


def read_events(path: Path):
    path = Path(path)
    if not path.exists():
        return None
    text = path.read_text()
    if text and not text.endswith("\n"):
        text = text[: text.rfind("\n") + 1]
    r = csv.reader(io.StringIO(text))
    header = next(r, None)
    if tuple(header or ()) != COLUMNS:
        raise ValueError(f"{path}: unexpected header {header!r}")
    rows = []
    for x in r:
        if len(x) == len(COLUMNS):
            try:
                d = dict(zip(COLUMNS, x))
                for c in ("seq", "mono_ns", "wall_ns", "bytes"):
                    d[c] = int(d[c])
                rows.append(d)
            except ValueError:
                pass
    return rows


# Registration message sizes (UDP payload): request = 24 fixed + 6 ext header/SPI + 32 HMAC; reply = 20 + 6 + 32.
MIP_REQUEST_BYTES, MIP_REPLY_BYTES = 62, 58
_SIZE = re.compile(r"(\d+)B")


def from_mip_events(mip_rows: list, approach: str, recovery: str = "registration"):
    """Map Stage 4 mip_events rows to common rows. recovery='registration' (basic MIP / MBB): first accepted
    registration reply after a link change; recovery='ro' (route optimisation): first RO binding active."""
    out, armed, seq = [], False, 0

    def add(r, kind, nbytes=0, detail=None):
        nonlocal seq
        seq += 1
        out.append({"seq": seq, "mono_ns": r["mono_ns"], "wall_ns": r["wall_ns"], "approach": approach,
                    "node": r["node"], "kind": kind, "bytes": nbytes, "detail": detail if detail is not None else r["detail"]})

    for r in mip_rows:
        k, node = r["kind"], r["node"]
        if node == "mn":
            if k == "link_change":
                armed = True
                add(r, "link_change")
            elif k == "attach":
                add(r, "attach")
            elif k == "reg_request_sent":
                add(r, "signal_tx", MIP_REQUEST_BYTES, "registration request")
            elif k == "reg_reply_received":
                add(r, "signal_rx", MIP_REPLY_BYTES, "registration reply")
                if recovery == "registration" and armed and r["code"] in (0, 1) and (r["lifetime_s"] or 0) > 0:
                    armed = False
                    add(r, "recovered", 0, "registration accepted; home agent now tunnels to the new care-of address")
            elif k == "reg_retransmit":
                add(r, "retransmit")
            elif k in ("reg_failed", "reg_timeout", "ro_failed"):
                add(r, "failure")
            elif k == "ro_signal_sent":
                m = _SIZE.search(r["detail"])
                add(r, "signal_tx", int(m.group(1)) if m else 0)
            elif k == "ro_signal_received":
                m = _SIZE.search(r["detail"])
                add(r, "signal_rx", int(m.group(1)) if m else 0)
            elif k == "ro_binding_active" and recovery == "ro" and armed:
                armed = False
                add(r, "recovered", 0, "route-optimisation binding active at the correspondent")
        elif node == "ha" and k == "auth_failure":
            add(r, "failure")
    return out


def write_rows(path: Path, rows: list):
    rec = CsvRecorder(path, COLUMNS, flush_interval_s=1e9)
    for r in rows:
        rec.write(*(r[c] for c in COLUMNS))
    rec.close()
