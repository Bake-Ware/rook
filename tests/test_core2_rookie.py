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

from rook.core2.hub.doors.web import caps as web_caps
from rook.core2.hub.doors.web import site as web_site
from tests.test_core2_dashboard import WHY, Client, Hub, Loopback, run, site  # noqa: F401 - the fixture
from tests.test_core2_worksurface import bearer as ws_bearer, serve as ws_serve

STATIC = Path(web_caps.__file__).with_name("static")
SKILL = Path(__file__).resolve().parents[1] / "rook/plugins/agents/skills/first-run.md"


def test_rookie_js_is_vendored_and_its_styles_and_themes_are_read_from_it():
    text = (STATIC / "rookie.js").read_text()
    assert "window.Rookie" in text          # a drop-in: any rookie.js that defines Rookie
    styles, themes = web_caps.rookie_styles(), web_caps.rookie_themes()
    assert styles[0] == "pixel" and "none" in styles and len(styles) >= 9
    assert set(styles) == set(re.findall(r"\bSKINS\.([a-z0-9_]+)\s*=\s*\{", text)) | {"none"}
    assert themes[0] == "stone" and {"gameboy", "matrix", "rose"} <= set(themes)
    for page in ("shell.html", "account.html"):
        html = (STATIC / page).read_text()
        assert "rookie.js: Rookie" in html and '<script src="/web/static/rookie.js">' in html


def test_look_and_feel_saves_loads_and_belongs_to_each_person(site):
    async def go():
        hub, tokens = await site()
        try:
            bake, kim = Client(hub), Client(hub)
            await bake.sign_in(tokens["bake"])
            await kim.sign_in(tokens["kim"])
            look = (await bake.call("page.site"))["result"]["look"]
            assert (look["sound"], look["rookie"], look["theme"]) == (False, "pixel", "stone")
            assert look["next"] == "sound" and "kawaii" in look["styles"] and "gameboy" in look["themes"]
            out = await bake.call("page.look", {"sound": True, "rookie": "Kawaii", "theme": "gameboy"})
            assert out["ok"], out
            r = out["result"]
            assert (r["sound"], r["rookie"], r["theme"]) == (True, "kawaii", "gameboy")
            assert r["chosen"] == {"sound": True, "rookie": True, "theme": True, "name": True}   # no agents here
            assert r["next"] is None
            assert (await bake.call("page.site"))["result"]["look"]["theme"] == "gameboy"
            mine = (await kim.call("page.look"))["result"]                      # kim's own
            assert (mine["sound"], mine["rookie"], mine["next"]) == (False, "pixel", "sound")
            for bad, err in (({"rookie": "clippy"}, "no Rookie style"), ({"theme": "plaid"}, "no Rookie theme")):
                out = await bake.call("page.look", bad)
                assert not out["ok"] and err in out["error"]
            stored = json.loads((hub.data / "person-settings.json").read_text())
            assert stored["person:bake"]["page"] == {"sound": True, "rookie": "kawaii", "rookie_theme": "gameboy"}
            # the house agent sets a typed answer for the person it acts for
            call = {"worker": "rook", "cap": "page.look", "args": {"rookie": "none", "sound": False}, "why": WHY}
            status, out = await bake.json("POST", "/call", call, bearer=tokens["claude"])
            assert out["ok"] and out["result"]["rookie"] == "none" and out["result"]["sound"] is False
            status, out = await bake.json("POST", "/call", call, bearer=tokens["narrow"])
            assert not out["ok"]                                    # an agent alone has no look
        finally:
            await hub.stop()
        # and it loads again after a restart (the session survives it too)
        hub2 = Hub(hub.cfg, transport=Loopback())
        await hub2.start()
        try:
            bake.port = next(d.port for d in hub2.doors if d.name == "http")
            look = (await bake.call("page.site"))["result"]["look"]
            assert (look["sound"], look["rookie"], look["theme"]) == (False, "none", "gameboy")
        finally:
            await hub2.stop()
    run(go())


def test_a_fresh_hub_asks_sound_style_theme_name_first_and_the_name_is_the_house_agents(tmp_path):
    """A fresh hub (init writes setup.json {done: false}) with the agents
    package: setup isn't done and look.next walks sound, style, theme, name
    in that order; the name becomes the house agent's (its prompt)
    and survives a restart. The skill and the site's kickoff ask them first."""
    async def go(hub, tokens):
        (hub.data / "setup.json").write_text(json.dumps({"done": False}))
        c, kim = Client(hub), Client(hub)
        await c.sign_in(tokens["bake"])
        await kim.sign_in(tokens["kim"])
        s = (await c.call("page.site"))["result"]
        assert s["setup"] == {"done": False} and s["look"]["name"] == "Rookie"
        out = await ws_bearer(c, tokens["bake"], "hub.setup")
        assert out["result"]["done"] is False and out["result"]["look"]["next"] == "sound"
        order = []
        for step, value in (("sound", True), ("rookie", "comic"), ("theme", "amber"), ("name", "  Juniper  ")):
            order.append((await c.call("page.look"))["result"]["next"])
            out = await c.call("page.look", {step: value})
            assert out["ok"], out
        assert order == ["sound", "rookie", "theme", "name"] and out["result"]["next"] is None
        assert out["result"]["name"] == "Juniper"
        # a viewer can't rename the house agent
        assert not (await kim.call("page.look", {"name": "Bob"}))["ok"]
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
    assert "FIRST question" in first and "page.look" in first and "YOUR name" in first
    shell = (STATIC / "shell.js").read_text()
    kickoff = shell[shell.index("SETUP_KICKOFF + "):shell.index("true);", shell.index("SETUP_KICKOFF + "))]
    assert "in order: sound, Rookie's style, \" +\n        \"Rookie's theme, Rookie's name" in kickoff


def test_the_activity_feed_shows_the_dock_agents_calls_for_you_only(site, monkeypatch):
    monkeypatch.setattr(web_site, "dock_agent", lambda hub: "claude")

    async def go():
        hub, tokens = await site()
        try:
            bake, kim = Client(hub), Client(hub)
            await bake.sign_in(tokens["bake"])
            await kim.sign_in(tokens["kim"])
            seq = (await bake.json("GET", "/web/api/activity"))[1]["seq"]
            kseq = (await kim.json("GET", "/web/api/activity"))[1]["seq"]
            call = {"worker": "rook", "cap": "page.list", "args": {}, "why": "a secret why"}
            await bake.json("POST", "/call", call, bearer=tokens["claude"])          # agent claude for bake
            await bake.json("POST", "/call", call, bearer=tokens["bake"])            # bake alone: not shown
            status, out = await bake.json("GET", f"/web/api/activity?since={seq}")
            evs = out["events"]
            assert [(e["phase"], e["cap"]) for e in evs] == [("start", "page.list"), ("end", "page.list")]
            assert evs[1]["ok"] is True and evs[1]["decision"] == "allow" and evs[0]["call"] == evs[1]["call"]
            assert "a secret why" not in json.dumps(out) and "args" not in evs[0]
            next(d for d in hub.doors if d.name == "http").web.poll_secs = 0.2
            status, out = await kim.json("GET", f"/web/api/activity?since={kseq}")
            assert out["events"] == []                                               # not kim's
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
    r = subprocess.run([node, str(script), str(STATIC / "rookie.js"), str(STATIC / "rookie-map.js")],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {}


def test_the_map_table_is_in_the_docs():
    doc = (Path(__file__).resolve().parents[1] / "docs/castling-first-run.md").read_text()
    src = (STATIC / "rookie-map.js").read_text()
    exact = dict(re.findall(r'^\s+"([a-z.-]+)": "([a-z]+)",', src, re.M))
    for cap, state in exact.items():
        row = next((ln for ln in doc.splitlines() if f"`{cap}`" in ln), None)
        assert row is not None and f"`{state}`" in row, cap
