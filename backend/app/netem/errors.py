"""Exception hierarchy for the network-emulation layer."""


class NetemError(Exception):
    """Base class for all errors raised by app.netem."""


class ConfigError(NetemError):
    """Invalid topology configuration."""


class SafetyError(NetemError):
    """A command was rejected by the allow-list; it was NOT executed."""


class CommandError(NetemError):
    """A command ran and failed (or the tool is missing)."""

    def __init__(self, message, argv=(), returncode=None, stderr=""):
        super().__init__(message)
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr


class PrivilegeError(CommandError):
    """sudo needs a password / no privilege available."""


class TopologyError(NetemError):
    """Topology-level problem (name clash, missing namespace, ...)."""
