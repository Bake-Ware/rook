#!/usr/bin/env bash
# Disposable Rook hub + throwaway workers for tests. Never touches a live install.
#
#   scripts/test-hub.sh start  [--workers N] [--data DIR] [--port-base P] [--bind ADDR]
#   scripts/test-hub.sh stop   [--data DIR]
#   scripts/test-hub.sh status [--data DIR]
#   scripts/test-hub.sh reset  [--data DIR]      # stop + delete the data dir
#
# What "isolated" means here:
#   * its own ROOK_DATA_DIR (default ./test-hub-data), marked with .rook-test-hub
#   * a freshly generated band key, dashboard password and MCP token
#   * non-default ports: relay = P, dashboard = P+1, MCP = P+2 (default P=17470);
#     7474/7005/8765 are refused outright
#   * loopback-only unless --bind says otherwise
#   * every process (relay, dashboard, MCP, workers) runs with HOME inside the
#     data dir, so none of them reads or writes a real ~/.rook-band-worker,
#     ~/.config/rook or an enrollment; workers get explicit --hub/--psk/--name
#     and no OTA update URL
#   * PID files in DIR/run, logs in DIR/logs, connection details in
#     DIR/test-hub.env (mode 600) for the integration tests
#
# Guards: start refuses when any of the three ports is already bound, or when
# the data dir holds Rook state without the .rook-test-hub marker (i.e. it looks
# like a live install), or is $HOME / a known live state directory.
#
# Needs: this repo installed in a virtualenv (python with `rook` importable;
# override with PYTHON=...), and the relay binary telesthete-hub, found as
# $TELESTHETE_HUB, then on PATH, then ~/.cargo/bin. Build it with:
#   cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium
set -euo pipefail

usage() { sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-2}"; }

CMD="${1:-}"; [ -n "$CMD" ] || usage
shift || true
case "$CMD" in start|stop|status|reset) ;; -h|--help|help) usage 0 ;; *) usage ;; esac

WORKERS=2
DATA="${ROOK_TEST_HUB_DATA:-$PWD/test-hub-data}"
PORT_BASE="${ROOK_TEST_HUB_PORT_BASE:-17470}"
BIND="127.0.0.1"
NAME_PREFIX="${ROOK_TEST_HUB_WORKER_PREFIX:-testw}"
KNOWLEDGE=1
DASHBOARD=1
while [ $# -gt 0 ]; do
  case "$1" in
    --workers)   WORKERS="$2"; shift 2 ;;
    --data)      DATA="$2"; shift 2 ;;
    --port-base) PORT_BASE="$2"; shift 2 ;;
    --bind)      BIND="$2"; shift 2 ;;
    --worker-prefix) NAME_PREFIX="$2"; shift 2 ;;
    --no-knowledge)  KNOWLEDGE=0; shift ;;
    --no-dashboard)  DASHBOARD=0; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage ;;
  esac
done

die() { echo "test-hub: $*" >&2; exit 1; }

# Absolute, normalised data dir (it need not exist yet).
case "$DATA" in /*) ;; *) DATA="$PWD/$DATA" ;; esac
DATA="$(realpath -m "$DATA")"
MARKER="$DATA/.rook-test-hub"
RUN="$DATA/run"
LOGS="$DATA/logs"
ENVFILE="$DATA/test-hub.env"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
  REPO="$(cd "$(dirname "$0")/.." && pwd)"
  if [ -x "$REPO/.venv/bin/python" ]; then PYTHON="$REPO/.venv/bin/python"; else PYTHON=python3; fi
fi

# --- guards ------------------------------------------------------------------

guard_data_dir() {
  local real_home
  real_home="$(realpath -m "${HOME:-/nonexistent}")"
  case "$DATA" in
    /|"$real_home"|"$real_home/.rook-band-worker"*|"$real_home/.config/rook"*|/var/lib/rook*|/etc/*)
      die "refusing data dir $DATA: that is (or holds) live Rook state" ;;
  esac
  [ -d "$DATA" ] || return 0
  [ -f "$MARKER" ] && return 0
  # An existing directory without our marker: fine if empty, refused if it
  # holds anything a real hub or worker writes.
  local f
  for f in quickstart.env setup.json enrollment.db oauth.json journal.db chat.db vault.db \
           knowledge.db guidance.db device_key worker_id; do
    [ -e "$DATA/$f" ] && die "refusing data dir $DATA: it contains $f but no .rook-test-hub marker, so it looks like a live install"
  done
  if [ -n "$(ls -A "$DATA" 2>/dev/null)" ]; then
    die "refusing data dir $DATA: not empty and not a test hub (no .rook-test-hub marker)"
  fi
}

guard_ports() {
  case "$PORT_BASE" in ''|*[!0-9]*) die "--port-base must be a number" ;; esac
  [ "$PORT_BASE" -ge 1024 ] && [ "$PORT_BASE" -le 65533 ] || die "--port-base must be within 1024..65533"
  RELAY_PORT=$PORT_BASE; DASHBOARD_PORT=$((PORT_BASE + 1)); MCP_PORT=$((PORT_BASE + 2))
  local p
  for p in $RELAY_PORT $DASHBOARD_PORT $MCP_PORT; do
    case "$p" in 7474|7005|8765) die "refusing port $p: it is a default port of a live Rook hub; pick another --port-base" ;; esac
  done
}

# Fails when a port is taken on any address (bind-test on the wildcard).
ports_free() {
  "$PYTHON" - "$RELAY_PORT" "$DASHBOARD_PORT" "$MCP_PORT" <<'PY'
import socket, sys
relay, dash, mcp = map(int, sys.argv[1:])
busy = []
for port, kind in ((relay, socket.SOCK_DGRAM), (dash, socket.SOCK_STREAM), (mcp, socket.SOCK_STREAM)):
    s = socket.socket(socket.AF_INET, kind)
    try:
        s.bind(("0.0.0.0", port))
    except OSError:
        busy.append(f"{port}/{'udp' if kind == socket.SOCK_DGRAM else 'tcp'}")
    finally:
        s.close()
if busy:
    print("ports already bound: " + ", ".join(busy), file=sys.stderr)
    sys.exit(1)
PY
}

find_relay() {
  if [ -n "${TELESTHETE_HUB:-}" ]; then
    [ -x "$TELESTHETE_HUB" ] || die "TELESTHETE_HUB=$TELESTHETE_HUB is not executable"
    echo "$TELESTHETE_HUB"; return
  fi
  if command -v telesthete-hub >/dev/null 2>&1; then command -v telesthete-hub; return; fi
  if [ -x "${HOME:-}/.cargo/bin/telesthete-hub" ]; then echo "$HOME/.cargo/bin/telesthete-hub"; return; fi
  die "telesthete-hub not found (set TELESTHETE_HUB, or: cargo install --locked --git https://github.com/Bake-Ware/telesthete telesthitium)"
}

# --- process bookkeeping -----------------------------------------------------

# A PID file only counts when the process is alive AND carries our data dir in
# its environment, so a recycled PID is never signalled.
pid_ours() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
  tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null | grep -qxF "ROOK_TEST_HUB_DIR=$DATA"
}

spawn() {  # spawn NAME HOME_DIR command...
  local name="$1" home="$2"; shift 2
  mkdir -p "$home" "$LOGS" "$RUN"
  # setsid: survive the calling shell (e.g. a Rook shell.exec) exiting.
  env -i PATH="$PATH" LANG="${LANG:-C.UTF-8}" HOME="$home" \
      XDG_CONFIG_HOME="$home/.config" XDG_DATA_HOME="$home/.local/share" \
      XDG_CACHE_HOME="$home/.cache" XDG_STATE_HOME="$home/.local/state" \
      ROOK_TEST_HUB_DIR="$DATA" ROOK_DATA_DIR="$home/rook-data" ROOK_UPDATE_URL= \
      "${SPAWN_ENV[@]}" \
      setsid "$@" >>"$LOGS/$name.log" 2>&1 < /dev/null &
  echo $! > "$RUN/$name.pid"
}

stop_all() {
  [ -d "$RUN" ] || return 0
  local f pid name any=0
  for f in "$RUN"/*.pid; do
    [ -e "$f" ] || continue
    name="$(basename "$f" .pid)"; pid="$(cat "$f" 2>/dev/null || true)"
    if pid_ours "$pid"; then
      kill -TERM "$pid" 2>/dev/null || true; any=1
      echo "stopping $name (pid $pid)"
    fi
  done
  if [ "$any" = 1 ]; then
    local i
    for i in $(seq 1 50); do
      local alive=0
      for f in "$RUN"/*.pid; do
        [ -e "$f" ] || continue
        pid_ours "$(cat "$f" 2>/dev/null || true)" && alive=1
      done
      [ "$alive" = 0 ] && break
      sleep 0.1
    done
    for f in "$RUN"/*.pid; do
      [ -e "$f" ] || continue
      pid="$(cat "$f" 2>/dev/null || true)"
      pid_ours "$pid" && { echo "killing $(basename "$f" .pid) (pid $pid)"; kill -KILL "$pid" 2>/dev/null || true; }
    done
  fi
  rm -f "$RUN"/*.pid
}

wait_tcp() {  # wait_tcp PORT SECONDS
  "$PYTHON" - "$1" "$2" <<'PY'
import socket, sys, time
port, deadline = int(sys.argv[1]), time.time() + float(sys.argv[2])
while time.time() < deadline:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        sys.exit(0)
    except OSError:
        time.sleep(0.2)
sys.exit(1)
PY
}

# --- commands ----------------------------------------------------------------

cmd_start() {
  guard_ports
  guard_data_dir
  case "$WORKERS" in ''|*[!0-9]*) die "--workers must be a number" ;; esac
  if [ -f "$MARKER" ] && [ -d "$RUN" ] && ls "$RUN"/*.pid >/dev/null 2>&1; then
    for f in "$RUN"/*.pid; do
      pid_ours "$(cat "$f")" && die "already running from $DATA (use stop or status)"
    done
  fi
  ports_free || die "ports $RELAY_PORT-$MCP_PORT are in use; pick another --port-base"
  local relay; relay="$(find_relay)"
  "$PYTHON" -c 'import rook' 2>/dev/null || die "$PYTHON cannot import rook (install this repo: pip install -e .)"

  mkdir -p "$DATA" "$RUN" "$LOGS" "$DATA/hub"
  chmod 700 "$DATA"
  touch "$MARKER"
  local secrets="$DATA/secrets.env"
  if [ ! -f "$secrets" ]; then
    ( umask 077
      { echo "ROOK_BAND_PSK=$("$PYTHON" -m rook.remote.psk)"
        echo "ROOK_WEB_PASS=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(12))')"
        echo "ROOK_MCP_STATIC_TOKEN=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')"
        echo "ROOK_MCP_AUTH_PASSWORD=$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(12))')"
      } > "$secrets" )
  fi
  # shellcheck disable=SC1090
  . "$secrets"

  local public="$BIND"
  [ "$BIND" = "0.0.0.0" ] && public="$(hostname -I 2>/dev/null | awk '{print $1}')"
  public="${public:-127.0.0.1}"

  SPAWN_ENV=(HUB_BIND="$BIND:$RELAY_PORT" HUB_PEER_TTL_SECS=60 HUB_PRUNE_SECS=10 RUST_LOG="${RUST_LOG:-warn}")
  spawn relay "$DATA/hub/home" "$relay"
  sleep 0.5

  SPAWN_ENV=(ROOK_DATA_DIR="$DATA/hub" ROOK_BAND_PSK="$ROOK_BAND_PSK" ROOK_MCP_STATIC_TOKEN="$ROOK_MCP_STATIC_TOKEN"
             ROOK_MCP_AUTH_PASSWORD="$ROOK_MCP_AUTH_PASSWORD" ROOK_KNOWLEDGE="$KNOWLEDGE")
  spawn mcp "$DATA/hub/home" "$PYTHON" -m rook.band_mcp --hub "127.0.0.1:$RELAY_PORT" \
      --bind "$BIND:$MCP_PORT" --allowed-hosts "$public:$MCP_PORT"
  if [ "$DASHBOARD" = 1 ]; then
    SPAWN_ENV=(ROOK_DATA_DIR="$DATA/hub" ROOK_BAND_PSK="$ROOK_BAND_PSK" ROOK_WEB_PASS="$ROOK_WEB_PASS"
               ROOK_KNOWLEDGE_ADMIN_URL="http://127.0.0.1:$MCP_PORT/knowledge/account-api")
    spawn dashboard "$DATA/hub/home" "$PYTHON" -m rook.remote.bootstrap --bind "$BIND" \
        --port "$DASHBOARD_PORT" --hub-host 127.0.0.1 --hub-port "$RELAY_PORT" \
        --domain "$public:$DASHBOARD_PORT" --hub-public "$public:$MCP_PORT" --band-name test-hub
  fi

  local names="" i
  for i in $(seq 1 "$WORKERS"); do
    local wname="$NAME_PREFIX-$i"
    SPAWN_ENV=()
    spawn "worker-$i" "$DATA/workers/$wname/home" "$PYTHON" -m rook.worker \
        --hub "127.0.0.1:$RELAY_PORT" --psk "$ROOK_BAND_PSK" --name "$wname" --update-url "" -v
    names="${names:+$names,}$wname"
  done

  ( umask 077
    cat > "$ENVFILE" <<EOF
# Written by scripts/test-hub.sh; read by tests/integration (ROOK_IT_HUB_ENV).
ROOK_IT_DATA_DIR=$DATA
ROOK_IT_MCP_URL=http://127.0.0.1:$MCP_PORT/mcp
ROOK_IT_DASHBOARD_URL=http://127.0.0.1:$DASHBOARD_PORT
ROOK_IT_RELAY=127.0.0.1:$RELAY_PORT
ROOK_IT_TOKEN=$ROOK_MCP_STATIC_TOKEN
ROOK_IT_WORKERS=$names
ROOK_IT_KNOWLEDGE=$KNOWLEDGE
EOF
  )

  if ! wait_tcp "$MCP_PORT" 30; then
    echo "MCP did not come up; last log lines:" >&2
    tail -n 20 "$LOGS/mcp.log" >&2 || true
    stop_all; exit 1
  fi
  cat <<EOF
Test hub running from $DATA
  MCP        http://$public:$MCP_PORT/mcp   (bearer token: ROOK_IT_TOKEN in $ENVFILE)
  Dashboard  http://$public:$DASHBOARD_PORT   (password: ROOK_WEB_PASS in $DATA/secrets.env)
  Relay      udp://$public:$RELAY_PORT
  Workers    ${names:-none}
  Logs       $LOGS
Run the integration suite against it:
  ROOK_IT=1 ROOK_IT_HUB_ENV=$ENVFILE python -m pytest -q tests/integration
EOF
}

cmd_status() {
  [ -f "$MARKER" ] || { echo "no test hub at $DATA"; return 1; }
  local f name pid up=0 down=0
  for f in "$RUN"/*.pid; do
    [ -e "$f" ] || continue
    name="$(basename "$f" .pid)"; pid="$(cat "$f" 2>/dev/null || true)"
    if pid_ours "$pid"; then echo "  $name: running (pid $pid)"; up=$((up + 1))
    else echo "  $name: stopped"; down=$((down + 1)); fi
  done
  [ -f "$ENVFILE" ] && grep -E '^ROOK_IT_(MCP_URL|DASHBOARD_URL|RELAY|WORKERS)=' "$ENVFILE" | sed 's/^/  /'
  if [ "$up" -gt 0 ] && [ "$down" = 0 ]; then echo "test hub at $DATA: running"; return 0; fi
  if [ "$up" -gt 0 ]; then echo "test hub at $DATA: degraded"; return 3; fi
  echo "test hub at $DATA: stopped"; return 3
}

cmd_stop() {
  [ -f "$MARKER" ] || die "no test hub at $DATA (missing .rook-test-hub marker)"
  stop_all
  echo "stopped test hub at $DATA"
}

cmd_reset() {
  [ -d "$DATA" ] || { echo "nothing to reset at $DATA"; return 0; }
  [ -f "$MARKER" ] || die "refusing to delete $DATA: no .rook-test-hub marker"
  stop_all
  rm -rf -- "$DATA"
  echo "removed test hub at $DATA"
}

"cmd_$CMD"
