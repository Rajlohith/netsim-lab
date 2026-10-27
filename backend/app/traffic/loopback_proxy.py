"""Userspace UDP impairment proxy: controlled loss / delay / blackout ("handoff") on loopback.

For tests and runnable examples WITHOUT root or namespaces. It sits between sender and receiver:

    sender -> proxy.front_addr ==(impair)==> receiver      receiver echo ==(impair)==> sender

It is NOT the netns/netem topology: its numbers describe this proxy, not Linux tc. Packets are
judged by their probe seq so scenarios are exactly reproducible (deterministic drops, seeded RNG).
Blackouts drop BOTH directions and are logged to events.csv as handoff_start/handoff_end.
"""
from __future__ import annotations

import heapq
import random
import select
import socket
import threading
from dataclasses import dataclass, field

from . import clock, wire
from .recorder import EventLog


@dataclass
class PathRules:
    delay_ms: float = 0.0                              # one-way, each direction
    delay_schedule: list = field(default_factory=list)  # [(from_elapsed_s, delay_ms)] overrides delay_ms
    drop_seqs: set = field(default_factory=set)        # forward direction
    drop_prob: float = 0.0                             # forward direction
    seed: int = 1
    blackouts: list = field(default_factory=list)      # [(start_s, end_s)] since start(), both directions

    def delay_at(self, elapsed_s: float) -> float:
        d = self.delay_ms
        for t, v in sorted(self.delay_schedule):
            if elapsed_s >= t:
                d = v
        return d


class ImpairmentProxy:
    def __init__(self, target, rules: PathRules, host="127.0.0.1", event_log: EventLog | None = None):
        self.target, self.rules, self.events = target, rules, event_log
        self.front = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.back = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.front.bind((host, 0))
        self.back.bind((host, 0))
        self.front_addr = self.front.getsockname()
        self._rng = random.Random(rules.seed)
        self._heap, self._n = [], 0
        self._client = None
        self._stop = threading.Event()
        self._thread = None
        self._t0 = None
        self.stats = {"fwd_in": 0, "fwd_dropped": 0, "rev_in": 0, "rev_dropped": 0}
        self._open_blackout = None

    def start(self):
        self._t0 = clock.mono_ns()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._open_blackout is not None and self.events:
            self.events.record(self._open_blackout, "handoff_end", "blackout cut short by proxy stop")
        for s in (self.front, self.back):
            s.close()

    # ---- internals ------------------------------------------------------------------------
    def _elapsed(self):
        return (clock.mono_ns() - self._t0) / 1e9

    def _in_blackout(self, el):
        return any(a <= el < b for a, b in self.rules.blackouts)

    def _schedule(self, due, sock, data, addr):
        self._n += 1
        heapq.heappush(self._heap, (due, self._n, sock, data, addr))

    def _track_blackouts(self, el):
        for i, (a, b) in enumerate(self.rules.blackouts):
            eid = f"handoff-{i + 1}"
            if self._open_blackout is None and a <= el < b:
                self._open_blackout = eid
                if self.events:
                    self.events.record(eid, "handoff_start", f"proxy blackout {a}s-{b}s")
            elif self._open_blackout == eid and el >= b:
                self._open_blackout = None
                if self.events:
                    self.events.record(eid, "handoff_end", f"proxy blackout {a}s-{b}s")

    def _run(self):
        socks = [self.front, self.back]
        while not self._stop.is_set():
            now = clock.mono_ns()
            timeout = 0.005
            if self._heap:
                timeout = max(0.0, min(timeout, (self._heap[0][0] - now) / 1e9))
            for s in select.select(socks, [], [], timeout)[0]:
                try:
                    data, addr = s.recvfrom(2048)
                except OSError:
                    continue
                self._ingest(s, data, addr)
            el = self._elapsed()
            self._track_blackouts(el)
            now = clock.mono_ns()
            while self._heap and self._heap[0][0] <= now:
                _, _, sock, data, addr = heapq.heappop(self._heap)
                try:
                    sock.sendto(data, addr)
                except OSError:
                    pass

    def _ingest(self, s, data, addr):
        el = self._elapsed()
        self._track_blackouts(el)
        due = clock.mono_ns() + int(self.rules.delay_at(el) * 1e6)
        try:
            p = wire.decode(data)
        except wire.WireError:
            return
        if s is self.front:                              # sender -> receiver
            self._client = addr
            self.stats["fwd_in"] += 1
            drop = (self._in_blackout(el) or p.seq in self.rules.drop_seqs
                    or (self.rules.drop_prob > 0 and self._rng.random() < self.rules.drop_prob))
            if drop:
                self.stats["fwd_dropped"] += 1
                return
            self._schedule(due, self.back, data, self.target)
        else:                                            # receiver echo -> sender
            self.stats["rev_in"] += 1
            if self._client is None or self._in_blackout(el):
                self.stats["rev_dropped"] += 1
                return
            self._schedule(due, self.front, data, self._client)
