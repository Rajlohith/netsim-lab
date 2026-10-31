#!/bin/sh
# Serve the real service layer with the stdlib adapter (scripts/service_http_adapter.py) and run the dashboard's API-layer tests against it.
# Needs: python3 + the backend's pure-python deps (numpy, pandas, scikit-learn, pyyaml), node 18+, esbuild. NOT FastAPI (that is `make api-test`).
set -eu
cd "$(dirname "$0")/../.."
PORT="${PORT:-8765}"; DATA="$(mktemp -d)"
PYTHONPATH=backend python3 scripts/service_http_adapter.py --port "$PORT" --data-dir "$DATA" >"$DATA/adapter.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true; rm -rf "$DATA"' EXIT
for i in $(seq 1 50); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 0.2; done
cd frontend && NETSIM_API_URL="http://127.0.0.1:$PORT/api" node scripts/run-sandbox-tests.mjs
