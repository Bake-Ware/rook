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

STATIC = Path(web_caps.__file__).with_name("static")
SKILL = Path(__file__).resolve().parents[1] / "rook/plugins/agents/skills/first-run.md"


def test_rookie_js_is_vendored_and_its_styles_are_read_from_it():
    text = (STATIC / "rookie.js").read_text()
    assert "window.Rookie" in text          # a drop-in: any rookie.js that defines Rookie
    styles = web_caps.rookie_styles()
    assert styles[0] == "pixel" and "none" in styles and len(styles) >= 9
    assert set(styles) == set(re.findall(r"\bSKINS\.([a-z0-9_]+)\s*=\s*\{", text)) | {"none"}
    for page in ("shell.html", "account.html"):
        html = (STATIC / page).read_text()
        assert "rookie.js: Rookie" in html and '<script src="/web/static/rookie.js">' in html


def test_the_style_saves_loads_and_belongs_to_each_person(site):
    async def go():
        hub, tokens = await site()
        try:
            bake, kim = Client(hub), Client(hub)
            await bake.sign_in(tokens["bake"])
            await kim.sign_in(tokens["kim"])
            look = (await bake.call("page.site"))["result"]["look"]
            assert look["rookie"] == "pixel" and look["chosen"] is False and "kawaii" in look["styles"]
            out = await bake.call("page.look", {"rookie": "Kawaii"})
            assert out["ok"] and out["result"]["rookie"] == "kawaii" and out["result"]["chosen"] is True
            assert (await bake.call("page.site"))["result"]["look"]["rookie"] == "kawaii"
            assert (await kim.call("page.look"))["result"] == {**look, "chosen": False}   # kim's own
            bad = await bake.call("page.look", {"rookie": "clippy"})
            assert not bad["ok"] and "no Rookie style" in bad["error"]
            # kept as the per-person setting page.rookie: the Settings page shows it
            stored = json.loads((hub.data / "person-settings.json").read_text())
            assert stored["person:bake"]["page"]["rookie"] == "kawaii"
            # the house agent sets a typed answer for the person it acts for
            call = {"worker": "rook", "cap": "page.look", "args": {"rookie": "none"}, "why": WHY}
            status, out = await bake.json("POST", "/call", call, bearer=tokens["claude"])
            assert out["ok"] and out["result"]["rookie"] == "none"
            status, out = await bake.json("POST", "/call", call, bearer=tokens["narrow"])
            assert not out["ok"]                                    # an agent alone has no look
        finally:
            await hub.stop()
        # and it loads again after a restart (the session survives it too)
        hub2 = Hub(hub.cfg, transport=Loopback())
        await hub2.start()
        try:
            bake.port = next(d.port for d in hub2.doors if d.name == "http")
            assert (await bake.call("page.site"))["result"]["look"] == {
                "rookie": "none", "chosen": True, "styles": web_caps.rookie_styles()}
        finally:
            await hub2.stop()
    run(go())


def test_a_fresh_hub_asks_look_and_feel_first(site):
    """On a fresh hub (init writes setup.json {done: false}) setup isn't
    done and no style is chosen; hub.setup tells the house agent so, and its
    first-run skill and the site's kickoff make the style the first question."""
    async def go():
        hub, tokens = await site()
        try:
            (hub.data / "setup.json").write_text(json.dumps({"done": False}))
            c = Client(hub)
            await c.sign_in(tokens["bake"])
            s = (await c.call("page.site"))["result"]
            assert s["setup"] == {"done": False} and s["look"]["chosen"] is False
            call = {"worker": "rook", "cap": "hub.setup", "args": {}, "why": WHY}
            status, out = await c.json("POST", "/call", call, bearer=tokens["claude"])
            assert out["result"]["done"] is False and out["result"]["look"]["chosen"] is False
        finally:
            await hub.stop()
    run(go())
    text = SKILL.read_text()
    steps = re.findall(r"^(\d+)\. \*\*(.+?)\*\*", text, re.M)
    assert steps[0] == ("1", "Look and feel.") and steps[1][1].startswith("Their name")
    first = text[text.index("1. **Look and feel.**"):text.index("2. **")]
    assert "FIRST question" in first and "page.look" in first and "Rookie" in first
    shell = (STATIC / "shell.js").read_text()
    kickoff = shell[shell.index("SETUP_KICKOFF + "):shell.index("true);", shell.index("SETUP_KICKOFF + "))]
    assert "first question is step 1, look and feel" in kickoff


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
const rk = { set: (s) => seen.push(s), say: (text, o) => said.push([text, o.then]) };
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
