#!/usr/bin/env bash
# Run a complete Rook hub on this machine: the UDP relay (telesthete-hub) and
# the hub (`rook-hub`: dashboard + MCP server in one process), in the
# foreground. Ctrl-C stops both.
#
# First run generates a band key, a dashboard password and an MCP token into
# $ROOK_DATA_DIR/quickstart.env (mode 600) and reuses them afterwards.
#
# Needs: `pip install` of this repo (rook-hub on PATH) and telesthete-hub on
# PATH or in ~/.cargo/bin (see README). Environment knobs:
#   ROOK_DATA_DIR  state directory             (default ./rook-data)
#   BIND           address all of it listens on (default 127.0.0.1; 0.0.0.0 for LAN workers)
#   RELAY_PORT     UDP relay                   (default 7474)
#   DASHBOARD_PORT dashboard, /mcp and /band   (default 7005)
#   MCP_PORT       the MCP server alone, as the separate rook-mcp served it
#                  (default 8765; "off" to serve MCP only on DASHBOARD_PORT)
#   ROOK_HUB_SPLIT=1  run the dashboard and the MCP server as two processes
#                  (rook-dashboard + rook-mcp, the pre-merge layout; compat)
set -euo pipefail

DATA="${ROOK_DATA_DIR:-$PWD/rook-data}"
BIND="${BIND:-127.0.0.1}"
RELAY_PORT="${RELAY_PORT:-7474}"
DASHBOARD_PORT="${DASHBOARD_PORT:-7005}"
MCP_PORT="${MCP_PORT:-8765}"
PYTHON="${PYTHON:-python3}"

# rustup adds ~/.cargo/bin to PATH only for new login shells, so a relay
# just installed with `cargo install` is often not on PATH yet.
HUB_BIN="$(command -v telesthete-hub 2>/dev/null || true)"
if [ -z "$HUB_BIN" ] && [ -x "${CARGO_HOME:-$HOME/.cargo}/bin/telesthete-hub" ]; then
  HUB_BIN="${CARGO_HOME:-$HOME/.cargo}/bin/telesthete-hub"
fi

SPLIT="${ROOK_HUB_SPLIT:-0}"
[ "$SPLIT" = 1 ] && [ "$MCP_PORT" = off ] && { echo "ROOK_HUB_SPLIT=1 needs an MCP_PORT" >&2; exit 1; }
if [ "$SPLIT" = 1 ]; then cmds="rook-dashboard rook-mcp"; else cmds="rook-hub"; fi

missing=""
[ -n "$HUB_BIN" ] || missing=" telesthete-hub"
for cmd in $cmds; do
  command -v "$cmd" >/dev/null 2>&1 || missing="$missing $cmd"
done
if [ -n "$missing" ]; then
  echo "missing on PATH:$missing" >&2
  echo "  relay: cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium" >&2
  echo "  rook:  pip install -e .   (from this repo, inside a virtualenv)" >&2
  exit 1
fi

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
  "$HUB_BIN" &
sleep 1
# The dashboard serves plain HTTP here, so its installer links say http://.
# Workers reach the relay through the hub's WebSocket bridge (--hub-public,
# ws://<host>:<port>/band on a non-443 port): one TCP port to allow, and the
# same path a public hub uses behind its TLS proxy.
if [ "$SPLIT" = 1 ]; then
  rook-dashboard --bind "$BIND" --port "$DASHBOARD_PORT" \
    --hub-host 127.0.0.1 --hub-port "$RELAY_PORT" \
    --domain "http://$PUBLIC_HOST:$DASHBOARD_PORT" --hub-public "$PUBLIC_HOST:$MCP_PORT" &
  rook-mcp --hub "127.0.0.1:$RELAY_PORT" --bind "$BIND:$MCP_PORT" \
    --allowed-hosts "$PUBLIC_HOST:$MCP_PORT" &
  MCP_URL="http://$PUBLIC_HOST:$MCP_PORT/mcp"
else
  # One process. MCP_PORT keeps the old MCP-only port for workers and agents
  # set up against it; new installers use the dashboard port for everything.
  mcp_bind="off"; extra_hosts=""
  if [ "$MCP_PORT" != off ]; then
    mcp_bind="$BIND:$MCP_PORT"; extra_hosts=",$PUBLIC_HOST:$MCP_PORT"
  fi
  rook-hub --bind "$BIND" --port "$DASHBOARD_PORT" --mcp-bind "$mcp_bind" \
    --hub-host 127.0.0.1 --hub-port "$RELAY_PORT" \
    --domain "http://$PUBLIC_HOST:$DASHBOARD_PORT" --hub-public "$PUBLIC_HOST:$DASHBOARD_PORT" \
    --allowed-hosts "$PUBLIC_HOST:$DASHBOARD_PORT$extra_hosts" &
  MCP_URL="http://$PUBLIC_HOST:$DASHBOARD_PORT/mcp"
fi

cat <<EOF

Rook hub running (Ctrl-C to stop). State: $DATA

  Dashboard   http://$PUBLIC_HOST:$DASHBOARD_PORT    password: $ROOK_WEB_PASS
  MCP         $MCP_URL      bearer token in $SECRETS
  Relay       udp://$PUBLIC_HOST:$RELAY_PORT

Install a worker:  http://$PUBLIC_HOST:$DASHBOARD_PORT/#install
  sign in with the password above, click "Get a pairing code" and run the
  command it shows on the new machine. (The first worker bundle builds in the
  background; give it a minute.)

Or join one directly (same machine or LAN), in another terminal:

  rook worker --hub $PUBLIC_HOST:$RELAY_PORT --psk "\$(grep ^ROOK_BAND_PSK= $SECRETS | cut -d= -f2-)" --name my-worker

EOF
wait
