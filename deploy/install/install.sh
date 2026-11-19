#!/usr/bin/env bash
# Rook: install a hub on this computer and open it in your browser.
#
#   curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/house-agent/deploy/install/install.sh | bash
#   deploy/install/install.sh                 # from a checkout: installs that checkout's commit
#
# It asks what you use for AI now and sets that up and tests it (Claude Code on
# this machine; the others are coming soon), installs the hub site and a worker
# for this machine as systemd user units, joins the worker to the hub, and opens
# the site. The house agent takes it from there.
#
# Options (all optional; to run beside other installs give a new --dir,
# --name and port):
#   --dir DIR          where it all lives (default ~/.rook)
#   --name NAME        unit name prefix: NAME-hub, NAME-worker (default rook)
#   --http-port N      the hub: its site, its MCP door and its workers' links (default 8740)
#   --ai CHOICE        skip the question: claude, codex, gemini, api, local, none
#   --ref REF          the Rook branch or commit to install (curl installs only)
#   --no-boot          don't start it at login (units are written and started, not enabled)
#   --no-browser       print the site's address instead of opening a browser
#   --open             just open the site again
#   --uninstall        stop it and remove what this installer made: its units and the folder
set -euo pipefail

# Everything is in main, so bash has read the whole script before any of it runs
# (under curl | bash its stdin is the script itself).
main() {

# REF: what a curl install fetches. It is the house-agent branch until there is a tag
# to point it at; this line is the one place to change it.
ROOT="$HOME/.rook" NAME=rook HTTP_PORT=8740
AI="" REF="house-agent" BOOT=1 BROWSER_OPEN=1 ONLY_OPEN=0 UNINSTALL=0
while [ $# -gt 0 ]; do
    case $1 in
        --dir) ROOT=$2; shift 2 ;;
        --name) NAME=$2; shift 2 ;;
        --http-port) HTTP_PORT=$2; shift 2 ;;
        --ai) AI=$2; shift 2 ;;
        --ref) REF=$2; shift 2 ;;
        --no-boot) BOOT=0; shift ;;
        --no-browser) BROWSER_OPEN=0; shift ;;
        --open) ONLY_OPEN=1; shift ;;
        --uninstall) UNINSTALL=1; shift ;;
        -h|--help) sed -n '2,22p' "$0" 2>/dev/null || true; exit 0 ;;
        *) printf 'install.sh: unknown option %s\n' "$1" >&2; exit 2 ;;
    esac
done
ROOT=${ROOT%/}
case $NAME in *[!a-z0-9-]*|"") printf 'install.sh: --name is lowercase letters, digits and -\n' >&2; exit 2 ;; esac
case $HTTP_PORT in *[!0-9]*|"") printf 'install.sh: --http-port is a port number\n' >&2; exit 2 ;; esac
case $ROOT in /*) ;; *) printf 'install.sh: --dir is a full path, such as %s/.rook\n' "$HOME" >&2; exit 2 ;; esac

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
say() { printf '  %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31mRook install stopped:\033[0m %s\n' "$*" >&2; exit 1; }

SRC=$ROOT/src VENV=$ROOT/venv PY=$ROOT/venv/bin/python
HUB_DATA=$ROOT/hub VAULT_KEY=$ROOT/vault.key WORKER_STATE=$ROOT/worker
FACTS=$ROOT/install.json LOG=$ROOT/install.log
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
URL="http://127.0.0.1:$HTTP_PORT"
MARK="# Made by Rook's installer (deploy/install/install.sh) for $ROOT; --uninstall removes it."
export PATH="$HOME/.local/bin:$PATH"

open_site() {
    if [ $BROWSER_OPEN = 1 ] && { [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; } &&
        command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1; then
        bold "Rook is open in your browser."
        say "If it isn't, open $URL"
    else
        bold "Open this in a browser on this computer:"
        printf '\n  %s\n\n' "$URL"
    fi
}

if [ $UNINSTALL = 1 ]; then
    # Exactly what this installer made: unit files carrying its mark for this folder, and
    # the folder, which it made whole (its install.json says so).
    if [ -d "$ROOT" ] && [ ! -f "$FACTS" ]; then
        die "$ROOT was not made by this installer (it has no install.json); nothing was removed."
    fi
    removed=0
    for u in "$NAME-worker" "$NAME-hub"; do
        f=$UNIT_DIR/$u.service
        if [ -f "$f" ] && grep -qxF "$MARK" "$f"; then
            systemctl --user disable --now "$u" >/dev/null 2>&1 || true
            rm -f "$f"
            removed=1
        fi
    done
    [ $removed = 1 ] && systemctl --user daemon-reload
    if [ -d "$ROOT" ]; then
        rm -rf "$ROOT"
        bold "Rook is stopped and removed: its units and $ROOT are gone."
    else
        bold "Rook's units are removed; there was no $ROOT."
    fi
    exit 0
fi

if [ $ONLY_OPEN = 1 ]; then
    [ -f "$FACTS" ] || die "nothing installed in $ROOT"
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
[ -f "$FACTS" ] && die "Rook is installed in $ROOT already. Open it with: $0 --open (or pick another --dir)."
[ -e "$ROOT" ] && [ -n "$(ls -A "$ROOT" 2>/dev/null)" ] && die "$ROOT is not empty; pick another --dir."
for u in "$NAME-hub" "$NAME-worker"; do
    [ -e "$UNIT_DIR/$u.service" ] && die "a unit called $u is there already; pick another --name."
done
if (exec 3<>"/dev/tcp/127.0.0.1/$HTTP_PORT") 2>/dev/null; then die "port $HTTP_PORT is in use; pick another with --http-port"; fi
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
    say "6) None of these"
    case $(ask "  Pick one [1]: " 1) in
        2) AI=codex ;; 3) AI=gemini ;; 4) AI=api ;; 5) AI=local ;; 6) AI=none ;; *) AI=claude ;;
    esac
fi
case $AI in
    claude) ;;
    none|codex|gemini|api|local)
        say "Setting Rook up with that is coming soon. For now Rook installs without an AI;"
        say "your house agent starts once Claude Code is set up on this computer."
        AI=none ;;
    *) die "--ai is one of claude, codex, gemini, api, local, none" ;;
esac

# -- 2. Set it up and test it -------------------------------------------------------
# Claude Code on this machine, under the subscription it is signed in with
# (Bake, Nov 9: "start it in claude code mode").
CLAUDE="" CLAUDE_INSTALLED=false
if [ "$AI" = claude ]; then
step "Setting up your AI: Claude Code"
if ! command -v claude >/dev/null; then
    say "Installing Claude Code (claude.ai's installer) ..."
    curl -fsSL https://claude.ai/install.sh | bash >>"$LOG" 2>&1 || die "Claude Code didn't install (see $LOG)"
    command -v claude >/dev/null || die "Claude Code installed but isn't on PATH (see $LOG)"
    CLAUDE_INSTALLED=true
fi
CLAUDE=$(command -v claude)
say "Claude Code $("$CLAUDE" --version </dev/null 2>/dev/null | head -1) at $CLAUDE"
signed_in() { "$CLAUDE" auth status </dev/null 2>>"$LOG" | grep '"loggedIn": *true' >/dev/null; }
until signed_in; do
    say "Claude Code isn't signed in yet. In another terminal run:  claude"
    say "sign in with your Claude account there, then come back here."
    [ -r /dev/tty ] || die "Claude Code isn't signed in: run claude once to sign in, then run this again"
    ask "  Press Enter once you've signed in: " "" >/dev/null
done
# One short turn the way Rook runs it: no MCP servers or settings of yours,
# permission mode auto. It takes as long as it takes.
say "Testing it ..."
probe=$ROOT/.probe
mkdir -p "$probe"
out=$(cd "$probe" && printf 'Say ok and nothing else.' | "$CLAUDE" -p --output-format json \
    --permission-mode auto --setting-sources project,local --strict-mcp-config 2>>"$LOG" || true)
rm -rf "$probe"
printf '%s\n' "$out" >>"$LOG"
reply=$(printf '%s' "$out" | grep -o '"result":"[^"]*"' | head -1 | sed 's/^"result":"\(.*\)"$/\1/' || true)
printf '%s' "$out" | grep '"is_error":false' >/dev/null && [ -n "$reply" ] ||
    die "Claude Code didn't answer a test prompt (see $LOG; run claude once to check it works)"
say "It works: Claude Code answered \"$reply\"."
fi

# -- 3. Rook itself -------------------------------------------------------------
step "Installing Rook in $ROOT"
UV_INSTALLED=false
if ! command -v uv >/dev/null; then
    say "Installing uv (the Python tool Rook installs with) ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh >>"$LOG" 2>&1 || die "uv didn't install (see $LOG)"
    command -v uv >/dev/null || die "uv installed but isn't on PATH (see $LOG)"
    UV_INSTALLED=true
fi
here=$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)
mkdir -p "$SRC"
if [ -n "$here" ] && [ -f "$here/../../hub/rook_hub/__main__.py" ] && git -C "$here" rev-parse HEAD >/dev/null 2>&1; then
    sha=$(git -C "$here" rev-parse HEAD)
    say "Code: this checkout, commit ${sha:0:8}"
    git -C "$(git -C "$here" rev-parse --show-toplevel)" archive "$sha" | tar -x -C "$SRC"
else
    say "Code: Bake-Ware/rook $REF"
    curl -fsSL "https://codeload.github.com/Bake-Ware/rook/tar.gz/$REF" | tar -xz -C "$SRC" --strip-components=1 ||
        die "couldn't download Rook ($REF)"
    sha=$REF
fi
echo "$sha" > "$SRC/.rook-version"
say "Python and Rook's packages (a minute or two the first time) ..."
UV_PYTHON_PREFERENCE=only-managed uv venv -q --python 3.12 "$VENV" >>"$LOG" 2>&1 || die "uv couldn't make Python 3.12 (see $LOG)"
uv pip install -q --python "$PY" -r "$SRC/requirements.txt" >>"$LOG" 2>&1 || die "Rook's packages didn't install (see $LOG)"
HUB_PATH=$SRC/protocol/python:$SRC/hub WORKER_PATH=$SRC/protocol/python:$SRC/core/python

# -- the hub: its data, its vault key, and a join token for this machine -----------
# It listens on every interface (0.0.0.0), as 0.5's install did. Open until claimed
# when the hub can start that way (--no-password, brief HA3); before that, its site
# asks the first visitor for a name and password.
NO_PASSWORD=""
if PYTHONPATH=$HUB_PATH "$PY" -m rook_hub --help 2>/dev/null | grep -q -- '--no-password'; then NO_PASSWORD=--no-password; fi
CODE=$(PYTHONPATH=$HUB_PATH "$PY" -m rook_hub join-token --data "$HUB_DATA" --vault-key "$VAULT_KEY" 2>>"$LOG" </dev/null) ||
    die "the hub didn't set up (see $LOG)"
[ -n "$CODE" ] || die "the hub set up without a join token for this machine"
say "Hub set up in $HUB_DATA."
WORKER_NAME=$(hostname -s 2>/dev/null || hostname)

# The install facts, for the house agent (setup) and for --open and --uninstall.
"$PY" - "$FACTS" "$sha" "$ROOT" "$NAME" "$HTTP_PORT" "$WORKER_NAME" "$AI" "$CLAUDE" \
    "$CLAUDE_INSTALLED" "$UV_INSTALLED" "${NO_PASSWORD:-}" <<'EOF'
import json, sys, time
path, sha, root, name, http, worker, ai, claude, claude_installed, uv_installed, no_password = sys.argv[1:]
facts = {
    "by": "deploy/install/install.sh", "when": time.strftime("%Y-%m-%d %H:%M %Z"), "version": sha,
    "folder": root, "hub_data": f"{root}/hub", "vault_key": f"{root}/vault.key", "worker_state": f"{root}/worker",
    "machine": worker, "units": [f"{name}-hub", f"{name}-worker"],
    "http": f"0.0.0.0:{http} (every interface)", "open_until_claimed": bool(no_password),
    "ai": ({"choice": "claude", "engine": "claude-code", "claude": claude, "permission_mode": "auto",
            "note": "Claude Code on this machine, under the Claude subscription it is signed in with."}
           if ai == "claude" else {"choice": "none", "note": "No AI yet: the others are coming soon."}),
    "installed_tools": [t for t, made in (("claude", claude_installed), ("uv", uv_installed)) if made == "true"],
}
with open(path, "w") as f:
    json.dump(facts, f, indent=1)
EOF

# -- the units --------------------------------------------------------------------
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/$NAME-hub.service" <<EOF
$MARK
[Unit]
Description=Rook: hub (site $URL)

[Service]
WorkingDirectory=$ROOT
Environment=PYTHONPATH=$HUB_PATH
ExecStart=$PY -m rook_hub --data $HUB_DATA --vault-key $VAULT_KEY --host 0.0.0.0 --port $HTTP_PORT${NO_PASSWORD:+ $NO_PASSWORD}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
cat > "$UNIT_DIR/$NAME-worker.service" <<EOF
$MARK
[Unit]
Description=Rook: worker $WORKER_NAME
Wants=$NAME-hub.service
After=$NAME-hub.service

[Service]
WorkingDirectory=$ROOT
Environment=PYTHONPATH=$WORKER_PATH
Environment=PATH=$(dirname "${CLAUDE:-$PY}"):$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$PY -m rook_worker $URL --state $WORKER_STATE
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload

step "Starting Rook"
systemctl --user start "$NAME-hub"
until curl -s -o /dev/null "$URL/"; do
    systemctl --user is-failed --quiet "$NAME-hub" && die "the hub didn't start (journalctl --user -u $NAME-hub)"
    sleep 1
done
say "The hub is running."
# This machine joins with the join token: once, in the foreground, until the hub has
# approved it; then its unit runs it from the state it kept.
joined=$ROOT/.join.out
PYTHONPATH=$WORKER_PATH "$PY" -m rook_worker "$URL" --state "$WORKER_STATE" --code "$CODE" </dev/null >"$joined" 2>&1 &
join=$!
CODE=""
until grep -q '^Approved as ' "$joined"; do
    if ! kill -0 "$join" 2>/dev/null; then
        cat "$joined" >>"$LOG"
        die "this machine didn't join the hub: $(tail -1 "$joined") (see $LOG)"
    fi
    sleep 1
done
kill -TERM "$join" 2>/dev/null || true
wait "$join" 2>/dev/null || true
cat "$joined" >>"$LOG"
rm -f "$joined"
since=$(date +%s)
systemctl --user start "$NAME-worker"
say "This machine ($WORKER_NAME) is joining the hub ..."
if [ $BOOT = 1 ]; then
    systemctl --user enable "$NAME-hub" "$NAME-worker" >>"$LOG" 2>&1 || true
    say "Rook starts when you log in."
fi
# The worker that carries the house agent's engine: wait until it is linked to the hub.
until journalctl --user -u "$NAME-worker" --since "@$since" -o cat --no-pager 2>/dev/null | grep -q '^Linked to '; do
    systemctl --user is-failed --quiet "$NAME-worker" && die "this machine's worker stopped (journalctl --user -u $NAME-worker)"
    sleep 1
done
if [ "$AI" = claude ]; then
    say "This machine is on the hub, and your house agent's engine (Claude Code) runs on it."
else
    say "This machine is on the hub."
fi

step "Done"
open_site
if [ -n "$NO_PASSWORD" ]; then
    say "Your house agent will walk you through the rest."
else
    say "Choose your name and password there; that makes you this hub's owner."
fi
}

main "$@"
