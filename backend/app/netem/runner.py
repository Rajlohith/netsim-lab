"""The only module that executes subprocesses (shell=False, allow-listed, sudo -n per command)."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .errors import CommandError, PrivilegeError
from .plan import Cmd
from .safety import validate_argv


@dataclass
class CmdResult:
    argv: tuple
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _default_executor(argv, timeout):
    try:
        p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, shell=False)
    except FileNotFoundError as e:
        raise CommandError(f"required tool not found: {argv[0]} ({e})", argv) from None
    except subprocess.TimeoutExpired:
        raise CommandError(f"command timed out after {timeout}s", argv) from None
    return CmdResult(tuple(argv), p.returncode, p.stdout, p.stderr)


class Runner:
    """Validates then runs commands.

    * The application itself stays unprivileged: privileged commands are prefixed with
      `sudo -n` (non-interactive; run `sudo -v` first to cache credentials).
    * dry_run=True prints/records privileged commands without running them.
      Read-only unprivileged commands (`ip netns list`) still run so plans reflect reality.
    """

    def __init__(self, prefix: str, dry_run: bool = True, use_sudo: bool | None = None,
                 executor=None, timeout: float = 60.0, popen_factory=None):
        self.prefix = prefix
        self.dry_run = dry_run
        self.use_sudo = (os.geteuid() != 0) if use_sudo is None else use_sudo
        self.real_exec = executor is None   # False for injected fakes (tests)
        self._exec = executor or _default_executor
        self.timeout = timeout
        self._popen = popen_factory or subprocess.Popen
        self.history: list[tuple] = []

    def run(self, cmd: Cmd, check: bool = True, timeout: float | None = None) -> CmdResult:
        validate_argv(cmd.argv, self.prefix)          # raises SafetyError -> nothing runs
        self.history.append(tuple(cmd.argv))
        runs_for_real = (not self.dry_run) or (cmd.readonly and not cmd.privileged)
        if not runs_for_real:
            return CmdResult(tuple(cmd.argv), 0, "", "")
        full = list(cmd.argv)
        if cmd.privileged and self.use_sudo:
            if not self.dry_run and shutil.which("sudo") is None:
                raise PrivilegeError("sudo is required (app runs unprivileged) but was not found", full)
            full = ["sudo", "-n", *full]
        try:
            res = self._exec(full, timeout or self.timeout)
        except CommandError:
            if self.dry_run:  # e.g. `ip` not installed while merely planning
                return CmdResult(tuple(cmd.argv), 0, "", "")
            raise
        if res.returncode != 0 and check:
            err = (res.stderr or "").strip()
            if "password is required" in err or "no tty present" in err or "a terminal is required" in err:
                raise PrivilegeError(
                    "sudo needs a password; run `sudo -v` first (scripts/topology.sh does this)", full,
                    res.returncode, err)
            raise CommandError(f"command failed (rc={res.returncode}): {' '.join(cmd.argv)}: {err}",
                               full, res.returncode, err)
        return res

    def popen(self, cmd: Cmd, stdout=None, stderr=None):
        """Start a long-running validated command (e.g. a measurement tool). None in dry-run."""
        validate_argv(cmd.argv, self.prefix)
        self.history.append(tuple(cmd.argv))
        if self.dry_run:
            return None
        full = list(cmd.argv)
        if cmd.privileged and self.use_sudo:
            full = ["sudo", "-n", *full]
        try:
            return self._popen(full, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL)
        except FileNotFoundError as e:
            raise CommandError(f"required tool not found: {full[0]} ({e})", full) from None
