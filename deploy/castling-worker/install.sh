#!/usr/bin/env bash
# Install (or upgrade) the Rook worker on Linux: one file, joined to a hub,
# run by a service that starts it at boot and again whenever it exits (a core
# update's trial relies on that). No Python, no venv, no pip.
#
#   install.sh --binary PATH|URL --hub https://castling.bakeforge.com [--name NAME] [--root DIR] [--code CODE]
#   install.sh --upgrade --binary PATH|URL        # a new installed file, same data and keys
#
# The binary is the release job's `rook-worker-linux-<arch>` (docs/castling-updates.md),
# a local path or a URL (fetched with curl). It goes to <root>/rook-worker; core
# updates over the band keep their files under <root>/data/core and never
# replace it. --upgrade over an earlier (Python) install keeps its config, data
# and identity.
# Unit: rook05-worker (Restart=always), enabled and started: a systemd --user
# unit, or a system unit when run as root (containers without a user bus).
# Root installs default to /opt/rook05.
# Joining: the machine waits until someone taps Join on the hub's Workers page.
# --code (or ROOK_JOIN_CODE) joins at once with a one-time code from the hub
# (hub.join_code, or POST /enroll/code with a token).
set -euo pipefail

say() { printf '== %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

HOME=${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}
export HOME
if [ "$(id -u)" = 0 ]; then ROOT=/opt/rook05; else ROOT="$HOME/.rook05"; fi
HUB="" CODE=${ROOK_JOIN_CODE:-} NAME="" BINARY="" upgrade=0
while [ $# -gt 0 ]; do
    case $1 in
        --binary) BINARY=$2; shift 2 ;;
        --hub) HUB=$2; shift 2 ;;
        --name) NAME=$2; shift 2 ;;
        --root) ROOT=$2; shift 2 ;;
        --code) CODE=$2; shift 2 ;;
        --upgrade) upgrade=1; shift ;;
        *) die "unknown argument $1" ;;
    esac
done
CFG=$ROOT/worker.yaml
BIN=$ROOT/rook-worker
UNIT=rook05-worker
if [ "$(id -u)" = 0 ]; then
    UNIT_DIR=/etc/systemd/system; SCTL="systemctl"; WANTED=multi-user.target
else
    UNIT_DIR=$HOME/.config/systemd/user; SCTL="systemctl --user"; WANTED=default.target
fi
[ -n "$BINARY" ] || die "--binary is required (a path, or a URL)"
[ $upgrade = 1 ] || [ -n "$HUB" ] || die "--hub is required"
[ $upgrade = 0 ] || [ -f "$CFG" ] || die "nothing to upgrade in $ROOT"
NAME=${NAME:-$(hostname -s)}

umask 077
mkdir -p "$ROOT" "$UNIT_DIR"
chmod 700 "$ROOT"

# -- the file ---------------------------------------------------------------------------
say "binary: $BINARY"
case $BINARY in
    http://*|https://*)
        command -v curl >/dev/null || die "needs curl to fetch $BINARY"
        curl -fsSL --max-time 300 -A rook-worker-install -o "$BIN.new" "$BINARY" ;;
    *) [ -f "$BINARY" ] || die "no file $BINARY"; cp "$BINARY" "$BIN.new" ;;
esac
chmod 755 "$BIN.new"
[ $upgrade = 1 ] && $SCTL stop $UNIT 2>/dev/null || true
mv "$BIN.new" "$BIN"

# -- config -------------------------------------------------------------------------
[ -f "$CFG" ] || printf 'data: data\nname: %s\n' "$NAME" > "$CFG"
chmod 600 "$CFG"

# -- unit ---------------------------------------------------------------------------
cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=Rook worker $NAME
After=network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=$BIN --config $CFG
Restart=always
RestartSec=5

[Install]
WantedBy=$WANTED
EOF
$SCTL daemon-reload

# -- join -------------------------------------------------------------------------------
if ! grep -q '^anchor:' "$CFG"; then
    say "enroll $NAME with $HUB"
    (cd "$ROOT" && timeout 600 "$BIN" --config "$CFG" enroll "$HUB" --name "$NAME" ${CODE:+--code "$CODE"})
fi
$SCTL enable $UNIT >/dev/null 2>&1 || true
$SCTL restart $UNIT
[ "$(id -u)" = 0 ] || loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
say "done: $NAME, unit $UNIT ($($SCTL is-active $UNIT))"
