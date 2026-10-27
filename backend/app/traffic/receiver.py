"""UDP probe receiver: logs every valid probe it sees (and optionally echoes it back).

Raw output: received.csv, receiver.meta.json (same run directory as the sender).
Stops on: signal / stop file, --duration, --max-packets, --idle-timeout (after first packet),
--startup-timeout (no packet ever arrived). Datagrams that are not valid probes are only counted.
"""
from __future__ import annotations

import argparse
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from . import clock, wire
from .recorder import CsvRecorder, RunMeta
from .schema import COLUMNS, new_run_id, validate_id
from .shutdown import ShutdownFlag


@dataclass
class ReceiverConfig:
    scenario_id: str
    out_dir: Path
    run_id: str | None = None
    bind_addr: str = "0.0.0.0"
    port: int = 5005
    echo: bool = True
    duration_s: float | None = None
    idle_timeout_s: float | None = 3.0
    startup_timeout_s: float = 30.0
    max_packets: int | None = None
    stop_file: Path | None = None
    ready_file: Path | None = None     # receives the bound port once listening
    flush_interval_s: float = 0.2


class ProbeReceiver:
    def __init__(self, cfg: ReceiverConfig):
        if not cfg.run_id:
            raise ValueError("run_id is required for the receiver (it must match the sender's)")
        validate_id(cfg.scenario_id, "scenario_id")
        validate_id(cfg.run_id, "run_id")
        self.cfg = cfg
        self.run_dir = Path(cfg.out_dir) / cfg.scenario_id / cfg.run_id
        self.flag = ShutdownFlag(cfg.stop_file)
        self.port = None
        self.counters = {"received_valid": 0, "invalid_datagrams": 0, "echo_sent": 0, "echo_errors": 0,
                         "non_probe_packets": 0}

    def run(self) -> int:
        cfg = self.cfg
        self.run_dir.mkdir(parents=True, exist_ok=True)
        rec = CsvRecorder(self.run_dir / "received.csv", COLUMNS["received.csv"], cfg.flush_interval_s)
        params = {"bind": f"{cfg.bind_addr}:{cfg.port}", "echo": cfg.echo, "duration_s": cfg.duration_s,
                  "idle_timeout_s": cfg.idle_timeout_s, "startup_timeout_s": cfg.startup_timeout_s,
                  "max_packets": cfg.max_packets}
        meta = RunMeta(self.run_dir / "receiver.meta.json", "receiver", cfg.scenario_id, cfg.run_id, 0,
                       params, {"received": "received.csv", "meta": "receiver.meta.json"})
        status, reason = "failed", "error:unknown"
        sock = None
        self.flag.install()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            sock.bind((cfg.bind_addr, cfg.port))
            self.port = sock.getsockname()[1]
            meta.data["params"]["bound_port"] = self.port
            meta.write()
            if cfg.ready_file:
                Path(cfg.ready_file).write_text(f"{self.port}\n")
            reason = self._loop(sock, rec)
            status = "interrupted" if reason.startswith(("signal:", "stop_file")) else "completed"
            return 0
        except BaseException as e:
            status, reason = "failed", f"error:{type(e).__name__}:{e}"
            raise
        finally:
            self.flag.restore()
            if sock:
                sock.close()
            rec.close()
            meta.finalize(status, reason, dict(self.counters, received_rows=rec.rows_written))

    def _loop(self, sock, rec: CsvRecorder) -> str:
        cfg = self.cfg
        sock.settimeout(0.05)
        start = clock.mono_ns()
        last_rx = None
        last_check = 0
        n = 0
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except (socket.timeout, OSError):
                data = None
            if data is not None:
                mono = clock.mono_ns()          # stamp immediately after recvfrom returns
                wall = clock.wall_ns()
                try:
                    p = wire.decode(data)
                except wire.WireError:
                    self.counters["invalid_datagrams"] += 1
                    p = None
                if p is not None and p.ptype != wire.TYPE_PROBE:
                    self.counters["non_probe_packets"] += 1
                    p = None
                if p is not None:
                    rec.write(p.seq, p.session_id, mono, wall, p.send_mono_ns, p.send_wall_ns,
                              len(data), addr[0], addr[1])
                    self.counters["received_valid"] += 1
                    n += 1
                    last_rx = mono
                    if cfg.echo:
                        try:
                            sock.sendto(wire.make_echo(data), addr)
                            self.counters["echo_sent"] += 1
                        except OSError:
                            self.counters["echo_errors"] += 1
                    if cfg.max_packets is not None and n >= cfg.max_packets:
                        return "max_packets"
            # Stop conditions are checked every <=50 ms even under continuous traffic
            # (a busy socket never times out, so they cannot live only in the timeout path).
            now = clock.mono_ns()
            if data is not None and now - last_check < 50_000_000:
                continue
            last_check = now
            if self.flag.requested:
                return self.flag.reason or "stop"
            if cfg.duration_s is not None and now - start >= cfg.duration_s * 1e9:
                return "duration"
            if last_rx is None:
                if now - start >= cfg.startup_timeout_s * 1e9:
                    return "startup_timeout"
            elif cfg.idle_timeout_s is not None and now - last_rx >= cfg.idle_timeout_s * 1e9:
                return "idle_timeout"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.traffic.receiver", description=__doc__)
    p.add_argument("--scenario-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--out-dir", type=Path, default=Path("data/results"))
    p.add_argument("--bind", default="0.0.0.0", dest="bind_addr")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--no-echo", action="store_true")
    p.add_argument("--duration", type=float, default=None, dest="duration_s")
    p.add_argument("--idle-timeout", type=float, default=3.0, dest="idle_timeout_s")
    p.add_argument("--startup-timeout", type=float, default=30.0, dest="startup_timeout_s")
    p.add_argument("--max-packets", type=int, default=None)
    p.add_argument("--stop-file", type=Path, default=None)
    p.add_argument("--ready-file", type=Path, default=None)
    a = p.parse_args(argv)
    cfg = ReceiverConfig(a.scenario_id, a.out_dir, a.run_id, a.bind_addr, a.port, not a.no_echo,
                         a.duration_s, a.idle_timeout_s, a.startup_timeout_s, a.max_packets,
                         a.stop_file, a.ready_file)
    try:
        r = ProbeReceiver(cfg)
        rc = r.run()
    except (ValueError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"scenario={cfg.scenario_id} run={cfg.run_id} dir={r.run_dir} counters={r.counters}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
