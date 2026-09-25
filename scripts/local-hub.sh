#!/usr/bin/env bash
# Run a complete Rook hub on this machine: the UDP relay (telesthete-hub), the
# dashboard and the MCP server, in the foreground. Ctrl-C stops all three.
#
# First run generates a band key, a dashboard password and an MCP token into
# $ROOK_DATA_DIR/quickstart.env (mode 600) and reuses them afterwards.
#
# Needs: `pip install` of this repo (rook-dashboard / rook-mcp on PATH) and
# telesthete-hub on PATH (see README). Environment knobs:
#   ROOK_DATA_DIR  state directory             (default ./rook-data)
#   BIND           address all three listen on (default 127.0.0.1; 0.0.0.0 for LAN workers)
#   RELAY_PORT     UDP relay                   (default 7474)
#   DASHBOARD_PORT web dashboard               (default 7005)
#   MCP_PORT       MCP server + /band bridge   (default 8765)
set -euo pipefail

DATA="${ROOK_DATA_DIR:-$PWD/rook-data}"
BIND="${BIND:-127.0.0.1}"
RELAY_PORT="${RELAY_PORT:-7474}"
DASHBOARD_PORT="${DASHBOARD_PORT:-7005}"
MCP_PORT="${MCP_PORT:-8765}"
PYTHON="${PYTHON:-python3}"

for cmd in telesthete-hub rook-dashboard rook-mcp; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "missing $cmd on PATH." >&2
    echo "  relay: cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium" >&2
    echo "  rook:  pip install -e .   (from this repo, inside a virtualenv)" >&2
    exit 1
  fi
done

mkdir -p "$DATA"
chmod 700 "$DATA"
SECRETS="$DATA/quickstart.env"
if [ ! -f "$SECRETS" ]; then
  (
    umask 077
    {
      echo "ROOK_BAND_PSK=$("$PYTHON" -m rook.remote.psk)"
      echo "ROOK_WEB_PASS=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(12))')"
      echo "ROOK_MCP_STATIC_TOKEN=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
      echo "ROOK_MCP_AUTH_PASSWORD=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(12))')"
    } > "$SECRETS"
  )
  echo "Generated band key, dashboard password and MCP token in $SECRETS"
fi
set -a
# shellcheck disable=SC1090
. "$SECRETS"
set +a
export ROOK_DATA_DIR="$DATA"

# The address workers and browsers use to reach this machine.
PUBLIC_HOST="$BIND"
[ "$BIND" = "0.0.0.0" ] && PUBLIC_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
PUBLIC_HOST="${PUBLIC_HOST:-127.0.0.1}"

trap 'trap - EXIT INT TERM; kill 0 2>/dev/null; wait 2>/dev/null' EXIT INT TERM

# Rook peers keep alive every 20s, so the relay must hold idle peers longer.
HUB_BIND="$BIND:$RELAY_PORT" HUB_PEER_TTL_SECS=60 HUB_PRUNE_SECS=10 RUST_LOG="${RUST_LOG:-warn}" \
  telesthete-hub &
sleep 1
rook-dashboard --bind "$BIND" --port "$DASHBOARD_PORT" \
  --hub-host 127.0.0.1 --hub-port "$RELAY_PORT" \
  --domain "$PUBLIC_HOST:$DASHBOARD_PORT" --hub-public "$PUBLIC_HOST:$MCP_PORT" &
rook-mcp --hub "127.0.0.1:$RELAY_PORT" --bind "$BIND:$MCP_PORT" \
  --allowed-hosts "$PUBLIC_HOST:$MCP_PORT" &

cat <<EOF

Rook hub running (Ctrl-C to stop). State: $DATA

  Dashboard   http://$PUBLIC_HOST:$DASHBOARD_PORT    password: $ROOK_WEB_PASS
  MCP         http://$PUBLIC_HOST:$MCP_PORT/mcp      bearer token in $SECRETS
  Relay       udp://$PUBLIC_HOST:$RELAY_PORT

Join a worker (same machine or LAN), in another terminal:

  rook worker --hub $PUBLIC_HOST:$RELAY_PORT --psk "\$(grep ^ROOK_BAND_PSK= $SECRETS | cut -d= -f2-)" --name my-worker

EOF
wait
