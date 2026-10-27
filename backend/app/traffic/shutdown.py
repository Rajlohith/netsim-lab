"""Cooperative shutdown: SIGINT/SIGTERM and/or a stop file.

`sudo`-launched processes cannot always be signalled by an unprivileged parent, so a stop file
is the portable way to ask a tool running inside a namespace to finish cleanly.
"""
from __future__ import annotations

import os
import signal
import threading
from pathlib import Path


class ShutdownFlag:
    def __init__(self, stop_file: Path | None = None):
        self.stop_file = Path(stop_file) if stop_file else None
        self.reason: str | None = None
        self.count = 0
        self._old = {}

    def request(self, reason: str):
        self.count += 1
        if self.reason is None:
            self.reason = reason

    @property
    def requested(self) -> bool:
        if self.reason is None and self.stop_file is not None and os.path.exists(self.stop_file):
            self.reason = "stop_file"
            self.count += 1
        return self.reason is not None

    def install(self):
        if threading.current_thread() is not threading.main_thread():
            return self
        for sig in (signal.SIGINT, signal.SIGTERM):
            self._old[sig] = signal.signal(sig, lambda s, _f: self.request(f"signal:{signal.Signals(s).name}"))
        return self

    def restore(self):
        for sig, old in self._old.items():
            signal.signal(sig, old)
        self._old.clear()
