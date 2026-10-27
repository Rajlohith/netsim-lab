# Privileged network operations (Stage 2)

The Python process (CLI, API, ML) **always runs as a normal user** and refuses to start as root
(`--allow-root` overrides, not recommended). Only individual `ip` commands are elevated, one at a
time, as `sudo -n ip ...` (non-interactive). `scripts/topology.sh` runs `sudo -v` first to cache
your credentials. Nothing runs privileged unless `--execute` is given (the wrapper script passes it
for create/destroy/shape) or `NETSIM_DRY_RUN=false`; `validate` and `plan` need no privileges.

## Guard rails (enforced in code: `backend/app/netem/safety.py`, tested in `test_netem_safety.py`)
- Every argv is checked against an allow-list **before** execution; anything else raises `SafetyError`.
- Only the `ip` binary is ever invoked (`tc`, `sysctl`, `ping` only via `ip netns exec <project ns>`).
- Namespace names must equal `<NETSIM_NETNS_PREFIX><node key>` (regex `^nsl-[a-z][a-z0-9]{0,9}$`).
  `default`, `docker`, `nsl-` + junk, uppercase, `../x`, shell metacharacters are refused.
- Host firewall: no `iptables`, `nft`, `ufw`, `firewall-cmd` anywhere in the grammar (cannot be expressed).
- Host default route / host interfaces: every `route`/`link`/`addr` command must be `ip -n <project ns> ...`.
- veth pairs are created inside the namespaces (`ip -n A link add .. peer name .. netns B`), so no
  temporary interface ever appears in the host namespace.
- Sysctls are limited to `net.ipv4.ip_forward` and `net.ipv4.conf.{all,default}.rp_filter`, executed
  with `ip netns exec`, which scopes them to the namespace (host values are unchanged).
- Cleanup deletes only names that are (a) valid project names and (b) listed in the YAML config or in
  `data/topology_state.json`, and that currently exist. A namespace like `nsl-extra` that merely shares
  the prefix is left alone. Deleting a namespace destroys its veth ends and qdiscs with it.
- `create` refuses to start if any target namespace already exists, and rolls back only the
  namespaces it created if a step fails.

## Commands that need privilege
| Purpose | Command | Scope |
|---|---|---|
| Create / delete namespace | `ip netns add\|del nsl-<key>` | project namespaces only |
| Bring up lo, add veth/addr/route | `ip -n nsl-<key> link\|addr\|route ...` | inside namespace |
| Forwarding / rp_filter | `ip netns exec nsl-<key> sysctl -w ...` | namespace-local |
| Latency / loss / rate | `ip netns exec nsl-<key> tc qdisc add\|replace\|del dev <if> root netem ...` | namespace-local |
| Handoff simulation (Stage 3) | `ip -n nsl-<key> link set <if> up\|down` (never `lo` down) | inside namespace |
| Run sender/receiver in a namespace (Stage 3) | `ip netns exec nsl-<key> setpriv --reuid=<uid> --regid=<gid> --clear-groups -- env PYTHONPATH=<abs> <abs python> -m app.traffic.sender\|receiver ...` | tool runs as **you**, not root; uid 0 refused |
| Inspect / measure | `ip -n ...`, `ip netns exec ... tc qdisc show`, `... ping` | read-only |
| List namespaces | `ip netns list` | unprivileged |

## One-time manual prerequisites (the project never does these for you)
- Zorin/Ubuntu: `sudo apt install iproute2 iputils-ping python3-venv`
- Fedora: `sudo dnf install iproute iproute-tc iputils`; if `tc ... netem` reports an unknown qdisc,
  `sudo dnf install kernel-modules-extra` and `sudo modprobe sch_netem` (loading a module is a host
  change, so it is left to you).

## Be aware
Allowing someone to run `ip netns exec` with sudo is effectively root. Do not add a blanket
`NOPASSWD` sudoers rule for `ip`; prefer cached credentials (`sudo -v`) as the wrapper does.
`ip netns add` creates `/run/netns/<name>` (standard behaviour; removed by `ip netns del` or reboot).

## Stage 3 additions
- `ip netns exec` needs root, so measurement tools are started through `setpriv`, which drops to the invoking user's uid/gid
  before Python starts. The allow-list only accepts `app.traffic.sender|receiver`, absolute paths, and arguments matching
  `[A-Za-z0-9_./:=,@+-]` (so output paths must not contain spaces).
- A root-owned `sudo` process cannot always be signalled by its unprivileged parent, so the orchestrator asks tools to stop with a
  stop file, not with `kill`.
- If a scenario aborts, interfaces it brought down are brought back up in cleanup; `topology.sh destroy`/`create` resets everything.

## Stage 4 additions (Mobile IP live demo)
| Purpose | Command (always `ip -n nsl-<node> ...`) |
|---|---|
| Virtual home addresses | `addr add 10.10.100.1/32 dev lo` (HA), `addr add 10.10.100.2/32 dev lo` (MN); removed with `addr del ... dev lo` (only `/32` on `lo`) |
| Tunnels | `tunnel add\|change tnl-<name> mode ipip local <ip> remote <ip>`, `link set tnl-<name> up`, `tunnel del tnl-<name>` (device must be named `tnl-*`, mode `ipip` only) |
| Route home address into the tunnel | `route replace <ip>/32 dev tnl-<name>`, `route del <ip>/32` (only `/32`, only onto `tnl-*`) |
| Daemons | `ip netns exec <ns> setpriv --reuid=<you> ... python -m app.mobileip.runtime ha\|mn ...` (allow-listed module) |
- The daemons never run privileged commands; the orchestrator applies them after checking that care-of addresses are lab addresses
  of the mobile node and the home address is the configured one.
- One-time prerequisite (not done for you): `sudo modprobe ipip` (Fedora may need `kernel-modules-extra`).
- Everything the demo creates (tunnels, `/32` addresses, brought-down interfaces) is removed or restored in `finally`, also after errors.
