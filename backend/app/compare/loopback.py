"""Validation of the TCP-reconnection application logic over REAL kernel TCP sockets (loopback).

Purpose: show that apps.py (resume protocol, reconnect policies) really keeps a session alive across an address
change with an actual TCP stack. NOT a performance environment: there is no path delay, so timings are not comparable
with the emulator (`timing_comparable: false`). The "address change" is emulated by blackholing the client's old socket
(loopback addresses cannot be removed) and reconnecting from a different 127.x.y.z source address.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from ..mobileip.runtime import RealClock
from ..traffic import clock as realclock
from .apps import StreamProbeClient, StreamProbeServer
from .events import ApproachEventLog
from .probe_files import ProbeFiles

LOCAL_IPS = {"eth-a": "127.1.0.2", "eth-b": "127.2.0.2"}


class RealScheduler:
    def __init__(self, loop):
        self.loop = loop

    def now_ns(self):
        return time.monotonic_ns()

    def wall_ns(self):
        return time.time_ns()

    def call_later(self, delay_s, fn):
        self.loop.call_later(max(0.0, delay_s), fn)

    def call_at(self, ns, fn):
        self.loop.call_later(max(0.0, (ns - time.monotonic_ns()) / 1e9), fn)


class RealConn(asyncio.Protocol):
    """One TCP connection seen through the apps.py Conn interface."""

    def __init__(self, on_made=None):
        self.transport = None
        self.on_connected = self.on_data = self.on_closed = None
        self.on_made = on_made
        self.dead = False                       # "blackholed": the path is gone, nothing flows either way
        self.local_ip = None

    def connection_made(self, transport):
        self.transport = transport
        self.local_ip = transport.get_extra_info("sockname")[0]
        if self.on_made:
            self.on_made(self)
        if self.on_connected:
            self.on_connected()

    def data_received(self, data):
        if not self.dead and self.on_data:
            self.on_data(data)

    def connection_lost(self, exc):
        if not self.dead and self.on_closed:
            self.on_closed("lost" if exc else "closed")

    def send(self, data):
        if self.dead or self.transport is None or self.transport.is_closing():
            return False
        self.transport.write(data)
        return True

    def abort(self):
        if self.transport is not None:
            self.transport.abort()

    def blackhole(self):
        self.dead = True


def run_loopback_tcp(spec, out_dir) -> Path:
    run_dir = Path(out_dir) / spec.scenario_id / spec.run_id
    run_dir.mkdir(parents=True)
    return asyncio.run(_run(spec, run_dir))


async def _run(spec, run_dir: Path) -> Path:
    loop = asyncio.get_running_loop()
    sched = RealScheduler(loop)
    session = 0x4C4F4F50
    files = ProbeFiles(run_dir, spec.scenario_id, spec.run_id, session, RealClock, boot_id=realclock.boot_id() or "unknown",
                       tool="netsim-udp-probe (loopback, real TCP)")
    log = ApproachEventLog(run_dir / "approach_events.csv", RealClock, spec.approach)
    state = {"pending": None, "attached": False, "n": 0, "client_conns": [], "closing": False}

    def recovered(detail):
        if state["pending"] and state["attached"]:
            hid, state["pending"] = state["pending"], None
            files.event(hid, "handoff_end", detail)
            log.emit("mn", "recovered", 0, detail)

    def on_event(kind, detail=""):
        if state["closing"]:
            return                                   # late callbacks while tearing down must not touch closed files
        if kind == "data_resumed":
            return recovered(detail)
        log.emit("mn", kind if kind in ("conn_opened", "conn_closed", "failure", "note") else "note", 0, detail)

    hooks = {"sent": files.sent, "echo": lambda seq, m, w, size: files.echo(seq, size),
             "recv": lambda p, size, dup: files.received(p, size, "127.0.0.1", 0), "event": on_event,
             "signal_tx": lambda n: log.emit("mn", "signal_tx", n, "resume hello")}
    server = StreamProbeServer(sched, spec.rate_pps, spec.duration_s - spec.probe_start_s, spec.payload, hooks, session=session)
    srv = await loop.create_server(lambda: _accept(server), "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]

    def connect_fn(local_ip):
        conn = RealConn()
        state["client_conns"].append(conn)

        async def go():
            try:
                await loop.create_connection(lambda: conn, "127.0.0.1", port, local_addr=(local_ip, 0))
            except OSError as e:
                if conn.on_closed:
                    conn.on_closed(f"connect failed: {e}")
        loop.create_task(go())
        return conn

    client = StreamProbeClient(sched, connect_fn, hooks, reconnect=True, detect=spec.tcp_detect,
                               detect_timeout_s=spec.tcp_detect_timeout_s, session=session)
    t0 = time.monotonic_ns()
    files.event("scenario", "scenario_start", spec.scenario_id)
    client.start(LOCAL_IPS[spec.initial_iface])
    server.start(t0 + int(spec.probe_start_s * 1e9))
    current = spec.initial_iface

    def move(iface):
        nonlocal current
        state["n"] += 1
        hid = f"handoff-{state['n']}"
        state["pending"], state["attached"] = hid, False
        files.event(hid, "handoff_start", f"{current} -> {iface}")
        log.emit("mn", "link_change", 0, f"{current} -> {iface}")
        if state["client_conns"]:
            state["client_conns"][-1].blackhole()          # the old path is gone
        current = iface

        def detected():
            state["attached"] = True
            log.emit("mn", "attach", 0, LOCAL_IPS[iface])
            client.on_network_change(LOCAL_IPS[iface])
        sched.call_later(spec.detect_delay_ms / 1e3, detected)

    for at, iface in spec.moves:
        sched.call_at(t0 + int(at * 1e9), lambda i=iface: move(i))
    await asyncio.sleep(spec.duration_s + 0.4)
    client.stop()
    await asyncio.sleep(0.1)
    files.event("scenario", "scenario_end", "ok")
    extra = {"approach": spec.approach, "environment": "loopback", "mode": "loopback-real-tcp", "timing_comparable": False,
             "overhead": {"mobility_bytes_per_packet": 0.0, "basis": "no mobility-specific header"},
             "continuity": {"transport_connection_preserved": client.connections_opened == 1, "endpoint_address_preserved": False,
                            "connections_opened": client.connections_opened, "server_resent_after_replace": server.resends},
             "model": {"transport": "real kernel TCP over loopback", "detect": spec.tcp_detect}}
    state["closing"] = True
    for c in state["client_conns"]:
        c.abort()
    srv.close()
    await asyncio.sleep(0.1)                         # let connection_lost callbacks run before the files are closed
    log.close()
    files.finish(t0, {"rate_pps": spec.rate_pps, "size_bytes": spec.payload, "echo": True},
                 ["LOOPBACK real TCP; path change emulated by blackholing the old socket; timing not comparable"],
                 mode="loopback")
    (run_dir / "experiment_summary.json").write_text(json.dumps(extra, indent=2, sort_keys=True))
    return run_dir


def _accept(server):
    conn = RealConn(on_made=server.on_accept)
    return conn
