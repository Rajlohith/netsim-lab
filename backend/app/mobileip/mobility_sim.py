"""Mobile IPv4-style handoff scenario on the discrete-event emulator (SIMPLIFIED EMULATION).

The REAL parts: the registration protocol code (HomeAgent / MobileNode), the wire formats, authentication,
replay protection, binding lifetime, and byte-accurate IP-in-IP encapsulation of the data packets.
The EMULATED parts: links, routers, delays and the movement itself (an interface is switched up/down).
Raw output uses the Stage 3 file formats (sent/received/echoes/events.csv + meta) plus mip_events.csv, so the
existing traffic analyser computes loss/interruption on simulated runs. Virtual time makes runs byte-repeatable.
"""
from __future__ import annotations

import ipaddress
import json
from dataclasses import dataclass, field
from pathlib import Path

from ..netem.topology_config import load_config
from ..traffic import wire
from ..traffic.clock import iso_utc
from ..traffic.recorder import CsvRecorder
from ..traffic.schema import COLUMNS, SCHEMA_VERSION
from . import ipip, messages as M
from .dataplane import HaDataPlane, MnDataPlane
from .events import MipEventLog
from .home_agent import HomeAgent
from .mobile_node import MobileNode
from .simnet import Sim, SkewedClock, build_from_topology

ROOT = Path(__file__).resolve().parents[3]
HOME_NET, AGENT_ADDR, HOME_ADDR = "10.10.100.0/24", "10.10.100.1", "10.10.100.2"
MN_CTRL_PORT, PROBE_SPORT, PROBE_DPORT = 4341, 40000, 5005
SIM_BOOT_ID = "simulated-virtual-clock"
DEFAULT_KEY = b"educational-demo-key-DO-NOT-USE!"


@dataclass
class MobilitySpec:
    scenario_id: str = "mip-handoff"
    run_id: str = "sim-1"
    duration_s: float = 10.0
    rate_pps: float = 100.0
    probe_payload: int = 64
    initial_iface: str = "eth-a"
    moves: tuple = ((4.0, "eth-b"), (7.0, "eth-a"))      # (at_s, interface to move to)
    lifetime_s: int = 30
    max_lifetime_s: int = 300
    detect_delay_ms: float = 20.0                        # modelled movement detection (agent advert / link-up)
    ha_processing_ms: float = 1.0
    mn_key: bytes = DEFAULT_KEY
    ha_key: bytes = DEFAULT_KEY
    mn_clock_skew_s: float = 0.0
    deregister_at_end: bool = True
    probe_start_s: float = 0.5
    make_before_break: bool = False        # keep the old link up until the new registration completes
    old_link_hold_ms: float = 0.0          # extra time the old link stays up after registration (MBB only)
    link_semantics: str = "both-ends"      # "both-ends" (default) | "kernel-veth": see simnet.SimLink.go_down
    topology: Path = ROOT / "config" / "topology.yaml"


class SimHaDataPlane(HaDataPlane):
    def __init__(self):
        self.tunnels = {}

    def install_binding(self, home, coa):
        self.tunnels[ipaddress.IPv4Address(home)] = ipaddress.IPv4Address(coa)

    def remove_binding(self, home):
        self.tunnels.pop(ipaddress.IPv4Address(home), None)


class SimMnDataPlane(MnDataPlane):
    """keep_old=True (make-before-break): packets for a previous care-of address are still decapsulated."""

    def __init__(self):
        self.care_of = None
        self.accept = set()
        self.keep_old = False

    def set_care_of(self, coa):
        self.care_of = ipaddress.IPv4Address(coa)
        self.accept = (self.accept | {self.care_of}) if self.keep_old else {self.care_of}

    def release(self, coa):
        self.accept.discard(ipaddress.IPv4Address(coa))

    def clear(self):
        self.care_of = None
        self.accept = set()


class MobilitySim:
    def __init__(self, spec: MobilitySpec, out_dir: Path):
        self.spec = spec
        self.run_dir = Path(out_dir) / spec.scenario_id / spec.run_id
        self.run_dir.mkdir(parents=True)
        self.sim = Sim()
        self.cfg = load_config(spec.topology)
        self.nodes = build_from_topology(self.sim, self.cfg)
        self.cn, self.ha_node, self.mn_node = self.nodes["cn"], self.nodes["ha"], self.nodes["mn"]
        self.cn_addr = next(iter(self.cn.ifaces.values()))[0].ip
        self.ha_node.lo_addrs.add(ipaddress.IPv4Address(AGENT_ADDR))
        self.mn_node.lo_addrs.add(ipaddress.IPv4Address(HOME_ADDR))
        self.mn_ifaces = [n for n in self.mn_node.ifaces]
        if spec.initial_iface not in self.mn_ifaces:
            raise ValueError("unknown initial interface")
        for n in self.mn_ifaces:                         # the mobile node starts attached to ONE network only
            if n == spec.initial_iface:
                self.mn_node.ifaces[n][1].go_up()
            else:
                self._link_down(n)
        self.current_iface = spec.initial_iface

        self.mip = MipEventLog(self.run_dir / "mip_events.csv", self.sim.clock)
        self.rec = {n: CsvRecorder(self.run_dir / n, COLUMNS[n], flush_interval_s=1e9)
                    for n in ("sent.csv", "echoes.csv", "received.csv", "events.csv")}
        self.ha_dp, self.mn_dp = SimHaDataPlane(), SimMnDataPlane()
        sa_mn = M.SecurityAssociation(256, spec.mn_key)
        sa_ha = M.SecurityAssociation(256, spec.ha_key)
        self.ha = HomeAgent(AGENT_ADDR, HOME_NET, {HOME_ADDR: sa_ha}, self.ha_dp, self.sim.clock, self.mip,
                            max_lifetime_s=spec.max_lifetime_s)
        self.mn = MobileNode(HOME_ADDR, AGENT_ADDR, sa_mn, SkewedClock(self.sim.clock, int(spec.mn_clock_skew_s * 1e9)),
                             self.mn_dp, self.mip, requested_lifetime_s=spec.lifetime_s)
        self.session_id = 0x4D495031
        self.allowed_tunnel_sources = {AGENT_ADDR}
        self._release_old = None
        self._attached_for_handoff = False
        if spec.make_before_break:
            self.mn_dp.keep_old = True
            self.mn_node.strong_host = True       # registration from the new CoA must leave through the new interface
        self.sent_count = 0
        self.handoffs = 0
        self._pending_handoff = None
        self._first_encap, self._first_decap = set(), set()
        self._wire_up()

    # ---- wiring --------------------------------------------------------------------------------
    def _wire_up(self):
        self.ha_node.udp[M.DEFAULT_PORT] = self._ha_udp
        self.ha_node.forward_hook = self._ha_forward
        self.mn_node.udp[MN_CTRL_PORT] = self._mn_ctrl_udp
        self.mn_node.udp[PROBE_DPORT] = self._mn_probe_udp
        self.mn_node.ipip_handler = self._mn_ipip
        self.cn.udp[PROBE_SPORT] = self._cn_echo_udp

    def _now(self):
        return self.sim.clock.mono, self.sim.clock.wall_ns()

    def _event(self, event_id, kind, detail=""):
        m, w = self._now()
        self.rec["events.csv"].write(event_id, kind, m, w, detail)

    # ---- Home Agent side -----------------------------------------------------------------------
    def _ha_udp(self, node, src, sport, dst, data):
        def process():
            for out in self.ha.handle_datagram(data, (str(src), sport)):
                node.send_udp(AGENT_ADDR, out.dst[0], M.DEFAULT_PORT, out.dst[1], out.data)
        self.sim.after(int(self.spec.ha_processing_ms * 1e6), process)

    def _ha_forward(self, node, h, packet):
        if h.dst not in ipaddress.IPv4Network(HOME_NET):
            return False
        coa = self.ha_dp.tunnels.get(h.dst)
        if coa is None:
            self._drop_event("ha", "no_binding")
            self.sim.drop("ha", "no_binding")
            return True
        try:
            outer = ipip.encapsulate(packet, AGENT_ADDR, coa)
        except ipip.IPError:
            self.sim.drop("ha", "ttl_expired")
            return True
        self.ha.note_tunnel_packet(len(packet), len(outer))
        if (str(h.dst), str(coa)) not in self._first_encap:
            self._first_encap.add((str(h.dst), str(coa)))
            self.mip.emit("ha", "tunnel_encap", h.dst, coa, detail="first packet tunnelled to this care-of address")
        node.send_ip(outer)
        return True

    def _drop_event(self, node, reason):
        if (node, reason) not in self.sim.drop_counts:      # first occurrence only; counts go to the summary
            self.mip.emit(node, "tunnel_drop", detail=reason)

    # ---- Mobile Node side ----------------------------------------------------------------------
    def _mn_send(self, outs):
        for o in outs:
            if self.mn.care_of is None:
                continue
            self.mn_node.send_udp(self.mn.care_of, o.dst[0], MN_CTRL_PORT, o.dst[1], o.data)

    def _mn_ctrl_udp(self, node, src, sport, dst, data):
        self._mn_send(self.mn.handle_datagram(data, (str(src), sport)))
        self._check_handoff_done()

    def _check_handoff_done(self):
        # only after the MN has actually re-attached: its state is still "registered" from the OLD binding until then
        if self._pending_handoff and self._attached_for_handoff and self._handoff_complete():
            hid = self._pending_handoff
            self._pending_handoff = None
            self._event(hid, "handoff_end", f"mn state={self.mn.state}, care-of={self.mn.care_of}")
            if self._release_old:
                old, self._release_old = self._release_old, None
                self.sim.after(int(self.spec.old_link_hold_ms * 1e6), lambda: self._drop_old_link(old))

    def _handoff_complete(self):
        return self.mn.state in ("registered", "failed")

    def _mn_ipip(self, node, h, packet):
        coa = self.mn_dp.care_of
        if coa is None or h.dst not in self.mn_dp.accept or str(h.src) not in self.allowed_tunnel_sources:
            return self.sim.drop("mn", "tunnel_unexpected")
        try:
            _outer, inner = ipip.decapsulate(packet)
            ih, ipayload = ipip.parse_ipv4(inner)
        except ipip.IPError:
            return self.sim.drop("mn", "bad_tunnel_packet")
        if str(h.dst) not in self._first_decap:
            self._first_decap.add(str(h.dst))
            self.mip.emit("mn", "tunnel_decap", HOME_ADDR, h.dst, detail="first packet decapsulated on this care-of address")
        if ih.dst in node.local_addresses():
            node._deliver(ih, inner, ipayload)
        else:
            self.sim.drop("mn", "inner_not_local")

    def _mn_probe_udp(self, node, src, sport, dst, data):
        m, w = self._now()
        try:
            p = wire.decode(data)
        except wire.WireError:
            return
        self.rec["received.csv"].write(p.seq, p.session_id, m, w, p.send_mono_ns, p.send_wall_ns, len(data),
                                       str(src), sport)
        node.send_udp(HOME_ADDR, src, PROBE_DPORT, sport, wire.make_echo(data))   # triangle routing back to CN

    # ---- Correspondent Node (probe source) ----------------------------------------------------
    def _cn_send(self, seq, scheduled):
        m, w = self._now()
        data = wire.encode(wire.TYPE_PROBE, self.session_id, seq, m, w, self.spec.probe_payload)
        self.rec["sent.csv"].write(seq, scheduled, m, w, len(data), 0)
        self.cn.send_udp(self.cn_addr, HOME_ADDR, PROBE_SPORT, PROBE_DPORT, data)

    def _cn_echo_udp(self, node, src, sport, dst, data):
        m, w = self._now()
        try:
            p = wire.decode(data)
        except wire.WireError:
            return
        if p.ptype == wire.TYPE_ECHO and p.session_id == self.session_id:
            self.rec["echoes.csv"].write(p.seq, m, w, len(data))

    # ---- scenario control ----------------------------------------------------------------------
    def move_to(self, at_s: float, iface: str):
        if iface not in self.mn_ifaces:
            raise ValueError("unknown interface")
        self.sim.at(self.t0 + int(at_s * 1e9), lambda: self._do_move(iface))

    def _do_move(self, iface):
        old = self.current_iface
        if iface == old:
            return
        self.handoffs += 1
        hid = f"handoff-{self.handoffs}"
        self._pending_handoff = hid
        self._attached_for_handoff = False
        self._event(hid, "handoff_start", f"{old} -> {iface}")
        make_first = self.spec.make_before_break
        if not make_first:
            self._link_down(old)                          # break-before-make: the old link vanishes with the movement
        else:
            self._release_old = old                       # make-before-break: both links up until registered
        self.mn_node.ifaces[iface][1].go_up()
        self.current_iface = iface
        coa = self.mn_node.ifaces[iface][0].ip
        self.mip.emit("mn", "link_change", HOME_ADDR, coa, detail=f"link {iface} up, {old} down (emulated movement)")

        def detected():
            self._attached_for_handoff = True
            self._mn_send(self.mn.attach(coa))
        self.sim.after(int(self.spec.detect_delay_ms * 1e6), detected)

    def _link_down(self, ifname):
        link = self.mn_node.ifaces[ifname][1]
        peers = {n for n in (link.a[0], link.b[0]) if n != "mn"} if self.spec.link_semantics == "kernel-veth" else ()
        link.go_down(peers)

    def _drop_old_link(self, old):
        if old == self.current_iface:
            return        # the mobile node moved BACK to this link before the release fired: it is the NEW link now
        self._link_down(old)
        self.mn_dp.release(self.mn_node.ifaces[old][0].ip)
        self.mip.emit("mn", "detach", HOME_ADDR, self.mn_node.ifaces[old][0].ip,
                      detail=f"old link {old} released after the new registration")

    def _tick(self):
        self.ha.tick()
        self._mn_send(self.mn.poll())
        self._check_handoff_done()
        if self.sim.clock.mono < self.t_end:
            self.sim.after(1_000_000, self._tick)            # 1 ms protocol timer resolution

    def run(self):
        s = self.spec
        self.t0 = self.sim.clock.mono
        self.t_end = self.t0 + int((s.duration_s + 2.0) * 1e9)
        self._event("scenario", "scenario_start", s.scenario_id)
        coa0 = self.mn_node.ifaces[s.initial_iface][0].ip
        self.sim.at(self.t0, lambda: self._mn_send(self.mn.attach(coa0)))
        n = int((s.duration_s - s.probe_start_s) * s.rate_pps)
        for k in range(n):
            t = self.t0 + int(s.probe_start_s * 1e9) + int(k * 1e9 / s.rate_pps)      # same formula as compare/apps.py
            self.sim.at(t, lambda k=k, t=t: self._cn_send(k, t))
        self.sent_count = n
        for at, iface in s.moves:
            self.move_to(at, iface)
        if s.deregister_at_end:
            self.sim.at(self.t0 + int((s.duration_s + 0.5) * 1e9), lambda: self._mn_send(self.mn.deregister()))
        self.sim.at(self.t0 + 1_000_000, self._tick)
        self.sim.run_until(self.t_end)
        self._finish()
        return self.run_dir

    # ---- outputs -------------------------------------------------------------------------------
    def _meta(self, role, counters, params):
        s, w0 = self.spec, self.sim.clock
        start_w = w0.wall_ns() - (w0.mono - self.t0)
        return {"schema_version": SCHEMA_VERSION, "tool": "netsim-udp-probe (simulated)", "role": role,
                "scenario_id": s.scenario_id, "run_id": s.run_id, "session_id": self.session_id,
                "status": "completed", "stop_reason": "duration", "start_wall_iso": iso_utc(start_w),
                "start_wall_ns": start_w, "start_mono_ns": self.t0, "end_wall_iso": iso_utc(w0.wall_ns()),
                "end_wall_ns": w0.wall_ns(), "end_mono_ns": w0.mono, "params": params, "counters": counters,
                "clock": {"monotonic_source": "simulated virtual clock", "wall_source": "simulated (fixed origin)",
                          "monotonic_resolution_ns": 1, "boot_id": SIM_BOOT_ID, "hostname": "simulator"},
                "files": {}, "notes": ["SIMULATED: produced by app.mobileip.mobility_sim, not by real sockets"]}

    def _finish(self):
        self.ha.shutdown()
        self._event("scenario", "scenario_end", "ok")
        for r in self.rec.values():
            r.close()
        self.mip.close()
        s, d = self.spec, self.run_dir
        rows = {n: r.rows_written for n, r in self.rec.items()}
        params = {"rate_pps": s.rate_pps, "size_bytes": s.probe_payload, "echo": True}
        (d / "sender.meta.json").write_text(json.dumps(self._meta("sender", {"sent_rows": rows["sent.csv"],
                                                                              "sent_attempted": rows["sent.csv"],
                                                                              "sent_ok": rows["sent.csv"],
                                                                              "echo_rows": rows["echoes.csv"]}, params), indent=2))
        (d / "receiver.meta.json").write_text(json.dumps(self._meta("receiver", {"received_rows": rows["received.csv"]}, params), indent=2))
        (d / "scenario.json").write_text(json.dumps({
            "schema_version": SCHEMA_VERSION, "scenario_id": s.scenario_id, "run_id": s.run_id,
            "clock": {"boot_id": SIM_BOOT_ID}, "mode": "simulation", "topology": self.cfg.name,
            "limitations": "virtual time; events and packets share one simulated clock"}, indent=2))
        drops = {f"{n}:{r}": c for (n, r), c in sorted(self.sim.drop_counts.items())}
        (d / "mip_summary.json").write_text(json.dumps({
            "schema_version": 1, "mode": "simulation", "home_address": HOME_ADDR, "agent_address": AGENT_ADDR,
            "tunnel_overhead_basis": "counted from byte-accurate encapsulated packets (ipip.encapsulate)",
            "probe_payload_bytes": s.probe_payload, "ha_stats": self.ha.stats, "drops": drops,
            "mn_final_state": self.mn.state, "moves": [list(m) for m in s.moves]}, indent=2, sort_keys=True))
