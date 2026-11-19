#!/bin/bash
set -Eeuo pipefail

: "${PORT:=10000}"
: "${BACKEND_PORT:=8081}"

export PORT BACKEND_PORT
envsubst '${PORT} ${BACKEND_PORT}' < /app/nginx.conf.template > /etc/nginx/nginx.conf

python /app/app.py &
APP_PID=$!

# Update 14: do NOT start nginx until the app is actually
# answering /healthz. Previously both started at once, so during
# a normal startup — and forever, if the app died in init_db()
# on a damaged replica before binding its port — nginx was up
# proxying to nothing on 127.0.0.1:8081 and Render's log filled
# with 'connect() failed (111: Connection refused) while
# connecting to upstream ... GET /healthz'. Waiting here means a
# connection-refused in the log now means the app really failed
# to start (its FATAL log line says why), not just 'nginx won
# the startup race'. If the app exits while we wait, exit too
# so Render restarts the container instead of serving 502s.
# Update 15: the loop above used to fall through and start
# nginx even when the app NEVER became healthy within the
# wait (120 attempts, no healthy flag) — exactly the
# connection-refused state it was meant to prevent, just 60
# seconds later. Now it exits if the app is not healthy by
# the end of the wait, so Render restarts the container and
# the log says why, instead of serving 502s indefinitely.
healthy=0
for _ in $(seq 1 120); do
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo 'FATAL: app process exited before becoming healthy; not starting nginx.' >&2
    wait "$APP_PID" 2>/dev/null || true
    exit 1
  fi
  if python3 -c 'import sys,urllib.request; sys.exit(0 if urllib.request.urlopen("http://127.0.0.1:'"$BACKEND_PORT"'/healthz", timeout=2).status == 200 else 1)' 2>/dev/null; then
    healthy=1
    break
  fi
  sleep 0.5
done
if [ "$healthy" -ne 1 ]; then
  if ! kill -0 "$APP_PID" 2>/dev/null; then
    echo 'FATAL: app process exited before becoming healthy; not starting nginx.' >&2
  else
    echo 'FATAL: app did not become healthy within 60s; not starting nginx.' >&2
    kill -TERM "$APP_PID" 2>/dev/null || true
  fi
  exit 1
fi

nginx -g 'daemon off;' &
NGINX_PID=$!

cleanup() {
  trap - EXIT TERM INT
  kill -TERM "$NGINX_PID" 2>/dev/null || true
  kill -TERM "$APP_PID" 2>/dev/null || true
  wait "$NGINX_PID" 2>/dev/null || true
  wait "$APP_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

wait -n "$APP_PID" "$NGINX_PID"
