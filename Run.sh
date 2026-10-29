#!/usr/bin/env sh
# DEM GIS stack via Docker Compose - three apps sharing one data volume:
#   engine         builds + serves tiles (port GIS_ENGINE_PORT, default 5000)
#   data-platform  downloads only, fetch page (GIS_DATA_PORT, default 5100)
#   viewer         optional web viewer (GIS_VIEWER_PORT, default 3100)
#
#   ./Run.sh [-y] [up|down|logs|build|restart|status] [service...]
#     ./Run.sh up                  all three
#     ./Run.sh up engine           serve + build only (no downloads)
#     ./Run.sh up data-platform    downloads only
#     ./Run.sh up viewer           viewer (starts the engine too)
#   -y  do not ask for confirmation when a job is running
set -eu
cd "$(dirname "$0")"

ASSUME_YES=0
if [ "${1:-}" = "-y" ] || [ "${1:-}" = "--yes" ]; then
  ASSUME_YES=1
  shift
fi
CMD="${1:-up}"
[ $# -gt 0 ] && shift
SERVICES="$*"

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
# Variables set in the shell win over .env (same rule as docker compose).
ENGINE_PORT=${GIS_ENGINE_PORT:-${GIS_PLATFORM_PORT:-$(env_value GIS_ENGINE_PORT "$(env_value GIS_PLATFORM_PORT 5000)")}}
DATA_PORT=${GIS_DATA_PORT:-$(env_value GIS_DATA_PORT 5100)}
VIEWER_PORT=${GIS_VIEWER_PORT:-$(env_value GIS_VIEWER_PORT 3100)}

for s in $SERVICES; do
  case "$s" in
    engine|data-platform|viewer) ;;
    *) echo "[ERROR] unknown service '$s' (engine, data-platform, viewer)" >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "[ERROR] Docker with Compose was not found on PATH." >&2
  echo "        Local (no Docker) mode: gis-data-platform/Run.sh + (cd gis-viewers && npm run dev)" >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "[ERROR] The Docker daemon is not reachable (is Docker running / do you have permission?)." >&2
  exit 1
fi

fetch_url() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 3 "$1" 2>/dev/null || true
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 3 -O - "$1" 2>/dev/null || true
  fi
}

# Recreating/stopping the engine kills a running BUILD job, the data-platform a
# running DOWNLOAD (both resume when started again). Ask first unless -y.
affects() {  # affects <service>: is it part of this command?
  [ -z "$SERVICES" ] && return 0
  case " $SERVICES " in *" $1 "*) return 0 ;; esac
  return 1
}
confirm_no_running_job() {
  running=""
  if affects engine && fetch_url "http://127.0.0.1:$ENGINE_PORT/api/jobs/current" \
      | grep -Eq '"state"[[:space:]]*:[[:space:]]*"running"'; then
    running="$running engine(build)"
  fi
  if affects data-platform && fetch_url "http://127.0.0.1:$DATA_PORT/api/jobs/current" \
      | grep -Eq '"state"[[:space:]]*:[[:space:]]*"running"'; then
    running="$running data-platform(download)"
  fi
  [ -z "$running" ] && return 0
  echo "[WARN] A job is RUNNING on:$running - '$CMD' restarts that app and interrupts it (it resumes later)."
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
}

wait_healthy() {  # wait_healthy <name> <url>
  i=0
  while [ $i -lt 60 ]; do
    if fetch_url "$2" | grep -q '"status"'; then
      echo "  $1: up"
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  echo "  $1: NOT answering on $2 yet - check: docker compose logs $1"
}

print_urls() {
  printf '\n'
  if affects engine || affects viewer; then
    wait_healthy engine "http://127.0.0.1:$ENGINE_PORT/health"
    printf 'Engine (apps connect here):\n'
    printf '  ArcGIS:  http://<this-host>:%s/arcgis/rest/services/<area>/ImageServer\n' "$ENGINE_PORT"
    printf '  Terrain: http://127.0.0.1:%s/tiles/terrain.json\n' "$ENGINE_PORT"
    printf '  API:     http://127.0.0.1:%s/docs\n' "$ENGINE_PORT"
  fi
  if affects data-platform; then
    wait_healthy data-platform "http://127.0.0.1:$DATA_PORT/health"
    printf 'Data-platform (downloads): http://127.0.0.1:%s/fetch/\n' "$DATA_PORT"
  fi
  if affects viewer; then
    printf 'Viewer:  http://127.0.0.1:%s/\n' "$VIEWER_PORT"
  fi
}

case "$CMD" in
  up)
    confirm_no_running_job
    # shellcheck disable=SC2086
    docker compose up -d --build --remove-orphans $SERVICES
    print_urls
    ;;
  down)
    confirm_no_running_job
    if [ -n "$SERVICES" ]; then
      # shellcheck disable=SC2086
      docker compose stop $SERVICES
    else
      docker compose down
    fi
    ;;
  logs)
    # shellcheck disable=SC2086
    docker compose logs -f $SERVICES ;;
  build)
    # shellcheck disable=SC2086
    docker compose build $SERVICES ;;
  restart)
    confirm_no_running_job
    # shellcheck disable=SC2086
    docker compose restart $SERVICES
    print_urls
    ;;
  status) docker compose ps ;;
  *)
    echo "Usage: $0 [-y] [up|down|logs|build|restart|status] [engine|data-platform|viewer ...]" >&2
    exit 2
    ;;
esac
