"""The house agent's memory (brief MEM5), through the MCP door and the site's path, on a real served hub
with a real worker. The engine is a stand-in (tests/plugins/memory-engine): it logs what each turn was
handed, makes the door calls a message asks for (<<cap {json}>>), and answers each dream with the
answer a test queued, so these tests check what the hub does with a dream's answer, not a model's
judgement. A real Claude Code run is in the pull request.
"""

import ast
import asyncio
import json
import math
import pathlib
import sqlite3
import sys
import importlib.util

import pytest

from rook_hub import Hub, backup
from test_house_agent import Rig as HouseRig

ENGINE = str(pathlib.Path(__file__).parent / "plugins" / "memory-engine")
MEMORY = pathlib.Path(__file__).parents[1] / "hub" / "rook_hub" / "defaults" / "agents" / "hub_memory.py"
WHY = "The memory tests (brief MEM5)."


def memory_module():
    spec = importlib.util.spec_from_file_location("hub_memory_under_test", MEMORY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Rig(HouseRig):
    """The house agent's rig with the memory stand-in as its engine, and Alice's own site session."""

    async def __aenter__(self):
        await super().__aenter__()
        self.dir = self.tmp_path / "engine"
        self.dir.mkdir()
        await self.ok("hub", "plugin.install", path=ENGINE)
        version = (await self.ok("hub", "config.set", worker=self.moniker,
                                 set={"memory-engine": {"dir": str(self.dir)}}))["config"]["version"]
        while self.hub.get_worker(self.moniker)["config_version"] != version:
            await asyncio.sleep(0.02)
        await self.ok("hub", "setting.set", plugin="agents", key="backend", value="memory-engine.turn")
        await self.linked()
        self.session = self.hub.auth.mint("session", user=self.user["guid"])    # as the site's login makes it
        return self

    async def own(self, text):
        """Alice's own words, from her site session (the path POST /api/call takes)."""
        reply = await self.hub.call("hub", "agent.chat.send", {"agent": "house", "text": text}, WHY, token=self.session)
        assert reply["type"] == "result", reply
        return reply["result"]

    def queue(self, *answers, name="dreams.json"):
        path = self.dir / name
        have = json.loads(path.read_text()) if path.exists() else []
        path.write_text(json.dumps(have + list(answers)))

    def calls(self):
        return [json.loads(line) for line in (self.dir / "calls.jsonl").read_text().splitlines()]

    def prompts(self):
        return [c["prompt"] for c in self.calls() if "schema" not in c]

    def said(self):
        return [json.loads(line) for line in (self.dir / "said.jsonl").read_text().splitlines()]

    async def dream(self):
        got = await self.ok("hub", "agent.dream", agent="house")
        assert "error" not in got, got
        return got

    def db(self):
        db = sqlite3.connect(self.tmp_path / "hub" / "plugin-data" / "agents" / "memory.db")
        db.row_factory = sqlite3.Row
        return db

    async def show(self, ref):
        return "\n".join((await self.ok("hub", "agent.memory.show", id=ref))["lines"])


def run(tmp_path, body):
    async def main():
        async with Rig(tmp_path) as rig:
            await body(rig)
    asyncio.run(main())


def fact(text, entries, said_by, kind="statement", held=(), new=None):
    return {"fact": text, "entries": list(entries), "said_by": said_by, "kind": kind, "held": list(held),
            "new_belief": new}


def belief(text, about="world", things=(), **more):
    return {"belief": text, "about": about, "about_things": list(things), **more}


ALICE = "person:Alice"


# ------------------------------------------------------------------------------- standing

def test_standing_and_maturity_cases():
    """Round two's rules: the newest strong link that may flip says which way; separate occasions mature it;
    weak words are recorded but cannot flip; an agent's word never flips; a pin stands."""
    m = memory_module()

    def L(i, stance, kind, who, day, flips=None):
        return {"id": i, "stance": stance, "strength": m.strength(kind, who), "at": day * 86400.0,
                "occasion": f"day {day}", "flips": who.startswith("person:") if flips is None else flips}

    held = [L(1, 1, "statement", ALICE, 1), L(2, 1, "statement", ALICE, 5), L(3, 1, "statement", ALICE, 9)]
    st = m.standing(held)
    assert st["direction"] == 1 and math.isclose(st["certainty"], 1 - math.exp(-1.8 / 1.5)) and st["label"] == "holds, mature"
    one = m.standing([L(1, 1, "correction", ALICE, 1)])
    assert math.isclose(one["certainty"], 1 - math.exp(-1 / 1.5)) and one["label"] == "holds, tentative"
    same_day = m.standing([L(1, 1, "statement", ALICE, 1), L(2, 1, "statement", ALICE, 1), L(3, 1, "statement", ALICE, 1)])
    assert same_day["occasions"] == 1                                        # one sitting is one occasion
    weak = m.standing(held + [L(4, -1, "mention", ALICE, 12)])               # 0.3: too weak to flip it
    assert weak["direction"] == 1 and weak["weak_against_since"] == 1
    agent = m.standing(held + [L(4, -1, "correction", "agent:quiet-lamp", 12)])   # 0.4, but an agent's: never flips
    assert agent["direction"] == 1 and "too weak to flip it" in m.summary(agent)
    flipped = m.standing(held + [L(4, -1, "correction", ALICE, 12)])
    assert flipped["direction"] == -1 and flipped["flips"] == 1 and flipped["conflict"]   # fresh one-off v mature
    assert "ask before acting" in flipped["label"] and "flipped 1 time" in m.summary(flipped)
    assert m.strength("statement", "house") == 0 and m.strength("statement", "agent:house") == 0
    pinned = m.standing(held + [L(4, -1, "correction", ALICE, 12)], pinned=True)
    assert pinned["value"] == 1.0 and m.summary(pinned) == "PINNED"


def test_recall_needs_no_model():
    """Reading memory needs nothing but Python's own sqlite3: the module imports only the standard library
    and core's plugin library (MEM5, answer 3: the graph alone ships)."""
    tree = ast.parse(MEMORY.read_text())
    imported = {n.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names}
    imported |= {node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= set(sys.stdlib_module_names) | {"rook_hub"}, imported


# --------------------------------------------------------------------------- who said it

def test_who_said_it_the_same_words_land_differently(tmp_path):
    """The same words through Alice's site session and through an agent with her token are recorded as
    hers and as the agent's; the turn is told which; a belief about what she wants comes only from hers."""
    async def body(rig):
        await rig.own("I want the backups on cachyrig.")                       # seq 1
        await rig.say("I want the backups on cachyrig.")                       # seq 4, an agent with her token
        heads = [p.split("\n\n")[0] for p in rig.prompts()]
        token = next(t["moniker"] for t in (await rig.ok("hub", "token.list"))["tokens"]
                     if t["minted_by"] is None and t["purpose"] == "agent")
        assert heads == ["Message from person:Alice:",
                         f"Message from agent:{token}, with person:Alice's token (not person:Alice's own words):"]
        rows = [tuple(r) for r in rig.db().execute("SELECT seq, speaker, channel FROM entries ORDER BY seq")]
        assert rows == [(1, ALICE, "site"), (3, "house", "reply"), (4, f"agent:{token}", "door"), (6, "house", "reply")]
        rig.queue({"facts": [
            fact("Alice said she wants the backups on cachyrig", ["s1"], ALICE, "decision",
                 new=belief("The backups go on cachyrig", ALICE, ["backups"])),
            # the dream claims the agent's line is hers: the record says whose it is
            fact("Alice wants the backups on cachyrig, says an agent", ["s4"], ALICE, "decision",
                 new=belief("The backups go on cachyrig, as Alice wants", ALICE))]})
        got = await rig.dream()
        assert got["new"] == 1 and got["facts"] == 2
        assert any(f"from agent:{token}'s words" in h["why"] for h in got["held_back"]), got["held_back"]
        said_by = [r[0] for r in rig.db().execute("SELECT said_by FROM episodes ORDER BY id")]
        assert said_by == [ALICE, f"agent:{token}"]
        dream_prompt = next(c for c in rig.calls() if "schema" in c)["prompt"]
        assert f"person:Alice (site): I want the backups on cachyrig." in dream_prompt
        assert f"agent:{token} (door): I want the backups on cachyrig." in dream_prompt
    run(tmp_path, body)


# ---------------------------------------------------------------------------- flips and the ask

def test_an_agents_correction_does_not_flip_the_person_is_asked_once(tmp_path):
    async def body(rig):
        await rig.own("Starscream is decommissioned.")                           # s1
        rig.queue({"facts": [fact("Alice said Starscream is decommissioned", ["s1"], ALICE, "correction",
                                  new=belief("Starscream is decommissioned", "world", ["Starscream"])),
                             fact("Alice said nothing runs on Starscream", ["s1"], ALICE, "statement",
                                  new=belief("Nothing runs on Starscream", "how", ["Starscream"]))],
                   "things": [{"name": "Starscream", "aliases": ["the old server"]}]})
        await rig.dream()
        await rig.say("Starscream is back in service.")                          # s4, an agent
        rig.queue({"facts": [fact("An agent said Starscream is back in service", ["s4"], "agent:x", "correction",
                                  held=[{"belief": "b1", "stance": "against"}, {"belief": "b2", "stance": "against"}])]})
        got = await rig.dream()
        assert got["asks"] == 2 and got["links"] == 2
        assert "holds" in (await rig.show("b1")).splitlines()[-1]              # it stands
        await rig.own("Morning. What's on the old server?")
        asked = rig.prompts()[-1]
        assert asked.count("To ask Alice (once, briefly, in this reply") == 1                  # one claim, one question
        assert "Starscream is back in service" in asked and '[b1] "Starscream is decommissioned"; [b2]' in asked
        assert "an agent said it no longer holds: ask before acting on it" in asked
        await rig.own("And the old server's disks?")
        assert "To ask" not in rig.prompts()[-1]                                # it does not nag
        seq = (await rig.own("Yes, Starscream really is back now."))["seq"]
        rig.queue({"facts": [fact("Alice said Starscream is back", [f"s{seq}"], ALICE, "correction",
                                  held=[{"belief": "b1", "stance": "against"}])]})
        got = await rig.dream()
        assert got["answers"] == 1
        assert [r[0] for r in rig.db().execute("SELECT answer FROM asks ORDER BY id")] == ["flipped", None]
        assert (await rig.show("b1")).splitlines()[-1].startswith("Now: does not hold")
    run(tmp_path, body)


# -------------------------------------------------------------------------------------- pins

PIN = '<<agent.memory.pin {"statement": "Starscream is gone for good."}>>'


def test_pins_are_said_undone_replaced_and_never_moved_by_a_dream(tmp_path):
    async def body(rig):
        sent = await rig.own(f"Starscream is gone for good. {PIN} <<reply: Understood.>>")
        assert sent["text"] == 'Understood.\n\n(Pinned: "Starscream is gone for good.")'     # said, though the agent was silent
        assert rig.said()[-1][0]["pinned"] == "b1"
        sent = await rig.own('<<agent.memory.unpin {}>> <<reply: Done.>>')                # undone the next message
        assert sent["text"] == 'Done.\n\n(Unpinned: "Starscream is gone for good.")'
        assert not rig.db().execute("SELECT pinned FROM beliefs WHERE id = 1").fetchone()[0]
        sent = await rig.own(f"Starscream is gone for good. {PIN} <<reply: Pinned that.>>")
        assert sent["text"] == "Pinned that."                                              # the agent said it: no line added
        for n in range(3):
            await rig.own(f"Something else {n}.")
        sent = await rig.own('That was not meant to be pinned. <<agent.memory.unpin {"belief": "b1"}>> <<reply: Unpinned.>>')
        assert sent["text"] == "Unpinned." and rig.said()[-1][0]["unpinned"] == "b1"       # undone turns later
        await rig.own(f"Starscream is gone for good. {PIN} <<reply: OK.>>")
        sent = await rig.own('Starscream is back, as the lab box. <<agent.memory.pin {"statement": "Starscream is back, '
                             'as the lab box.", "belief": "b1"}>> <<reply: OK.>>')
        assert sent["text"] == ('OK.\n\n(That replaces what was pinned: "Starscream is gone for good.". '
                                'Pinned now: "Starscream is back, as the lab box.")')
        # An agent's message cannot pin, unpin or replace.
        await rig.say('<<agent.memory.unpin {"belief": "b1"}>>')
        assert rig.said()[-1][0]["unpinned"] is None and "not person:Alice's own words" in rig.said()[-1][0]["note"]
        # A dream cannot move it: a link against is recorded; a change is held back.
        seq = (await rig.own("Starscream is gone again, I think."))["seq"]
        rig.queue({"facts": [fact("Alice thinks Starscream is gone again", [f"s{seq}"], ALICE, "statement",
                                  held=[{"belief": "b1", "stance": "against"}])],
                   "changes": [{"belief": "b1", "now": "Starscream is gone", "entries": [f"s{seq}"]}]})
        got = await rig.dream()
        assert any("is pinned" in h["why"] for h in got["held_back"])
        history = await rig.show("b1")
        assert "Now: PINNED (1 link against since, not counted)" in history
        assert "REPLACE: \"Starscream is back, as the lab box.\" (was \"Starscream is gone for good.\")" in history
        pins = [r[0] for r in rig.db().execute("SELECT action FROM pins ORDER BY id")]
        assert pins == ["pin", "unpin", "pin", "unpin", "pin", "replace"]
    run(tmp_path, body)


# ---------------------------------------------------------------- exceptions, for-now and changes

def test_exceptions_and_for_now_sit_beside_a_standing_belief(tmp_path):
    async def body(rig):
        await rig.own("Dark mode on every screen, please.")                                   # s1
        rig.queue({"facts": [fact("Alice wants dark mode on every screen", ["s1"], ALICE, "decision",
                                  new=belief("Dark mode on every screen", ALICE, ["dark mode"]))],
                   "things": [{"name": "dark mode", "aliases": ["dark theme"]}]})
        await rig.dream()
        await rig.own("Light mode on the kitchen tablet for a bit.")                          # s4
        rig.queue({"facts": [fact("Alice wants light mode on the kitchen tablet for now", ["s4"], ALICE, "decision",
                                  held=[{"belief": "b1", "stance": "against"}],
                                  new=belief("Light mode on the kitchen tablet, for now", ALICE,
                                             ["kitchen tablet"], for_now_beside="b1"))],
                   "things": [{"name": "kitchen tablet", "aliases": ["the tablet in the kitchen"]}]})
        got = await rig.dream()
        assert any("sits beside" in h["why"] for h in got["held_back"])                       # never against it
        assert "Now: holds" in await rig.show("b1")
        await rig.own("What theme is the kitchen tablet on? And dark mode elsewhere?")
        block = rig.prompts()[-1].split("## Memory", 1)[1]
        assert block.index("[b2]") < block.index("[b1]")                                       # the narrower first
        assert "for now, beside b1 (beside it, never against it)" in block
        rig.queue({"settle": [{"belief": "b2", "how": "lapsed"}]})
        await rig.own("The tablet is back to dark.")
        await rig.dream()
        assert rig.db().execute("SELECT ended FROM edges WHERE rel = 'for_now'").fetchone()[0] == "lapsed"
        # A value that changed is the same belief changed, with its history.
        seq = (await rig.own("The backups run at 02:00."))["seq"]
        rig.queue({"facts": [fact("Alice said backups run at 02:00", [f"s{seq}"], ALICE, "decision",
                                  new=belief("The backups run at 02:00", "how", ["backups"]))]})
        await rig.dream()
        seq = (await rig.own("Move the backups to 03:00."))["seq"]
        rig.queue({"facts": [fact("Alice moved the backups to 03:00", [f"s{seq}"], ALICE, "decision")],
                   "changes": [{"belief": "b3", "now": "The backups run at 03:00", "entries": [f"s{seq}"]}]})
        got = await rig.dream()
        assert got["changes"] == 1 and got["new"] == 0
        history = await rig.show("b3")
        assert "The backups run at 03:00" in history and 'once worded: "The backups run at 02:00"' in history
    run(tmp_path, body)


# ----------------------------------------------------------- recall, the hint, misses and healing

def test_recall_the_hint_on_a_thin_recall_and_a_miss_that_heals(tmp_path):
    async def body(rig):
        await rig.own("hello")
        first = rig.prompts()[-1]
        assert "Nothing held is close to this message. Before you say you don't remember" in first
        assert (await rig.ok("hub", "agent.memory.misses"))["counts"] == {"open": 1}
        seq = (await rig.own("Starscream is decommissioned now."))["seq"]
        rig.queue({"facts": [fact("Alice said Starscream is decommissioned", [f"s{seq}"], ALICE, "correction",
                                  new=belief("Starscream is decommissioned", "world", ["Starscream"]))],
                   "things": [{"name": "Starscream", "aliases": ["the old server"]}],
                   "nothing_held": ["m1", "m2"]})     # new information both times, not misses
        await rig.dream()
        await rig.own("What happened to the old server?")
        assert "[b1] holds, tentative" in rig.prompts()[-1] and "found by about 'the old server'" in rig.prompts()[-1]
        # Other words: nothing named, so recall is thin; the turn gets the nearest things and searches itself.
        sent = await rig.own('Is the rack box still up? <<agent.memory.search {"query": "\\u0053tarscream"}>>')
        thin = rig.prompts()[-1]
        assert "Things memory holds nearest this message by name: [t1] Starscream (also: the old server)" in thin
        assert rig.said()[-1][0]["beliefs"][0]["id"] == "b1"
        missed = (await rig.ok("hub", "agent.memory.misses"))["misses"][-1]
        assert missed["found_by_search"] == ["b1"] and missed["status"] == "open"
        rig.queue({"heals": [{"miss": missed["id"], "thing": "t1", "alias": "the rack box", "belief": "b1",
                              "why": "Alice calls Starscream the rack box"}]})
        got = await rig.dream()
        assert got["heals"] == 1
        await rig.own("Is the rack box still up?")
        assert "found by about 'the rack box'" in rig.prompts()[-1]                          # the same words hit now
        missed = (await rig.ok("hub", "agent.memory.misses"))["misses"]
        assert [m["status"] for m in missed] == ["nothing held", "nothing held", "healed"]
        assert missed[2]["healed_with"][0].startswith("alias the rack box")
        assert sent["seq"] > 0
    run(tmp_path, body)


def test_an_alias_is_a_name_not_a_sentence(tmp_path):
    async def body(rig):
        await rig.own("The NAS in the closet holds every photo we have.")
        rig.queue({"things": [{"name": "the NAS", "aliases": ["the NAS in the closet holds every photo we have",
                                                               "the closet NAS"]}], "facts": []})
        got = await rig.dream()
        assert got["aliases"] == 1 and any("whole sentence" in h["why"] for h in got["held_back"])
        names = [r[0] for r in rig.db().execute("SELECT name FROM names ORDER BY id")]
        assert names == ["the NAS", "the closet NAS"]
    run(tmp_path, body)


def test_a_near_duplicate_is_checked_and_linked_to_the_held_belief(tmp_path):
    async def body(rig):
        await rig.own("Backups go to cachyrig.")
        rig.queue({"facts": [fact("Alice said backups go to cachyrig", ["s1"], ALICE, "decision",
                                  new=belief("The backups go to cachyrig", "how"))]})
        await rig.dream()
        await rig.own("Remember, the backups go to cachyrig every night.")
        rig.queue({"facts": [fact("Alice said again the backups go to cachyrig", ["s4"], ALICE, "statement",
                                  new=belief("The backups go to cachyrig nightly", "how"))]})
        rig.queue({"verdicts": [{"pair": 0, "verdict": "same"}]}, name="same.json")
        got = await rig.dream()
        assert got["same"] == 1 and got["new"] == 0
        assert rig.db().execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == 1
        assert rig.db().execute("SELECT COUNT(*) FROM links WHERE belief = 1").fetchone()[0] == 2
    run(tmp_path, body)


# --------------------------------------------------------------------------------- the triggers

def test_the_dream_forks_the_session_and_runs_before_compaction_and_when_idle(tmp_path):
    async def body(rig):
        await rig.own("hello")
        rig.queue({"facts": []})
        await rig.own("COMPACT now")                 # the engine compacts: its hook asks for a dream first
        dreams = [tuple(r) for r in rig.db().execute("SELECT trigger, state, seq_from, seq_to FROM dreams")]
        assert dreams == [("compaction", "done", 1, 4)]
        forked = next(c for c in rig.calls() if "schema" in c)
        harness = rig.calls()[0]
        assert forked["fork"] is True and forked["session"] and "session" not in harness
        assert "[Memory dream." in forked["prompt"] and "[s4]" in forked["prompt"]
        await rig.ok("hub", "setting.set", plugin="agents", key="memory_idle_minutes", value=0.005)
        rig.queue({"facts": []})
        await rig.own("anything new")
        async with asyncio.timeout(20):
            while len(rig.db().execute("SELECT * FROM dreams").fetchall()) < 2:
                await asyncio.sleep(0.05)
        while rig.db().execute("SELECT state FROM dreams WHERE id = 2").fetchone()[0] == "running":
            await asyncio.sleep(0.05)
        assert tuple(rig.db().execute("SELECT trigger, state, seq_from FROM dreams WHERE id = 2").fetchone()) == (
            "idle", "done", 6)
    run(tmp_path, body)


def test_a_failed_dream_is_tried_again_and_a_cut_off_one_after_a_restart(tmp_path):
    async def body(rig):
        await rig.own("Starscream is decommissioned.")
        rig.queue("FAIL")
        got = await rig.ok("hub", "agent.dream", agent="house")
        assert got["error"].startswith("the engine") or "fail" in got["error"]
        assert rig.db().execute("SELECT dreamed_to FROM sessions").fetchone()[0] == 0     # still undreamed
        rig.queue({"facts": [fact("Alice said Starscream is decommissioned", ["s1"], ALICE, "correction",
                                  new=belief("Starscream is decommissioned", "world", ["Starscream"]))],
                   "things": [{"name": "Starscream", "aliases": []}]})
        assert (await rig.dream())["new"] == 1
        states = [tuple(r) for r in rig.db().execute("SELECT state, seq_from FROM dreams")]
        assert states == [("failed", 1), ("done", 1)]
        # A dream cut off by a restart: said so, and its words dreamed again at the next trigger.
        await rig.own("Starscream's disks went to the NAS.")
        rig.db().execute("INSERT INTO dreams (session, trigger, started_at, seq_from, seq_to) SELECT session, 'idle', 0, 4, 6"
                         " FROM sessions").connection.commit()
        await rig.stop_worker()
        await rig.stop_hub()
        await rig.start_hub()
        rig.start_worker()
        await rig.linked()
        cut = rig.db().execute("SELECT state, error FROM dreams WHERE id = 3").fetchone()
        assert cut[0] == "cut off" and "dreamed again at the next trigger" in cut[1]
        found = await rig.ok("hub", "agent.memory.search", query="Starscream")              # nothing was lost
        assert [b["id"] for b in found["beliefs"]] == ["b1"]
        rig.queue({"facts": []})
        assert (await rig.dream())["dream"] == 4
        assert rig.db().execute("SELECT seq_from FROM dreams WHERE id = 4").fetchone()[0] == 4
    run(tmp_path, body)


def test_backup_carries_memory_and_a_restore_has_it(tmp_path):
    async def body(rig):
        await rig.own("Starscream is decommissioned.")
        rig.queue({"facts": [fact("Alice said Starscream is decommissioned", ["s1"], ALICE, "correction",
                                  new=belief("Starscream is decommissioned", "world", ["Starscream"]))],
                   "things": [{"name": "Starscream", "aliases": ["the old server"]}]})
        await rig.dream()
        await rig.ok("hub", "backup.run", to=str(tmp_path / "hub.backup"))
        (tmp_path / "vault.key").write_text("00" * 32)
        backup.restore(tmp_path / "hub.backup", tmp_path / "restored", tmp_path / "vault.key")
        db = sqlite3.connect(tmp_path / "restored" / "plugin-data" / "agents" / "memory.db")
        assert db.execute("SELECT text FROM beliefs").fetchall() == [("Starscream is decommissioned",)]
        restored = Hub(tmp_path / "restored", vault_key=bytes(32))
        try:
            got = await restored.call("hub", "agent.memory.search", {"query": "the old server"}, WHY,
                                      user=rig.user["guid"])
            assert got["type"] == "result" and got["result"]["beliefs"][0]["found_by"] == "about 'the old server'"
        finally:
            restored.close()
    run(tmp_path, body)


def test_memory_off_keeps_the_record_and_the_first_dream_reads_all_of_it(tmp_path):
    async def body(rig):
        await rig.ok("hub", "setting.set", plugin="agents", key="memory", value=False)
        await rig.own("Starscream is decommissioned.")
        assert "## Memory" not in rig.prompts()[-1]
        await rig.ok("hub", "setting.set", plugin="agents", key="memory", value=True)
        await rig.own("And the NAS is in the closet.")
        rig.queue({"facts": []})
        await rig.dream()
        prompt = next(c for c in rig.calls() if "schema" in c)["prompt"]
        assert "[s1]" in prompt and "Starscream is decommissioned." in prompt and "[s4]" in prompt
    run(tmp_path, body)


@pytest.mark.parametrize("words,expected", [("the old server's disks", True), ("old", False)])
def test_a_thing_is_named_only_when_all_its_words_are_there(tmp_path, words, expected):
    m = memory_module()
    s = m.Store(tmp_path / "m.db")
    tid = s.add_thing("Starscream", 1.0)
    s.add_name(tid, "the old server", 1.0)
    assert (tid in m.match_things(s, m.words(words))) is expected
