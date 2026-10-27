"""Crash-tolerant raw-data writers.

* CSV rows are flushed at most `flush_interval_s` apart, so a SIGKILL loses at most that window
  and can truncate only the final line (readers drop an unterminated last line).
* Metadata JSON is rewritten atomically; its status stays "running" if the process dies without
  finalising, which is how incomplete runs are recognised later.
"""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

from . import clock
from .schema import COLUMNS, EVENT_KINDS, SCHEMA_VERSION


class CsvRecorder:
    def __init__(self, path: Path, columns, flush_interval_s: float = 0.2):
        self.path = Path(path)
        self.columns = tuple(columns)
        self._f = open(self.path, "x", newline="")      # 'x': never overwrite raw data
        self._w = csv.writer(self._f, lineterminator="\n")
        self._w.writerow(self.columns)
        self._f.flush()
        self._interval = flush_interval_s
        self._last_flush = time.monotonic()
        self.rows_written = 0

    def write(self, *values):
        if len(values) != len(self.columns):
            raise ValueError("wrong number of columns")
        self._w.writerow(values)
        self.rows_written += 1
        if time.monotonic() - self._last_flush >= self._interval:
            self.flush()

    def flush(self):
        self._f.flush()
        self._last_flush = time.monotonic()

    def close(self):
        if not self._f.closed:
            self._f.flush()
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass
            self._f.close()


class RunMeta:
    """<role>.meta.json: written at start (status=running) and finalised at the end."""

    def __init__(self, path: Path, role: str, scenario_id: str, run_id: str, session_id: int,
                 params: dict, files: dict, notes=()):
        self.path = Path(path)
        w, m = clock.wall_ns(), clock.mono_ns()
        self.data = {
            "schema_version": SCHEMA_VERSION, "tool": "netsim-udp-probe", "role": role,
            "scenario_id": scenario_id, "run_id": run_id, "session_id": session_id,
            "status": "running", "stop_reason": None,
            "start_wall_iso": clock.iso_utc(w), "start_wall_ns": w, "start_mono_ns": m,
            "end_wall_iso": None, "end_wall_ns": None, "end_mono_ns": None,
            "params": params, "counters": {}, "clock": clock.clock_info(),
            "files": files, "notes": list(notes),
        }
        self.write()

    def write(self):
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        os.replace(tmp, self.path)

    def finalize(self, status: str, stop_reason: str, counters: dict):
        w, m = clock.wall_ns(), clock.mono_ns()
        self.data.update(status=status, stop_reason=stop_reason, counters=counters,
                         end_wall_iso=clock.iso_utc(w), end_wall_ns=w, end_mono_ns=m)
        self.write()


class EventLog:
    """events.csv: orchestrator-side markers (scenario start/end, handoff start/end)."""

    def __init__(self, run_dir: Path):
        self._rec = CsvRecorder(Path(run_dir) / "events.csv", COLUMNS["events.csv"], flush_interval_s=0.0)

    def record(self, event_id: str, kind: str, detail: str = ""):
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        mono = clock.mono_ns()          # take both stamps back to back
        self._rec.write(event_id, kind, mono, clock.wall_ns(), detail)
        self._rec.flush()
        return mono

    def close(self):
        self._rec.close()
