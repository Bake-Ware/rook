"""The guided first run (Bake, Nov 9 2026): two panes - the work pane the
house agent drives and the chat with Rookie - clickable choices, the vault
for anything secret, one live Rookie that travels, the network and
other-machines steps in plain words, and the 0.4 brand."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from rook.core2.hub.doors.web import caps as web_caps
from rook.core2.hub.doors.web.site import SHELL_CSP
from tests.test_core2_dashboard import Client, run, site  # noqa: F401 - the fixture
from tests.test_core2_worksurface import bearer as ws_bearer, serve as ws_serve

STATIC = Path(web_caps.__file__).with_name("static")
REPO = Path(__file__).resolve().parents[1]
SHELL = REPO / "rook/plugins/agents/shell"
SKILL = REPO / "rook/plugins/agents/skills/first-run.md"


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    return node


PARSE = r"""
const W = require(process.argv[2]);
console.log(JSON.stringify(W.parse(process.argv[3])));
"""


def parse(tmp_path, text):
    script = tmp_path / "parse.js"
    script.write_text(PARSE)
    r = subprocess.run([_node(), str(script), str(SHELL / "chat-widgets.js"), text],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_the_agent_can_put_choices_and_work_pane_lines_in_a_message(tmp_path):
    text = ("How do you want to open this site?\n"
            "[[choices: Just this computer | Other devices at home | From anywhere]]\n"
            "[[open: vault | new=host_password | for=sudo on box]]\n"
            "[[open: page hub/workers]]\n"
            "  [[point: vault.value]]  \n"
            "Type it on the left.\n"
            "[[nonsense: x]]")
    assert parse(tmp_path, text) == [
        {"kind": "text", "text": "How do you want to open this site?"},
        {"kind": "choices", "items": ["Just this computer", "Other devices at home", "From anywhere"]},
        {"kind": "open", "view": "vault", "arg": "", "args": {"new": "host_password", "for": "sudo on box"}},
        {"kind": "open", "view": "page", "arg": "hub/workers", "args": {}},
        {"kind": "point", "target": "vault.value", "say": ""},
        {"kind": "text", "text": "Type it on the left.\n[[nonsense: x]]"},     # unknown lines stay words
    ]
    # agents don't always give a block its own line: it counts anywhere
    assert parse(tmp_path, "Which style? [[choices: Pixel | Voxel]] or type one.") == [
        {"kind": "text", "text": "Which style?"}, {"kind": "choices", "items": ["Pixel", "Voxel"]},
        {"kind": "text", "text": "or type one."}]
    assert parse(tmp_path, "empty [[choices: ]] stays") == [{"kind": "text", "text": "empty [[choices: ]] stays"}]


def test_one_live_rookie_per_page():
    """Bake, Nov 9: "only one instance of it on any page at a time". The shell
    mounts exactly one (the chat's, which travels); the tab icon copies his
    canvas and the pickers draw still thumbnails (Rookie.bake); the account
    page mounts none."""
    shell = (SHELL / "dock.js").read_text()
    assert len(re.findall(r"\bRookie\.mount\(", shell)) == 1
    assert "Rookie.bake(" in shell
    for f in [*STATIC.glob("*.js"), *SHELL.glob("*.js")]:
        if f.name == "rookie.js":
            continue
        code = f.read_text()
        assert not re.search(r"\bRookie\.favicon\(", code), f.name              # it mounts a hidden second one
        if f.name != "dock.js":
            assert not re.search(r"\bRookie\.mount\(", code), f.name
    # the one Rookie travels: a smooth path both ways, never placed at a new spot without one
    travel = shell[shell.index("function travel("):shell.index("function point(")]
    assert "transition" in travel and "cubic-bezier" in travel
    point = shell[shell.index("function point("):shell.index("\n}\n", shell.index("function point("))]
    assert point.count("travel(") == 2                                          # there and back


def test_the_vault_view_saves_secrets_and_lists_only_names(site):
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            out = (await c.json("POST", "/web/api/secret", {"name": "host_password", "value": "hunter2",
                                                            "description": "sudo on box"}))[1]
            assert out == {"ok": True, "name": "host_password"}
            assert hub.vault.get("host_password") == "hunter2"
            listed = (await c.json("GET", "/web/api/secrets"))[1]
            assert [s["name"] for s in listed["secrets"]] == ["host_password"]
            assert "hunter2" not in json.dumps(listed)                         # names, never values
        finally:
            await hub.stop()
    run(go())


def test_the_first_run_skill_guides_and_makes_the_site_reachable():
    text = SKILL.read_text()
    guide = text[text.index("## How you guide"):text.index("## When you need root")]
    for line in ("[[choices:", "[[open:", "[[point:", "account.name", "vault.value", "network.home"):
        assert line in guide
    root = text[text.index("## When you need root"):text.index("1. **Sound.**")]
    assert "Never ask for a password in the chat" in root
    assert "new=host_password" in root and '"sudo", "-S"' in root and "stdin" in root
    assert "{{" not in text                         # a vault reference here would be filled in
    net = text[text.index("7. **Reaching this site.**"):text.index("8. **Other machines.**")]
    assert "[[choices: Just this computer | Other devices at home | From anywhere]]" in net
    assert "Directive" in net and "actually reachable" in net
    for tool in ("ufw", "firewall-cmd", "nft", "iptables"):
        assert tool in net
    assert "relay" in net and "udp" in net.lower() and "not localhost" in net and "/health" in net
    machines = text[text.index("8. **Other machines.**"):text.index("9. **")]
    assert machines.index("Rook worker") < machines.index("[[choices:")        # explain first, then ask
    assert "install.sh" in machines and "Android" in machines and "[[open: page hub/workers]]" in machines
    account = text[text.index("5. **Their name and a password.**"):text.index("6. **")]
    assert "[[open: account]]" in account


def test_the_site_wears_the_04_brand():
    css = (STATIC / "shell.css").read_text()
    for token in ("--bg: #101310", "--accent: #d8ad6d", "--good: #a4bc92", "'DM Sans'", "'IBM Plex Mono'"):
        assert token in css
    # the fonts are the hub's own (static/fonts), for the shell's pages and, inlined, every page
    fonts = (STATIC / "fonts.css").read_text()
    assert "'DM Sans'" in fonts and "'IBM Plex Mono'" in fonts
    for name in re.findall(r"/web/static/fonts/([a-z0-9-]+\.woff2)", fonts):
        assert (STATIC / "fonts" / name).read_bytes()[:4] == b"wOF2", name
    for page in ("shell.html", "account.html", "login.html", "public.html"):
        html = (STATIC / page).read_text()
        assert '/web/static/fonts.css' in html and "googleapis" not in html, page
        if page != "public.html":
            assert 'class="mark"' in html
    assert '<section id="work"' in (SHELL / "dock.js").read_text()            # the work pane


# -- right after an install: the worker isn't there yet ---------------------------------

def test_agent_site_says_when_the_house_agent_can_answer(tmp_path):
    """No provider of agent.backend served yet (the worker joins a few seconds
    after an install): ready is false and the site waits ("Getting ready...")."""
    async def agents(hub, tokens):                # the fake brain serves agent.backend on the hub
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        assert (await c.call("agent.site"))["result"]["ready"] is True
        await hub.plugins.unload("agents-local")   # its provider gone: nothing serves agent.backend
        assert (await c.call("agent.site"))["result"]["ready"] is False
    ws_serve(tmp_path / "agents", agents, ("agents",))


def test_every_turn_carries_the_setup_skill_until_setup_is_done(tmp_path):
    async def go(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])

        async def ask(text):
            out = await c.call("agent.chat.send", {"agent": "house", "text": text})
            assert out["ok"], out
            return out["result"]["text"]
        state = hub.data / "plugins" / "agent" / "setup.json"      # the agents plugin's own data
        state.parent.mkdir(parents=True, exist_ok=True)
        assert await ask("are we in setup") == "setup brief: no"           # none: done (an existing hub)
        state.write_text(json.dumps({"done": False}))                      # what the installer writes
        assert await ask("are we in setup") == "setup brief: yes"
        assert await ask("are we in setup") == "setup brief: yes"          # every turn, not just the first
        state.write_text(json.dumps({"done": True}))
        assert await ask("are we in setup") == "setup brief: no"
    ws_serve(tmp_path, go, ("agents",))


def test_agent_setup_keeps_the_state_and_the_install_facts(tmp_path):
    """Guided setup is the agents package's: agent.setup reads and sets it,
    in the plugin's own data, where the installer leaves its facts."""
    async def go(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        state = hub.data / "plugins" / "agent" / "setup.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        assert (await c.call("agent.site"))["result"]["setup"] == {"done": True}    # none: done
        state.write_text(json.dumps({"done": False, "install": {"machine": "box"}}))   # the installer's
        assert (await c.call("agent.site"))["result"]["setup"] == {"done": False}   # no install facts here
        out = await ws_bearer(c, tokens["bake"], "agent.setup")
        assert out["result"] == {"done": False, "install": {"machine": "box"}}
        out = await ws_bearer(c, tokens["bake"], "agent.setup", done=True)
        assert out["ok"] and out["result"] == {"done": True, "install": {"machine": "box"}}
        assert (await c.call("agent.site"))["result"]["setup"] == {"done": True}
    ws_serve(tmp_path, go, ("agents",))


def test_guided_setup_does_not_keep_the_site_open(tmp_path):
    """castling-design, Agents: after the hub is claimed "the site asks for
    sign-in even while guided setup continues". The same core rule as a hub
    without the agents package (test_core2_dashboard)."""
    from rook.core2.hub import passwords

    async def go(hub, tokens):
        (hub.data / passwords.UNCLAIMED).touch()             # init --no-password
        state = hub.data / "plugins" / "agent" / "setup.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"done": False}))         # the installer's
        c = Client(hub)
        status, headers, _ = await c.req("GET", "/")
        assert status == 200 and "HttpOnly" in headers["Set-Cookie"]
        c.cookie = headers["Set-Cookie"].split(";")[0]
        me = (await c.json("GET", "/web/api/me"))[1]
        assert me["person"] == "person:bake"
        c.csrf = me["csrf"]
        assert (await c.call("agent.site"))["result"]["setup"] == {"done": False}
        assert (await c.json("POST", "/web/api/password", {"password": "mine"}))[1]["ok"]
        assert (await c.call("agent.site"))["result"]["setup"] == {"done": False}  # setup carries on
        fresh = Client(hub)
        status, headers, _ = await fresh.req("GET", "/")
        assert status == 303 and headers["Location"] == "/login" and "Set-Cookie" not in headers
        assert (await fresh.req("GET", "/login"))[0] == 200
        assert (await fresh.req("POST", "/login", {"user": "bake", "password": "mine"}))[0] == 200
    ws_serve(tmp_path, go, ("agents",))


def test_the_site_waits_for_the_backend_and_never_shows_not_ready_as_a_message():
    shell = (SHELL / "dock.js").read_text()
    kick = shell[shell.index("async function kickoff()"):shell.index("async function startDock(")]
    assert kick.index("await untilReady()") < kick.index("await say(setupBrief(")
    assert "Getting ready" in shell[shell.index("async function untilReady()"):]
    nr = re.search(r"const NOT_READY = /(.+)/i;", shell)[1].replace("\\.", ".")
    assert re.search(nr, "no single worker serves agents-opencode.turn", re.I)
    err = shell[shell.index('ev.type === "status" && d.state === "error"'):]
    err = err[:err.index('bubble("bad"')]
    assert "NOT_READY.test" in err and "return;" in err and "kickoff()" in err
    start = shell[shell.index("async function startDock("):]
    assert "if (!dock.replies)" in start                 # the agent never got to answer: kick off again


def test_the_installer_waits_for_the_house_agent_before_done():
    sh = (REPO / "deploy/install/install.sh").read_text()
    wait = sh[sh.index("ready=0"):sh.index('step "Done"')]
    assert '"cap": "agent.site"' in wait and "get('ready')" in wait


# -- Claude Code mode (Bake, Nov 9: "start it in claude code mode") -------------------

def _claude_step(tmp_path, auth: str, reply: str):
    """Run the installer's Claude Code step with a stand-in claude on PATH."""
    sh = (REPO / "deploy/install/install.sh").read_text()
    step = sh[sh.index("if [ $AI = claude ]; then"):sh.index('step "Setting up your AI: OpenCode')]
    step = step[:step.rindex("else")] + "fi\n"
    helpers = sh[sh.index("bold() {"):sh.index("SRC=$ROOT/src")] + sh[sh.index("ask() {"):sh.index('bold "Rook"')]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text("#!/bin/sh\n"
                    'case "$1" in\n'
                    "  --version) echo '9.9.9 (Claude Code)' ;;\n"
                    f"  auth) echo '{{\"loggedIn\": {auth}}}' ;;\n"
                    f"  *) echo \"$*\" > \"$ROOK_ARGS\"; cat > /dev/null; echo '{reply}' ;;\n"
                    "esac\n")
    fake.chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text("set -euo pipefail\nAI=claude ROOT=$1 LOG=$1/install.log\n" + helpers + step +
                      'echo "CLAUDE=$CLAUDE MODEL=$MODEL"\n')
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "ROOK_ARGS": str(tmp_path / "args")}
    root = tmp_path / "root"
    root.mkdir()
    return subprocess.run(["setsid", "bash", str(script), str(root)], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=60), tmp_path / "args"


def test_the_installer_sets_up_claude_code_and_tests_it(tmp_path):
    ok = '{"type":"result","subtype":"success","is_error":false,"result":"ok","session_id":"s1"}'
    proc, args = _claude_step(tmp_path, "true", ok)
    assert proc.returncode == 0, proc.stderr
    assert 'Claude Code answered "ok"' in proc.stdout and "MODEL=opus" in proc.stdout
    assert str(tmp_path / "bin" / "claude") in proc.stdout
    sent = args.read_text()                # the test turn runs the way Rook runs it
    assert "-p --output-format json --permission-mode auto" in sent and "--strict-mcp-config" in sent


def test_the_installer_asks_for_a_claude_sign_in(tmp_path):
    proc, _ = _claude_step(tmp_path, "false", "{}")
    assert proc.returncode != 0
    assert "In another terminal run:  claude" in proc.stdout
    assert "run claude once to sign in" in proc.stderr


def test_the_installer_stops_when_claude_code_does_not_answer(tmp_path):
    bad = '{"type":"result","subtype":"success","is_error":true,"result":"Invalid API key"}'
    proc, _ = _claude_step(tmp_path, "true", bad)
    assert proc.returncode != 0 and "didn't answer a test prompt" in proc.stderr


def test_claude_mode_configures_the_worker_and_the_hub():
    sh = (REPO / "deploy/install/install.sh").read_text()
    assert "none|claude) ;;" in sh                   # option 1 is no longer "coming soon"
    assert 'if [ -n "$CLAUDE" ]; then BACKEND=agents-claude-code; elif [ -n "$OPENCODE" ]; then BACKEND=agents-opencode; fi' in sh
    assert "contracts" not in sh                     # hub.yaml names no engine: the agents plugin's setting does
    assert 'stored.setdefault("agent", {})["backend"] = f"{backend}.turn"' in sh
    assert ('{"agents-claude-code": {"claude": claude, "permission_mode": "auto", "door_url": door}}' in sh
            and 'door = f"http://127.0.0.1:{mcp}/mcp"' in sh)
    assert "curl -fsSL https://claude.ai/install.sh | bash" in sh


# -- the level (docs/castling-design.md: "The installer's first question becomes which level you want") --

def _level_plugins(level: str) -> set:
    """The installer's own list for a level, run against this checkout's plugins."""
    sh = (REPO / "deploy/install/install.sh").read_text()
    start = sh.index("HUB_PLUGINS=$(\"$PY\" - \"$plugins\" \"$LEVEL\" <<'EOF'\n")
    code = sh[sh.index("\n", start) + 1:sh.index("\nEOF\n", start)]
    out = subprocess.run([sys.executable, "-c", code, str(REPO / "rook/plugins"), level],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return set(out.stdout.split())


def test_each_level_links_its_packages_plugins_from_their_manifests():
    package = {}
    for m in (REPO / "rook/plugins").glob("*/plugin.yaml"):
        package[m.parent.name] = yaml.safe_load(m.read_text()).get("package")
    minimal, assistant, team = (_level_plugins(level) for level in ("minimal", "assistant", "team"))
    assert minimal == {p for p, pkg in package.items() if pkg in ("core", "devices")} and {"notify", "jobs"} <= minimal
    # devices at every level: a worker's caps that run on the hub exist only if its library has them
    assert {"battery", "adb", "camera", "torrent", "hid-desktop", "screen-desktop"} <= minimal
    assert assistant - minimal == {p for p, pkg in package.items() if pkg == "agents"}
    assert {"agents", "trust", "agents-claude-code", "agents-opencode"} <= assistant
    assert team - assistant == {p for p, pkg in package.items() if pkg == "workbench"}
    assert {"tasks", "projects", "knowledge", "concepts", "whiteboard", "changes"} <= team
    # this machine's worker runs shell and file, as before
    sh = (REPO / "deploy/install/install.sh").read_text()
    assert 'WORKER_PLUGINS="shell file $BACKEND"' in sh


def _level_step(tmp_path, *args):
    sh = (REPO / "deploy/install/install.sh").read_text()
    step = sh[sh.index("# -- 1. Which level?"):sh.index("# -- 2. What do you use for AI now?")]
    helpers = sh[sh.index("bold() {"):sh.index("SRC=$ROOT/src")] + sh[sh.index("ask() {"):sh.index('bold "Rook"')]
    script = tmp_path / "level.sh"
    script.write_text("set -euo pipefail\nLEVEL=${1:-}\n" + helpers + step + ":\nfi\necho LEVEL=$LEVEL\n")
    return subprocess.run(["setsid", "bash", str(script), *args], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=30)


def test_the_first_question_is_the_level_and_minimal_asks_no_engine(tmp_path):
    asked = _level_step(tmp_path)              # no terminal: the default, today's install
    assert asked.returncode == 0 and "LEVEL=team" in asked.stdout, asked.stderr
    assert "LEVEL=minimal" in _level_step(tmp_path, "minimal").stdout
    bad = _level_step(tmp_path, "huge")
    assert bad.returncode != 0 and "--level is one of minimal, assistant, team" in bad.stderr
    sh = (REPO / "deploy/install/install.sh").read_text()
    assert sh.index("Which level do you want?") < sh.index("What do you use for AI now?")
    engine = sh[sh.index("if [ $LEVEL != minimal ]; then"):sh.index("# -- 4. Rook itself")]
    assert "What do you use for AI now?" in engine and "Setting up your AI: OpenCode" in engine
