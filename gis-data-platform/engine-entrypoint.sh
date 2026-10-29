#!/bin/bash
# ENGINE container entry point (PID 1 is tini, which reaps the build
# subprocesses and forwards signals here):
#   uvicorn  - the engine's FastAPI on 0.0.0.0:8787 (127.0.0.1 for nginx, and
#              engine:8787 on the compose network for the data-platform's
#              hand-off; not published on the host)
#   nginx    - port 80 (host GIS_ENGINE_PORT, default 5000): tile files from
#              the volume, everything else proxied to uvicorn
# If either process exits, the other is stopped and the container exits, so
# Docker's restart policy brings the whole engine back.
set -u

nginx -t -q || { echo "engine: nginx configuration test failed" >&2; exit 1; }

python -m app --host 0.0.0.0 --port 8787 &
api=$!
nginx -g 'daemon off;' &
web=$!

stop() {
    kill -TERM "$api" "$web" 2>/dev/null
}
trap 'echo "engine: stopping"; stop' TERM INT

wait -n "$api" "$web"
rc=$?
if kill -0 "$api" 2>/dev/null && kill -0 "$web" 2>/dev/null; then
    # woken by the trap (docker stop): wait for both to finish
    wait "$api" "$web"
    exit 0
fi
echo "engine: a process exited (code $rc) - stopping the container" >&2
stop
wait
[ "$rc" -eq 0 ] && rc=1
exit "$rc"
