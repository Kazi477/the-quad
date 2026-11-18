#!/bin/bash
set -Eeuo pipefail

: "${PORT:=10000}"
: "${BACKEND_PORT:=8081}"

export PORT BACKEND_PORT
envsubst '${PORT} ${BACKEND_PORT}' < /app/nginx.conf.template > /etc/nginx/nginx.conf

python /app/app.py &
APP_PID=$!
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
