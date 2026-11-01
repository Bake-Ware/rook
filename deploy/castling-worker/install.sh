#!/usr/bin/env bash
# Install (or upgrade) a Rook 0.5 worker beside a machine's 0.4 worker and join
# it to a 0.5 hub. Shares nothing with 0.4 (not ~/.rook-band-worker, its venv,
# unit, port or keys).
#
#   install.sh --hub https://castling.bakeforge.com [--name NAME] [--ref SHA]
#              [--plugins "shell file ..."] [--root ~/.rook05]
#   install.sh --upgrade [--ref SHA]           # new code, same data and keys
#
# Code: a codeload export of Bake-Ware/rook at REF (no git clone needed).
# Unit: systemd --user rook05-worker (Restart=always), enabled and started.
# Joining: the device-code enrollment; a hub in move-in approves it at once
# (castling-switchover-decisions, "Usable first").
set -euo pipefail

say() { printf '== %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

HUB="" NAME="" REF="castling" PLUGINS="" ROOT="$HOME/.rook05" upgrade=0
while [ $# -gt 0 ]; do
    case $1 in
        --hub) HUB=$2; shift 2 ;;
        --name) NAME=$2; shift 2 ;;
        --ref) REF=$2; shift 2 ;;
        --plugins) PLUGINS=$2; shift 2 ;;
        --root) ROOT=$2; shift 2 ;;
        --upgrade) upgrade=1; shift ;;
        *) die "unknown argument $1" ;;
    esac
done
CFG=$ROOT/worker.yaml
SRC=$ROOT/src
VENV=$ROOT/venv
PY=$VENV/bin/python
UNIT_DIR=$HOME/.config/systemd/user
UNIT=rook05-worker
[ $upgrade = 1 ] || [ -n "$HUB" ] || die "--hub is required"
[ $upgrade = 0 ] || [ -f "$CFG" ] || die "nothing to upgrade in $ROOT"
NAME=${NAME:-$(hostname -s)}
for c in curl tar git; do command -v $c >/dev/null || die "needs $c"; done
PYTHON=""
for p in python3.13 python3.12 python3.11 python3; do
    if command -v $p >/dev/null && $p -c 'import sys, venv; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        PYTHON=$(command -v $p); break
    fi
done
[ -n "$PYTHON" ] || command -v uv >/dev/null || die "needs python 3.11+ (with venv) or uv"

umask 077
mkdir -p "$ROOT" "$UNIT_DIR"
chmod 700 "$ROOT"

# -- code ---------------------------------------------------------------------------
say "code: Bake-Ware/rook@$REF"
rm -rf "$SRC.new"; mkdir -p "$SRC.new"
curl -fsSL --max-time 300 -A rook05-install "https://codeload.github.com/Bake-Ware/rook/tar.gz/$REF" \
    | tar -xz -C "$SRC.new" --strip-components=1
sha=$(curl -fsSL --max-time 30 -A rook05-install -H 'Accept: application/vnd.github.sha' \
    "https://api.github.com/repos/Bake-Ware/rook/commits/$REF" 2>/dev/null || echo "$REF")
echo "$sha" > "$SRC.new/.castling-sha"
[ $upgrade = 1 ] && systemctl --user stop $UNIT 2>/dev/null || true
rm -rf "$SRC.old"; [ -d "$SRC" ] && mv "$SRC" "$SRC.old"
mv "$SRC.new" "$SRC"
say "venv: $VENV"
if [ ! -x "$PY" ]; then
    if [ -n "$PYTHON" ]; then "$PYTHON" -m venv "$VENV"; else uv venv -q --python 3.12 "$VENV"; fi
fi
extras=""
{ [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; } && extras="[desktop]"
if [ -x "$VENV/bin/pip" ]; then
    "$VENV/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
    "$VENV/bin/pip" install -q "$SRC$extras"
else
    uv pip install -q --python "$PY" "$SRC$extras"
fi
rm -rf "$SRC.old"

# -- config -------------------------------------------------------------------------
if [ -z "$PLUGINS" ]; then
    PLUGINS="shell file"
    if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; then
        PLUGINS="$PLUGINS screen-desktop hid-desktop notification-desktop"
    fi
fi
"$PY" - "$CFG" "$NAME" "$SRC/rook/plugins" "$PLUGINS" <<'EOF'
import os, sys, yaml
from pathlib import Path
path, name, plugins, wanted = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4].split()
cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
cfg.setdefault("data", str(path.parent / "data"))
cfg.setdefault("name", name)
cfg["plugins"] = [str(plugins / p) for p in wanted if (plugins / p / "plugin.yaml").is_file()]
if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
    cfg["hardware"] = sorted(set(cfg.get("hardware") or []) | {"display"})
cfg.setdefault("log", {"level": "info", "format": "text"})
path.write_text(yaml.safe_dump(cfg, sort_keys=False))
EOF
chmod 600 "$CFG"

# -- unit ---------------------------------------------------------------------------
cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=Rook 0.5 worker $NAME (beside 0.4)
After=network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$PY -m rook.core2.worker --config $CFG
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

# -- join -------------------------------------------------------------------------------
if ! grep -q '^anchor:' "$CFG"; then
    say "enroll $NAME with $HUB"
    (cd "$ROOT" && timeout 600 "$PY" -m rook.core2.worker --config "$CFG" enroll "$HUB" --name "$NAME")
fi
systemctl --user enable $UNIT >/dev/null 2>&1 || true
systemctl --user restart $UNIT
loginctl enable-linger "$USER" >/dev/null 2>&1 || true
say "done: $NAME on $(cut -c1-8 "$SRC/.castling-sha"), unit $UNIT ($(systemctl --user is-active $UNIT))"
