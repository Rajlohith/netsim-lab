"""Applies data-plane handshake requests on the Linux testbed with allow-listed `ip` commands.

Runs in the (unprivileged) orchestrator; each command is executed via Runner (sudo -n per command).
Guard rails: the home address must be the configured one, and a care-of address may only be an address that
the topology config assigns to the mobile node, so tunnel endpoints can never point outside the lab.
"""
from __future__ import annotations

import ipaddress

from ..netem.errors import NetemError
from . import dataplane as dp
from .dataplane import DataPlaneError


class LinuxDataPlaneApplier:
    def __init__(self, runner, cfg: dp.LinuxMipConfig, allowed_coas):
        self.runner, self.cfg = runner, cfg
        self.allowed = {ipaddress.IPv4Address(a) for a in allowed_coas}
        self.ha_tunnel = self.mn_tunnel = False

    def _run(self, cmds):
        try:
            for c in cmds:
                self.runner.run(c)
        except NetemError as e:
            raise DataPlaneError(str(e)) from None

    def _coa(self, params):
        coa = ipaddress.IPv4Address(params["coa"])
        if coa not in self.allowed:
            raise DataPlaneError(f"care-of address {coa} is not a testbed address of the mobile node")
        return coa

    def apply(self, op: str, params: dict):
        c = self.cfg
        if op in ("ha_install", "ha_remove") and params.get("home") != c.home_address:
            raise DataPlaneError("unexpected home address")
        if op == "ha_install":
            self._run(dp.linux_ha_install(c, self._coa(params), self.ha_tunnel))
            self.ha_tunnel = True
        elif op == "ha_remove":
            if self.ha_tunnel:
                self._run(dp.linux_ha_remove(c))
                self.ha_tunnel = False
        elif op == "mn_set_care_of":
            self._run(dp.linux_mn_set_care_of(c, self._coa(params), self.mn_tunnel))
            self.mn_tunnel = True
        elif op == "mn_clear":
            if self.mn_tunnel:
                self._run(dp.linux_mn_clear(c))
                self.mn_tunnel = False
        else:
            raise DataPlaneError(f"unknown operation {op!r}")

    def cleanup(self):
        """Best-effort removal of everything this applier created (idempotent)."""
        for flag, cmds in (("ha_tunnel", dp.linux_ha_remove(self.cfg)), ("mn_tunnel", dp.linux_mn_clear(self.cfg))):
            if getattr(self, flag):
                for c in cmds:
                    try:
                        self.runner.run(c, check=False)
                    except NetemError:
                        pass
                setattr(self, flag, False)
