#!/usr/bin/env bash
# Wrapper for the topology CLI. Runs as a NORMAL user; individual `ip`/`tc` calls are
# elevated one by one with `sudo -n`. Credentials are cached up-front with `sudo -v`.
#
#   scripts/topology.sh validate            check YAML (no privileges)
#   scripts/topology.sh plan                print the commands create would run
#   scripts/topology.sh create              build the topology
#   scripts/topology.sh status              inspect addresses, routes, forwarding, netem
#   scripts/topology.sh ping mn 10.10.1.2   measure RTT/loss (extra args: -c 20 -i 0.05)
#   scripts/topology.sh shape ana-mn --delay-ms 80 --loss 2   change netem on a link (--clear to remove)
#   scripts/topology.sh destroy             remove ONLY this project's namespaces
#
# Options via env: NETSIM_NETNS_PREFIX (default nsl-), TOPOLOGY_CONFIG (default config/topology.yaml)
set -euo pipefail
cd "$(dirname "$0")/.."

[ "$(id -u)" -eq 0 ] && { echo "Run as a normal user, not root." >&2; exit 1; }
cmd="${1:-}"; [ -n "$cmd" ] || { sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
shift

PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
args=()
[ -n "${TOPOLOGY_CONFIG:-}" ] && args+=(--config "$TOPOLOGY_CONFIG")

case "$cmd" in
  validate|plan) exec env PYTHONPATH=backend "$PY" -m app.netem "$cmd" ${args[@]+"${args[@]}"} "$@" ;;
  create|destroy|shape) sudo -v; exec env PYTHONPATH=backend "$PY" -m app.netem "$cmd" --execute ${args[@]+"${args[@]}"} "$@" ;;
  status|ping)          sudo -v; exec env PYTHONPATH=backend "$PY" -m app.netem "$cmd" ${args[@]+"${args[@]}"} "$@" ;;
  *) echo "unknown command: $cmd" >&2; exit 2 ;;
esac
