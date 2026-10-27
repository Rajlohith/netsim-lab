"""Route-optimisation experiment on the emulator: Mobile IP registration with the Home Agent, then the simplified RO
model with the correspondent node (see ro.py for exactly what is and is not modelled)."""
from __future__ import annotations

import json
import ipaddress

from . import ipip
from .mobility_sim import HOME_ADDR, PROBE_DPORT, PROBE_SPORT, MobilitySim
from .ro import RO_PORT, RoCorrespondent, RoMobile
from ..traffic import wire

RO_SECRET = b"ro-demo-cn-secret-DO-NOT-USE-0001"


class RoSim(MobilitySim):
    def __init__(self, spec, out_dir, ro_lifetime_s: int = 40):
        super().__init__(spec, out_dir)
        self.ro_cn = RoCorrespondent(self.cn_addr, self.sim.clock, RO_SECRET, self.mip, max_lifetime_s=ro_lifetime_s)
        self.ro_mn = RoMobile(HOME_ADDR, self.cn_addr, self.sim.clock, self.mip, bu_lifetime_s=ro_lifetime_s)
        self.cn.udp[RO_PORT] = self._ro_cn_udp
        self.mn_node.udp[RO_PORT] = self._ro_mn_udp
        self.allowed_tunnel_sources.add(str(self.cn_addr))
        self._ro_for = None
        self.ro_stats = {"packets": 0, "inner_bytes": 0, "outer_bytes": 0}

    # ---- correspondent: direct delivery once a binding exists --------------------------------------
    def _cn_send(self, seq, scheduled):
        m, w = self._now()
        data = wire.encode(wire.TYPE_PROBE, self.session_id, seq, m, w, self.spec.probe_payload)
        self.rec["sent.csv"].write(seq, scheduled, m, w, len(data), 0)
        pkt = ipip.build_udp_ip(self.cn_addr, HOME_ADDR, PROBE_SPORT, PROBE_DPORT, data)
        coa = self.ro_cn.lookup(HOME_ADDR)
        if coa is not None:                         # route-optimised: CN -> care-of address, bypassing the Home Agent
            outer = ipip.encapsulate(pkt, self.cn_addr, coa, decrement_inner_ttl=False)
            self.ro_stats["packets"] += 1
            self.ro_stats["inner_bytes"] += len(pkt)
            self.ro_stats["outer_bytes"] += len(outer)
            pkt = outer
        self.cn.send_ip(pkt)

    def _ro_cn_udp(self, node, src, sport, dst, data):
        for o in self.ro_cn.handle_datagram(data, (str(src), sport)):
            node.send_udp(self.cn_addr, o.dst[0], RO_PORT, o.dst[1], o.data)

    # ---- mobile node ------------------------------------------------------------------------------
    def _ro_send(self, outs):
        for o in outs:
            self.mn_node.send_ip(ipip.build_udp_ip(o.src, o.dst[0], RO_PORT, o.dst[1], o.data))

    def _ro_mn_udp(self, node, src, sport, dst, data):
        self._ro_send(self.ro_mn.handle_datagram(data, (str(src), sport)))
        self._check_handoff_done()

    def _maybe_start_ro(self):
        coa = self.mn.care_of
        if self.mn.state == "registered" and coa is not None and self._ro_for != coa:
            self._ro_for = coa
            self._ro_send(self.ro_mn.start(coa))

    def _mn_ctrl_udp(self, node, src, sport, dst, data):
        super()._mn_ctrl_udp(node, src, sport, dst, data)
        self._maybe_start_ro()

    def _handoff_complete(self):
        if self.mn.state == "failed" or self.ro_mn.state == "failed":
            return True
        return self.ro_mn.state == "active" and self.ro_mn.active_coa == self.mn.care_of

    def _tick(self):
        self._ro_send(self.ro_mn.poll())
        self.ro_cn.tick()
        super()._tick()

    def _finish(self):
        super()._finish()
        p = self.run_dir / "mip_summary.json"
        s = json.loads(p.read_text())
        s["mode"] = "simulation-ro"
        s["ro"] = dict(self.ro_stats, model="simplified route optimisation (see ro.py), not Mobile IPv6")
        p.write_text(json.dumps(s, indent=2, sort_keys=True))
