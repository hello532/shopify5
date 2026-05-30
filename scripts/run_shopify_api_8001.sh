#!/bin/zsh
set -u

APP_DIR="/Users/doi/Desktop/amazon"
PYTHON_BIN="/opt/homebrew/bin/python3"
APP_FILE="$APP_DIR/shopify_api_server.py"
LOG_FILE="$APP_DIR/logs/shopify_api_server_8001.log"
PID_FILE="$APP_DIR/logs/shopify_api_server_8001.pid"
LOCK_DIR="$APP_DIR/logs/shopify_api_server_8001.lock"

mkdir -p "$APP_DIR/logs"
cd "$APP_DIR" || exit 1

export PYTHONUNBUFFERED=1
child_pid=""

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  printf '[%s] another shopify_api_server.py runner is already active; exiting\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
  exit 0
fi

cleanup() {
  if [[ -n "${child_pid:-}" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  rmdir "$LOCK_DIR" 2>/dev/null || true
}

stop_runner() {
  cleanup
  exit 0
}

trap stop_runner INT TERM HUP
trap cleanup EXIT

while true; do
  printf '[%s] starting shopify_api_server.py on 127.0.0.1:8001\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
  "$PYTHON_BIN" "$APP_FILE" >> "$LOG_FILE" 2>&1 &
  child_pid=$!
  printf '%s\n' "$child_pid" > "$PID_FILE"
  wait "$child_pid"
  exit_code=$?
  child_pid=""
  printf '[%s] server exited with code %s; restarting in 3s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$exit_code" >> "$LOG_FILE"
  sleep 3
done
