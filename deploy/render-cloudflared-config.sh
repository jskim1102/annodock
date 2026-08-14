#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$PROJECT_ROOT"

if [ -f .env ]; then
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue ;; esac
    case "$line" in *=*) export "${line%%=*}=${line#*=}" ;; esac
  done < .env
fi

: "${PROXY_PORT:?PROXY_PORT is required in .env}"
case "$PROXY_PORT" in
  *[!0-9]*|'')
    echo "error: PROXY_PORT must be an integer" >&2
    exit 1
    ;;
esac
if (( PROXY_PORT < 1 || PROXY_PORT > 65535 )); then
  echo "error: PROXY_PORT must be between 1 and 65535" >&2
  exit 1
fi

RUNTIME_DIR=.prod-runtime
RUNTIME_CONFIG="$RUNTIME_DIR/cloudflared-config.yml"
mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

TEMP_CONFIG=$(mktemp "$RUNTIME_DIR/cloudflared-config.yml.XXXXXX")
cleanup() {
  rm -f -- "$TEMP_CONFIG"
}
trap cleanup EXIT

sed "s/__PROXY_PORT__/${PROXY_PORT}/g" deploy/cloudflared-config.yml > "$TEMP_CONFIG"
if grep -q '__PROXY_PORT__' "$TEMP_CONFIG"; then
  echo "error: unresolved proxy port placeholder" >&2
  exit 1
fi

/home/kim_3090/.local/bin/cloudflared --config "$TEMP_CONFIG" tunnel ingress validate
chmod 600 "$TEMP_CONFIG"
mv -f -- "$TEMP_CONFIG" "$RUNTIME_CONFIG"
trap - EXIT
