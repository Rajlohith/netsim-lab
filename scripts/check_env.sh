#!/usr/bin/env bash
# READ-ONLY diagnostics. Makes no host network changes and never uses sudo.
# The unshare probe creates a throw-away user+network namespace that vanishes on exit.
set -uo pipefail

ok()   { printf '  [ok]   %s\n' "$1"; }
warn() { printf '  [warn] %s\n' "$1"; }

echo "OS:"; (. /etc/os-release 2>/dev/null && echo "  ${PRETTY_NAME:-unknown}")
[ "$(id -u)" -eq 0 ] && warn "running as root (not recommended)" || ok "running as unprivileged user"

echo "Tools:"
for t in python3 git node npm ip tc ping unshare; do
  command -v "$t" >/dev/null && ok "$t" || warn "$t not found"
done
command -v ip >/dev/null || echo "    install: Zorin 'sudo apt install iproute2' | Fedora 'sudo dnf install iproute iproute-tc'"

echo "Capabilities:"
if unshare -Urn true 2>/dev/null; then
  ok "unprivileged user+net namespaces work (no host changes made)"
else
  warn "unprivileged namespaces disabled; later stages will need the privileged helper (docs/PRIVILEGED_OPERATIONS.md)"
fi
if command -v tc >/dev/null && unshare -Urn sh -c 'ip link set lo up && tc qdisc add dev lo root netem delay 1ms' 2>/dev/null; then
  ok "tc netem usable inside a private namespace"
else
  warn "tc netem not usable (Fedora: sudo dnf install iproute-tc kernel-modules-extra; Zorin: sudo modprobe sch_netem)"
fi
