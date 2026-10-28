#!/usr/bin/env sh
# GIS data platform - local API (no Docker).
#   ./Run.sh                 -> http://127.0.0.1:8787
#   PORT=8790 HOST=0.0.0.0 ./Run.sh   (0.0.0.0 = reachable over the LAN)
# Creates .venv on first run and installs requirements.txt into it.
set -eu
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"
VENV_PY=".venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
  PY=""
  for candidate in python3.13 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
  done
  if [ -z "$PY" ]; then
    echo "[ERROR] Python 3.11+ was not found on PATH." >&2
    exit 1
  fi
  echo "Creating .venv with $PY and installing requirements..."
  "$PY" -m venv .venv
  "$VENV_PY" -m pip install --disable-pip-version-check -r requirements.txt
fi

if ! "$VENV_PY" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "[ERROR] Dependencies are missing from .venv. Install them with:" >&2
  echo "  $VENV_PY -m pip install -r requirements.txt" >&2
  exit 1
fi

echo
echo "  GIS data platform"
echo "  bind    : $HOST:$PORT"
echo "  docs    : http://127.0.0.1:$PORT/docs"
echo "  health  : http://127.0.0.1:$PORT/health"
echo "  terrain : http://127.0.0.1:$PORT/tiles/terrain.json"
echo "  Ctrl+C to stop."
echo

exec "$VENV_PY" -m app --host "$HOST" --port "$PORT"
