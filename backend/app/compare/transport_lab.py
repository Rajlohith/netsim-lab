"""Emulated lab for the transport-based approaches (TCP reconnection, QUIC migration): same topology, same probe
workload, same movement/detection timing and same raw files as the Mobile IP labs."""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path

from ..mobileip.simnet import Sim, build_from_topology
from ..netem.topology_config import load_config
from ..traffic import wire
from .apps import StreamProbeClient, StreamProbeServer
from .events import ApproachEventLog
from .probe_files import ProbeFiles
from .tcplite import TcpLiteStack

SERVER_PORT = 5201


class EmuScheduler:
    def __init__(self, sim):
        self.sim = sim

    def now_ns(self):
        return self.sim.clock.mono

    def wall_ns(self):
        return self.sim.clock.wall_ns()

    def call_later(self, delay_s, fn):
        self.sim.after(int(delay_s * 1e9), fn)

    def call_at(self, ns, fn):
        self.sim.at(ns, fn)


class TransportLab:
    def __init__(self, spec, out_dir):
        self.spec = spec
        self.run_dir = Path(out_dir) / spec.scenario_id / spec.run_id
        self.run_dir.mkdir(parents=True)
        self.sim = Sim()
        self.cfg = load_config(spec.topology)
        self.nodes = build_from_topology(self.sim, self.cfg)
        self.cn, self.mn = self.nodes["cn"], self.nodes["mn"]
        self.cn_addr = next(iter(self.cn.ifaces.values()))[0].ip
        if spec.initial_iface not in self.mn.ifaces:
            raise ValueError("unknown initial interface")
        for n in self.mn.ifaces:
            if n == spec.initial_iface:
                self.mn.ifaces[n][1].go_up()
            else:
                self._link_down(n)
        self.current_iface = spec.initial_iface
        self.sched = EmuScheduler(self.sim)
        self.session_id = 0x51554943
        self.files = ProbeFiles(self.run_dir, spec.scenario_id, spec.run_id, self.session_id, self.sim.clock)
        self.log = ApproachEventLog(self.run_dir / "approach_events.csv", self.sim.clock, spec.approach)
        self.handoffs, self._pending, self._attached, self._release_old = 0, None, False, None
        self.hooks = {"sent": self.files.sent, "echo": lambda seq, m, w, size: self.files.echo(seq, size),
                      "recv": self._recv, "event": self._event, "signal_tx": lambda n: self.log.emit("mn", "signal_tx", n, "resume hello")}
        self.server = self.client = None
        self.after_build = []              # callables(lab) run once the stacks exist (test/failure injection)

    # ---- hooks ---------------------------------------------------------------------------------------
    def _recv(self, p, size, dup):
        self.files.received(p, size, self.cn_addr, SERVER_PORT)

    def _event(self, kind, detail=""):
        if kind == "data_resumed":
            return self.recovered(detail)
        self.log.emit("mn", kind if kind in ("conn_opened", "conn_closed", "failure", "note") else "note", 0, detail)

    def _link_down(self, ifname):
        link = self.mn.ifaces[ifname][1]
        peers = {n for n in (link.a[0], link.b[0]) if n != "mn"} if getattr(self.spec, "link_semantics", "both-ends") == "kernel-veth" else ()
        link.go_down(peers)

    def mn_ip(self, iface):
        return self.mn.ifaces[iface][0].ip

    # ---- movement (identical mechanics for every approach) ------------------------------------------
    def move_to(self, at_s, iface):
        self.sim.at(self.t0 + int(at_s * 1e9), lambda: self._do_move(iface))

    def _do_move(self, iface):
        old = self.current_iface
        if iface == old:
            return
        self.handoffs += 1
        hid = f"handoff-{self.handoffs}"
        self._pending, self._attached = hid, False
        self.files.event(hid, "handoff_start", f"{old} -> {iface}")
        self.log.emit("mn", "link_change", 0, f"{old} -> {iface}")
        if self.spec.handoff_mode == "mbb":
            self._release_old = old
        else:
            self._link_down(old)
        self.mn.ifaces[iface][1].go_up()
        self.current_iface = iface
        ip = self.mn_ip(iface)

        def detected():
            self._attached = True
            self.log.emit("mn", "attach", 0, str(ip))
            self.on_attached(ip)
        self.sim.after(int(self.spec.detect_delay_ms * 1e6), detected)

    def recovered(self, detail):
        if not (self._pending and self._attached):
            return
        hid, self._pending = self._pending, None
        self.files.event(hid, "handoff_end", detail)
        self.log.emit("mn", "recovered", 0, detail)
        if self._release_old:
            old, self._release_old = self._release_old, None
            self.sim.after(int(self.spec.old_link_hold_ms * 1e6), lambda: self._release(old))

    def _release(self, old):
        if old != self.current_iface:                  # moved back to it meanwhile: it is the NEW link, keep it
            self._link_down(old)

    # ---- to be provided by subclasses ----------------------------------------------------------------
    def build(self):
        raise NotImplementedError

    def on_attached(self, ip):
        raise NotImplementedError

    def summary(self) -> dict:
        raise NotImplementedError

    # ---- run --------------------------------------------------------------------------------------------
    def run(self):
        s = self.spec
        self.t0 = self.sim.clock.mono
        self.t_end = self.t0 + int((s.duration_s + 2.0) * 1e9)
        try:
            self.files.event("scenario", "scenario_start", s.scenario_id)
            self.build()
            for fn in self.after_build:
                fn(self)
            for at, iface in s.moves:
                self.move_to(at, iface)
            self.sim.run_until(self.t_end)
            self.files.event("scenario", "scenario_end", "ok")
            extra = self.summary()
        except BaseException:
            for r in self.files.rec.values():
                r.close()
            self.log.close()
            raise
        self.log.close()
        params = {"rate_pps": s.rate_pps, "size_bytes": s.payload, "echo": True}
        self.files.finish(self.t0, params, ["SIMULATED: produced by app.compare.transport_lab"])
        drops = {f"{n}:{r}": c for (n, r), c in sorted(self.sim.drop_counts.items())}
        (self.run_dir / "experiment_summary.json").write_text(json.dumps(
            dict(extra, approach=s.approach, environment="emulated", drops=drops), indent=2, sort_keys=True))
        return self.run_dir


class TcpLab(TransportLab):
    def build(self):
        s = self.spec
        self.cn_stack = TcpLiteStack(self.cn, self.sim)
        self.mn_stack = TcpLiteStack(self.mn, self.sim, seg_observer=self._seg)
        n_s = s.duration_s - s.probe_start_s
        self.server = StreamProbeServer(self.sched, s.rate_pps, n_s, s.payload, self.hooks, session=self.session_id)
        self.cn_stack.listen(SERVER_PORT, self.server.on_accept)
        self.client = StreamProbeClient(
            self.sched, lambda ip: self.mn_stack.connect(ip, self.cn_addr, SERVER_PORT), self.hooks,
            reconnect=True, detect=s.tcp_detect, detect_timeout_s=s.tcp_detect_timeout_s, session=self.session_id)
        self.sim.at(self.t0, lambda: self.client.start(self.mn_ip(s.initial_iface)))
        self.server.start(self.t0 + int(s.probe_start_s * 1e9))
        # stop the stall watchdog shortly after the stream legitimately ends (otherwise it would 'detect' the silence)
        self.sim.at(self.t0 + int((s.duration_s + 0.2) * 1e9), self.client.stop)

    def _seg(self, direction, kind, nbytes):
        if kind in ("syn", "handshake_ack"):
            self.log.emit("mn", "signal_tx", nbytes, f"TCP {kind}")
        elif kind == "synack":
            self.log.emit("mn", "signal_rx", nbytes, "TCP SYN-ACK")

    def on_attached(self, ip):
        self.client.on_network_change(str(ip))

    def summary(self):
        c = self.client
        return {"mode": "emulated-tcplite",
                "overhead": {"mobility_bytes_per_packet": 0.0,
                             "basis": "no mobility-specific header; the TCP/IP headers are the same as without mobility"},
                "continuity": {"transport_connection_preserved": c.connections_opened == 1,
                               "endpoint_address_preserved": False,
                               "connections_opened": c.connections_opened,
                               "server_resent_after_replace": self.server.resends},
                "model": {"transport": "TcpLite (simplified TCP model, see app/compare/tcplite.py)",
                          "detect": self.spec.tcp_detect, "detect_timeout_s": self.spec.tcp_detect_timeout_s,
                          "tcp_stats_mn": self.mn_stack.stats, "tcp_stats_cn": self.cn_stack.stats}}
