#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_ROOT"

if [ -f .env ]; then
  set -a
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in *=*) export "${line%%=*}=${line#*=}" ;; esac
  done < .env
  set +a
fi

: "${PROXY_PORT:?PROXY_PORT is required in .env}"
: "${BACKEND_PORT:?BACKEND_PORT is required in .env}"
: "${AUTH_PORT:?AUTH_PORT is required in .env}"

CADDY_BIN=${CADDY_BIN:-${HOME:?}/.local/bin/caddy}
PIDFILE=.prod.caddy.pid
LOGDIR=.prod-logs

if [ ! -x "$CADDY_BIN" ]; then
  echo "error: caddy binary not found at $CADDY_BIN" >&2
  exit 1
fi

port_in_use() {
  python3 - "$1" <<'PY'
import socket
import sys

with socket.socket() as sock:
    sock.settimeout(0.1)
    raise SystemExit(0 if sock.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}

wait_port_free() {
  for _ in $(seq 1 50); do
    port_in_use "$1" || return 0
    sleep 0.1
  done
  echo "error: port $1 did not become free" >&2
  return 1
}

case "${1:-}" in
  up)
    if [ -f "$PIDFILE" ]; then
      existing_pid=$(tr -d '[:space:]' < "$PIDFILE")
      if [[ "$existing_pid" =~ ^[0-9]+$ ]] && kill -0 "$existing_pid" 2>/dev/null; then
        echo "already up — deploy/prod.sh down first" >&2
        exit 1
      fi
      echo "error: stale $PIDFILE found; inspect it before removing" >&2
      exit 1
    fi
    if port_in_use "$PROXY_PORT"; then
      echo "error: PROXY_PORT $PROXY_PORT is already in use" >&2
      exit 1
    fi

    npm --prefix frontend run build
    "$CADDY_BIN" validate --config deploy/Caddyfile --adapter caddyfile
    mkdir -p "$LOGDIR"
    setsid "$CADDY_BIN" run --config deploy/Caddyfile --adapter caddyfile \
      >"$LOGDIR/caddy.log" 2>&1 &
    caddy_pid=$!
    printf '%s\n' "$caddy_pid" > "$PIDFILE"

    for _ in $(seq 1 50); do
      if curl -fsS -o /dev/null "http://127.0.0.1:${PROXY_PORT}/"; then
        echo "up — loopback proxy port: $PROXY_PORT (logs: $LOGDIR/caddy.log)"
        exit 0
      fi
      if ! kill -0 "$caddy_pid" 2>/dev/null; then
        echo "error: caddy exited during startup; inspect $LOGDIR/caddy.log" >&2
        exit 1
      fi
      sleep 0.1
    done
    echo "error: caddy did not become ready; inspect $LOGDIR/caddy.log" >&2
    exit 1
    ;;
  down)
    if [ ! -f "$PIDFILE" ]; then
      echo "not running"
      exit 0
    fi
    caddy_pid=$(tr -d '[:space:]' < "$PIDFILE")
    if ! [[ "$caddy_pid" =~ ^[0-9]+$ ]]; then
      echo "error: invalid PID in $PIDFILE; inspect it manually" >&2
      exit 1
    fi
    kill -- "-$caddy_pid" 2>/dev/null || kill "$caddy_pid" 2>/dev/null || true
    wait_port_free "$PROXY_PORT"
    rm -f -- "$PIDFILE"
    echo "down — port free: $PROXY_PORT"
    ;;
  *)
    echo "usage: deploy/prod.sh up|down" >&2
    exit 2
    ;;
esac

