"""The agents package core (rook/plugins/agents): the runtime driven through
its caps and hooks with a real Ctx, a fake agent.backend behind the ctx's
dispatcher, the plugin's own SQLite and the hub's one stream store
(ctx.streams) in tmp."""

import asyncio
import json

import pytest

from port_helpers import CONTRACTS, PLUGINS, go
from rook.kit.ctx import Caller, Ctx, KitError, invoke, load_run
from rook.kit.manifest import check, load
from rook.streams import SqliteStore, Streams

AGENTS = PLUGINS / "agents"
ROLES = ["archivist", "house", "lead", "pm", "producer", "qa", "security", "writer"]
OWN = ["house", "security"]                     # the agents package's own; the rest ship with the workbench
WORKBENCH = {"task": "tasks", "knowledge": "knowledge", "concept": "concepts"}    # plugin -> folder
BAKE = Caller(person="person:bake", band="b1", why="talk to my agents", owner=True)
GUEST = Caller(person="person:guest", band="b1", why="say hi")
PM = Caller(agent="agent:pm", band="b1", why="pm keeps notes")


class Backend:
    """A fake agent.backend: echoes the last prompt line, keeps a session
    id per conversation, answers dream prompts with ``dream``."""

    def __init__(self):
        self.calls = []
        self.installed = dict(WORKBENCH)   # what hub.manifests shows: plugin -> folder in PLUGINS
        self.pages = {}                    # knowledge.get: slug -> page
        self.fail = None
        self.hold = None                   # an asyncio.Event a turn waits on
        self.dream = {"add": [], "keep": []}

    async def __call__(self, cap, args, caller):
        if cap == "hub.manifests":         # the hub's builtin, as it answers the plugin itself
            out = []
            for plugin, folder in sorted(self.installed.items()):
                m = load(PLUGINS / folder, CONTRACTS).data
                if m.get(args["section"]):
                    out.append({"plugin": plugin, "version": m["version"], args["section"]: m[args["section"]],
                                "folder": str(PLUGINS / folder)})
            return {"manifests": out}
        if cap == "knowledge.get":
            if args["id"] not in self.pages:
                raise KitError(f"no page {args['id']}")
            return self.pages[args["id"]]
        self.calls.append((cap, args, caller))
        if self.hold is not None:
            await self.hold.wait()
        if self.fail:
            raise KitError(self.fail)
        if _dreaming(args):
            text = self.dream if isinstance(self.dream, str) else \
                "Here you go:\n```json\n" + json.dumps(self.dream) + "\n```"
            return {"text": text, "session": "d"}
        return {"text": "echo " + args["prompt"].splitlines()[-1],
                "session": args.get("session") or f"h{len(self.calls)}", "usage": {"input_tokens": 3}}

    def turns(self):
        return [(cap, a, c) for cap, a, c in self.calls if not _dreaming(a)]

    def dreams(self):
        return [(cap, a, c) for cap, a, c in self.calls if _dreaming(a)]


def _dreaming(args):
    return "curate an agent's memory" in args.get("system", "")


class Rig:
    def __init__(self, tmp_path, **settings):
        self.manifest = load(AGENTS, CONTRACTS).data
        self.data = tmp_path / "agents"
        self.settings = {"warm": "10s", **settings}
        self.backend = Backend()
        self.modules = {}
        self.streams = Streams(SqliteStore(str(tmp_path / "stream-events.db")), host="hub")   # the hub's

    def ctx(self, caller, cap=None):
        return Ctx(self.manifest, caller, dispatch=self.backend, settings=self.settings,
                   data_dir=self.data, cap=cap, streams=self.streams)

    async def call(self, fn, caller=BAKE, **args):
        func = load_run(AGENTS, f"scripts/agents.py:{fn}", self.modules)
        return await invoke(func, self.ctx(caller, fn.replace("_", ".")), args)

    async def start(self):
        await self.call("start", Caller(plugin="agents", why="agents hook: start"))

    async def stop(self):
        await self.call("stop", Caller(plugin="agents", why="agents hook: stop"))
        self.modules = {}                  # the next start loads the code afresh, like a reload

    @property
    def rt(self):
        return next(iter(self.modules.values()))._RT

    async def events(self, agent, caller=BAKE, **args):
        return [(e["type"], e["data"]) for e in (await self.call("chat_read", caller, agent=agent, **args))["events"]]


async def until(pred, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def run(rig, body, timeout=20):
    async def main():
        await rig.start()
        try:
            return await body()
        finally:
            await rig.stop()
    return go(main, timeout)


# -- content ------------------------------------------------------------------------

@pytest.mark.parametrize("folder", ["agents", "agents-claude-code"])
def test_manifests_are_clean(folder):
    data, issues = check(PLUGINS / folder, CONTRACTS)
    assert data is not None and [str(i) for i in issues] == []
    assert (PLUGINS / folder / "docs" / "manual.md").is_file()


def test_every_role_has_a_prompt_and_its_skills():
    """The agents package ships the house agent and security triage; the
    workbench's plugins ship its team roles (castling-dashboard). Every
    persona's skills exist in some installed manifest."""
    found, skills = {}, {}
    for folder in ["agents", *WORKBENCH.values()]:
        m = load(PLUGINS / folder, CONTRACTS).data
        for name, p in (m.get("personas") or {}).items():
            assert name not in found, name
            found[name] = p
            assert (PLUGINS / folder / p["prompt"]).read_text().strip(), name
        for name, rel in (m.get("skills") or {}).items():
            text = (PLUGINS / folder / rel).read_text()
            assert text.startswith("---\ndescription: ") and "\nwhen: " in text, name
            skills[name] = folder
    assert sorted(found) == ROLES
    assert sorted(load(AGENTS, CONTRACTS).data["personas"]) == OWN
    for name, p in found.items():
        assert set(p["skills"]) <= set(skills), name
        assert "consult" in p["skills"] and "notepad" in p["skills"], name
    assert {n for n, p in found.items() if p.get("always_on")} == {"house", "security"}
    assert {skills[s] for s in ("ticket-sweep", "handoff", "verify", "wiki-sweep")} == {"tasks", "knowledge"}


def test_core_and_agents_alone_get_only_their_own_roles(tmp_path):
    """Without the workbench there is no task deck to sweep, so no PM."""
    rig = Rig(tmp_path)
    rig.backend.installed = {}

    async def body():
        assert [a["name"] for a in await rig.call("list_agents")] == OWN
        with pytest.raises(KitError, match="no persona pm"):
            await rig.call("create", name="x", persona="pm")
        assert "- consult: Pull in another agent" in rig.rt.system_prompt(rig.rt.agent("house"))
        # The workbench installed later: its roles appear on the next look.
        rig.backend.installed = dict(WORKBENCH)
        await rig.rt.ready(force=True)
        assert [a["name"] for a in await rig.call("list_agents")] == ROLES
        assert rig.rt.agent("producer")["origin"] == "concept@0.5.0"
    run(rig, body)


# -- roles, create, update, archive -----------------------------------------------------

def test_install_makes_the_ready_made_roles_and_edits_win(rig):
    async def body():
        listed = await rig.call("list_agents")
        assert [a["name"] for a in listed] == ROLES
        assert {a["name"] for a in listed if a["always_on"]} == {"house", "security"}
        pm = await rig.call("get", name="pm")
        assert pm["origin"] == "task@0.5.0" and "keep the work moving" in pm["prompt"]
        await rig.call("update", name="house", summary="Mine now")
        # pm from an older release, unedited: the next start refreshes it.
        a = rig.rt.agent("pm")
        rig.rt.save({**a, "prompt": "old", "origin": "task@0.4.0"})
        await rig.stop()
        await rig.start()
        assert (await rig.call("get", name="house"))["summary"] == "Mine now"
        assert "keep the work moving" in (await rig.call("get", name="pm"))["prompt"]
        assert [a["name"] for a in await rig.call("list_agents")] == ROLES     # nothing made twice
    run(rig, body)


def test_create_update_archive(rig):
    async def body():
        made = await rig.call("create", name="re", persona="lead", title="Reverse engineer",
                              backend="claude-code", model={"two": "sonnet"})
        assert made == {"name": "re", "title": "Reverse engineer", "summary": rig.rt.agent("lead")["summary"],
                        "persona": "lead", "backend": "claude-code", "always_on": False,
                        "archived": None, "awake": False}
        assert rig.rt.agent("re")["creator"] == "person:bake"
        for args, error in (({"name": "re"}, "already exists"), ({"name": "x", "persona": "nope"}, "no persona")):
            with pytest.raises(KitError, match=error):
                await rig.call("create", **args)
        with pytest.raises(KitError, match="keeps its persona"):
            await rig.call("update", name="re", persona="qa")
        out = await rig.call("chat_send", agent="re", text="what is this binary?")
        assert out["text"] == "echo what is this binary?"
        cap, args, _ = rig.backend.turns()[-1]
        assert (cap, args["model"]) == ("agents-claude-code.turn", "sonnet")
        sid = out["stream"]
        assert rig.rt.streams.get(sid).attrs["harness"]
        await rig.call("update", name="re", backend="default")
        assert rig.rt.streams.get(sid).attrs["harness"] is None      # another harness can't resume it
        assert (await rig.call("archive", name="re"))["archived"]
        assert "re" not in [a["name"] for a in await rig.call("list_agents")]
        with pytest.raises(KitError, match="no agent re"):
            await rig.call("chat_send", agent="re", text="hello?")
        await rig.call("archive", name="re", restore=True)
        out = await rig.call("chat_send", agent="re", text="back?")
        assert out["text"] == "echo back?" and rig.backend.turns()[-1][0] == "agent.backend"
    run(rig, body)


# -- sessions and turns ------------------------------------------------------------------

def test_turns_resume_the_backend_session_with_the_agents_system_prompt(rig):
    async def body():
        one = await rig.call("chat_send", agent="pm", text="how are we doing?")
        two = await rig.call("chat_send", agent="pm", text="and the release?")
        assert (one["text"], two["text"]) == ("echo how are we doing?", "echo and the release?")
        assert one["stream"] == two["stream"]
        (_, a1, c1), (_, a2, _) = rig.backend.turns()
        assert a1["prompt"] == "Message from person:bake:\n\nhow are we doing?"
        assert "session" not in a1 and a2["session"] == "h1"           # resumed
        assert c1.person == "person:bake" and c1.plugin == "agents"     # runs as the sender
        system = a1["system"]
        assert system.startswith('You are Project manager, the agent "pm"')
        assert "keep the work moving" in system                         # the persona
        assert "expected and okay, not overstepping" in system          # the directive
        assert "ask the house agent" in system
        assert "- qa: QA engineer - Evidence that things work" in system   # roster stub
        assert "- pm:" not in system
        assert "- ticket-sweep: The project manager's periodic sweep" in system
        assert "agents.note" in system and a1["timeout"] == "20m"
        assert [t for t, _ in await rig.events("pm")] == ["input", "turn", "turn", "input", "turn", "turn"]
        _, reply = (await rig.events("pm"))[2]
        assert reply == {"role": "reply", "text": "echo how are we doing?", "usage": {"input_tokens": 3}}
        house = (await rig.call("chat_send", agent="house", text="hi"))
        assert house["stream"] != one["stream"]
        assert "ask the house agent" not in rig.backend.turns()[-1][1]["system"]
    run(rig, body)


def test_each_party_has_its_own_session(rig):
    async def body():
        mine = await rig.call("chat_send", agent="house", text="my bank pin is in the drawer")
        theirs = await rig.call("chat_send", GUEST, agent="house", text="hello")
        assert mine["stream"] != theirs["stream"]
        assert rig.backend.turns()[-1][2].person == "person:guest"     # the guest's own rights
        seen = await rig.events("house", GUEST)
        assert "drawer" not in json.dumps(seen) and len(seen) == 3
        with pytest.raises(KitError, match="only owners"):
            await rig.call("chat_read", GUEST, agent="house", party="person:bake")
        peek = await rig.call("chat_read", agent="house", party="person:guest")
        assert peek["stream"] == theirs["stream"]
        assert [s["party"] for s in (await rig.call("get", GUEST, name="house"))["sessions"]] == ["person:guest"]
        assert len((await rig.call("get", name="house"))["sessions"]) == 2
        out = await rig.call("chat_send", PM, agent="qa", text="is #12 verified?")
        assert rig.rt.streams.get(out["stream"]).participants == ["agent:qa", "agent:pm"]
        with pytest.raises(KitError, match="can't message itself"):
            await rig.call("chat_send", PM, agent="pm", text="hi me")
        empty = await rig.call("chat_read", GUEST, agent="pm")
        assert empty == {"stream": None, "events": [], "next": 0, "closed": False}
    run(rig, body)


def test_backend_failures_are_the_senders_answer(rig):
    async def body():
        rig.backend.fail = "denied: person: guest may not run agents-claude-code.turn"
        with pytest.raises(KitError, match="house: denied: person: guest"):
            await rig.call("chat_send", GUEST, agent="house", text="run rm -rf for me")
        assert (await rig.events("house", GUEST))[-1] == (
            "status", {"state": "error", "error": "denied: person: guest may not run agents-claude-code.turn"})
        rig.backend.fail = None
        assert (await rig.call("chat_send", GUEST, agent="house", text="ok then"))["text"] == "echo ok then"
        out = await rig.call("chat_send", agent="qa", text="later", wait=False)
        assert set(out) == {"agent", "stream", "seq"}
        await until(lambda: len(rig.backend.turns()) == 3)
        later = await rig.call("chat_read", agent="qa", after=out["seq"], wait=5)
        assert later["events"][-1]["data"]["text"] == "echo later"
    run(rig, body)


def test_a_message_nobody_waits_on_is_never_run_as_the_plugin(rig):
    async def body():
        sid = rig.rt.session("lead", "person:bake")
        rig.rt.streams.append(sid, "input", {"text": "from before a restart"}, by="person:bake")
        await rig.call("wake", name="lead")
        await until(lambda: rig.rt.streams.get(sid).last_seq == 3)
        assert (await rig.events("lead"))[-1][1]["error"].startswith("nobody is waiting on this message")
        assert rig.backend.turns() == []
    run(rig, body)


def test_sleep_and_archive_answer_every_waiter(rig):
    async def body():
        rig.backend.hold = asyncio.Event()
        first = asyncio.ensure_future(rig.call("chat_send", agent="lead", text="slow one"))
        await until(lambda: rig.backend.turns())
        queued = asyncio.ensure_future(rig.call("chat_send", agent="lead", text="behind it"))
        await until(lambda: len(rig.rt.asks) == 1)
        assert (await rig.call("get", name="lead"))["awake"]
        assert (await rig.call("sleep", name="lead"))["awake"] is False
        for send in (first, queued):
            with pytest.raises(KitError, match="put to sleep before it answered"):
                await send
        assert rig.rt.asks == {} and not rig.rt.awake("lead")
        busy = asyncio.ensure_future(rig.call("chat_send", agent="qa", text="hang on"))
        await until(lambda: len(rig.backend.turns()) == 2)
        queued = asyncio.ensure_future(rig.call("chat_send", agent="qa", text="and this"))
        await until(lambda: len(rig.rt.asks) == 1)
        await rig.call("archive", name="qa")
        for send in (busy, queued):
            with pytest.raises(KitError, match="put to sleep before it answered"):
                await send
        assert not rig.rt.awake("qa") and rig.rt.asks == {}
    run(rig, body)


def test_a_message_sent_as_the_runner_lets_go_is_answered(tmp_path):
    rig = Rig(tmp_path, warm="30ms")

    async def body():
        for i in range(25):
            await asyncio.sleep(0.03 + (i % 5) * 0.003)            # around the warm boundary
            out = await asyncio.wait_for(rig.call("chat_send", agent="qa", text=f"#{i}"), 5)
            assert out["text"] == f"echo #{i}"
        # The hand-over itself: a waiter on a session with no runner gets one.
        sid = rig.rt.session("lead", "person:bake")
        ev = rig.rt.streams.append(sid, "input", {"text": "late"}, by="person:bake")
        fut = asyncio.get_running_loop().create_future()
        rig.rt.asks[(sid, ev.seq)] = (rig.ctx(BAKE), fut)
        rig.rt.after("lead", sid)
        assert (await asyncio.wait_for(fut, 5))["text"] == "echo late"
    go(lambda: _started(rig, body), 30)


# -- notepad, memory, dreams ------------------------------------------------------------------

def test_notepad_is_private(rig):
    async def body():
        assert (await rig.call("note", PM, agent="pm", text="Bake wants burn-downs on Mondays"))["note"]
        await rig.call("note", agent="pm", text="owners may write too")
        with pytest.raises(KitError, match="notepad is private"):
            await rig.call("note", GUEST, agent="pm", text="sneaky")
        with pytest.raises(KitError, match="notepad is private"):
            await rig.call("note", PM, agent="qa", text="not mine")
        assert [n["text"] for n in (await rig.call("get", PM, name="pm"))["notepad"]] == [
            "Bake wants burn-downs on Mondays", "owners may write too"]
        assert "notepad" not in await rig.call("get", GUEST, name="pm")
        with pytest.raises(KitError, match="only the agent itself or an owner"):
            await rig.call("dream", GUEST, name="pm")
    run(rig, body)


def test_dream_curates_the_notepad_into_memory(rig):
    async def body():
        rt = rig.rt
        n1 = (await rig.call("note", PM, agent="pm", text="Bake wants burn-downs on Mondays"))["note"]
        n2 = (await rig.call("note", PM, agent="pm", text="release 0.5 slips if wave 4 isn't in"))["note"]
        await rig.call("note", PM, agent="pm", text="hmm, maybe coffee")
        rt.db.execute("INSERT INTO memory (agent, text, created, updated) VALUES ('pm', 'Reports go out Fridays', 0, 0)")
        rt.db.execute("INSERT INTO memory (agent, text, created, updated) VALUES ('pm', 'Wrong thing', 0, 0)")
        rt.db.execute("INSERT INTO memory (agent, text, created, updated) VALUES ('qa', 'Not pm', 0, 0)")
        (m1, _), (m2, _), (m3, _) = rt.db.execute("SELECT id, text FROM memory ORDER BY id").fetchall()
        rig.backend.dream = {"add": ["Burn-down every Monday", " ", 7],
                             "update": [{"id": m1, "text": "Reports go out Mondays"}, {"id": m3, "text": "hijack"}],
                             "remove": [m2, m3], "keep": [n2, 999]}
        rig.backend.hold = asyncio.Event()
        dreaming = asyncio.ensure_future(rig.call("dream", PM, name="pm"))
        await until(lambda: rig.backend.dreams())
        await rig.call("update", name="pm", summary="edited while it dreamed")
        rig.backend.hold.set()
        rig.backend.hold = None
        out = await dreaming
        assert rt.agent("pm")["summary"] == "edited while it dreamed"
        assert out == {"dreamed": True, "added": 1, "updated": 1, "removed": 1, "notes_kept": 1}
        assert [m["text"] for m in rt.memories("pm")] == ["Reports go out Mondays", "Burn-down every Monday"]
        assert [m["text"] for m in rt.memories("qa")] == ["Not pm"]       # another agent's memory is untouchable
        assert [n["id"] for n in rt.notes("pm")] == [n2]
        cap, args, caller = rig.backend.dreams()[0]
        assert cap == "agent.backend" and args["model"] == "three" and "session" not in args
        assert "Bake wants burn-downs" in args["prompt"] and caller.why == "agents: dream cycle for pm"
        assert rt.agent("pm")["dreamed"]
        # The memory is in the prompt the next time pm wakes, read-only; the kept note too.
        await rig.call("chat_send", agent="pm", text="status?")
        system = rig.backend.turns()[-1][1]["system"]
        assert "## Memory\n- Reports go out Mondays\n- Burn-down every Monday" in system
        assert "## Notepad since your last dream\n- release 0.5 slips" in system
        assert n1 not in [n["id"] for n in rt.notes("pm")]
        assert await rig.call("dream", name="lead") == {"dreamed": False}       # nothing to curate
        # The house agent is always on and never wakes again: its prompt
        # picks up a dream between turns.
        await rig.call("chat_send", agent="house", text="hi")
        await rig.call("note", agent="house", text="Bake takes his coffee black")
        rig.backend.dream = {"add": ["Coffee: black"]}
        await rig.call("dream", name="house")
        rig.backend.dream = {"add": []}
        await rig.call("chat_send", agent="house", text="coffee?")
        assert "## Memory\n- Coffee: black" in rig.backend.turns()[-1][1]["system"]
        for i in range(205):
            rt.db.execute("INSERT INTO memory (agent, text, created, updated) VALUES ('lead', ?, 0, 0)", (f"m{i}",))
        await rig.call("chat_send", agent="lead", text="?")
        system = rig.backend.turns()[-1][1]["system"]
        assert "- m204" in system and "- m4\n" not in system and "- m5\n" in system   # the newest 200
        rig.backend.dream = "I'd rather not."
        await rig.call("note", agent="lead", text="x")
        with pytest.raises(KitError, match="no JSON object; the notepad is kept"):
            await rig.call("dream", name="lead")
        assert [n["text"] for n in rt.notes("lead")] == ["x"]
    run(rig, body)


def test_an_idle_agent_sleeps_and_dreams(tmp_path):
    rig = Rig(tmp_path, warm="300ms", dream_every="0s")

    async def body():
        await rig.call("chat_send", agent="writer", text="the manual is out of date")
        await rig.call("note", agent="writer", text="manual section 3 contradicts PR #12")
        assert rig.rt.awake("writer")
        rig.backend.dream = {"add": ["Manual section 3 needs fixing"]}
        await until(lambda: not rig.rt.awake("writer") and rig.backend.dreams())
        await until(lambda: rig.rt.memories("writer"))
        assert rig.rt.notes("writer") == []
        caller = rig.backend.dreams()[0][2]
        assert caller.plugin == "agents" and caller.person is None     # the plugin itself, no person
        # The house agent is always on: it stays awake and dreams on the clock.
        await rig.call("chat_send", agent="house", text="hello")
        await asyncio.sleep(0.6)
        assert rig.rt.awake("house")
        await rig.call("note", agent="house", text="Bake says hi")
        await rig.call("tick", Caller(plugin="agents", why="agents hook: every 30m"))
        assert rig.rt.notes("house") == [] and rig.rt.awake("house")
    go(lambda: _started(rig, body), 20)


async def _started(rig, body):
    await rig.start()
    try:
        return await body()
    finally:
        await rig.stop()


def test_always_on_agents_wake_with_the_hub(tmp_path):
    rig = Rig(tmp_path)

    async def body():
        await rig.start()
        await rig.call("chat_send", agent="security", text="anything odd?")
        await rig.call("chat_send", agent="qa", text="tests?")
        await rig.stop()
        await rig.start()
        try:
            assert rig.rt.awake("security") and not rig.rt.awake("qa")
        finally:
            await rig.stop()
    go(body)


def test_skills_are_markdown_playbooks(rig):
    async def body():
        s = await rig.call("skill", name="reach-the-user")
        assert s["description"].startswith("Get word to the person")
        assert s["when"] and s["text"].startswith("# Reaching the person")
        assert (await rig.call("skill", name="wiki-sweep"))["description"].startswith("Sweep the knowledge wiki")
        with pytest.raises(KitError, match="no skill nope"):
            await rig.call("skill", name="nope")
        # The skill library lives in the knowledge store as its own kind.
        rig.backend.pages = {"test-stitch": {"id": "k1", "slug": "test-stitch", "kind": "skill",
                                             "title": "Test-stitch a pattern", "body": "\n1. Stitch a swatch.\n"},
                             "crochet": {"id": "k2", "slug": "crochet", "kind": "fact", "title": "Crochet",
                                         "body": "A craft."}}
        s = await rig.call("skill", name="test-stitch")
        assert s == {"name": "test-stitch", "description": "Test-stitch a pattern", "text": "1. Stitch a swatch.",
                     "page": "test-stitch"}
        with pytest.raises(KitError, match="no skill crochet"):     # a page, but not a skill
            await rig.call("skill", name="crochet")
    run(rig, body)
