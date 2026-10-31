# Shared settings for the Castling (0.5) sidecar scripts. Sourced, not run.
# Everything lives under $ROOT and three user units; nothing of the 0.4
# install (~/.rook-band-worker, its venv, units, ports, keys) is read or touched.
set -euo pipefail

ROOT=${ROOK_CASTLING_HOME:-$HOME/.rook-castling}
OWNER=${ROOK_CASTLING_OWNER:-$(id -un | tr "[:upper:]" "[:lower:]")}   # your Rook user name
WORKER_NAME=${ROOK_CASTLING_WORKER:-$(hostname -s)}
RELAY_PORT=${ROOK_CASTLING_RELAY_PORT:-7475}     # UDP, 127.0.0.1 only (0.4's relay is 7474 elsewhere)
HTTP_BIND=${ROOK_CASTLING_HTTP_BIND:-0.0.0.0:8750}   # dashboard + HTTP door: localhost and the LAN
MCP_BIND=${ROOK_CASTLING_MCP_BIND:-127.0.0.1:8751}   # MCP door: this machine only
HTTP_PORT=${HTTP_BIND##*:}
LOCAL_URL="http://127.0.0.1:$HTTP_PORT"

SRC=$ROOT/src            # git archive of the castling ref (no clone)
VENV=$ROOT/venv
PY=$VENV/bin/python
HUB_CFG=$ROOT/hub/hub.yaml
WORKER_CFG=$ROOT/worker/worker.yaml
SECRETS=$ROOT/secrets    # 0700: owner.token, recovery-key.txt, mcp.token
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
UNITS=(rook-castling-relay rook-castling-hub rook-castling-worker)

say() { printf '== %s\n' "$*"; }
die() { printf 'castling-sidecar: %s\n' "$*" >&2; exit 1; }

# wait_http URL SECONDS: until the door answers (any HTTP status).
wait_http() {
    local url=$1 n=${2:-30}
    for _ in $(seq "$n"); do
        curl -s -o /dev/null --max-time 2 "$url" && return 0
        sleep 1
    done
    return 1
}
