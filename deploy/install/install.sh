#!/usr/bin/env bash
# Rook: install a hub on this computer and open it in your browser.
#
#   curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/c12/first-run/deploy/install/install.sh | bash
#   deploy/install/install.sh                 # from a checkout: installs that checkout's commit
#
# It asks what you use for AI now, sets that up and tests it (1 = Claude Code on
# this machine; the rest start on OpenCode's free model for now), installs the
# hub site, the band relay and a worker for this machine as systemd user
# units, and opens the site (no sign-in until setup is completed). The house agent
# takes it from there.
#
# Options (all optional; to run beside other installs give a new --dir,
# --name and ports):
#   --dir DIR          where it all lives (default ~/.rook)
#   --name NAME        unit name prefix: NAME-hub, NAME-relay, NAME-worker (default rook)
#   --http-port N      the hub site (default 8740)
#   --mcp-port N       the hub's MCP door for agents (default 8741)
#   --relay-port N     the band relay, UDP (default 7474)
#   --ai CHOICE        skip the question: none, claude, codex, gemini, api, local
#   --ref REF          the Rook branch or commit to install (curl installs only)
#   --no-boot          don't start it at login (units are written and started, not enabled)
#   --no-browser       print the sign-in link instead of opening a browser
#   --open             just open the site again (after setup: with a sign-in link)
#   --uninstall        stop and remove it (the folder is moved aside to DIR.removed-<time>, not deleted)
set -euo pipefail

# Everything is in main, so bash has read the whole script before any of it runs
# (under curl | bash its stdin is the script itself).
main() {

ROOT="$HOME/.rook" NAME=rook HTTP_PORT=8740 MCP_PORT=8741 RELAY_PORT=7474
AI="" REF="c12/first-run" BOOT=1 BROWSER_OPEN=1 ONLY_OPEN=0 UNINSTALL=0
while [ $# -gt 0 ]; do
    case $1 in
        --dir) ROOT=$2; shift 2 ;;
        --name) NAME=$2; shift 2 ;;
        --http-port) HTTP_PORT=$2; shift 2 ;;
        --mcp-port) MCP_PORT=$2; shift 2 ;;
        --relay-port) RELAY_PORT=$2; shift 2 ;;
        --ai) AI=$2; shift 2 ;;
        --ref) REF=$2; shift 2 ;;
        --no-boot) BOOT=0; shift ;;
        --no-browser) BROWSER_OPEN=0; shift ;;
        --open) ONLY_OPEN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,25p' "$0" 2>/dev/null || true; exit 0 ;;
        *) printf 'install.sh: unknown option %s\n' "$1" >&2; exit 2 ;;
    esac
done
ROOT=${ROOT%/}
case $NAME in *[!a-z0-9-]*|"") printf 'install.sh: --name is lowercase letters, digits and -\n' >&2; exit 2 ;; esac

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say() { printf '  %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31mRook install stopped:\033[0m %s\n' "$*" >&2; exit 1; }

SRC=$ROOT/src VENV=$ROOT/venv PY=$ROOT/venv/bin/python
HUB_CFG=$ROOT/hub/hub.yaml WORKER_CFG=$ROOT/worker/worker.yaml
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
URL="http://127.0.0.1:$HTTP_PORT"
OWNER=$(id -un | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_\n-' '_')
WORKER_NAME=$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' || echo this-machine)
LOG=$ROOT/install.log
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"

# While setup isn't completed the site needs no sign-in: the hub's address
# opens straight into the setup chat (Bake, Nov 6: "If configuration isn't
# completed assume we are still in setup mode"). After setup, --open gives a
# sign-in link (login-link; it keeps working).
open_site() {
    local link=$URL
    if "$PY" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("done") else 1)' \
        "$ROOT/hub/data/setup.json" 2>/dev/null; then
        link=$("$PY" -m rook.core2.hub --config "$HUB_CFG" login-link --url "$URL") || die "couldn't make a sign-in link"
    fi
    if [ $BROWSER_OPEN = 1 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } &&
        command -v xdg-open >/dev/null && xdg-open "$link" >/dev/null 2>&1; then
        bold "Rook is open in your browser."
        say "If it isn't, open $URL"
    else
        bold "Open this in a browser on this computer:"
        printf '\n  %s\n\n' "$link"
    fi
}

if [ $UNINSTALL = 1 ]; then
    for u in "$NAME-worker" "$NAME-hub" "$NAME-relay"; do
        systemctl --user disable --now "$u" >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/$u.service"
    done
    systemctl --user daemon-reload
    if [ -d "$ROOT" ]; then
        aside="$ROOT.removed-$(date +%Y%m%d-%H%M%S)"
        mv "$ROOT" "$aside"
        bold "Rook is stopped and removed; its folder is now $aside (delete it when you like)."
    else
        bold "Rook's units are removed; there was no $ROOT."
    fi
    exit 0
fi

if [ $ONLY_OPEN = 1 ]; then
    [ -f "$HUB_CFG" ] || die "nothing installed in $ROOT"
    open_site
    exit 0
fi

ask() {   # ask PROMPT DEFAULT: one line from the terminal (works under curl | bash)
    local answer=""
    if [ -r /dev/tty ] && { exec 3</dev/tty; } 2>/dev/null; then
        printf '%s' "$1" >/dev/tty
        IFS= read -r answer <&3 || true
        exec 3<&-
    fi
    printf '%s' "${answer:-$2}"
}

bold "Rook"
say "This sets up a Rook hub on this computer: a home for your AI assistant and your machines."
[ "$(uname -s)" = Linux ] || die "this installer is for Linux."
command -v systemctl >/dev/null || die "it needs systemd (systemctl) to keep Rook running."
command -v curl >/dev/null || die "it needs curl; install it with your package manager and run this again."
command -v git >/dev/null || die "it needs git; install it with your package manager and run this again."
[ -f "$HUB_CFG" ] && die "Rook is installed in $ROOT already. Open it with: $0 --open (or pick another --dir)."
for p in "$HTTP_PORT" "$MCP_PORT"; do
    if (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null; then die "port $p is in use; pick others with --http-port / --mcp-port"; fi
done
mkdir -p "$ROOT"
: > "$LOG"

# -- 1. What do you use for AI now? ----------------------------------------------
if [ -z "$AI" ]; then
    step "What do you use for AI now?"
    say "1) A Claude subscription (Claude Code)"
    say "2) ChatGPT / Codex"
    say "3) Gemini"
    say "4) An API key (Anthropic, OpenAI, ...)"
    say "5) A model running on my own computer"
    say "6) None of these: give me something free to start with"
    case $(ask "  Pick one [6]: " 6) in
        1) AI=claude ;; 2) AI=codex ;; 3) AI=gemini ;; 4) AI=api ;; 5) AI=local ;; *) AI=none ;;
    esac
fi
case $AI in
    none|claude) ;;
    codex|gemini|api|local)
        say "Setting Rook up with that is coming soon. For now you'll start with a free model"
        say "(no account needed); your house agent can switch it later."
        AI=none ;;
    *) die "--ai is one of none, claude, codex, gemini, api, local" ;;
esac

# -- 2. Set it up and test it -------------------------------------------------------
# claude = the Claude Code CLI on this machine, under the subscription it is
# signed in with (Bake, Nov 9: "start it in claude code mode"); none =
# OpenCode with a free model.
CLAUDE="" OPENCODE="" MODEL=""
if [ $AI = claude ]; then
step "Setting up your AI: Claude Code"
if ! command -v claude >/dev/null; then
    say "Installing Claude Code (claude.ai's installer) ..."
    curl -fsSL https://claude.ai/install.sh | bash >>"$LOG" 2>&1 || die "Claude Code didn't install (see $LOG)"
    command -v claude >/dev/null || die "Claude Code installed but isn't on PATH (see $LOG)"
fi
CLAUDE=$(command -v claude)
say "Claude Code $("$CLAUDE" --version </dev/null 2>/dev/null | head -1) at $CLAUDE"
signed_in() { "$CLAUDE" auth status </dev/null 2>>"$LOG" | grep '"loggedIn": *true' >/dev/null; }
for _ in 1 2 3; do
    signed_in && break
    say "Claude Code isn't signed in yet. In another terminal run:  claude"
    say "sign in with your Claude account there, then come back here."
    [ -r /dev/tty ] || die "Claude Code isn't signed in: run claude once to sign in, then run this again"
    ask "  Press Enter once you've signed in: " "" >/dev/null
done
signed_in || die "Claude Code still isn't signed in: run claude once to sign in, then run this again"
# One short turn the way Rook runs it: no MCP servers or settings of yours,
# permission mode auto.
say "Testing it ..."
probe=$ROOT/.probe
mkdir -p "$probe"
out=$(cd "$probe" && printf 'Say ok and nothing else.' | timeout 120 "$CLAUDE" -p --output-format json \
    --permission-mode auto --setting-sources project,local --strict-mcp-config 2>>"$LOG" || true)
rm -rf "$probe"
printf '%s\n' "$out" >>"$LOG"
reply=$(printf '%s' "$out" | grep -o '"result":"[^"]*"' | head -1 | sed 's/^"result":"\(.*\)"$/\1/' || true)
printf '%s' "$out" | grep '"is_error":false' >/dev/null && [ -n "$reply" ] ||
    die "Claude Code didn't answer a test prompt (see $LOG; run claude once to check it works)"
MODEL=opus
say "It works: Claude Code answered \"$reply\"."
else
step "Setting up your AI: OpenCode with a free model"
if ! command -v opencode >/dev/null; then
    say "Installing OpenCode (opencode.ai) ..."
    curl -fsSL https://opencode.ai/install | bash >>"$LOG" 2>&1 || die "OpenCode didn't install (see $LOG)"
    command -v opencode >/dev/null || die "OpenCode installed but isn't on PATH (see $LOG)"
fi
OPENCODE=$(command -v opencode)
say "OpenCode $("$OPENCODE" --version </dev/null 2>/dev/null | head -1) at $OPENCODE"
# Free models need no account. Test them with an empty config (as Rook runs
# them), best first, until one answers.
probe=$ROOT/.probe
mkdir -p "$probe/config/opencode"
echo '{"autoupdate": false, "share": "disabled"}' > "$probe/config/opencode/opencode.json"
models=$(cd "$probe" && XDG_CONFIG_HOME="$probe/config" OPENCODE_DISABLE_AUTOUPDATE=1 \
    timeout 120 "$OPENCODE" models </dev/null 2>>"$LOG" | grep '^opencode/' || true)
[ -n "$models" ] || die "OpenCode lists no free models (is this computer online? see $LOG)"
ordered=$( { for m in opencode/glm-5.3-flash-free opencode/deepseek-v4.1-flash-free opencode/mimo-v2.6-flash-free opencode/big-pickle; do
                 printf '%s\n' "$models" | grep -qx "$m" && echo "$m"; done
             printf '%s\n' "$models" | grep -- '-free$'
             printf '%s\n' "$models"; } | awk '!seen[$0]++' | head -8)
MODEL=""
for m in $ordered; do
    say "Testing $m ..."
    reply=$(cd "$probe" && XDG_CONFIG_HOME="$probe/config" OPENCODE_DISABLE_AUTOUPDATE=1 \
        timeout 60 "$OPENCODE" run --format json -m "$m" "Say ok and nothing else." </dev/null 2>>"$LOG" |
        grep -o '"type":"text","text":"[^"]*"' | tail -1 | sed 's/.*"text":"\(.*\)"/\1/' || true)
    if [ -n "$reply" ]; then MODEL=$m; break; fi
done
rm -rf "$probe"
[ -n "$MODEL" ] || die "none of OpenCode's free models answered (they may be busy; try again in a while; see $LOG)"
say "It works: $MODEL answered \"$reply\"."
fi

# -- 3. Rook itself -------------------------------------------------------------
step "Installing Rook in $ROOT"
if ! command -v uv >/dev/null; then
    say "Installing uv (the Python tool Rook installs with) ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh >>"$LOG" 2>&1 || die "uv didn't install (see $LOG)"
fi
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
rm -rf "$SRC.new"
mkdir -p "$SRC.new"
if [ -n "$here" ] && [ -f "$here/../../rook/core2/hub/__main__.py" ] && git -C "$here" rev-parse HEAD >/dev/null 2>&1; then
    sha=$(git -C "$here" rev-parse HEAD)
    say "Code: this checkout, commit ${sha:0:8}"
    git -C "$(git -C "$here" rev-parse --show-toplevel)" archive "$sha" | tar -x -C "$SRC.new"
else
    say "Code: Bake-Ware/rook $REF"
    curl -fsSL "https://codeload.github.com/Bake-Ware/rook/tar.gz/$REF" | tar -xz -C "$SRC.new" --strip-components=1 ||
        die "couldn't download Rook ($REF)"
    sha=$REF
fi
echo "$sha" > "$SRC.new/.rook-version"
rm -rf "$SRC"
mv "$SRC.new" "$SRC"
say "Python and Rook's packages (a minute or two the first time) ..."
UV_PYTHON_PREFERENCE=only-managed uv venv -q --python 3.12 "$VENV" >>"$LOG" 2>&1 || die "uv couldn't make Python 3.12 (see $LOG)"
uv pip install -q --python "$PY" -e "$SRC[manifest]" >>"$LOG" 2>&1 || die "Rook's packages didn't install (see $LOG)"

# -- the hub: config, keys, no password (the site asks for one) -------------------
# Everything listens on every interface (0.0.0.0; docs/castling-first-run.md
# "For the hardening pass"). Of init's output a new user sees only the
# recovery key (at the end); its developer lines (how to start the hub, add a
# machine) are for people running it by hand, and the API token stays in this
# script, for enrolling this machine (Bake's review, Nov 9).
mkdir -p "$ROOT/hub" "$ROOT/worker" "$UNIT_DIR"
initout=$("$PY" -m rook.core2.hub --config "$HUB_CFG" init --owner "$OWNER" --no-password \
    --relay "127.0.0.1:$RELAY_PORT" --url "$URL" --bind "0.0.0.0:$HTTP_PORT" </dev/null) ||
    die "the hub didn't set up (see $LOG)"
TOKEN=$(printf '%s\n' "$initout" | awk '/^API token/{getline; print $1; exit}')
RECOVERY=$(printf '%s\n' "$initout" | awk '/^Recovery key/{on=1; next} on && /^  [^ ]/{print $1; exit}')
initout=""
[ -n "$TOKEN" ] || die "the hub set up without a token for enrolling this machine"
say "Hub set up in $ROOT/hub (you sign in as $OWNER)."
plugins=$SRC/rook/plugins
HUB_PLUGINS="agents notify changes tasks projects knowledge concepts whiteboard"
if [ $AI = claude ]; then BACKEND=agents-claude-code; else BACKEND=agents-opencode; fi
WORKER_PLUGINS="shell file $BACKEND"
mkdir -p "$ROOT/hub/library"
for p in $HUB_PLUGINS $WORKER_PLUGINS; do
    [ -f "$plugins/$p/plugin.yaml" ] && ln -sfn "$plugins/$p" "$ROOT/hub/library/$p"
done
"$PY" - "$HUB_CFG" "$MCP_PORT" "$BACKEND" <<'EOF'
import sys, yaml
from pathlib import Path
path, mcp, backend = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
text = path.read_text()
head = "".join(l for l in text.splitlines(True) if l.startswith("#"))
cfg = yaml.safe_load(text)
cfg["doors"]["mcp"] = {"bind": f"0.0.0.0:{mcp}"}
cfg["contracts"] = {"agent.backend": f"{backend}.turn"}
cfg["log"] = {"level": "info", "format": "text"}
path.write_text(head + yaml.safe_dump(cfg, sort_keys=False))
EOF
"$PY" - "$ROOT" "$NAME" "$WORKER_NAME" "$HTTP_PORT" "$MCP_PORT" "$RELAY_PORT" "$MODEL" "$sha" "$AI" <<'EOF'
import json, os, sys, time
from pathlib import Path
root, name, worker, http, mcp, relay, model, sha, ai = sys.argv[1:]
path = Path(root) / "hub" / "data" / "setup.json"
state = json.loads(path.read_text()) if path.exists() else {}
state["install"] = {
    "by": "deploy/install/install.sh", "when": time.strftime("%Y-%m-%d %H:%M %Z"), "version": sha,
    "folder": root, "hub_config": f"{root}/hub/hub.yaml", "worker_config": f"{root}/worker/worker.yaml",
    "machine": worker, "units": [f"{name}-relay", f"{name}-hub", f"{name}-worker"],
    "http": f"0.0.0.0:{http} (every interface)", "mcp": f"0.0.0.0:{mcp}",
    "relay": f"0.0.0.0:{relay} (UDP, every interface)", "security": "off",
    "ai": ({"choice": "claude", "backend": "agents-claude-code.turn", "model": model,
            "note": "Claude Code on this machine, under the Claude subscription it is signed in with "
                    "(permission mode auto). Switch by installing another agents-* backend."}
           if ai == "claude" else
           {"choice": "none", "backend": "agents-opencode.turn", "model": model,
            "note": "OpenCode's free model: no account. Switch by installing another agents-* backend."})}
path.write_text(json.dumps(state, indent=1))
EOF
"$PY" - "$WORKER_CFG" "$WORKER_NAME" "$plugins" "$WORKER_PLUGINS" "$OPENCODE" "$MODEL" "$MCP_PORT" "$CLAUDE" <<'EOF'
import sys, yaml
from pathlib import Path
path, name, plugins, wanted, opencode, model, mcp, claude = sys.argv[1:]
plugins = Path(plugins)
door = f"http://127.0.0.1:{mcp}/mcp"
if claude:
    backend = {"agents-claude-code": {"claude": claude, "permission_mode": "auto", "door_url": door}}
else:
    backend = {"agents-opencode": {"opencode": opencode, "model": model, "door_url": door}}
cfg = {"data": "./data", "name": name,
       "plugins": [str(plugins / p) for p in wanted.split() if (plugins / p / "plugin.yaml").is_file()],
       "settings": backend,
       "log": {"level": "info", "format": "text"}}
Path(path).write_text(yaml.safe_dump(cfg, sort_keys=False))
EOF

# -- the units --------------------------------------------------------------------
cat > "$UNIT_DIR/$NAME-relay.service" <<EOF
[Unit]
Description=Rook: band relay (UDP 127.0.0.1:$RELAY_PORT)

[Service]
ExecStart=$PY $SRC/tests/e2e/relay.py $RELAY_PORT 0.0.0.0
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/$NAME-hub.service" <<EOF
[Unit]
Description=Rook: hub (site $URL)
Wants=$NAME-relay.service
After=$NAME-relay.service

[Service]
WorkingDirectory=$ROOT/hub
ExecStart=$PY -m rook.core2.hub --config $HUB_CFG
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/$NAME-worker.service" <<EOF
[Unit]
Description=Rook: worker $WORKER_NAME
Wants=$NAME-hub.service
After=$NAME-hub.service

[Service]
WorkingDirectory=$ROOT/worker
Environment=PATH=$(dirname "${CLAUDE:-$OPENCODE}"):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$PY -m rook.core2.worker --config $WORKER_CFG
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

step "Starting Rook"
systemctl --user start "$NAME-relay" "$NAME-hub"
for _ in $(seq 60); do curl -s -o /dev/null --max-time 2 "$URL/health" && break; sleep 1; done
curl -s -o /dev/null --max-time 2 "$URL/health" || die "the hub didn't start (journalctl --user -u $NAME-hub)"
say "The hub is running."
qr=$(curl -sf --max-time 10 -H "Authorization: Bearer $TOKEN" -d '{}' "$URL/enroll/code" |
    "$PY" -c 'import json,sys; print(json.load(sys.stdin)["qr"])') || die "the hub gave no enrollment code"
(cd "$ROOT/worker" && "$PY" -m rook.core2.worker --config "$WORKER_CFG" enroll "$qr" --name "$WORKER_NAME") </dev/null >>"$LOG" 2>&1 ||
    die "this machine didn't join the hub (see $LOG)"
systemctl --user start "$NAME-worker"
say "This machine ($WORKER_NAME) is joining the hub ..."
if [ $BOOT = 1 ]; then
    systemctl --user enable "$NAME-relay" "$NAME-hub" "$NAME-worker" >>"$LOG" 2>&1 || true
    say "Rook starts when you log in."
fi
# The worker reaches the hub at its first keepalive (~20 s), then announces
# its plugins; the house agent needs it serving agent.backend. Wait for the
# same "ready" the site waits on (page.site) before saying Done.
ready=0
for _ in $(seq 75); do
    if curl -s --max-time 5 -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
        -d '{"worker": "rook", "cap": "page.site", "args": {}, "why": "The installer waits until the house agent can answer."}' \
        "$URL/call" | "$PY" -c \
        "import json,sys; sys.exit(0 if (json.load(sys.stdin).get('result') or {}).get('ready') else 1)" 2>/dev/null
    then ready=1; break; fi
    sleep 2
done
if [ $ready = 1 ]; then
    say "This machine is on the hub and your house agent is ready."
else
    say "This machine hasn't reached the hub yet; it keeps trying (journalctl --user -u $NAME-worker). The site waits for it."
fi

if [ -n "$RECOVERY" ]; then
    step "Your recovery key"
    say "Keep it somewhere safe, away from this computer: it brings this hub back if this computer dies."
    printf '\n  %s\n' "$RECOVERY"
fi

step "Done"
open_site
say "Your house agent will walk you through the rest."
}

main "$@"
