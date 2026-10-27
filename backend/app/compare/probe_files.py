"""Stage 3 raw-file writer shared by the transport labs (sent/received/echoes/events.csv + metas + scenario.json)."""
from __future__ import annotations

import json
from pathlib import Path

from ..traffic.clock import iso_utc
from ..traffic.recorder import CsvRecorder
from ..traffic.schema import COLUMNS, SCHEMA_VERSION

SIM_BOOT_ID = "simulated-virtual-clock"


class ProbeFiles:
    def __init__(self, run_dir: Path, scenario_id: str, run_id: str, session_id: int, clock, boot_id: str = SIM_BOOT_ID,
                 tool: str = "netsim-udp-probe (simulated)"):
        self.dir, self.sid, self.rid, self.session, self.clock = Path(run_dir), scenario_id, run_id, session_id, clock
        self.boot_id, self.tool = boot_id, tool
        self.rec = {n: CsvRecorder(self.dir / n, COLUMNS[n], flush_interval_s=1e9)
                    for n in ("sent.csv", "echoes.csv", "received.csv", "events.csv")}

    def stamps(self):
        return self.clock.mono_ns(), self.clock.wall_ns()

    def sent(self, seq, scheduled, mono, wall, size):
        self.rec["sent.csv"].write(seq, scheduled, mono, wall, size, 0)

    def echo(self, seq, size):
        m, w = self.stamps()
        self.rec["echoes.csv"].write(seq, m, w, size)

    def received(self, p, size, src_ip, src_port):
        m, w = self.stamps()
        self.rec["received.csv"].write(p.seq, p.session_id, m, w, p.send_mono_ns, p.send_wall_ns, size, str(src_ip), src_port)

    def event(self, event_id, kind, detail=""):
        m, w = self.stamps()
        self.rec["events.csv"].write(event_id, kind, m, w, detail)

    def finish(self, t0_mono, params, notes, mode="simulation"):
        for r in self.rec.values():
            r.close()
        rows = {n: r.rows_written for n, r in self.rec.items()}
        end_m, end_w = self.clock.mono_ns(), self.clock.wall_ns()
        start_w = end_w - (end_m - t0_mono)

        def meta(role, counters):
            return {"schema_version": SCHEMA_VERSION, "tool": self.tool, "role": role,
                    "scenario_id": self.sid, "run_id": self.rid, "session_id": self.session, "status": "completed",
                    "stop_reason": "duration", "start_wall_iso": iso_utc(start_w), "start_wall_ns": start_w,
                    "start_mono_ns": t0_mono, "end_wall_iso": iso_utc(end_w), "end_wall_ns": end_w, "end_mono_ns": end_m,
                    "params": params, "counters": counters,
                    "clock": {"monotonic_source": "simulated virtual clock" if self.boot_id == SIM_BOOT_ID else "time.monotonic_ns",
                              "wall_source": "simulated (fixed origin)" if self.boot_id == SIM_BOOT_ID else "time.time_ns",
                              "monotonic_resolution_ns": 1, "boot_id": self.boot_id, "hostname": "simulator" if self.boot_id == SIM_BOOT_ID else "localhost"},
                    "files": {}, "notes": notes}
        (self.dir / "sender.meta.json").write_text(json.dumps(meta("sender", {
            "sent_rows": rows["sent.csv"], "sent_attempted": rows["sent.csv"], "sent_ok": rows["sent.csv"],
            "echo_rows": rows["echoes.csv"]}), indent=2))
        (self.dir / "receiver.meta.json").write_text(json.dumps(meta("receiver", {"received_rows": rows["received.csv"]}), indent=2))
        (self.dir / "scenario.json").write_text(json.dumps({
            "schema_version": SCHEMA_VERSION, "scenario_id": self.sid, "run_id": self.rid,
            "clock": {"boot_id": self.boot_id}, "mode": mode}, indent=2))
        return rows
