#!/usr/bin/env bash
# Install (or upgrade) a Rook 0.5 worker on Linux and join it to a hub, run by
# a service that starts it at boot and again whenever it exits (a core
# update's trial relies on that). Two ways, the same unit and config files:
#
#   install.sh --hub https://castling.bakeforge.com [--name NAME] [--root DIR] [--code CODE]
#              [--binary PATH|URL]                     # the one-file worker (no Python)
#              [--ref SHA] [--plugins "shell file ..."] # the Python worker (without --binary)
#   install.sh --upgrade [--binary PATH|URL | --ref SHA]   # new code, same data and keys
#
# --binary: the release job's `rook-worker-linux-<arch>` (docs/castling-updates.md),
# a local path or a URL (curl). It goes to <root>/rook-worker; core updates over
# the band keep their files under <root>/data/core and never replace it.
# Without --binary: the Python worker from a codeload export of Bake-Ware/rook
# at REF, in a venv. --upgrade --binary over a Python install keeps its config,
# data and identity.
# Unit: rook05-worker (Restart=always), enabled and started: a systemd --user
# unit, or a system unit when run as root (containers without a user bus).
# Root installs default to /opt/rook05 (the Python worker then runs as root,
# ROOK_RUN_AS=<account> for another, as the 0.4 worker runs there).
# Where nothing can be installed (no service, no rights): copy worker/rook_worker.py and
# run `python3 rook_worker.py enroll <hub>`, then `python3 rook_worker.py`.
# Joining: the machine waits until someone taps Join on the hub's Workers page.
# --code (or ROOK_JOIN_CODE) joins at once with a one-time code from the hub
# (hub.join_code, or POST /enroll/code with a token).
set -euo pipefail

say() { printf '== %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

HOME=${HOME:-$(getent passwd "$(id -un)" | cut -d: -f6)}
export HOME
if [ "$(id -u)" = 0 ]; then ROOT=/opt/rook05; else ROOT="$HOME/.rook05"; fi
HUB="" CODE=${ROOK_JOIN_CODE:-} NAME="" BINARY="" REF="castling" PLUGINS="" upgrade=0
while [ $# -gt 0 ]; do
    case $1 in
        --hub) HUB=$2; shift 2 ;;
        --name) NAME=$2; shift 2 ;;
        --root) ROOT=$2; shift 2 ;;
        --code) CODE=$2; shift 2 ;;
        --binary) BINARY=$2; shift 2 ;;
        --ref) REF=$2; shift 2 ;;
        --plugins) PLUGINS=$2; shift 2 ;;
        --upgrade) upgrade=1; shift ;;
        *) die "unknown argument $1" ;;
    esac
done
CFG=$ROOT/worker.yaml
UNIT=rook05-worker
if [ "$(id -u)" = 0 ]; then
    UNIT_DIR=/etc/systemd/system; SCTL="systemctl"; WANTED=multi-user.target
else
    UNIT_DIR=$HOME/.config/systemd/user; SCTL="systemctl --user"; WANTED=default.target
fi
[ $upgrade = 1 ] || [ -n "$HUB" ] || die "--hub is required"
[ $upgrade = 0 ] || [ -f "$CFG" ] || die "nothing to upgrade in $ROOT"
NAME=${NAME:-$(hostname -s)}
RUN=()       # the worker's command line, before --config (each install function sets it)
DONE=""      # what the last line says was installed

# The one-file worker: the binary, a config if there is none.
binary_worker() {
    local bin=$ROOT/rook-worker
    say "binary: $BINARY"
    case $BINARY in
        http://*|https://*)
            command -v curl >/dev/null || die "needs curl to fetch $BINARY"
            curl -fsSL --max-time 300 -A rook-worker-install -o "$bin.new" "$BINARY" ;;
        *) [ -f "$BINARY" ] || die "no file $BINARY"; cp "$BINARY" "$bin.new" ;;
    esac
    chmod 755 "$bin.new"
    [ $upgrade = 1 ] && $SCTL stop $UNIT 2>/dev/null || true
    mv "$bin.new" "$bin"
    [ -f "$CFG" ] || printf 'data: data\nname: %s\n' "$NAME" > "$CFG"
    RUN=("$bin")
    DONE="the one-file worker"
}

# The Python worker: the code, a venv, its config. This path goes when the
# Python worker (rook/core2/worker) goes.
python_worker() {
    local src=$ROOT/src venv=$ROOT/venv python="" p sha
    local py=$venv/bin/python
    command -v tar >/dev/null || die "needs tar"
    command -v git >/dev/null || die "needs git: pip fetches telesthete from GitHub (Debian: apt-get install -y git)"
    for p in python3.13 python3.12 python3.11 python3; do
        if command -v $p >/dev/null && $p -c 'import sys, venv; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
            python=$(command -v $p); break
        fi
    done
    [ -n "$python" ] || command -v uv >/dev/null || die "needs python 3.11+ (with venv) or uv"

    fetch() {   # URL [sha]: the body (or, with sha, the GitHub commit sha) on stdout
        local accept=""; [ "${2:-}" = sha ] && accept="application/vnd.github.sha"
        if command -v curl >/dev/null; then
            curl -fsSL --max-time 300 -A rook05-install ${accept:+-H "Accept: $accept"} "$1"
        else
            "${python:-python3}" - "$1" "$accept" <<'PYEOF'
import sys, urllib.request as u
h = {"User-Agent": "rook05-install"}
if sys.argv[2]:
    h["Accept"] = sys.argv[2]
sys.stdout.buffer.write(u.urlopen(u.Request(sys.argv[1], headers=h), timeout=300).read())
PYEOF
        fi
    }

    say "code: Bake-Ware/rook@$REF"
    rm -rf "$src.new"; mkdir -p "$src.new"
    fetch "https://codeload.github.com/Bake-Ware/rook/tar.gz/$REF" | tar -xz -C "$src.new" --strip-components=1
    sha=$(fetch "https://api.github.com/repos/Bake-Ware/rook/commits/$REF" sha 2>/dev/null || echo "$REF")
    echo "$sha" > "$src.new/.castling-sha"
    [ $upgrade = 1 ] && $SCTL stop $UNIT 2>/dev/null || true
    rm -rf "$src.old"; [ -d "$src" ] && mv "$src" "$src.old"
    mv "$src.new" "$src"
    say "venv: $venv"
    if [ ! -x "$py" ]; then
        if [ -n "$python" ]; then "$python" -m venv "$venv"; else uv venv -q --python 3.12 "$venv"; fi
    fi
    if [ -x "$venv/bin/pip" ]; then
        "$venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
        "$venv/bin/pip" install -q "$src"
    else
        uv pip install -q --python "$py" "$src"
    fi
    rm -rf "$src.old"

    if [ -z "$PLUGINS" ]; then
        PLUGINS="shell file"
        if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; then
            PLUGINS="$PLUGINS screen-desktop hid-desktop notification-desktop"
        fi
    fi
    "$py" - "$CFG" "$NAME" "$src/rook/plugins" "$PLUGINS" <<'EOF'
import os, sys, yaml
from pathlib import Path
path, name, plugins, wanted = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4].split()
cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
cfg.setdefault("data", str(path.parent / "data"))
cfg.setdefault("name", name)
cfg["plugins"] = [str(plugins / p) for p in wanted if (plugins / p / "plugin.yaml").is_file()]
if os.getuid() == 0:
    cfg.setdefault("run_as", os.environ.get("ROOK_RUN_AS") or "root")   # as 0.4 ran on this machine
if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
    cfg["hardware"] = sorted(set(cfg.get("hardware") or []) | {"display"})
cfg.setdefault("log", {"level": "info", "format": "text"})
path.write_text(yaml.safe_dump(cfg, sort_keys=False))
EOF
    RUN=("$py" -m rook.core2.worker)
    DONE="the Python worker at $(cut -c1-8 "$src/.castling-sha")"
}

umask 077
mkdir -p "$ROOT" "$UNIT_DIR"
chmod 700 "$ROOT"
if [ -n "$BINARY" ]; then binary_worker; else python_worker; fi
chmod 600 "$CFG"

# -- unit ---------------------------------------------------------------------------
cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=Rook 0.5 worker $NAME
After=network-online.target

[Service]
WorkingDirectory=$ROOT
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${RUN[*]} --config $CFG
Restart=always
RestartSec=5

[Install]
WantedBy=$WANTED
EOF
$SCTL daemon-reload

# -- join -------------------------------------------------------------------------------
if ! grep -q '^anchor:' "$CFG"; then
    say "enroll $NAME with $HUB"
    (cd "$ROOT" && timeout 600 "${RUN[@]}" --config "$CFG" enroll "$HUB" --name "$NAME" ${CODE:+--code "$CODE"})
fi
$SCTL enable $UNIT >/dev/null 2>&1 || true
$SCTL restart $UNIT
[ "$(id -u)" = 0 ] || loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true
say "done: $NAME, $DONE, unit $UNIT ($($SCTL is-active $UNIT))"
