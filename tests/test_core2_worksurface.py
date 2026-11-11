"""The hub site as a work surface (castling-dashboard, castling-attention):
ready-made components arranged into layout pages, the start page, the house
agent's chat dock (with the agents package), workbench pages and page builds
as tasks (with the workbench), lifecycle caps called by a person, an agent
and a trigger, in all four install combinations (core, +agents, +workbench,
both). A hub runs in-process (loopback band); requests go through its HTTP
door. Every test runs under a hard timeout (``run``)."""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")

from test_core2_dashboard import BAND, WHY, Client, make_library, run  # noqa: E402
from rook.core2.hub.doors.web import caps as web_caps  # noqa: E402
from rook.core2.hub.doors.web.site import PAGE_CSP  # noqa: E402
from rook.core2.hub.identity import Caller  # noqa: E402
from rook.core2.hub.runtime import Config, Hub, Loopback  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PLUGINS = REPO / "rook" / "plugins"
BRAIN = REPO / "tests" / "fixtures" / "castling" / "house-brain"
WORKBENCH = ("tasks", "projects", "knowledge", "concepts", "whiteboard")
COMBOS = {"core": (), "agents": ("agents",), "workbench": ("workbench",), "both": ("agents", "workbench")}

# A trigger: "when the experiment ends, archive the page" (castling-dashboard).
LAB = """\
manifest: 1
plugin: lab
version: 1.0.0
doc: Runs experiments.
runs_on: hub
uses: [page.archive]
hooks:
  on: [ { event: lab.experiment.ended, call: page.archive, args: { page: page/run, reason: The experiment ended. } } ]
"""


def library(tmp_path: Path, packages=()) -> Path:
    lib = make_library(tmp_path)          # the board plugin: a page and caps
    if "agents" in packages:
        for folder in ("agents", "trust"):
            (lib / folder).symlink_to(PLUGINS / folder, target_is_directory=True)
        shutil.copytree(BRAIN, lib / "agents-local")
    if "workbench" in packages:
        for folder in WORKBENCH:
            (lib / folder).symlink_to(PLUGINS / folder, target_is_directory=True)
    (lib / "lab").mkdir()
    (lib / "lab" / "plugin.yaml").write_text(LAB)
    return lib


async def start(tmp_path: Path, packages=()):
    cfg = Config(data=tmp_path / "data", band=BAND, library=library(tmp_path, packages),
                 users={"bake": {"preset": "owner"}, "kim": {"preset": "viewer"}},
                 presets={"viewer": {"grants": ["page.*", "board.count", "agent.chat.*", "agent.site", "agent.look"],
                                     "max_risk": "write"}},
                 agents={"narrow": {"grants": ["page.*", "board.count"]}},
                 settings={"agent": {"warm": "300ms"}},
                 doors={"http": {"bind": "127.0.0.1:0"}})
    hub = Hub(cfg, transport=Loopback())
    await hub.start()
    tokens = {"bake": (cfg.data / "owner.token").read_text().strip(),
              "kim": hub.add_token(Caller(person="person:kim")),
              "narrow": hub.add_token(Caller(agent="agent:narrow"))}
    return hub, tokens


def serve(tmp_path: Path, body, packages=()) -> None:
    """Run ``body(hub, tokens)`` against a fresh hub, stopped whatever happens."""
    async def go():
        hub, tokens = await start(tmp_path, packages)
        try:
            await body(hub, tokens)
        finally:
            await hub.stop()
    run(go())


async def bearer(c: Client, token: str, cap: str, **args) -> dict:
    return (await c.json("POST", "/call", {"worker": "rook", "cap": cap, "args": args, "why": WHY}, bearer=token))[1]


async def tasks_of(c: Client, tokens: dict) -> list:
    return (await bearer(c, tokens["bake"], "task.list"))["result"]


async def soon(fn, seconds=10.0):
    """Await ``fn()`` until it gives something truthy (what an on: hook does after the call)."""
    deadline = time.monotonic() + seconds
    while not (got := await fn()):
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)
    return got


async def until(pred, seconds=10.0):
    deadline = time.monotonic() + seconds
    while True:
        got = pred()
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


# -- the four install combinations ------------------------------------------------------

@pytest.mark.parametrize("combo", COMBOS)
def test_every_install_combination(tmp_path, combo):
    packages = COMBOS[combo]
    agents, workbench = "agents" in packages, "workbench" in packages

    async def go(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        s = (await c.call("page.site"))["result"]
        assert s == {"person": "person:bake", "start": None}
        # the dock is the agents package's part of the shell: none in core, not even its files
        status, _, shell = await c.req("GET", "/")
        assert status == 200 and (b"/web/plugin/agent/dock" in shell) is agents
        if not agents:
            assert not re.search(rb"dock|rookie|chat", shell, re.I)
            assert (await c.req("GET", "/web/plugin/agent/dock"))[0] == 404
            static = Path(web_caps.__file__).with_name("static")
            for f in ("shell.js", "shell.css", "account.js", "account.html"):
                assert not re.search(r"dock|rookie|house|chat", (static / f).read_text(), re.I), f
        else:
            dock = (await c.call("agent.site"))["result"]
            assert dock["dock"] == "house" and dock["ready"] is True    # the fake brain serves agent.backend
            assert dock["look"]["next"] == "sound" and dock["look"]["name"] == "Rookie"
            assert dock["look"]["chosen"]["name"] is False
            assert dock["setup"] == {"done": True}       # guided setup only when the installer asks for it

        # every page in the sidebar, in its plugin's segment ("nothing is curated out"): the packages
        # add theirs (the agents package's Agents page and trust map, "it matters most to the agents")
        sidebar = (await c.call("page.list"))["result"]
        listed = {p["page"]: (s["segment"], p) for s in sidebar["segments"] for p in s["pages"]}
        work = {"task/tasks", "project/projects", "knowledge/knowledge"}
        assert set(listed) == {"hub/workers", "token/tokens", "board/board", "board/holds", "journal/journal",
                               "setting/settings", "secret/vault"} | \
            (work if workbench else set()) | ({"agent/agents", "trust/trust"} if agents else set())
        assert {pid: seg for pid, (seg, _) in listed.items()} == {
            pid: {"hub": "Machines", "board": "Garden", "task": "Work", "project": "Work", "knowledge": "Work",
                  "agent": "Agents", "trust": "Agents"}.get(pid.partition("/")[0], "Hub") for pid in listed}
        trust_caps = [cap for cap in hub.plugins.caps() if cap.startswith("trust.")]
        assert bool(trust_caps) is agents, trust_caps
        for pid in ("journal/journal", "setting/settings", "secret/vault"):
            assert (await c.open(pid))["ok"]

        comps = {x["component"] for x in (await c.call("page.components"))["result"]["components"]}
        assert {"page/text", "page/value", "hub/workers", "journal/recent"} <= comps
        assert ("task/list" in comps) == workbench

        # a page build: with the workbench it is a task linked to the page
        layout = [{"component": "hub/workers"}] + ([{"component": "task/list"}] if workbench else [])
        made = await bearer(c, tokens["bake"], "page.create", name="home", title="Home", layout=layout,
                            note="Bake wants the workers at a glance.")
        assert made["ok"] and made["result"] == {"page": "page/home"}, made     # tasks are not core
        if workbench:                  # the workbench hears page.created and makes the build task
            tasks = await soon(lambda: tasks_of(c, tokens))
            t = tasks[0]
            assert len(tasks) == 1 and t["title"] == "Page: Home" and t["state"] == "in_progress"
            assert t["assignee"] == "person:bake"
            assert [(x["kind"], x["ref"], x["relation"]) for x in t["links"]] == [("page", "page/home", "produced")]
            # every later change is noted on the build record
            assert (await bearer(c, tokens["bake"], "page.update", page="page/home", change="Narrowed it.",
                                 layout=[{"component": "hub/workers"}]))["ok"]

            async def noted():
                got = await bearer(c, tokens["bake"], "task.comment.list", where={"task": t["id"]})
                return [n["text"] for n in got["result"]]
            assert await soon(noted) == ["Changed the page page/home: Narrowed it."]

        # past page.*, the shell calls only what plugins' shell parts declare (the dock's chat)
        status, out = await c.json("POST", "/web/api/call", {"cap": "agent.chat.read", "args": {"agent": "house"}})
        assert (status, out.get("ok")) == ((200, True) if agents else (403, False)), out
        assert (await c.json("POST", "/web/api/call", {"cap": "agent.list", "args": {"agent": "house"}}))[0] == 403
    serve(tmp_path, go, packages)


# -- components and layout pages --------------------------------------------------------

def test_layout_pages_are_checked_and_arranged_in_the_sandbox(tmp_path):
    async def go(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        base = {"name": "glance", "title": "At a glance", "note": "x"}
        for bad, why in (({}, "html or a layout"), ({"html": "<p>", "layout": [{"component": "hub/workers"}]}, "html or"),
                         ({"layout": []}, "1 to 50"), ({"layout": [{"component": "nope/nope"}]}, "no component"),
                         ({"layout": [{"component": "page/text"}]}, "required"),
                         ({"layout": [{"component": "page/text", "props": {"text": 3}}]}, "props"),
                         ({"layout": [{"component": "hub/workers", "colour": "red"}]}, "unknown colour")):
            out = await bearer(c, tokens["bake"], "page.create", **base, **bad)
            assert why in out["error"], (bad, out)
        layout = [{"component": "page/text", "props": {"text": "Hello </script><b>there</b>"}, "title": "<i>Hi</i>"},
                  {"component": "hub/workers", "wide": True}, {"component": "hub/workers"}]
        assert (await bearer(c, tokens["narrow"], "page.create", **base, layout=layout, caps=["board.count"]))["ok"]

        page = (await c.call("page.get", {"page": "page/glance"}))["result"]
        assert page["caps"] == ["board.count", "hub.workers"]           # its own plus its components'
        assert page["layout"] == layout                                 # what an agent changes next
        opened = await c.open("page/glance")
        status, headers, body = await c.req("GET", opened["frame"])
        assert status == 200 and headers["Content-Security-Policy"] == PAGE_CSP
        doc = body.decode()
        assert doc.index("window.rook") < doc.index('<div class="layout">')
        assert doc.count('rook._as("hub/workers")') == 1               # one script per component
        assert '<h3>&lt;i&gt;Hi&lt;/i&gt;</h3>' in doc and 'class="card part wide"' in doc
        assert "</script><b>" not in doc and "\\u003c/script>\\u003cb>there" in doc   # props can't close the script
        assert doc.index('rook._as("page/text")') < doc.index("rook._mount(")

        # the sandbox scope: its components' caps, never more than its author (narrow)
        t = opened["token"]
        assert (await c.page(t, "hub.workers"))["error"] == "denied: agent: grants"
        assert (await c.page(t, "board.count"))["ok"]
        assert (await c.page(t, "board.secret"))["error"] == "denied: agent: grants"   # narrow is the caller now; the page layer refuses too
        # a layout written by the owner may call what its components call
        assert (await bearer(c, tokens["bake"], "page.create", **{**base, "name": "mine"},
                             layout=[{"component": "hub/workers"}]))["ok"]
        t = (await c.open("page/mine"))["token"]
        assert (await c.page(t, "hub.workers"))["ok"]
        assert (await c.page(t, "board.count"))["error"] == "denied: page: grants"
        # html replaces the layout
        assert (await bearer(c, tokens["bake"], "page.update", page="page/mine", change="Hand-written now.",
                             html="<p>mine</p>"))["ok"]
        status, _, body = await c.req("GET", (await c.open("page/mine"))["frame"])
        assert body.decode().endswith("<p>mine</p>")
        assert (await c.call("page.get", {"page": "page/mine"}))["result"]["caps"] == []
    serve(tmp_path, go)


def test_a_component_file_stays_inside_its_plugin(tmp_path):
    lib = tmp_path / "library"
    for name, file in (("sneak", "../../outside.js"), ("gone", "components/gone.js")):
        (lib / name / "components").mkdir(parents=True)
        (lib / name / "plugin.yaml").write_text(f"manifest: 1\nplugin: {name}\nversion: 1.0.0\ndoc: x\nruns_on: hub\n"
                                                f"components:\n  it: {{ file: {file} }}\n")
    (tmp_path / "outside.js").write_text("rook.component(() => {});")
    (lib / "gone" / "components" / "gone.js").write_text("rook.component(() => {});")

    async def go(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        comps = {x["component"] for x in (await c.call("page.components"))["result"]["components"]}
        assert "sneak/it" not in comps and "gone/it" in comps         # refused at load: its file is outside
        assert (await bearer(c, tokens["bake"], "page.create", name="s", title="S", note="x",
                             layout=[{"component": "gone/it"}, {"component": "page/text", "props": {"text": "hi"}}]))["ok"]
        (lib / "gone" / "components" / "gone.js").unlink()            # the plugin changed under the page
        status, _, body = await c.req("GET", (await c.open("page/s"))["frame"])
        assert status == 200 and "gone/it: its file is missing" in body.decode() and 'rook._as("page/text")' in body.decode()
    serve(tmp_path, go)


# -- the start page ------------------------------------------------------------------------

def test_start_page_per_person_and_the_hubs_own(tmp_path):
    async def go(hub, tokens):
        bake, kim = Client(hub), Client(hub)
        await bake.sign_in(tokens["bake"])
        await kim.sign_in(tokens["kim"])
        assert (await bake.call("page.start", {"page": "nope/nope"}))["error"] == "no page nope/nope"
        out = await bake.call("page.start", {"page": "board/board"})
        assert out["result"] == {"start": "board/board", "for": "person:bake"}
        assert (await kim.call("page.site"))["result"]["start"] is None
        # an agent or job acting alone sets the hub's own: everyone without one of their own
        assert (await bearer(bake, tokens["narrow"], "page.start", page="hub/workers"))["result"]["for"] == "*"
        assert (await kim.call("page.site"))["result"]["start"] == "hub/workers"
        assert (await bake.call("page.site"))["result"]["start"] == "board/board"
        # an archived start page doesn't count
        await bake.call("page.archive", {"page": "board/board"})
        assert (await bake.call("page.site"))["result"]["start"] == "hub/workers"
        await bake.call("page.restore", {"page": "board/board"})
        assert (await bake.call("page.start", {}))["result"] == {"start": "hub/workers", "for": "person:bake"}
        assert web_caps._read_json(hub.data / "pages.json")["start"] == {"*": "hub/workers"}
    serve(tmp_path, go)


# -- lifecycle: a person, an agent and a trigger ---------------------------------------------

def test_a_trigger_archives_a_page_when_the_experiment_ends(tmp_path):
    async def go(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])
        assert (await bearer(c, tokens["narrow"], "page.create", name="run", title="Run", html="<p>loss</p>",
                             note="Watch the training run; archive it when the experiment ends."))["ok"]
        hub._emit("lab.experiment.ended", {"run": 7}, "lab")
        await until(lambda: "page/run" in hub.pages.archived)
        assert hub.pages.archived["page/run"] == {**hub.pages.archived["page/run"],
                                                   "by": "system:plugin.lab", "reason": "The experiment ended."}
        assert (await c.call("page.restore", {"page": "page/run"}))["ok"]       # and a person brings it back
        assert (await c.open("page/run"))["ok"]
    serve(tmp_path, go)


# -- the house agent in the dock ----------------------------------------------------------------

def test_the_house_agent_builds_and_archives_a_page_from_the_dock(tmp_path):
    async def go(hub, tokens):
        c = Client(hub)
        await c.sign_in(tokens["bake"])

        async def say(text):
            out = await c.call("agent.chat.send", {"agent": "house", "text": text})
            assert out["ok"], out
            return out["result"]["text"]

        assert (await c.call("agent.chat.read", {"agent": "house"}))["result"]["events"] == []   # first run
        assert "How do you want to use the site?" in await say("[site] First visit to the hub site. Ask me.")
        reply = await say("[site] The person is on the page hub/workers (Workers).\nmake a page uptime")
        assert reply.startswith("Made page/uptime")
        s = (await c.call("page.site"))["result"]
        assert s["start"] == "page/uptime"
        page = (await c.call("page.get", {"page": "page/uptime"}))["result"]
        assert page["made_by"]["authors"] == [{"person": "person:bake", "agent": "agent:house"}]   # the agent, for bake
        assert "asked for a uptime page" in page["note"]
        task = (await soon(lambda: tasks_of(c, tokens)))[0]
        assert task["links"][0]["ref"] == "page/uptime" and task["state"] == "in_progress"
        assert set(page["caps"]) == {"hub.workers", "task.list"}
        t = (await c.open("page/uptime"))["token"]
        assert (await c.page(t, "task.list", {"limit": 5}))["ok"]

        assert "Archived page/uptime" in await say("[site] The person is on the page page/uptime.\n"
                                                     "archive page/uptime")
        assert "page/uptime" in hub.pages.archived
        assert (await c.call("page.site"))["result"]["start"] is None
        # the conversation is the person's own session with the house agent
        events = (await c.call("agent.chat.read", {"agent": "house", "limit": 100}))["result"]["events"]
        assert [e["data"]["text"].splitlines()[-1] for e in events if e["type"] == "input"][-1] == "archive page/uptime"
        kim = Client(hub)
        await kim.sign_in(tokens["kim"])
        assert (await kim.call("agent.chat.read", {"agent": "house"}))["result"]["events"] == []
    serve(tmp_path, go, ("agents", "workbench"))


# -- the front end ---------------------------------------------------------------------------------

def test_the_component_and_workbench_javascript_parses():
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    scratch = []
    files = sorted(web_caps.BUILTIN.glob("*/components/*.js")) + sorted(PLUGINS.glob("*/components/*.js")) + \
        sorted(PLUGINS.glob("*/shell/*.js"))
    pages = sorted(PLUGINS.glob("*/pages/*.html"))
    assert {p.stem for p in pages} >= {"tasks", "projects", "knowledge"}
    try:
        for page in pages:
            for i, script in enumerate(re.findall(r"<script>(.*?)</script>", page.read_text(), re.S)):
                f = page.parent / f".check-{page.stem}-{i}.js"
                f.write_text(script)
                scratch.append(f)
        for f in files + scratch:
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, f"{f}: {r.stderr}"
    finally:
        for f in scratch:
            f.unlink()


def test_workbench_manifests_with_pages_are_valid():
    from rook.kit.manifest import check
    for folder in ("tasks", "projects", "knowledge"):
        raw, issues = check(PLUGINS / folder)
        assert raw is not None and not [i for i in issues if i.level == "error"], [str(i) for i in issues]
        assert raw["pages"] and not [i for i in issues if i.code == "unknown-key"]


def test_the_docks_long_poll_fits_the_chat_read_timeout():
    from rook.kit._types import parse_duration
    from rook.kit.manifest import load
    read = load(PLUGINS / "agents").data["caps"]["agent.chat.read"]
    js = (PLUGINS / "agents" / "shell" / "dock.js").read_text()
    assert "await read(55)" in js and parse_duration(read["timeout"]) > read["args"]["wait"]["max"] >= 55
