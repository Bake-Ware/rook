#!/usr/bin/env bash
# End-to-end test of `rook hub` deploys against a THROWAWAY test hub whose
# dashboard and MCP run as systemd --user units. Never touches a live install.
#
#   scripts/hub-deploy-e2e.sh --dir DIR [--port-base P] [--keep] [--deadman]
#
#   --dir DIR        new directory for everything (must not exist, or be a
#                    previous run's dir with the .rook-hubdeploy-e2e marker)
#   --port-base P    test hub ports P..P+2 (default 17480)
#   --keep           leave the hub, units and DIR in place afterwards
#   --deadman        also test the dead-man timer (adds ~75 s)
#
# What it does: starts a test hub with scripts/test-hub.sh (relay + 1 worker),
# moves its dashboard and MCP into user units rook-e2e-P-{dashboard,mcp}, makes
# a private update signing key and a private clone of this repo, then deploys
# releases built from throwaway commits: first deploy, dashboard-only deploy,
# a release that crashes at start (auto-rollback), a stray drop-in (refused,
# then adopted), optionally a dead-man rollback, manual rollback and prune, and
# runs the integration suite against the deployed hub.
#
# Needs: PYTHON (default: the repo's .venv) with this repo installed
# (preferably non-editable, so the release has to win over the install),
# telesthete-hub (see scripts/test-hub.sh), git, and a systemd user manager.
set -euo pipefail

DIR=""; PORT_BASE=17480; KEEP=0; DEADMAN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --port-base) PORT_BASE="$2"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    --deadman) DEADMAN=1; shift ;;
    -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ -n "$DIR" ] || { echo "--dir is required" >&2; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
case "$DIR" in /*) ;; *) DIR="$PWD/$DIR" ;; esac
DIR="$(realpath -m "$DIR")"
MARK="$DIR/.rook-hubdeploy-e2e"
if [ -e "$DIR" ] && [ ! -e "$MARK" ]; then
  echo "refusing: $DIR exists and is not a hub-deploy e2e dir" >&2; exit 1
fi
systemctl --user is-system-running >/dev/null 2>&1 || \
  systemctl --user status >/dev/null 2>&1 || { echo "no systemd user manager" >&2; exit 1; }

TH="$DIR/th"; UNITDIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DASH_UNIT="rook-e2e-$PORT_BASE-dashboard.service"; MCP_UNIT="rook-e2e-$PORT_BASE-mcp.service"
RELAY=$PORT_BASE; DPORT=$((PORT_BASE + 1)); MPORT=$((PORT_BASE + 2))
CFG="$DIR/hub-deploy.json"
export ROOK_UPDATE_KEY="$DIR/update-signing-key"
unset ROOK_UPDATE_PUBKEY
PASS=0
ok()   { PASS=$((PASS + 1)); echo "  PASS: $*"; }
fail() { echo "  FAIL: $*" >&2; exit 1; }
hub()  { "$PYTHON" -m rook hub --config "$CFG" "$@"; }
jq_()  { "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
sel()  { hub status --json | jq_ "d['services']['$1']['selected']"; }
eff()  { hub status --json | jq_ "d['services']['$1']['effective']"; }

cleanup() {
  [ "$KEEP" = 1 ] && { echo "kept: $DIR (units $DASH_UNIT $MCP_UNIT)"; return; }
  echo "cleaning up"
  systemctl --user stop "$DASH_UNIT" "$MCP_UNIT" 2>/dev/null || true
  for u in $(systemctl --user list-units --all --plain --no-legend 'rook-hub-deadman-*' 2>/dev/null \
             | awk '{print $1}'); do
    grep -qF "$DIR/" "/run/user/$(id -u)/systemd/transient/$u" 2>/dev/null \
      && systemctl --user stop "$u" 2>/dev/null || true
  done
  rm -rf "$UNITDIR/$DASH_UNIT" "$UNITDIR/$MCP_UNIT" "$UNITDIR/$DASH_UNIT.d" "$UNITDIR/$MCP_UNIT.d"
  systemctl --user daemon-reload || true
  PYTHON="$PYTHON" "$REPO/scripts/test-hub.sh" reset --data "$TH" >/dev/null 2>&1 || true
  rm -rf "$DIR"
}
trap cleanup EXIT

mkdir -p "$DIR"; touch "$MARK"
echo "== test hub (relay + worker) on $PORT_BASE"
PYTHON="$PYTHON" "$REPO/scripts/test-hub.sh" start --data "$TH" --port-base "$PORT_BASE" --workers 1 >/dev/null
# shellcheck disable=SC1091
. "$TH/secrets.env"
for name in mcp dashboard; do  # move these two under systemd
  pid="$(cat "$TH/run/$name.pid")"
  tr '\0' '\n' < "/proc/$pid/environ" | grep -qxF "ROOK_TEST_HUB_DIR=$TH" && kill "$pid"
  rm -f "$TH/run/$name.pid"
done
sleep 1

echo "== user units $DASH_UNIT, $MCP_UNIT"
mkdir -p "$UNITDIR" "$DIR/env"
HH="$TH/hub/home"
common="HOME=$HH
XDG_CONFIG_HOME=$HH/.config
XDG_DATA_HOME=$HH/.local/share
XDG_CACHE_HOME=$HH/.cache
ROOK_DATA_DIR=$TH/hub
ROOK_UPDATE_URL=
ROOK_BAND_PSK=$ROOK_BAND_PSK"
( umask 077
  printf '%s\nROOK_WEB_PASS=%s\nROOK_KNOWLEDGE_ADMIN_URL=http://127.0.0.1:%s/knowledge/account-api\n' \
    "$common" "$ROOK_WEB_PASS" "$MPORT" > "$DIR/env/dashboard.env"
  printf '%s\nROOK_MCP_STATIC_TOKEN=%s\nROOK_MCP_AUTH_PASSWORD=%s\nROOK_KNOWLEDGE=1\n' \
    "$common" "$ROOK_MCP_STATIC_TOKEN" "$ROOK_MCP_AUTH_PASSWORD" > "$DIR/env/mcp.env" )
cat > "$UNITDIR/$DASH_UNIT" <<EOF
[Unit]
Description=Rook hub-deploy e2e dashboard ($DIR)
[Service]
EnvironmentFile=$DIR/env/dashboard.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON -m rook.remote.bootstrap --bind 127.0.0.1 --port $DPORT --hub-host 127.0.0.1 --hub-port $RELAY --domain 127.0.0.1:$DPORT --hub-public 127.0.0.1:$MPORT --band-name test-hub
Restart=on-failure
RestartSec=2
EOF
cat > "$UNITDIR/$MCP_UNIT" <<EOF
[Unit]
Description=Rook hub-deploy e2e MCP ($DIR)
[Service]
EnvironmentFile=$DIR/env/mcp.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON -m rook.band_mcp --hub 127.0.0.1:$RELAY --bind 127.0.0.1:$MPORT --allowed-hosts 127.0.0.1:$MPORT
Restart=on-failure
RestartSec=2
EOF
systemctl --user daemon-reload

cat > "$CFG" <<EOF
{"root": "$DIR/hub", "mode": "systemd", "systemd": {"scope": "user"},
 "python": "$PYTHON", "databases": ["$TH/hub/*.db"], "keep": 2,
 "deadman_minutes": 10, "health_timeout": 45, "settle_seconds": 3,
 "services": {
   "dashboard": {"unit": "$DASH_UNIT", "health": ["http://127.0.0.1:$DPORT/"]},
   "mcp": {"unit": "$MCP_UNIT",
           "health": ["tcp://127.0.0.1:$MPORT", "http://127.0.0.1:$MPORT/healthz"]}}}
EOF

echo "== private signing key + clone"
"$PYTHON" -m rook.remote.update_keys generate >/dev/null 2>&1 || "$PYTHON" "$REPO/rook/remote/update_keys.py" generate >/dev/null
git clone -q "$REPO" "$DIR/repo"
git -C "$DIR/repo" config user.email e2e@example.com
git -C "$DIR/repo" config user.name e2e
release() {  # release MSG [file-edit-python]  -> prints manifest path
  if [ -n "${2:-}" ]; then (cd "$DIR/repo" && "$PYTHON" -c "$2"); fi
  git -C "$DIR/repo" commit -q --allow-empty -am "$1"
  hub release build --repo "$DIR/repo" --out "$DIR/dist" | awk 'END{print $1}'
}
ver() { "$PYTHON" -c "import json,sys; print(json.load(open(sys.argv[1]))['version'])" "$1"; }

echo "== 1. first deploy (both services, dashboard first)"
A="$(release 'release A')"; VA="$(ver "$A")"
hub deploy "$A" --yes
[ "$(sel dashboard)" = "$VA" ] && [ "$(sel mcp)" = "$VA" ] || fail "selectors after first deploy"
[ "$(eff mcp)" = "$VA" ] || fail "systemd does not apply $VA"
pid="$(systemctl --user show -p MainPID --value "$MCP_UNIT")"
tr '\0' '\n' < "/proc/$pid/environ" | grep -qxF "PYTHONPATH=$DIR/hub/releases/$VA" || fail "mcp not on release path"
ls "$DIR"/hub/state/deploys/*/db/*.db >/dev/null || fail "no DB backups"
ok "deployed $VA to dashboard + mcp, DBs backed up, MCP runs from the release dir"

echo "== 2. integration suite against the deployed hub"
( cd "$REPO" && ROOK_IT=1 ROOK_IT_HUB_ENV="$TH/test-hub.env" "$PYTHON" -m pytest -q -p no:cacheprovider tests/integration ) \
  || fail "integration suite"
ok "integration suite passes on $VA"

echo "== 3. dashboard-only deploy"
B="$(release 'release B')"; VB="$(ver "$B")"
mcp_pid_before="$(systemctl --user show -p MainPID --value "$MCP_UNIT")"
hub deploy "$B" --services dashboard
[ "$(sel dashboard)" = "$VB" ] && [ "$(sel mcp)" = "$VA" ] || fail "dashboard-only selectors"
[ "$(systemctl --user show -p MainPID --value "$MCP_UNIT")" = "$mcp_pid_before" ] || fail "mcp was restarted"
hub deploy "$B" --yes --services mcp
[ "$(sel mcp)" = "$VB" ] || fail "mcp to B"
ok "dashboard-only deploy left the MCP running; then MCP caught up to $VB"

echo "== 4. release that crashes at start -> automatic rollback"
C="$(release 'release C (crashes)' "import re,pathlib; p=pathlib.Path('rook/remote/bootstrap.py'); s=p.read_text(); p.write_text(s.replace('def _cli_main() -> None:\n', 'def _cli_main() -> None:\n    raise SystemExit(\"e2e: broken release\")\n', 1))")"
VC="$(ver "$C")"
if hub deploy "$C" --yes; then fail "broken release deployed"; fi
[ "$(sel dashboard)" = "$VB" ] && [ "$(sel mcp)" = "$VB" ] || fail "not rolled back to $VB"
systemctl --user is-active -q "$DASH_UNIT" || fail "dashboard not running after rollback"
[ "$(eff dashboard)" = "$VB" ] || fail "effective release after rollback"
ok "$VC failed verification on the dashboard, rolled back to $VB, MCP untouched"

echo "== 5. stray drop-in"
mkdir -p "$UNITDIR/$DASH_UNIT.d"
printf '[Service]\nEnvironment=PYTHONPATH=/nonexistent/old-release\nEnvironment=ROOK_RELEASE=1.old.release\n' \
  > "$UNITDIR/$DASH_UNIT.d/95-hotfix.conf"
systemctl --user daemon-reload
[ "$(eff dashboard)" = "1.old.release" ] || fail "stray not effective"
hub status | grep -q "stray release drop-in" || fail "status does not flag the stray"
D="$(release 'release D' "import re,pathlib; p=pathlib.Path('rook/remote/bootstrap.py'); s=p.read_text(); p.write_text(s.replace('    raise SystemExit(\"e2e: broken release\")\n', '', 1))")"
VD="$(ver "$D")"
if hub deploy "$D" --yes 2>/dev/null; then fail "deploy ignored the stray"; fi
hub deploy "$D" --yes --adopt-strays
[ ! -e "$UNITDIR/$DASH_UNIT.d/95-hotfix.conf" ] || fail "stray still present"
[ "$(eff dashboard)" = "$VD" ] || fail "effective after adopt"
ok "stray refused, then adopted; systemd applies $VD"

if [ "$DEADMAN" = 1 ]; then
  echo "== 6. dead-man: broken deploy left unfinished"
  hub deploy "$C" --yes --allow-downgrade --services dashboard --no-auto-rollback --deadman-minutes 1 \
    && fail "broken release deployed"
  [ "$(hub status --json | jq_ "len(d['armed'])")" = 1 ] || fail "dead-man not armed"
  echo "   waiting for the dead-man timer..."
  for _ in $(seq 1 60); do
    [ "$(hub status --json | jq_ "len(d['armed'])")" = 0 ] && [ "$(eff dashboard)" = "$VD" ] && break
    sleep 2
  done
  [ "$(sel dashboard)" = "$VD" ] || fail "dead-man did not roll back"
  sleep 5; systemctl --user is-active -q "$DASH_UNIT" || fail "dashboard down after dead-man"
  hub history | grep -q "reason=deadman" || fail "no dead-man history entry"
  ok "dead-man timer rolled the dashboard back to $VD without the deploy tool"
fi

echo "== 7. manual rollback + prune"
hub rollback --yes
[ "$(sel dashboard)" = "$VB" ] && [ "$(sel mcp)" = "$VB" ] || fail "manual rollback"
hub status
hub prune --keep 2
hub status --json | jq_ "[r['version'] for r in d['releases']]"
ok "rollback to $VB and prune"

echo "== 8. integration suite again"
( cd "$REPO" && ROOK_IT=1 ROOK_IT_HUB_ENV="$TH/test-hub.env" "$PYTHON" -m pytest -q -p no:cacheprovider tests/integration ) \
  || fail "integration suite after rollback"
ok "integration suite passes on $VB"
hub history
echo "ALL $PASS CHECKS PASSED"
