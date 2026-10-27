import ipaddress

from app.mobileip import messages as M
from app.mobileip.dataplane import RecordingHaDataPlane, RecordingMnDataPlane
from app.mobileip.home_agent import HomeAgent
from app.mobileip.mobile_node import MobileNode

HOME, AGENT, NET = "10.10.100.2", "10.10.100.1", "10.10.100.0/24"
KEY = b"0123456789abcdef-test-key"
SA = M.SecurityAssociation(256, KEY)


class FakeClock:
    def __init__(self, wall_s=1_800_000_000.0):
        self.m, self.w = 1_000_000_000, int(wall_s * 1e9)

    def mono_ns(self):
        return self.m

    def wall_ns(self):
        return self.w

    def advance(self, s):
        self.m += int(s * 1e9)
        self.w += int(s * 1e9)


class ListEvents:
    def __init__(self):
        self.rows = []

    def emit(self, node, kind, home_address="", care_of_address="", identification=None, code=None,
             lifetime_s=None, detail=""):
        self.rows.append(dict(node=node, kind=kind, home=str(home_address), coa=str(care_of_address),
                              ident=identification, code=code, life=lifetime_s, detail=detail))

    def kinds(self, node=None):
        return [r["kind"] for r in self.rows if node in (None, r["node"])]


class Pair:
    """HomeAgent + MobileNode wired together in memory (no sockets, controllable clock)."""

    def __init__(self, *, mn_key=KEY, ha_key=KEY, max_lifetime=300, lifetime=60, skew_s=0.0, **mn_kw):
        self.clock = FakeClock()
        self.mn_clock = FakeClock()
        self.mn_clock.w += int(skew_s * 1e9)
        self.mn_clock = _Skew(self.clock, int(skew_s * 1e9))
        self.events = ListEvents()
        self.ha_dp, self.mn_dp = RecordingHaDataPlane(), RecordingMnDataPlane()
        self.ha = HomeAgent(AGENT, NET, {HOME: M.SecurityAssociation(256, ha_key)}, self.ha_dp, self.clock,
                            self.events, max_lifetime_s=max_lifetime)
        self.mn = MobileNode(HOME, AGENT, M.SecurityAssociation(256, mn_key), self.mn_clock, self.mn_dp,
                             self.events, requested_lifetime_s=lifetime, **mn_kw)
        self.wire = []                                   # (direction, bytes) log

    def exchange(self, outs, src=("10.10.4.2", 4341)):
        """Deliver MN->HA requests and HA->MN replies until quiet; returns number of messages."""
        n = 0
        queue = list(outs)
        while queue:
            o = queue.pop(0)
            self.wire.append(("req", o.data))
            n += 1
            for rep in self.ha.handle_datagram(o.data, src):
                self.wire.append(("rep", rep.data))
                n += 1
                queue += self.mn.handle_datagram(rep.data, (AGENT, 4340))
        return n

    def attach(self, coa="10.10.4.2"):
        self.exchange(self.mn.attach(coa), (coa, 4341))

    def advance(self, s, step=0.05):
        t = 0.0
        while t < s - 1e-9:
            self.clock.advance(step)
            t += step
            self.ha.tick()
            self.exchange(self.mn.poll())


class _Skew:
    def __init__(self, base, skew):
        self.base, self.skew = base, skew

    def mono_ns(self):
        return self.base.mono_ns()

    def wall_ns(self):
        return self.base.wall_ns() + self.skew


def raw_request(*, lifetime=60, coa="10.10.4.2", flags=M.FLAG_D, ident=None, clock=None, home=HOME, agent=AGENT,
                sa=SA):
    clock = clock or FakeClock()
    ident = M.ident_from_wall_ns(clock.wall_ns()) if ident is None else ident
    req = M.RegistrationRequest(flags, lifetime, ipaddress.IPv4Address(home), ipaddress.IPv4Address(agent),
                                ipaddress.IPv4Address(coa), ident)
    return M.encode_request(req, sa), req
