#!/usr/bin/env bash
# Install (or upgrade) a Castling 0.5 hub + worker beside the live 0.4 system.
#
#   deploy/castling-sidecar/install.sh [REF]            # default REF: origin/castling
#   deploy/castling-sidecar/install.sh --upgrade [REF]  # new code, same data and keys
#
# Run from any checkout of Bake-Ware/rook. Everything goes under
# ~/.rook-castling (see docs/castling-sidecar.md); the units are written but
# not enabled at boot. Secrets land in ~/.rook-castling/secrets (0600) and are
# never printed.
here=$(cd "$(dirname "$0")" && pwd)
. "$here/common.sh"

upgrade=0
if [ "${1:-}" = "--upgrade" ]; then upgrade=1; shift; fi
REF=${1:-origin/castling}
repo=$(git -C "$here" rev-parse --show-toplevel)

command -v uv >/dev/null || die "needs uv (https://docs.astral.sh/uv/)"
command -v curl >/dev/null || die "needs curl"
if [ -f "$HUB_CFG" ] && [ $upgrade = 0 ]; then
    die "$ROOT is set up already; use --upgrade to move it to new code"
fi
if [ ! -f "$HUB_CFG" ] && [ $upgrade = 1 ]; then
    die "nothing to upgrade in $ROOT; run without --upgrade first"
fi

umask 077
mkdir -p "$ROOT" "$SECRETS" "$ROOT/hub" "$ROOT/worker" "$UNIT_DIR"
chmod 700 "$ROOT" "$SECRETS"

# -- code: an exported tree of REF (no clone), and its own venv ------------------
say "code: $REF"
case $REF in origin/*) git -C "$repo" fetch -q origin "${REF#origin/}" ;; esac
sha=$(git -C "$repo" rev-parse --verify "$REF^{commit}")
[ $upgrade = 1 ] && "$here/stop.sh" >/dev/null 2>&1 || true
rm -rf "$SRC.new"
mkdir -p "$SRC.new"
git -C "$repo" archive "$sha" | tar -x -C "$SRC.new"
echo "$sha" > "$SRC.new/.castling-sha"
rm -rf "$SRC.old"
[ -d "$SRC" ] && mv "$SRC" "$SRC.old"
mv "$SRC.new" "$SRC"
rm -rf "$SRC.old"
chmod -R go-w "$SRC"
say "venv: $VENV"
[ -x "$PY" ] || uv venv -q --python 3.12 "$VENV"
uv pip install -q --python "$PY" -e "$SRC[manifest,desktop]"

# -- the units ------------------------------------------------------------------------
say "units: ${UNITS[*]} (not enabled at boot)"
cat > "$UNIT_DIR/rook-castling-relay.service" <<EOF
[Unit]
Description=Rook 0.5 sidecar: band relay (UDP 127.0.0.1:$RELAY_PORT)

[Service]
ExecStart=$PY $SRC/tests/e2e/relay.py $RELAY_PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/rook-castling-hub.service" <<EOF
[Unit]
Description=Rook 0.5 sidecar: hub (dashboard $HTTP_BIND, MCP $MCP_BIND)
Wants=rook-castling-relay.service
After=rook-castling-relay.service

[Service]
WorkingDirectory=$ROOT/hub
ExecStart=$PY -m rook.core2.hub --config $HUB_CFG
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/rook-castling-worker.service" <<EOF
[Unit]
Description=Rook 0.5 sidecar: worker $WORKER_NAME
Wants=rook-castling-hub.service
After=rook-castling-hub.service

[Service]
WorkingDirectory=$ROOT/worker
Environment=PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$PY -m rook.core2.worker --config $WORKER_CFG
# always: a core update that exits cleanly must still come back (and roll back).
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

# -- first run: the hub, the owner's secrets, the config --------------------------
if [ $upgrade = 0 ]; then
    say "hub: init (owner $OWNER)"
    "$PY" -m rook.core2.hub --config "$HUB_CFG" init --owner "$OWNER" \
        --relay "127.0.0.1:$RELAY_PORT" --url "$LOCAL_URL" --bind "$HTTP_BIND" > "$SECRETS/init.out"
    # The token and the recovery key go to their own 0600 files; init's own
    # printout (which shows both) is removed.
    "$PY" - "$SECRETS" <<'EOF'
import sys
from pathlib import Path
d = Path(sys.argv[1])
lines = (d / "init.out").read_text().splitlines()
token = lines[next(i for i, l in enumerate(lines) if l.startswith("Sign-in token")) + 1].strip()
(d / "owner.token").write_text(token + "\n")
EOF
    rm -f "$SECRETS/init.out"
    "$PY" -m rook.core2.hub --config "$HUB_CFG" recovery-key > "$SECRETS/recovery-key.txt"
fi
plugins="$SRC/rook/plugins"
# The library: the hub's trusted plugin folders (hub plugins run there; the
# worker ones are pushable). The worker loads its own straight from the code.
HUB_PLUGINS="agents notify changes tasks projects knowledge concepts whiteboard"
WORKER_PLUGINS="shell file notification-desktop screen-desktop torrent agents-claude-code agents-codex llm-cli"
mkdir -p "$ROOT/hub/library"
find "$ROOT/hub/library" -maxdepth 1 -type l -delete
for p in $HUB_PLUGINS $WORKER_PLUGINS; do
    [ -f "$plugins/$p/plugin.yaml" ] && ln -s "$plugins/$p" "$ROOT/hub/library/$p"
done
"$PY" - "$HUB_CFG" "$MCP_BIND" <<'EOF'
import sys, yaml
from pathlib import Path
path, mcp = Path(sys.argv[1]), sys.argv[2]
text = path.read_text()
head = "".join(l for l in text.splitlines(True) if l.startswith("#"))
cfg = yaml.safe_load(text)
cfg.setdefault("agents", {}).setdefault("claude-code", {"grants": ["*"], "max_risk": "exec"})
cfg["doors"]["mcp"] = {"bind": mcp}
cfg["log"] = {"level": "info", "format": "text"}
path.write_text(head + yaml.safe_dump(cfg, sort_keys=False))
EOF
chmod 600 "$HUB_CFG"

"$PY" - "$WORKER_CFG" "$WORKER_NAME" "$plugins" "$WORKER_PLUGINS" <<'EOF'
import os, shutil, sys, yaml
from pathlib import Path
path, name, plugins, wanted = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), sys.argv[4].split()
cfg = yaml.safe_load(path.read_text()) if path.exists() else {}
cfg.setdefault("data", "./data")
cfg.setdefault("name", name)
cfg["plugins"] = [str(plugins / p) for p in wanted if (plugins / p / "plugin.yaml").is_file()]
claude = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
cfg.setdefault("settings", {}).setdefault("agents-claude-code", {"claude": claude})
if (plugins / "agents-codex" / "plugin.yaml").is_file():
    codex = shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
    cfg["settings"].setdefault("agents-codex", {"codex": codex})
if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"):
    cfg["hardware"] = sorted(set(cfg.get("hardware") or []) | {"display"})   # screen + notification ports
cfg["log"] = {"level": "info", "format": "text"}
path.write_text(yaml.safe_dump(cfg, sort_keys=False))
EOF

# -- start the hub, enroll the worker with a one-time code, start it ---------------
say "start: relay + hub"
systemctl --user start rook-castling-relay rook-castling-hub
wait_http "$LOCAL_URL/health" 40 || die "the hub didn't answer on $LOCAL_URL (journalctl --user -u rook-castling-hub)"
if ! grep -q '^anchor:' "$WORKER_CFG" 2>/dev/null; then
    say "worker: enroll $WORKER_NAME"
    qr=$(curl -sf --max-time 10 -H "Authorization: Bearer $(cat "$SECRETS/owner.token")" \
        -d '{}' "$LOCAL_URL/enroll/code" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["qr"])')
    "$PY" -m rook.core2.worker --config "$WORKER_CFG" enroll "$qr" --name "$WORKER_NAME"
fi
if [ ! -s "$SECRETS/mcp.token" ]; then
    "$PY" -m rook.core2.hub --config "$HUB_CFG" token --person "$OWNER" --agent claude-code \
        --label sidecar-mcp > "$SECRETS/mcp.token"
fi
# A Claude Code MCP config for the door (claude --mcp-config <it>); it holds the token.
"$PY" - "$SECRETS" "http://$MCP_BIND/mcp" <<'EOF'
import json, sys
from pathlib import Path
d, url = Path(sys.argv[1]), sys.argv[2]
token = (d / "mcp.token").read_text().strip()
(d / "claude-mcp.json").write_text(json.dumps({"mcpServers": {"rook05": {
    "type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}, indent=2) + "\n")
EOF
chmod 600 "$SECRETS"/*
say "start: worker"
systemctl --user start rook-castling-worker
say "done: $(cat "$SRC/.castling-sha" | cut -c1-8) installed"
cat <<EOF

  Dashboard:     http://$(ip -4 route get 1.1.1.1 2>/dev/null | grep -oP 'src \K\S+' || hostname):$HTTP_PORT/  (or $LOCAL_URL/ here)
  Sign-in token: $SECRETS/owner.token
  Recovery key:  $SECRETS/recovery-key.txt
  MCP door:      http://$MCP_BIND/mcp  (token in $SECRETS/mcp.token)
  Claude Code:   claude --mcp-config $SECRETS/claude-mcp.json
  Check it:      $SRC/deploy/castling-sidecar/check.sh
EOF
