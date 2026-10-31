#!/usr/bin/env bash
# Start / stop / inspect the NetSim Lab backend (FastAPI) and frontend (Vite dev server).
#
#   scripts/stack.sh start [api|frontend|all]    default: all
#   scripts/stack.sh stop  [api|frontend|all]
#   scripts/stack.sh restart [api|frontend|all]
#   scripts/stack.sh status
#   scripts/stack.sh logs [api|frontend]         tail the log
#
# Everything runs as the CURRENT, unprivileged user. Nothing here calls sudo or touches network configuration.
# State: .run/<name>.pid (process-group id), logs: .run/logs/<name>.log.  A pid file is only trusted if the process
# it names still looks like ours (command line check), so a recycled PID is never signalled.
#
# Environment: NETSIM_API_HOST (127.0.0.1) NETSIM_API_PORT (8000) NETSIM_FRONTEND_PORT (5173) NETSIM_STOP_GRACE_S (8)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
RUN="$ROOT/.run"; LOGS="$RUN/logs"
API_HOST="${NETSIM_API_HOST:-127.0.0.1}"; API_PORT="${NETSIM_API_PORT:-8000}"
FE_PORT="${NETSIM_FRONTEND_PORT:-5173}"; GRACE="${NETSIM_STOP_GRACE_S:-8}"
PY="${PYTHON:-}"; [ -n "$PY" ] || { [ -x "$ROOT/.venv/bin/python" ] && PY="$ROOT/.venv/bin/python" || PY="python3"; }
mkdir -p "$LOGS"

die() { echo "error: $*" >&2; exit 1; }
alive() { kill -0 "$1" 2>/dev/null; }
pidfile() { echo "$RUN/$1.pid"; }
# pattern that must appear in the command line of the leader process of each service
pattern() { case "$1" in api) echo "uvicorn";; frontend) echo "vite";; esac; }

# prints the pgid if the service is running and really ours, else nothing
running_pgid() {
  local f; f="$(pidfile "$1")"; [ -f "$f" ] || return 0
  local pg; pg="$(cat "$f" 2>/dev/null || true)"
  [ -n "$pg" ] && alive "$pg" || { rm -f "$f"; return 0; }
  if ps -eo pgid=,args= | awk -v g="$pg" '$1==g' | grep -q -- "$(pattern "$1")"; then echo "$pg"; else rm -f "$f"; fi
}

wait_http() {  # url seconds
  local i; for i in $(seq 1 $(( $2 * 5 ))); do
    curl -fsS -o /dev/null "$1" 2>/dev/null && return 0; sleep 0.2
  done; return 1
}

port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && return 0 || return 1; }

start_api() {
  [ -z "$(running_pgid api)" ] || { echo "api: already running (pgid $(running_pgid api))"; return 0; }
  port_busy "$API_PORT" && die "port $API_PORT is already in use by another process (set NETSIM_API_PORT)"
  "$PY" -c "import fastapi, uvicorn, pydantic, pydantic_settings" 2>/dev/null \
    || die "fastapi/uvicorn/pydantic are not importable with '$PY'. Run 'make setup' first (needs network)."
  if [ "$(id -u)" -eq 0 ] && [ "${NETSIM_API_ALLOW_ROOT:-0}" != "1" ]; then
    die "refusing to start the API as root (the job manager refuses too). Run as a normal user."
  fi
  # no --reload: the reloader would restart the job manager (and its worker processes) on every file change
  ( cd "$ROOT/backend" && PYTHONPATH="$ROOT/backend" setsid "$PY" -m uvicorn app.main:app --host "$API_HOST" --port "$API_PORT" \
      >"$LOGS/api.log" 2>&1 < /dev/null & echo $! >"$(pidfile api)" )
  if wait_http "http://$API_HOST:$API_PORT/api/health" 20; then
    echo "api: up   http://$API_HOST:$API_PORT/api/health   docs: http://$API_HOST:$API_PORT/docs   (pgid $(cat "$(pidfile api)"))"
  else
    echo "api: did not answer /api/health within 20 s; last log lines:" >&2; tail -n 15 "$LOGS/api.log" >&2
    stop_one api >/dev/null 2>&1 || true; exit 1
  fi
}

start_frontend() {
  [ -z "$(running_pgid frontend)" ] || { echo "frontend: already running (pgid $(running_pgid frontend))"; return 0; }
  port_busy "$FE_PORT" && die "port $FE_PORT is already in use by another process (set NETSIM_FRONTEND_PORT)"
  [ -d "$ROOT/frontend/node_modules/vite" ] || die "frontend dependencies missing. Run 'make frontend-install' first (needs network)."
  ( cd "$ROOT/frontend" && setsid npx --no-install vite --host 127.0.0.1 --port "$FE_PORT" --strictPort \
      >"$LOGS/frontend.log" 2>&1 < /dev/null & echo $! >"$(pidfile frontend)" )
  if wait_http "http://127.0.0.1:$FE_PORT/" 30; then
    echo "frontend: up   http://127.0.0.1:$FE_PORT/   (pgid $(cat "$(pidfile frontend)"))"
  else
    echo "frontend: did not answer within 30 s; last log lines:" >&2; tail -n 15 "$LOGS/frontend.log" >&2
    stop_one frontend >/dev/null 2>&1 || true; exit 1
  fi
}

stop_one() {
  local pg; pg="$(running_pgid "$1")"
  [ -n "$pg" ] || { echo "$1: not running"; rm -f "$(pidfile "$1")"; return 0; }
  kill -TERM -- "-$pg" 2>/dev/null || true      # whole process group: uvicorn + anything it spawned
  local i; for i in $(seq 1 $(( GRACE * 5 ))); do
    ps -eo pgid= | awk -v g="$pg" '$1==g{f=1} END{exit !f}' || break; sleep 0.2
  done
  if ps -eo pgid= | awk -v g="$pg" '$1==g{f=1} END{exit !f}'; then
    echo "$1: still alive after ${GRACE}s, sending SIGKILL"; kill -KILL -- "-$pg" 2>/dev/null || true; sleep 0.3
  fi
  rm -f "$(pidfile "$1")"; echo "$1: stopped"
}

status_one() {
  local pg; pg="$(running_pgid "$1")"
  if [ -n "$pg" ]; then echo "$1: running (pgid $pg)"; else echo "$1: stopped"; fi
}

targets() { case "${1:-all}" in api) echo api;; frontend) echo frontend;; all) echo "api frontend";; *) die "unknown target '$1' (api|frontend|all)";; esac; }

cmd="${1:-}"; tgt="${2:-all}"
case "$cmd" in
  start)   for t in $(targets "$tgt"); do start_$t; done ;;
  stop)    for t in $(targets "$tgt" | tr ' ' '\n' | tac); do stop_one "$t"; done ;;
  restart) "$0" stop "$tgt"; "$0" start "$tgt" ;;
  status)  status_one api; status_one frontend
           curl -fsS "http://$API_HOST:$API_PORT/api/health" 2>/dev/null && echo || echo "api health: unreachable" ;;
  logs)    [ "$tgt" != all ] || tgt=api; tail -n 50 -f "$LOGS/$tgt.log" ;;
  *)       sed -n 2,12p "$0"; exit 2 ;;
esac
