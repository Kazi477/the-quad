#!/bin/bash
# Start The Quad with TWO backends + nginx failover (DEV / local only).
# Both backends share ONE SQLite database file in the same DATA_DIR
# (WAL mode), so a post/account made via one is visible via the other.
# Nginx sends all traffic to the primary (BACKEND_PORT) and fails over
# to the secondary (BACKEND_PORT_SECONDARY, marked `backup` in
# nginx_failover.conf.template) when the primary is unavailable.
#
# Usage:
#   DATA_DIR=/var/data APP_SECRET=<one shared secret> ./start_failover.sh
# Defaults: DATA_DIR is the folder this script lives in, ports are
#   primary 8081, secondary 8082, nginx listens on $PORT (default 10000).
#   If APP_SECRET is not set, one is generated and shared by BOTH
#   backends — never generate a different secret per backend, or
#   sessions/CSRF/reset tokens will break across a failover.
# Stop with Ctrl-C; both backends and nginx are stopped together.
# Requires: python3, nginx. This is NOT the production deploy shape —
# see DUAL_BACKEND.md (production needs managed Postgres + object
# storage, because two separate Render instances cannot share one disk).
set -Eeuo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${PORT:=10000}"
: "${BACKEND_PORT:=8081}"
: "${BACKEND_PORT_SECONDARY:=8082}"
: "${DATA_DIR:=$HERE}"
if [ -z "${APP_SECRET:-}" ]; then
  APP_SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
  echo "APP_SECRET was not set; generated one shared secret for both backends."
  echo "Save it if you want the same sessions to survive a restart."
fi
export PORT BACKEND_PORT BACKEND_PORT_SECONDARY DATA_DIR APP_SECRET
export PRODUCTION="${PRODUCTION:-0}" TRUST_PROXY="${TRUST_PROXY:-1}"

envsubst '${PORT} ${BACKEND_PORT} ${BACKEND_PORT_SECONDARY}' \
  < "$HERE/nginx_failover.conf.template" > /etc/nginx/nginx.conf
nginx -t

echo "Starting primary backend on 127.0.0.1:$BACKEND_PORT ..."
BACKEND_PORT="$BACKEND_PORT" python3 "$HERE/the_network_production.py" &
APP_A_PID=$!

wait_health() {
  local port="$1" name="$2"
  for _ in $(seq 1 80); do
    if python3 -c "import urllib.request,sys;urllib.request.urlopen('http://127.0.0.1:$port/healthz',timeout=1)" 2>/dev/null; then
      echo "$name is up on $port."
      return 0
    fi
    sleep 0.5
  done
  echo "$name failed to start on $port." >&2
  return 1
}
wait_health "$BACKEND_PORT" "Primary backend"

echo "Starting secondary backend on 127.0.0.1:$BACKEND_PORT_SECONDARY (same DATA_DIR: $DATA_DIR) ..."
BACKEND_PORT="$BACKEND_PORT_SECONDARY" python3 "$HERE/the_network_production.py" &
APP_B_PID=$!
wait_health "$BACKEND_PORT_SECONDARY" "Secondary backend"

echo "Starting nginx on port $PORT (primary $BACKEND_PORT, backup $BACKEND_PORT_SECONDARY) ..."
nginx -g 'daemon off;' &
NGINX_PID=$!

cleanup() {
  trap - EXIT TERM INT
  kill -TERM "$NGINX_PID" "$APP_A_PID" "$APP_B_PID" 2>/dev/null || true
  wait "$NGINX_PID" "$APP_A_PID" "$APP_B_PID" 2>/dev/null || true
}
trap cleanup EXIT TERM INT
wait -n "$NGINX_PID" "$APP_A_PID" "$APP_B_PID"
