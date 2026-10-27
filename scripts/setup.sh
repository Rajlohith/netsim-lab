#!/usr/bin/env bash
# Create .venv and install Python deps. Must NOT be run as root.
# Does not touch network configuration and does not call sudo.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(id -u)" -eq 0 ]; then
  echo "Refusing to run as root. Run as your normal user." >&2
  exit 1
fi

PYTHON="${PYTHON:-python3}"
"$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Python >= 3.10 required, found {sys.version.split()[0]}")
PY

if ! "$PYTHON" -m venv .venv 2>/dev/null; then
  echo "python3-venv missing. Zorin/Ubuntu: sudo apt install python3-venv" >&2
  exit 1
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-dev.txt
[ -f .env ] || cp .env.example .env
echo "Python environment ready: source .venv/bin/activate"
