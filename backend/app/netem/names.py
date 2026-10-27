"""Naming rules. Every namespace this project creates is  <prefix><node-key>.

Cleanup and the command allow-list only ever accept names matching these patterns,
so a namespace such as "default", "docker0" or "mynetns" can never be touched.
"""
import re

NODE_KEY_RE = re.compile(r"^[a-z][a-z0-9]{0,9}$")
PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,5}-$")
IFNAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,14}$")  # Linux limit: 15 chars
LINK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


def validate_prefix(prefix: str) -> str:
    from .errors import ConfigError

    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
        raise ConfigError(
            f"invalid namespace prefix {prefix!r}: must match {PREFIX_RE.pattern} (e.g. 'nsl-')"
        )
    return prefix


def ns_name(prefix: str, node_key: str) -> str:
    return f"{prefix}{node_key}"


def is_project_ns(name: str, prefix: str) -> bool:
    """True only for names of the form <prefix><valid node key>."""
    return (
        isinstance(name, str)
        and name.startswith(prefix)
        and NODE_KEY_RE.fullmatch(name[len(prefix):]) is not None
    )
