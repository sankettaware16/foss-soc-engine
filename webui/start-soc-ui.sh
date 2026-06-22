#!/usr/bin/env bash
# ====================================================================
#  FOSS SOC Engine - Web UI launcher (Linux / macOS)
#  On first run it creates a private virtual env and installs the two
#  tiny dependencies (Flask + PyYAML). After that it just launches.
#
#  Usage:  ./start-soc-ui.sh        (cd into webui/ first, or run by path)
# ====================================================================
set -e

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$ROOT/.venv-ui"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "Python 3 was not found. Install python3 (e.g. 'sudo apt install python3 python3-venv') and re-run."
  exit 1
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating a private environment (one time only)..."
  "$PY" -m venv "$VENV"
fi
VPY="$VENV/bin/python"

if ! "$VPY" -c "import flask, yaml" >/dev/null 2>&1; then
  echo "Installing dependencies (one time only)..."
  "$VPY" -m pip install --upgrade pip >/dev/null
  "$VPY" -m pip install -r "$ROOT/webui/requirements-ui.txt"
fi

echo ""
echo "Starting the FOSS SOC Web UI...  (open the printed URL in your browser)"
echo "Press Ctrl+C to stop."
echo ""
exec "$VPY" "$ROOT/webui/app.py"
