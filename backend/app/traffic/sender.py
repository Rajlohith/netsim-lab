"""Sequence-numbered UDP probe sender (also reads echoes to obtain RTT).

Raw output (in <out>/<scenario_id>/<run_id>/):
  sent.csv, echoes.csv, sender.meta.json
Pacing uses absolute deadlines on the monotonic clock (no cumulative drift); the schedule slip
of every packet is recorded (scheduled_mono_ns vs send_mono_ns).
"""
from __future__ import annotations

import argparse
import select
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from . import clock, wire
from .recorder import CsvRecorder, RunMeta
from .schema import COLUMNS, new_run_id, validate_id
from .shutdown import ShutdownFlag


@dataclass
class SenderConfig:
    target: tuple
    scenario_id: str
    out_dir: Path
    run_id: str | None = None
    rate_pps: float = 100.0
    duration_s: float | None = 10.0
    count: int | None = None
    size: int = 64
    bind_addr: str | None = None
    drain_s: float = 1.0
    start_at_mono_ns: int | None = None
    stop_file: Path | None = None
    ready_file: Path | None = None
    flush_interval_s: float = 0.2
    session_id: int | None = None


class ProbeSender:
    def __init__(self, cfg: SenderConfig):
        if cfg.rate_pps <= 0 or cfg.rate_pps > 100_000:
            raise ValueError("rate_pps must be in (0, 100000]")
        if cfg.duration_s is None and cfg.count is None:
            raise ValueError("give duration_s and/or count")
        if cfg.duration_s is not None and cfg.duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        cfg.run_id = cfg.run_id or new_run_id()
        validate_id(cfg.scenario_id, "scenario_id")
        validate_id(cfg.run_id, "run_id")
        wire.encode(wire.TYPE_PROBE, 0, 0, 0, 0, cfg.size)       # validates size
        self.cfg = cfg
        self.run_dir = Path(cfg.out_dir) / cfg.scenario_id / cfg.run_id
        self.flag = ShutdownFlag(cfg.stop_file)
        self.session_id = cfg.session_id if cfg.session_id is not None else int.from_bytes(__import__("os").urandom(4), "big")
        self.counters = {"sent_attempted": 0, "sent_ok": 0, "send_errors": 0,
                         "echoes_received": 0, "invalid_datagrams": 0, "foreign_echoes": 0}

    # ------------------------------------------------------------------
    def run(self) -> int:
        cfg = self.cfg
        self.run_dir.mkdir(parents=True, exist_ok=True)
        files = {"sent": "sent.csv", "echoes": "echoes.csv", "meta": "sender.meta.json"}
        sent = CsvRecorder(self.run_dir / "sent.csv", COLUMNS["sent.csv"], cfg.flush_interval_s)
        echoes = CsvRecorder(self.run_dir / "echoes.csv", COLUMNS["echoes.csv"], cfg.flush_interval_s)
        params = {"target": f"{cfg.target[0]}:{cfg.target[1]}", "rate_pps": cfg.rate_pps,
                  "duration_s": cfg.duration_s, "count": cfg.count, "size_bytes": cfg.size,
                  "bind_addr": cfg.bind_addr, "drain_s": cfg.drain_s,
                  "start_at_mono_ns": cfg.start_at_mono_ns}
        meta = RunMeta(self.run_dir / "sender.meta.json", "sender", cfg.scenario_id, cfg.run_id,
                       self.session_id, params, files)
        status, reason = "failed", "error:unknown"
        self.flag.install()
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            if cfg.bind_addr:
                sock.bind((cfg.bind_addr, 0))
            sock.setblocking(False)
            if cfg.ready_file:
                Path(cfg.ready_file).write_text("ready\n")
            reason = self._loop(sock, sent, echoes)
            status = "interrupted" if reason.startswith(("signal:", "stop_file")) else "completed"
            return 0
        except BaseException as e:          # incl. unexpected errors: record, then re-raise
            status, reason = "failed", f"error:{type(e).__name__}:{e}"
            raise
        finally:
            self.flag.restore()
            if sock:
                sock.close()
            sent.close()
            echoes.close()
            meta.finalize(status, reason, dict(self.counters,
                                               sent_rows=sent.rows_written, echo_rows=echoes.rows_written))

    # ------------------------------------------------------------------
    def _loop(self, sock, sent: CsvRecorder, echoes: CsvRecorder) -> str:
        cfg = self.cfg
        interval_ns = int(1e9 / cfg.rate_pps)
        duration_ns = int(cfg.duration_s * 1e9) if cfg.duration_s is not None else None
        drain_ns = int(cfg.drain_s * 1e9)
        t0 = cfg.start_at_mono_ns if cfg.start_at_mono_ns is not None else clock.mono_ns()
        seq = 0
        sending, reason, drain_deadline = True, "", 0

        while True:
            now = clock.mono_ns()
            if sending:
                if self.flag.requested:
                    sending, reason = False, self.flag.reason or "stop"
                    drain_deadline = now + drain_ns
                elif cfg.count is not None and seq >= cfg.count:
                    sending, reason, drain_deadline = False, "count", now + drain_ns
                elif duration_ns is not None and seq * interval_ns >= duration_ns:
                    sending, reason, drain_deadline = False, "duration", now + drain_ns
                else:
                    sched = t0 + seq * interval_ns
                    if now >= sched:
                        self._send_one(sock, sent, seq, sched)
                        seq += 1
                        self._drain_echoes(sock, echoes)
                        continue
                    wait_ns = sched - now
            if not sending:
                if now >= drain_deadline or self.flag.count >= 2:
                    return reason
                wait_ns = drain_deadline - now
            wait = max(0.0, min(wait_ns / 1e9, 0.05))
            if select.select([sock], [], [], wait)[0]:
                self._drain_echoes(sock, echoes)

    def _send_one(self, sock, sent: CsvRecorder, seq: int, scheduled: int):
        cfg = self.cfg
        mono = clock.mono_ns()
        wall = clock.wall_ns()
        pkt = wire.encode(wire.TYPE_PROBE, self.session_id, seq, mono, wall, cfg.size)
        errno = 0
        try:
            sock.sendto(pkt, cfg.target)
        except OSError as e:
            errno = e.errno or -1
        self.counters["sent_attempted"] += 1
        if errno:
            self.counters["send_errors"] += 1
        else:
            self.counters["sent_ok"] += 1
        sent.write(seq, scheduled, mono, wall, len(pkt), errno)

    def _drain_echoes(self, sock, echoes: CsvRecorder):
        for _ in range(10_000):
            try:
                data, _addr = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            mono = clock.mono_ns()
            wall = clock.wall_ns()
            try:
                p = wire.decode(data)
            except wire.WireError:
                self.counters["invalid_datagrams"] += 1
                continue
            if p.ptype != wire.TYPE_ECHO or p.session_id != self.session_id:
                self.counters["foreign_echoes"] += 1
                continue
            self.counters["echoes_received"] += 1
            echoes.write(p.seq, mono, wall, len(data))


def _parse_target(text: str):
    host, _, port = text.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("target must be HOST:PORT (IPv4)")
    return host, int(port)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.traffic.sender", description=__doc__)
    p.add_argument("target", type=_parse_target, help="receiver IPv4:PORT")
    p.add_argument("--scenario-id", required=True)
    p.add_argument("--run-id")
    p.add_argument("--out-dir", type=Path, default=Path("data/results"))
    p.add_argument("--rate", type=float, default=100.0, dest="rate_pps", help="packets per second")
    p.add_argument("--duration", type=float, default=None, dest="duration_s", help="seconds of sending")
    p.add_argument("--count", type=int, default=None, help="number of probes")
    p.add_argument("--size", type=int, default=64, help="UDP payload bytes (36..1472)")
    p.add_argument("--bind", default=None, dest="bind_addr")
    p.add_argument("--drain", type=float, default=1.0, dest="drain_s", help="seconds to wait for late echoes")
    p.add_argument("--start-at-mono-ns", type=int, default=None,
                   help="absolute CLOCK_MONOTONIC start (same-host orchestration only)")
    p.add_argument("--stop-file", type=Path, default=None)
    p.add_argument("--ready-file", type=Path, default=None)
    a = p.parse_args(argv)
    if a.duration_s is None and a.count is None:
        a.duration_s = 10.0
    cfg = SenderConfig(a.target, a.scenario_id, a.out_dir, a.run_id, a.rate_pps, a.duration_s, a.count,
                       a.size, a.bind_addr, a.drain_s, a.start_at_mono_ns, a.stop_file, a.ready_file)
    try:
        sender = ProbeSender(cfg)
        rc = sender.run()
    except (ValueError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"scenario={cfg.scenario_id} run={cfg.run_id} dir={sender.run_dir} counters={sender.counters}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
