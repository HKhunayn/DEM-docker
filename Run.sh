#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || {
  echo "[ERROR] Docker with Compose was not found on PATH." >&2
  exit 1
}

case "${1:-up}" in
  up)
    docker compose up -d --build
    printf '\nViewer: http://127.0.0.1:5173/\nAPI:    http://127.0.0.1:8787/docs\nFetch:  http://127.0.0.1:5173/fetch/\n'
    ;;
  down) docker compose down ;;
  logs) docker compose logs -f ;;
  build) docker compose build ;;
  restart)
    docker compose down
    docker compose up -d
    ;;
  *)
    echo "Usage: $0 [up|down|logs|build|restart]" >&2
    exit 2
    ;;
esac
