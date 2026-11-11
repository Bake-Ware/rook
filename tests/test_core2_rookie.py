"""Rookie, the house agent's avatar on the hub site (castling-first-run;
Bake, Nov 5 2026: "Yeah add it and translate for 0.5. Add the rookie style
as the first question it asks the user"): the per-person style setting, the
first setup question, the 0.5 event -> state map and the activity feed it
reads."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from rook.core2.hub.identity import Caller
from rook.kit.ctx import load_run
from tests.test_core2_dashboard import Client, Hub, Loopback, run, site  # noqa: F401 - the fixture
from tests.test_core2_worksurface import WHY, bearer as ws_bearer, serve as ws_serve, start as ws_start

AGENTS = Path(__file__).resolve().parents[1] / "rook/plugins/agents"
SHELL = AGENTS / "shell"
SKILL = AGENTS / "skills/first-run.md"


def test_rookie_js_is_vendored_and_its_styles_and_themes_are_read_from_it():
    text = (SHELL / "rookie.js").read_text()
    assert "window.Rookie" in text          # a drop-in: any rookie.js that defines Rookie
    styles, themes = load_run(AGENTS, "scripts/agents.py:rookie_js", {})()
    assert styles[0] == "pixel" and "none" in styles and len(styles) >= 9
    assert set(styles) == set(re.findall(r"\bSKINS\.([a-z0-9_]+)\s*=\s*\{", text)) | {"none"}
    assert themes[0] == "stone" and {"gameboy", "matrix", "rose"} <= set(themes)
    manifest = (AGENTS / "plugin.yaml").read_text()
    assert "rookie.js is Rookie by Bake" in manifest and "rookie: { file: shell/rookie.js }" in manifest


def test_look_and_feel_saves_loads_and_belongs_to_each_person(tmp_path):
    async def go():
        hub, tokens = await ws_start(tmp_path, ("agents",))
        try:
            bake, kim = Client(hub), Client(hub)
            await bake.sign_in(tokens["bake"])
            await kim.sign_in(tokens["kim"])
            look = (await bake.call("agent.site"))["result"]["look"]
            assert (look["sound"], look["rookie"], look["theme"]) == (False, "pixel", "stone")
            assert look["next"] == "sound" and "kawaii" in look["styles"] and "gameboy" in look["themes"]
            out = await bake.call("agent.look", {"sound": True, "rookie": "Kawaii", "theme": "gameboy"})
            assert out["ok"], out
            r = out["result"]
            assert (r["sound"], r["rookie"], r["theme"]) == (True, "kawaii", "gameboy")
            assert r["chosen"] == {"sound": True, "rookie": True, "theme": True, "name": False}
            assert r["next"] == "name"
            assert (await bake.call("agent.site"))["result"]["look"]["theme"] == "gameboy"
            mine = (await kim.call("agent.look"))["result"]                     # kim's own
            assert (mine["sound"], mine["rookie"], mine["next"]) == (False, "pixel", "sound")
            stored = json.loads((hub.data / "person-settings.json").read_text())
            assert stored["person:bake"]["agent"] == {"sound": True, "rookie": "kawaii", "rookie_theme": "gameboy"}
            # an agent sets a typed answer for the person it acts for
            house = hub.add_token(Caller(person="person:bake", agent="agent:house"))
            call = {"worker": "rook", "cap": "agent.look", "args": {"rookie": "none", "sound": False}, "why": WHY}
            status, out = await bake.json("POST", "/call", call, bearer=house)
            assert out["ok"] and out["result"]["rookie"] == "none" and out["result"]["sound"] is False, out
        finally:
            await hub.stop()
        # and it loads again after a restart (the session survives it too)
        hub2 = Hub(hub.cfg, transport=Loopback())
        await hub2.start()
        try:
            bake.port = next(d.port for d in hub2.doors if d.name == "http")
            look = (await bake.call("agent.site"))["result"]["look"]
            assert (look["sound"], look["rookie"], look["theme"]) == (False, "none", "gameboy")
        finally:
            await hub2.stop()
    run(go())


def test_a_fresh_hub_asks_sound_style_theme_name_first_and_the_name_is_the_house_agents(tmp_path):
    """A fresh hub the installer made, with the agents package: setup isn't done and look.next walks sound, style, theme, name
    in that order; the name becomes the house agent's (its prompt)
    and survives a restart. The skill and the site's kickoff ask them first."""
    async def go(hub, tokens):
        state = hub.data / "plugins" / "agent" / "setup.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"done": False}))         # the installer's
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        assert (await c.call("agent.site"))["result"]["setup"] == {"done": False}
        assert (await c.call("agent.site"))["result"]["look"]["name"] == "Rookie"
        out = await ws_bearer(c, tokens["bake"], "agent.look")
        assert out["result"]["next"] == "sound"
        order = []
        for step, value in (("sound", True), ("rookie", "comic"), ("theme", "amber"), ("name", "  Juniper  ")):
            order.append((await c.call("agent.look"))["result"]["next"])
            out = await c.call("agent.look", {step: value})
            assert out["ok"], out
        assert order == ["sound", "rookie", "theme", "name"] and out["result"]["next"] is None
        assert out["result"]["name"] == "Juniper"
        # the name is the house agent's: its prompt, the hub setting
        reply = (await c.call("agent.chat.send", {"agent": "house", "text": "who are you"}))["result"]["text"]
        assert reply.startswith('You are Juniper, the agent "house"')
        assert json.loads((hub.data / "settings.json").read_text())["agent"]["house_name"] == "Juniper"
    ws_serve(tmp_path, go, ("agents",))
    text = SKILL.read_text()
    steps = re.findall(r"^(\d+)\. \*\*(.+?)\*\*", text, re.M)
    assert [t for _, t in steps[:5]] == ["Sound.", "Rookie's style.", "Rookie's theme.", "Rookie's name.",
                                        "Their name and a password."]
    first = text[text.index("1. **Sound.**"):text.index("5. **")]
    assert "The site asks steps 1-4 itself" in first and "agent.look" in first and "YOUR name" in first
    # the site asks them, on the left, in this order; the agent hears once they're done
    shell = (SHELL / "dock.js").read_text()
    assert '["sound", "rookie", "theme", "name"].map(' in shell
    brief = shell[shell.index("function setupBrief("):shell.index("async function kickoff(")]
    assert "already picked the look and feel" in brief and "step 5" in brief


def test_the_chat_side_is_each_persons_and_the_agent_can_flip_it(tmp_path):
    """The site design, point 1: "The person can put the chat pane on the left
    or the right (per-person setting; the agent can flip it when asked)":
    agent.look side, right until set, not a first-run question."""
    async def go(hub, tokens):
        bake, kim = Client(hub), Client(hub)
        await bake.sign_in(tokens["bake"])
        await kim.sign_in(tokens["kim"])
        look = (await bake.call("agent.site"))["result"]["look"]
        assert look["side"] == "right" and look["next"] == "sound"
        out = await bake.call("agent.look", {"side": "left"})
        assert out["ok"] and out["result"]["side"] == "left" and out["result"]["next"] == "sound", out
        assert (await bake.call("agent.site"))["result"]["look"]["side"] == "left"
        assert (await kim.call("agent.site"))["result"]["look"]["side"] == "right"     # kim's own
        # the house agent flips it back for the person it acts for
        house = hub.add_token(Caller(person="person:bake", agent="agent:house"))
        out = await ws_bearer(bake, house, "agent.look", side="right")
        assert out["ok"] and out["result"]["side"] == "right", out
        # the generated Settings page shows it as a choice of two, under Agents
        rows = (await ws_bearer(bake, tokens["bake"], "setting.list", plugin="agent"))["result"]["settings"]
        row = next(r for r in rows if r["key"] == "chat_side")
        assert row["enum"] == ["left", "right"] and row["segment"] == "Agents" and row["value"] == "right"
    ws_serve(tmp_path, go, ("agents",))


def test_the_activity_feed_has_every_call_as_journaled(site):
    """The site's live feed Rookie reads: every call on the hub, start and
    end, with its args and why (Bake, Nov 6: "Remove all restrictions and
    gates"); the dock picks its agent's calls for the person who is here."""
    async def go():
        hub, tokens = await site()
        try:
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            seq = (await c.json("GET", "/web/api/activity"))[1]["seq"]
            call = {"worker": "rook", "cap": "page.list", "args": {"archived": True}, "why": "checking the pages"}
            await c.json("POST", "/call", call, bearer=tokens["claude"])
            status, out = await c.json("GET", f"/web/api/activity?since={seq}")
            mine = [e for e in out["events"] if e.get("why") == "checking the pages"]
            assert [e["phase"] for e in mine] == ["start", "end"]
            assert mine[0]["cap"] == "page.list" and mine[0]["args"] == {"archived": True}
            assert mine[0]["agent"] == "agent:claude" and mine[0]["person"] == "person:bake"
            assert mine[1]["ok"] is True and mine[0]["call"] == mine[1]["call"]
        finally:
            await hub.stop()
    run(go())


def _node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    return node


MAP_TEST = r"""
const fs = require("fs");
globalThis.window = {};
eval(fs.readFileSync(process.argv[2], "utf8"));             // rookie.js: window.Rookie
const STATES = window.Rookie.STATES;
const M = require(process.argv[3]);
const out = {};
const expect = (name, got, want) => { if (got !== want) out[name] = [got, want]; };
// cap families -> states
const caps = {
  "agent.backend": "thinking", "agents-opencode.turn": "thinking", "shell.exec": "exec", "shell.open": "exec", "file.read": "reading",
  "file.search": "searching", "file.write": "writing", "browser.goto": "websearch",
  "voice.speak": "talking", "voice.ask": "listening", "notify.send": "messaging",
  "telegram.send": "messaging", "core.elevate": "locking", "token.revoke": "locking",
  "core.plugin.install": "deploying", "backup-store.put": "uploading", "backup-store.pull": "downloading",
  "stream.read": "loading", "media.play": "music", "knowledge.create": "writing",
  "page.archive": "delete", "calendar.delete": "delete", "hub.workers": "reading",
  "github.checks": "testing", "appliance.test": "testing", "hid.click": "working", "huddle.post": "messaging",
};
for (const [cap, want] of Object.entries(caps)) expect(cap, M.capState(cap), want);
// every state the map can give is one rookie.js has
const src = fs.readFileSync(process.argv[3], "utf8");
for (const s of new Set([...src.matchAll(/"([a-z]+)"/g)].map((m) => m[1])))
  if (/^(thinking|messaging|reading|writing|spawning|loading|exec|talking|listening|locking|deploying|uploading|downloading|websearch|music|searching|delete|testing|working|confused|error|waiting|sleeping|idle)$/.test(s) && !STATES.includes(s))
    out["state " + s] = "not in rookie.js";
// the driver
let t = 0, seen = [], said = [];
const rk = { set: (s) => seen.push(s), say: (text, o) => said.push([text, o.then]) };   // like shell.js's rookieBoth
const d = new M.Driver(rk, () => t);
expect("start idle", d.tick(), "idle");
expect("typing", d.key(), "listening");
expect("sent", d.chat({ type: "input", data: { text: "hi" } }), "thinking");
expect("turn", d.activity({ phase: "start", call: "a", cap: "agent.backend" }), "thinking");
expect("shell", d.activity({ phase: "start", call: "b", cap: "shell.exec" }), "exec");
expect("shell done", d.activity({ phase: "end", call: "b", cap: "shell.exec", decision: "allow", ok: true }), "thinking");
expect("held", d.activity({ phase: "start", call: "c", cap: "file.write" }), "writing");
expect("held end", d.activity({ phase: "end", call: "c", cap: "file.write", decision: "held", hold: "h1" }), "thinking");
expect("denied", d.activity({ phase: "end", call: "x", cap: "secret.get", decision: "deny", ok: false }), "confused");
t += M.FLASH_MS + 1;
expect("after flash", d.tick(), "thinking");
expect("reply", d.chat({ type: "turn", data: { role: "reply", text: "Pick a style" } }), "talking");
expect("say then", said[0][1], "waiting");                    // the hold still waits on the person
t += 10000;
expect("hold waits", d.tick(), "waiting");
expect("released", d.activity({ phase: "end", call: "c", cap: "file.write", decision: "released", ok: true, hold: "h1" }), "idle");
expect("failed", d.activity({ phase: "end", call: "d", cap: "shell.exec", decision: "allow", ok: false }), "error");
expect("turn error", d.chat({ type: "status", data: { state: "error", error: "x" } }), "error");
t += M.FLASH_MS + 1;
expect("idle again", d.tick(), "idle");
t += M.SLEEP_MS;
expect("asleep", d.tick(), "sleeping");
expect("woken", d.key(), "listening");
console.log(JSON.stringify(out));
"""


def test_the_05_event_to_state_map(tmp_path):
    node = _node()
    script = tmp_path / "map_test.js"
    script.write_text(MAP_TEST)
    r = subprocess.run([node, str(script), str(SHELL / "rookie.js"), str(SHELL / "rookie-map.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {}


def test_the_map_table_is_in_the_docs():
    doc = (Path(__file__).resolve().parents[1] / "docs/castling-first-run.md").read_text()
    src = (SHELL / "rookie-map.js").read_text()
    exact = dict(re.findall(r'^\s+"([a-z.-]+)": "([a-z]+)",', src, re.M))
    for cap, state in exact.items():
        row = next((ln for ln in doc.splitlines() if ln.startswith("|") and f"`{cap}`" in ln), None)
        assert row is not None and f"`{state}`" in row, cap
