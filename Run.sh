#!/usr/bin/env sh
# GIS platform + viewers via Docker Compose.
#   ./Run.sh [-y] [up|down|logs|build|restart]
#   -y  do not ask for confirmation when a DEM job is running
set -eu
cd "$(dirname "$0")"

ASSUME_YES=0
if [ "${1:-}" = "-y" ] || [ "${1:-}" = "--yes" ]; then
  ASSUME_YES=1
  shift
fi
CMD="${1:-up}"

if [ ! -f .env ] && [ -f .env.example ]; then
  cp .env.example .env
  echo "Created .env from .env.example"
fi

# Read a KEY=value from .env (last match wins, CR stripped), default otherwise.
env_value() {
  value=""
  if [ -f .env ]; then
    value=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" .env | tr -d '\r"' | tail -n 1)
  fi
  [ -n "$value" ] && printf '%s' "$value" || printf '%s' "$2"
}
API_PORT=$(env_value GIS_PLATFORM_PORT 8787)
VIEWER_PORT=$(env_value GIS_VIEWER_PORT 5173)

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] Docker with Compose was not found on PATH." >&2
  echo "        Local (no Docker) mode: gis-data-platform/Run.sh + (cd gis-viewers && npm run dev)" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "[ERROR] The Docker daemon is not reachable (is Docker running / do you have permission?)." >&2
  exit 1
fi

# Recreating/stopping the data-platform container kills a running fetch/build
# job (job state is in memory). Ask first unless -y.
confirm_no_running_job() {
  url="http://127.0.0.1:$API_PORT/api/jobs/current"
  body=""
  if command -v curl >/dev/null 2>&1; then
    body=$(curl -fsS -m 3 "$url" 2>/dev/null || true)
  elif command -v wget >/dev/null 2>&1; then
    body=$(wget -q -T 3 -O - "$url" 2>/dev/null || true)
  fi
  if printf '%s' "$body" | grep -Eq '"state"[[:space:]]*:[[:space:]]*"running"'; then
    echo "[WARN] A DEM job is RUNNING on http://127.0.0.1:$API_PORT - '$CMD' will restart the API and kill it."
    if [ "$ASSUME_YES" -eq 1 ]; then
      echo "       -y given, continuing."
      return 0
    fi
    printf 'Continue anyway? [y/N] '
    answer=""
    read -r answer || true
    case "$answer" in
      y|Y|yes|YES) ;;
      *) echo "Aborted."; exit 1 ;;
    esac
  fi
}

case "$CMD" in
  up)
    confirm_no_running_job
    docker compose up -d --build
    printf '\nViewer: http://127.0.0.1:%s/\nAPI:    http://127.0.0.1:%s/docs\nFetch:  http://127.0.0.1:%s/fetch/\n' \
      "$VIEWER_PORT" "$API_PORT" "$VIEWER_PORT"
    ;;
  down)
    confirm_no_running_job
    docker compose down
    ;;
  logs) docker compose logs -f ;;
  build) docker compose build ;;
  restart)
    confirm_no_running_job
    docker compose down
    docker compose up -d
    ;;
  *)
    echo "Usage: $0 [-y] [up|down|logs|build|restart]" >&2
    exit 2
    ;;
esac
