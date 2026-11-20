"""The plain mechanics (rounds two and three): standing from links (strong evidence flips, weak does
not; opposites), pins in his words, who-said-it and how/should rules when a dream's answer is written,
the same check, recall and the two clocks, compaction, the importer, the MCP tools and the command line. The model
and the embedding model are stand-ins here; nothing tests what a model writes (that is
madeup/expect.toml, run with `check`)."""

import io
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rook_memory import dream, importer, knobs as knobs_mod, mcp, model, pin, recall, search, standing, turn  # noqa: E402
from rook_memory.__main__ import main  # noqa: E402
from rook_memory.store import Store  # noqa: E402

MADEUP = pathlib.Path(__file__).resolve().parent.parent / "madeup" / "conversations.json"


FAKE = {"repo": "fake", "pooling": "none", "query_prefix": "", "miss_below": 0.3, "pinned_miss_below": 0.2,
        "newer_words": 0.5, "pin_match": 0.7, "same_check_from": 0.8}


@pytest.fixture
def knobs():
    k = knobs_mod.load()
    k["models"]["fake"] = dict(FAKE)
    k["search"]["model"] = "fake"
    return k


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mem")
    yield s
    s.close()


def link(i, stance, strength, at, occ=None):
    return {"id": i, "stance": stance, "strength": strength, "at": at, "occasion": occ or at[:10]}


# --- standing: the three cases from the brief ---

def test_one_statement_against_a_mature_belief_is_a_tentative_conflict(knobs):
    held = [link(1, 1, 0.6, "2026-05-04"), link(2, 1, 0.6, "2026-05-19"), link(3, 1, 0.6, "2026-06-20")]
    before = standing.standing(held, knobs)
    assert before["direction"] == 1 and before["certainty"] >= knobs["weigh"]["mature_at"]
    after = standing.standing(held + [link(4, -1, 0.6, "2026-07-01")], knobs)
    assert after["direction"] == -1                       # the most recent is what we go on
    assert after["certainty"] < knobs["weigh"]["tentative_below"]  # but one occasion is tentative
    assert after["conflict"] and "ask before acting" in after["label"]
    assert after["flips"] == 1


def test_the_same_statement_on_three_separate_days_matures_and_ends_the_conflict(knobs):
    held = [link(1, 1, 0.6, "2026-05-04"), link(2, 1, 0.6, "2026-05-19"), link(3, 1, 0.6, "2026-06-20")]
    three = held + [link(4, -1, 0.6, "2026-07-01"), link(5, -1, 0.6, "2026-07-08"), link(6, -1, 0.6, "2026-07-20")]
    s = standing.standing(three, knobs)
    assert s["direction"] == -1 and s["occasions"] == 3
    assert s["certainty"] >= knobs["weigh"]["mature_at"] and not s["conflict"]
    # the same thing said three times in one sitting is one occasion
    same_day = held + [link(4, -1, 0.6, "2026-07-01"), link(5, -1, 0.6, "2026-07-01"), link(6, -1, 0.6, "2026-07-01")]
    assert standing.standing(same_day, knobs)["occasions"] == 1
    assert standing.standing(same_day, knobs)["conflict"]


def test_a_flip_and_a_flip_back(knobs):
    links = [link(1, 1, 0.6, "2026-05-19"), link(2, -1, 0.6, "2026-07-08"), link(3, 1, 0.6, "2026-07-20")]
    s = standing.standing(links, knobs)
    assert s["direction"] == 1 and s["flips"] == 2
    # the older support counts at before_flip_weight: more than the newest alone, less than both in full
    alone = standing.standing([link(3, 1, 0.6, "2026-07-20")], knobs)["certainty"]
    full = standing.standing([link(1, 1, 0.6, "2026-05-19"), link(3, 1, 0.6, "2026-07-20")], knobs)["certainty"]
    assert alone < s["certainty"] < full


def test_one_garbled_voice_line_is_barely_anything(knobs):
    w = standing.strength(knobs, "unclear", "bake", "voice")
    s = standing.standing([link(1, 1, w, "2026-06-10")], knobs)
    assert s["certainty"] < 0.1 and "tentative" in s["label"]


def test_strength_is_kind_by_who_by_channel(knobs):
    assert standing.strength(knobs, "correction", "bake", "chat") == 1.0
    assert standing.strength(knobs, "correction", "steward", "chat") == pytest.approx(0.6)
    assert standing.strength(knobs, "statement", "bake", "voice") == pytest.approx(0.36)


def test_the_house_agents_own_words_count_for_nothing(knobs, store):
    assert standing.strength(knobs, "statement", "house", "chat") == 0
    sid = store.session("s", "live", "s")["id"]
    h = store.add_entry(sid, "2026-06-16T14:00:20Z", "house", "chat", "Starscream is decommissioned, so no retry.")
    held = store.add_belief("Starscream is decommissioned", "world")
    answer = {"facts": [{"fact": "The house agent said Starscream is decommissioned.", "entries": [f"s{h}"],
                         "said_by": "house", "kind": "statement", "held": [{"belief": f"b{held}", "stance": "supports"}],
                         "new_belief": {"belief": "No retries on Starscream", "about": "world"}}]}
    d = store.start_dream(sid, "fork", "hand", h, h)
    out = dream.apply(store, knobs, d, sid, store.undreamed(sid), answer, "2026-06-16T14:01:00Z", log=lambda *a: None)
    assert out["new"] == 0 and "stand on nothing" in out["held_back"][0]["why"]
    assert store.links(held)[0]["strength"] == 0


def test_zero_strength_links_are_history_only(knobs):
    s = standing.standing([link(1, 1, 0.6, "2026-05-04"), link(2, -1, 0.0, "2026-06-01")], knobs)
    assert s["direction"] == 1 and s["flips"] == 0 and s["links"] == 2 and s["counted"] == 1


# --- pins ---

def entry(store, speaker="bake", text="x", at="2026-06-16T14:03:00Z", session=None):
    session = session or store.session("t", "live", "t")["id"]
    return store.add_entry(session, at, speaker, "chat", text)


def test_pin_makes_a_new_pinned_belief_from_his_words_and_unpin_drops_it_back(store, knobs):
    e = entry(store, text="Pin this: Starscream is decommissioned for good.")
    bid, said = pin.pin(store, knobs, "Starscream is decommissioned for good", e, final_words="for good")
    assert bid and "new belief" in said and store.pinned(bid)
    links = store.links(bid)
    assert len(links) == 1 and links[0]["said_by"] == "bake" and links[0]["kind"] == "decision"
    assert standing.of(store, knobs, bid)["pinned"]
    e2 = entry(store, text="no, don't pin that", at="2026-06-16T14:04:00Z")
    got, said = pin.unpin(store, knobs, e2)
    assert got == bid and not store.pinned(bid) and "ordinary" in said
    assert [p["action"] for p in store.pins(bid)] == ["pin", "unpin"]
    assert store.one("SELECT COUNT(*) AS n FROM beliefs")["n"] == 1   # kept, never removed


def test_pin_matches_a_close_held_belief(store, knobs):
    held = store.add_belief("starscream is decommissioned", "world")
    e = entry(store, text="pin it: starscream is decommissioned")
    bid, said = pin.pin(store, knobs, "starscream is decommissioned", e, final_words="pin it")
    assert bid == held and "closest held belief" in said


def test_only_his_own_words_pin(store, knobs):
    e = entry(store, speaker="steward", text="pin that Bake wants PDFs")
    bid, said = pin.pin(store, knobs, "Bake wants PDFs", e, final_words="pin that")
    assert bid is None and "Only his own words" in said
    assert store.one("SELECT COUNT(*) AS n FROM beliefs")["n"] == 0


def test_a_pinned_belief_does_not_move_but_its_evidence_shows(store, knobs):
    e = entry(store, text="Starscream is gone, pin it")
    bid, _ = pin.pin(store, knobs, "Starscream is decommissioned", e, final_words="pin it")
    ep = store.add_episode(1, "An agent said Starscream is back", "agent", "chat", "statement", [e], "2026-08-05T10:00:00Z")
    store.add_link(bid, ep, -1, 0.24, "2026-08-05")
    s = standing.of(store, knobs, bid)
    assert s["value"] == 1.0 and s["against_since_pin"] == 1 and "1 link against" in standing.summary(s)


# --- writing a dream's answer: who said it ---

def test_dream_answer_keeps_who_said_it(store, knobs):
    sid = store.session("s", "live", "s")["id"]
    st = store.add_entry(sid, "2026-06-02T15:02:00Z", "steward", "chat", "Bake said he wants all reports as PDFs.")
    bk = store.add_entry(sid, "2026-06-02T15:03:00Z", "bake", "chat", "I like dark mode on everything.")
    likes = store.add_belief("Bake likes short answers", "bake")
    entries = store.undreamed(sid)
    answer = {"facts": [
        {"fact": "The steward said Bake wants PDFs.", "entries": [f"s{st}"], "said_by": "steward", "kind": "statement",
         "held": [{"belief": f"b{likes}", "stance": "supports"}],
         "new_belief": {"belief": "Bake wants all reports as PDFs", "about": "bake"}},
        {"fact": "Bake said he likes dark mode.", "entries": [f"s{bk}"], "said_by": "bake", "kind": "statement", "held": [],
         "new_belief": {"belief": "Bake likes dark mode", "about": "bake"}},
        {"fact": "Something with no source.", "entries": ["s999"], "kind": "statement", "held": [], "new_belief": None},
        {"fact": "Claimed as Bake's but the record says steward.", "entries": [f"s{st}"], "said_by": "bake",
         "kind": "correction", "held": [], "new_belief": None},
        {"fact": "Bake's line, but resting on the steward's.", "entries": [f"s{bk}", f"s{st}"], "said_by": "steward",
         "kind": "statement", "held": [{"belief": "bake likes short answers", "stance": "supports"}], "new_belief": None},
    ]}
    d = store.start_dream(sid, "replay", "hand", entries[0]["id"], entries[-1]["id"])
    out = dream.apply(store, knobs, d, sid, entries, answer, "2026-06-02T15:03:00Z", log=lambda *a: None)
    assert out["facts"] == 4 and out["new"] == 1
    whys = [h["why"] for h in out["held_back"]]
    assert any("from steward's words" in w for w in whys) and any("names no entry" in w for w in whys)
    assert [b["text"] for b in store.all("SELECT * FROM beliefs WHERE about = 'bake' ORDER BY id")] == \
        ["Bake likes short answers", "Bake likes dark mode"]
    steward_links = store.links(likes)
    assert len(steward_links) == 2   # the second named the belief by its text
    assert all(l["strength"] == 0 and "not bake's own words" in l["note"] for l in steward_links)
    eps = store.all("SELECT * FROM episodes ORDER BY id")
    assert [e["said_by"] for e in eps] == ["steward", "bake", "steward", "steward"]   # the record decides
    assert json.loads(eps[-1]["entries"]) == [st]   # tied to the steward's line only


# --- recall and the two clocks ---

def seeded(store):
    sid = store.session("s", "live", "s")["id"]
    e = store.add_entry(sid, "2026-05-04T13:07:00Z", "bake", "chat", "starscream is gone backups go to cachyrig")
    ep = store.add_episode(sid, "bake said starscream was decommissioned", "bake", "chat", "correction", [e],
                           "2026-05-04T13:07:00Z")
    b = store.add_belief("starscream is decommissioned", "world", at="2026-05-04T13:07:00Z")
    store.add_link(b, ep, 1, 1.0, "2026-05-04")
    return sid, e, ep, b


def test_recall_finds_by_closeness_and_misses_say_nothing(store, knobs):
    sid, e, ep, b = seeded(store)
    r = recall.recall(store, knobs, "is starscream decommissioned", "2026-08-01T00:00:00Z")
    assert [i.id for i in r.beliefs] == [b] and [i.id for i in r.episodes] == [ep]
    miss = recall.recall(store, knobs, "sister birthday party", "2026-08-01T00:00:00Z")
    assert miss.missed and "don't remember" in recall.for_turn(miss)
    assert store.one("SELECT COUNT(*) AS n FROM beliefs")["n"] == 1   # nothing written about the miss


def test_a_pinned_belief_clears_a_lower_floor(store, knobs):
    sid, e, ep, b = seeded(store)
    knobs["models"]["fake"]["miss_below"] = 0.9
    knobs["models"]["fake"]["pinned_miss_below"] = 0.3
    q = "is starscream back in service"
    assert not recall.recall(store, knobs, q, "2026-08-01T00:00:00Z").beliefs
    store.add_pin(b, "pin", "2026-06-16T14:03:00Z", e, "pin it")
    assert [i.id for i in recall.recall(store, knobs, q, "2026-08-01T00:00:00Z").beliefs] == [b]


def test_newer_words_in_the_scratch_pad_go_first(store, knobs):
    sid, e, ep, b = seeded(store)
    store.run("UPDATE entries SET condensed_at = 'x'")
    store.add_entry(sid, "2026-08-01T00:00:00Z", "bake", "chat", "starscream is back and decommissioned no more")
    r = recall.recall(store, knobs, "starscream decommissioned", "2026-08-02T00:00:00Z")
    assert r.beliefs[0].extra.get("newer_words")


def test_a_dream_reading_memory_is_not_use_a_turn_with_him_is(store, knobs, monkeypatch, tmp_path):
    sid, e, ep, b = seeded(store)
    out = io.StringIO()
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                         "params": {"name": "search", "arguments": {"query": "starscream decommissioned"}}})]
    mcp.serve(store, knobs, "dream", session=sid, at="2026-08-01T00:00:00Z", stdin=io.StringIO("\n".join(lines)), stdout=out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [t["name"] for t in replies[0]["result"]["tools"]] == ["search", "things", "neighbourhood"]
    assert "starscream" in replies[1]["result"]["content"][0]["text"]
    assert store.one("SELECT last_used FROM beliefs WHERE id = ?", b)["last_used"] is None

    calls = []

    def fake_call(store_, knobs_, purpose, prompt, **kw):
        calls.append((purpose, prompt, kw))
        return {"result": "Backups go to cachyrig.", "session_id": "cc-1"}
    monkeypatch.setattr(model, "call", fake_call)
    store.run("UPDATE sessions SET dreamed_to = ? WHERE id = ?", e, sid)   # no idle dream before this turn
    s = store.one("SELECT * FROM sessions WHERE id = ?", sid)
    answer, r = turn.say(store, knobs, s, "where do backups go now that starscream is gone", "bake", "2026-08-02T09:00:00Z")
    assert answer == "Backups go to cachyrig." and "[b%d]" % b in calls[0][1] and calls[0][2]["new_session"]
    assert store.one("SELECT last_used FROM beliefs WHERE id = ?", b)["last_used"] == "2026-08-02T09:00:00+00:00"
    assert store.one("SELECT cc_session FROM sessions WHERE id = ?", sid)["cc_session"] == "cc-1"
    # a turn from someone else does not move the clock
    store.run("UPDATE beliefs SET last_used = NULL")
    s = store.one("SELECT * FROM sessions WHERE id = ?", sid)
    turn.say(store, knobs, s, "starscream decommissioned?", "agent", "2026-08-02T09:01:00Z")
    assert calls[-1][2]["resume"] == "cc-1"
    assert store.one("SELECT last_used FROM beliefs WHERE id = ?", b)["last_used"] is None


# --- the dream's wiring (stand-in model) ---

def test_dream_forks_a_live_session_condenses_and_an_idle_gap_triggers_it(store, knobs, monkeypatch):
    sid = store.session("s", "live", "s")["id"]
    store.run("UPDATE sessions SET cc_session = 'cc-9' WHERE id = ?", sid)
    e = store.add_entry(sid, "2026-07-01T12:00:00Z", "bake", "chat", "We're dropping wake word.")
    seen = []

    def fake_call(store_, knobs_, purpose, prompt, **kw):
        seen.append(kw)
        return {"structured_output": {"facts": [{"fact": "Bake dropped the wake word.", "entries": [f"s{e}"],
                                                 "kind": "correction", "held": [],
                                                 "new_belief": {"belief": "Push to talk only", "about": "bake"}}]}}
    monkeypatch.setattr(model, "call", fake_call)
    assert not dream.due(store, knobs, sid, "2026-07-01T12:05:00Z")
    assert dream.due(store, knobs, sid, "2026-07-01T12:30:00Z")
    out = dream.run(store, knobs, sid, log=lambda *a: None)
    assert seen[0]["resume"] == "cc-9" and seen[0]["fork"] and not seen[0]["persist"] and seen[0]["tools"] == ["search", "things", "neighbourhood"]
    assert out["new"] == 1 and not store.scratch()
    assert store.one("SELECT dreamed_to FROM sessions WHERE id = ?", sid)["dreamed_to"] == e
    assert dream.run(store, knobs, sid, log=lambda *a: None) is None   # nothing new


def test_a_failed_dream_leaves_the_record_undreamed(store, knobs, monkeypatch):
    sid = store.session("s", "x.json", "c1")["id"]
    store.add_entry(sid, "2026-07-01T12:00:00Z", "bake", "chat", "hello")

    def broken(*a, **kw):
        raise model.ModelError("no")
    monkeypatch.setattr(model, "call", broken)
    assert dream.run(store, knobs, sid, log=lambda *a: None) is None
    assert len(store.undreamed(sid)) == 1 and len(store.scratch()) == 1


# --- taking conversations in ---

def test_export_keeps_his_words_and_final_answers(tmp_path, knobs):
    export = [{"uuid": "c1", "name": "a chat", "created_at": "2025-01-02T00:00:00Z", "chat_messages": [
        {"sender": "human", "created_at": "2025-01-02T00:00:00Z", "text": "find it",
         "content": [{"type": "text", "text": "find it"}], "attachments": [{"file_name": "notes.txt", "extracted_content": "secret"}]},
        {"sender": "assistant", "created_at": "2025-01-02T00:00:10Z", "text": "",
         "content": [{"type": "text", "text": "Let me search."}, {"type": "tool_use", "name": "web_search", "input": {}},
                     {"type": "tool_result", "content": [{"type": "text", "text": "junk"}]},
                     {"type": "text", "text": "Found it."}]}]}]
    import zipfile
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("conversations.json", json.dumps(export))
    c = importer.conversations(z, knobs)[0]
    assert [(m["speaker"], m["text"]) for m in c["messages"]] == [("bake", "find it\n[attached: notes.txt]"), ("agent", "Found it.")]


def test_the_made_up_set_is_marked_and_stamped(knobs, store):
    convs = importer.conversations(MADEUP, knobs)
    assert all(c["made_up"] and c["name"].startswith("MADE UP") for c in convs)
    speakers = {m["speaker"] for c in convs for m in c["messages"]}
    assert speakers == {"bake", "steward", "agent", "house"}
    assert any(m["channel"] == "voice" for c in convs for m in c["messages"])
    importer.take(store, knobs, MADEUP, convs[:2], log=lambda *a: None)
    importer.take(store, knobs, MADEUP, convs[:2], log=lambda *a: None)   # again: nothing new
    assert store.one("SELECT COUNT(*) AS n FROM entries")["n"] == sum(len(c["messages"]) for c in convs[:2])


# --- the command line ---

def test_command_line(tmp_path, capsys, monkeypatch):
    base = ["--store", str(tmp_path / "mem"), "--set", "search.model=fake"]
    base += [f"--set=models.fake.{key}={json.dumps(v)}" for key, v in FAKE.items()]
    assert main(base + ["pin", "Starscream is decommissioned for good"]) == 0
    assert "Pinned b1" in capsys.readouterr().out
    assert main(base + ["ask", "starscream decommissioned"]) == 0
    assert "PINNED" in capsys.readouterr().out
    assert main(base + ["history", "b1"]) == 0
    assert "PIN from his message" in capsys.readouterr().out
    assert main(base + ["unpin"]) == 0
    assert "Unpinned b1" in capsys.readouterr().out
    assert main(base + ["show"]) == 0
    assert "beliefs (1)" in capsys.readouterr().out
    assert main(base + ["reset"]) == 0
    assert not list((tmp_path / "mem").glob("memory.sqlite3*"))


# --- round three: strong evidence flips, weak does not ---

def test_play_2s_failure_an_agents_question_does_not_flip_a_mature_belief(knobs):
    corr = standing.strength(knobs, "correction", "bake", "chat")
    held = [link(1, 1, corr, "2026-05-04"), link(2, 1, corr, "2026-05-19")]
    asked = standing.standing(held + [link(3, -1, standing.strength(knobs, "mention", "agent", "chat"), "2026-06-16")], knobs)
    assert asked["direction"] == 1 and asked["flips"] == 0 and not asked["conflict"]
    assert asked["weak_against_since"] == 1 and "too weak to flip" in standing.summary(asked)
    stated = standing.standing(held + [link(3, -1, standing.strength(knobs, "statement", "agent", "chat"), "2026-08-05")], knobs)
    assert stated["direction"] == 1
    corrected = standing.standing(held + [link(3, -1, standing.strength(knobs, "correction", "agent", "chat"), "2026-08-05")],
                                  knobs)
    assert corrected["direction"] == -1 and corrected["conflict"]   # strong, so it flips, tentatively


def test_flip_at_is_the_line(knobs):
    s = lambda kind, who, ch="chat": standing.strength(knobs, kind, who, ch)
    assert standing.strong(knobs, s("statement", "bake")) and standing.strong(knobs, s("decision", "steward"))
    assert standing.strong(knobs, s("decision", "agent"))
    for weak in (s("mention", "bake"), s("statement", "bake", "voice"), s("statement", "steward"),
                 s("statement", "agent"), s("unclear", "bake")):
        assert not standing.strong(knobs, weak)
    knobs["weigh"]["flip_at"] = 0.0
    held = [link(1, 1, 1.0, "2026-05-04"), link(2, -1, 0.12, "2026-06-16")]
    assert standing.standing(held, knobs)["direction"] == -1   # round two's rule, for comparison


def test_cases_lists_what_flips(knobs):
    text = "\n".join(standing.cases(knobs))
    assert "agent mention" in text and "Play 2's failure" in text and "recorded only" in text


# --- opposites ---

def test_opposites_count_against_each_other_only_with_the_knob(store, knobs):
    sid = store.session("s", "live", "s")["id"]
    e = store.add_entry(sid, "2026-07-01T12:00:00Z", "bake", "chat", "x")
    uses = store.add_belief("Bake uses the wake word", "bake")
    ptt = store.add_belief("Push to talk only", "bake")
    for day in ("2026-05-04", "2026-05-19", "2026-06-20"):
        ep = store.add_episode(sid, "uses it", "bake", "chat", "statement", [e], day + "T10:00:00Z")
        store.add_link(uses, ep, 1, 0.6, day)
    for day in ("2026-07-01", "2026-07-08", "2026-07-20"):
        ep = store.add_episode(sid, "push to talk", "bake", "chat", "statement", [e], day + "T10:00:00Z")
        store.add_link(ptt, ep, 1, 0.6, day)
    store.mark_opposites(ptt, uses, "2026-07-01", "dream 1")
    assert store.opposites(uses) == [ptt]
    off = standing.of(store, knobs, uses)
    assert off["direction"] == 1 and off["certainty"] > 0.6   # nothing linked against it directly
    knobs["beliefs"]["opposites"] = True
    on = standing.of(store, knobs, uses)
    assert on["direction"] == -1 and on["occasions"] == 3 and on["certainty"] > 0.6
    assert any(l.get("via") == ptt for l in standing.links_of(store, knobs, uses))


# --- pins in his words ---

def test_a_pin_rewords_the_matched_belief_to_his_words(store, knobs):
    held = store.add_belief("starscream is decommissioned and backups go elsewhere", "world")
    e = entry(store, text="Listen: starscream is decommissioned and backups go elsewhere. That is final.")
    bid, said = pin.pin(store, knobs, "starscream is decommissioned and backups go elsewhere. That is final.", e,
                        final_words="That is final")
    assert bid == held
    assert store.one("SELECT text FROM beliefs WHERE id = ?", held)["text"] == \
        "starscream is decommissioned and backups go elsewhere. That is final."
    assert store.one("SELECT text FROM belief_texts WHERE belief = ?", held)["text"].startswith("starscream is decommissioned")
    assert pin.his_words("Starscream is gone forever (paraphrase)", "Listen: it is over.") == "Listen: it is over."


# --- how and should; the same check ---

def test_the_steward_may_set_how_but_not_should(store, knobs):
    sid = store.session("s", "live", "s")["id"]
    st = store.add_entry(sid, "2026-08-25T09:00:00Z", "steward", "chat", "I'm moving the weekly report to Monday.")
    answer = {"facts": [
        {"fact": "The steward moved the weekly report to Monday.", "entries": [f"s{st}"], "said_by": "steward",
         "kind": "decision", "held": [], "new_belief": {"belief": "The weekly report runs on Monday (the steward's call)",
                                                         "about": "how"}},
        {"fact": "The steward said the dashboard should show the calendar.", "entries": [f"s{st}"], "said_by": "steward",
         "kind": "statement", "held": [], "new_belief": {"belief": "Bake's dashboard should show the calendar", "about": "bake"}}]}
    d = store.start_dream(sid, "fork", "hand", st, st)
    out = dream.apply(store, knobs, d, sid, store.undreamed(sid), answer, "2026-08-25T09:01:00Z", log=lambda *a: None)
    assert out["new"] == 1 and len(out["held_back"]) == 1
    how = store.one("SELECT * FROM beliefs WHERE about = 'how'")
    s = standing.of(store, knobs, how["id"])
    assert s["direction"] == 1 and store.links(how["id"])[0]["strength"] == pytest.approx(0.6)


def test_the_same_check_reuses_a_held_belief_or_marks_an_opposite(store, knobs, monkeypatch):
    sid = store.session("s", "live", "s")["id"]
    e = store.add_entry(sid, "2026-07-08T16:02:00Z", "bake", "chat", "Keep it push to talk.")
    held = store.add_belief("bake wants push to talk only no wake word", "bake")
    store.add_belief("bake uses the hey rook wake word", "bake")
    asked = []

    def fake_call(store_, knobs_, purpose, prompt, **kw):
        asked.append(prompt)
        lines = [l for l in prompt.splitlines() if l[:1].isdigit()]
        verdicts = []
        for line in lines:
            k = int(line.split(".")[0])
            verdicts.append({"pair": k, "verdict": "same" if "push to talk only" in prompt.split(f"{k}. new:")[1].split(
                "\n")[1] else "opposite"})
        return {"structured_output": {"verdicts": verdicts}}
    monkeypatch.setattr(model, "call", fake_call)
    knobs["models"]["fake"]["same_check_from"] = 0.3
    answer = {"facts": [{"fact": "Bake said keep push to talk.", "entries": [f"s{e}"], "said_by": "bake", "kind": "statement",
                         "held": [], "new_belief": {"belief": "bake wants push to talk only no wake word ever", "about": "bake"}}]}
    d = store.start_dream(sid, "fork", "hand", e, e)
    out = dream.apply(store, knobs, d, sid, store.undreamed(sid), answer, "2026-07-08T16:03:00Z", log=lambda *a: None)
    assert asked and out["same"] == 1 and out["new"] == 0
    assert len(store.links(held)) == 1 and store.links(held)[0]["stance"] == 1


# --- one session that compacts ---

def test_an_idle_gap_dreams_then_compacts_the_same_session(store, knobs, monkeypatch):
    sid = store.session("house", "live", "house")["id"]
    store.run("UPDATE sessions SET cc_session = 'cc-1' WHERE id = ?", sid)
    store.add_entry(sid, "2026-05-04T13:00:00Z", "bake", "chat", "hello")
    seen = []

    def fake_call(store_, knobs_, purpose, prompt, **kw):
        seen.append((purpose, prompt, kw))
        if purpose.startswith("dream"):
            return {"structured_output": {"facts": []}}
        return {"result": "ok", "session_id": "cc-1"}
    monkeypatch.setattr(model, "call", fake_call)
    s = store.one("SELECT * FROM sessions WHERE id = ?", sid)
    turn.say(store, knobs, s, "next day", "bake", "2026-05-05T09:00:00Z")
    assert [p for p, _, _ in seen] == ["dream 1", "compact house", "turn house"]
    assert seen[1][1] == "/compact" and seen[1][2]["resume"] == "cc-1" and seen[2][2]["resume"] == "cc-1"
    assert store.one("SELECT COUNT(*) AS n FROM compactions")["n"] == 1
    assert store.one("SELECT recalled FROM turns")["recalled"] is not None


# --- the lighter searches ---

def test_keyword_search_finds_a_belief_by_the_dreams_other_wordings(store, knobs):
    knobs["search"]["model"] = "keyword"
    b = store.add_belief("Bake dropped the wake word; push to talk only", "bake",
                         wordings=["the kitchen speaker no longer listens all the time", "no always-on microphone"])
    store.add_belief("Bake likes dark mode on everything", "bake")
    r = recall.recall(store, knobs, "Is the kitchen speaker still always listening?", "2026-08-01T00:00:00Z")
    assert [i.id for i in r.beliefs] == [b]
    assert recall.recall(store, knobs, "Who is my dentist?", "2026-08-01T00:00:00Z").missed
    # rewording a belief (a pin) re-indexes it
    store.reword(b, "Push to talk, for good", "2026-08-02T00:00:00Z", "test")
    store.run("UPDATE beliefs SET wordings = NULL WHERE id = ?", b)
    assert recall.recall(store, knobs, "Is the kitchen speaker still always listening?", "2026-08-03T00:00:00Z").missed
    assert search.dice("Bake dropped the wake word", "the wake word was dropped") > 0.5


def test_hybrid_recalls_what_either_search_finds(store, knobs):
    knobs["models"]["hybrid"]["static"] = "fake"
    knobs["search"]["model"] = "hybrid"
    by_words = store.add_belief("zstd compresses old logs", "how", wordings=["log files are kept small"])
    r = recall.recall(store, knobs, "How are the log files kept small?", "2026-08-01T00:00:00Z")
    assert by_words in [i.id for i in r.beliefs]
    assert r.beliefs[0].closeness >= 1.0      # the hybrid's floor is 1.0: a multiple of each search's own floor


# --- round four: the graph ---

def graph_store(store, knobs):
    knobs["search"]["model"] = "graph"
    sid = store.session("s", "live", "s")["id"]
    e = store.add_entry(sid, "2026-07-01T12:00:00Z", "bake", "chat", "We're dropping wake word. Push to talk only.")
    ep = store.add_episode(sid, "Bake dropped the wake word", "bake", "chat", "decision", [e], "2026-07-01T12:00:00Z")
    ptt = store.add_belief("Push to talk only", "bake", at="2026-07-01T12:00:00Z")
    store.add_link(ptt, ep, 1, 1.0, "2026-07-01")
    uses = store.add_belief("Bake uses 'hey rook' every morning", "bake", at="2026-05-04T13:00:00Z")
    store.add_link(uses, ep, -1, 1.0, "2026-07-01")
    store.mark_opposites(ptt, uses, "2026-07-01", "dream 1")
    speaker = store.add_thing("kitchen speaker", "2026-05-04")
    wake = store.add_thing("wake word", "2026-05-04")
    store.add_edge("belief", ptt, "about", "thing", wake, "2026-07-01", dream=1)
    store.add_edge("thing", wake, "part_of", "thing", speaker, "2026-07-01", dream=1)
    return sid, e, ptt, uses, speaker, wake


def test_graph_recall_walks_from_a_named_thing_and_brings_opposites(store, knobs):
    sid, e, ptt, uses, speaker, wake = graph_store(store, knobs)
    r = recall.recall(store, knobs, "What wake word am I using?", "2026-08-01T00:00:00Z")
    found = {b.id: b.extra.get("via") for b in r.beliefs}
    assert ptt in found and "about 'wake word'" in found[ptt]
    assert found.get(uses) == f"the opposite of b{ptt}"
    # one step out: the kitchen speaker is related to the wake word
    knobs["models"]["graph"]["hops"] = 1
    r = recall.recall(store, knobs, "Is the kitchen speaker on push to talk?", "2026-08-01T00:00:00Z")
    assert ptt in [b.id for b in r.beliefs]
    knobs["models"]["graph"]["hops"] = 0
    r = recall.recall(store, knobs, "Is the kitchen speaker still always listening?", "2026-08-01T00:00:00Z")
    assert ptt not in [b.id for b in r.beliefs]


def test_a_heal_makes_the_same_wording_hit_next_time(store, knobs):
    sid, e, ptt, uses, speaker, wake = graph_store(store, knobs)
    q = "Is Rook still always listening?"
    assert not recall.recall(store, knobs, q, "2026-08-01T00:00:00Z").beliefs
    m = store.run("INSERT INTO misses (session, entry, at, query, recalled, found) VALUES (?, ?, ?, ?, '[]', ?)",
                  sid, e, "2026-08-01T00:00:00Z", q, json.dumps([f"b{ptt}"]))
    entries = store.undreamed(sid)
    d = store.start_dream(sid, "fork", "idle", entries[0]["id"], entries[-1]["id"])
    answer = {"facts": [], "heals": [{"miss": f"m{m}", "thing": "wake word", "alias": "always listening",
                                     "belief": f"b{ptt}", "why": "the agent found it by searching 'wake word'"}]}
    out = dream.apply(store, knobs, d, sid, entries, answer, "2026-08-01T00:10:00Z", log=lambda *a: None)
    assert out["heals"] == 1 and store.one("SELECT status FROM misses WHERE id = ?", m)["status"] == "healed"
    assert ptt in [b.id for b in recall.recall(store, knobs, q, "2026-08-02T00:00:00Z").beliefs]


def test_the_dream_reuses_a_held_thing_by_name_or_alias(store, knobs):
    sid, e, ptt, uses, speaker, wake = graph_store(store, knobs)
    store.add_alias(wake, "hey rook", "2026-05-04")
    entries = store.undreamed(sid)
    d = store.start_dream(sid, "fork", "idle", entries[0]["id"], entries[-1]["id"])
    answer = {"facts": [], "things": [{"name": "Hey Rook", "aliases": ["the trigger phrase"], "entries": [f"s{e}"]},
                                      {"name": "the porch tablet"}],
              "about": [{"belief": f"b{uses}", "thing": "hey rook"}]}
    out = dream.apply(store, knobs, d, sid, entries, answer, "2026-07-01T13:00:00Z", log=lambda *a: None)
    assert out["things"] == 1 and out["things_reused"] == 1
    assert store.one("SELECT COUNT(*) AS n FROM things")["n"] == 3
    assert store.thing_named("the trigger phrase")["id"] == wake
    assert store.one("SELECT 1 FROM edges WHERE src = ? AND rel = 'about' AND dst = ?", uses, wake)


def test_the_graph_read_path_needs_no_numpy(store, knobs):
    import subprocess
    code = ("import sys; sys.path.insert(0, %r); from rook_memory import knobs, recall; from rook_memory.store import Store;"
            "k = knobs.load(overrides=['search.model=graph']); s = Store(%r);"
            "recall.recall(s, k, 'What wake word am I using?', '2026-08-01T00:00:00Z');"
            "print('numpy' in sys.modules)") % (str(pathlib.Path(__file__).resolve().parent.parent), str(store.folder))
    graph_store(store, knobs)
    assert subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip() == "False"


def test_a_pin_needs_the_words_that_make_it_final_when_the_knob_lists_them(store, knobs):
    knobs["pins"]["final_words"] = ["final", "never", "always", "for good", "not coming back", "pin"]
    e = entry(store, text="Again, Starscream is decommissioned. Stop reaching for Starscream.")
    bid, said = pin.pin(store, knobs, "Starscream is decommissioned", e, final_words="Stop reaching for Starscream")
    assert bid is None and "Not pinned" in said and not store.pinned(1) if bid else True
    e2 = entry(store, text="Never again put the guest network on the main VLAN.", at="2026-08-20T17:05:00Z")
    bid, said = pin.pin(store, knobs, "Never again put the guest network on the main VLAN.", e2, final_words="Never again")
    assert bid and store.pinned(bid)
    assert pin.final(knobs, "it is not coming back", "The old NAS is gone and it is not coming back.") == "not coming back"
    assert pin.final(knobs, "for good", "Starscream is gone.") is None     # not in his message


def test_dream_run_keeps_the_whole_answer(store, knobs, monkeypatch):
    knobs["search"]["model"] = "graph"
    sid = store.session("s", "live", "s")["id"]
    store.run("UPDATE sessions SET cc_session = 'cc-2' WHERE id = ?", sid)
    e = store.add_entry(sid, "2026-05-04T13:07:00Z", "bake", "chat", "Starscream is gone, I decommissioned it.")

    def fake_call(store_, knobs_, purpose, prompt, **kw):
        return {"structured_output": {
            "facts": [{"fact": "Bake said Starscream is gone.", "entries": [f"s{e}"], "said_by": "bake", "kind": "correction",
                       "held": [], "new_belief": {"belief": "Starscream is decommissioned", "about": "world",
                                                  "about_things": ["Starscream"]}}],
            "things": [{"name": "Starscream", "aliases": ["the old server"], "entries": [f"s{e}"]},
                       {"name": "backups", "aliases": ["copies"], "entries": []}],
            "relations": [{"thing": "Starscream", "rel": "related", "other": "backups"}]}}
    monkeypatch.setattr(model, "call", fake_call)
    out = dream.run(store, knobs, sid, log=lambda *a: None)
    assert out["aliases"] == 2 and out["edges"] == 2
    assert recall.recall(store, knobs, "Is the old server back?", "2026-06-01T00:00:00Z").beliefs


# --- Bake's answers of 16:20: agents' corrections ask, exceptions and for-now, changes, pin feedback ---

def _dream(store, knobs, sid, answer, when):
    entries = store.undreamed(sid)
    d = store.start_dream(sid, "fork", "idle", entries[0]["id"], entries[-1]["id"])
    out = dream.apply(store, knobs, d, sid, entries, answer, when, log=lambda *a: None)
    store.run("UPDATE sessions SET dreamed_to = ? WHERE id = ?", entries[-1]["id"], sid)
    return out


def test_an_agents_correction_does_not_flip_the_house_agent_asks_and_his_answer_decides(store, knobs):
    knobs["search"]["model"] = "graph"
    sid = store.session("s", "live", "s")["id"]
    e1 = store.add_entry(sid, "2026-05-04T13:07:00Z", "bake", "chat", "Starscream is gone, I decommissioned it.")
    _dream(store, knobs, sid, {"facts": [{"fact": "Bake said Starscream is gone.", "entries": [f"s{e1}"], "said_by": "bake",
                                          "kind": "correction", "held": [],
                                          "new_belief": {"belief": "Starscream is decommissioned", "about": "world"}}]},
           "2026-05-04T13:10:00Z")
    b = store.one("SELECT id FROM beliefs")["id"]
    e2 = store.add_entry(sid, "2026-08-05T10:00:00Z", "agent", "chat", "[inventory agent] Starscream is back in service.")
    out = _dream(store, knobs, sid, {"facts": [{"fact": "The inventory agent said Starscream is back in service.",
                                                "entries": [f"s{e2}"], "said_by": "agent", "kind": "correction",
                                                "held": [{"belief": f"b{b}", "stance": "against"}], "new_belief": None}]},
                 "2026-08-05T10:10:00Z")
    assert out["asks"] == 1 and standing.of(store, knobs, b)["direction"] == 1      # it stands
    assert store.links(b)[-1]["flips"] == 0
    # his next turn puts the question in front of the house agent, once
    seen = []
    model_call = model.call
    model.call = lambda *a, **kw: seen.append(a[3]) or {"result": "Is Starscream back?", "session_id": "cc"}
    try:
        s = store.one("SELECT * FROM sessions WHERE id = ?", sid)
        turn.say(store, knobs, s, "Morning.", "bake", "2026-08-12T18:00:00Z")
        turn.say(store, knobs, s, "Anything else?", "bake", "2026-08-12T18:01:00Z")
    finally:
        model.call = model_call
    assert "To ask Bake" in seen[0] and "To ask Bake" not in seen[1]
    # his no counts for the belief and closes the ask
    e3 = store.add_entry(sid, "2026-08-12T18:02:00Z", "bake", "chat", "No, Starscream is still gone.")
    out = _dream(store, knobs, sid, {"facts": [{"fact": "Bake said Starscream is still gone.", "entries": [f"s{e3}"],
                                                "said_by": "bake", "kind": "correction",
                                                "held": [{"belief": f"b{b}", "stance": "supports"}], "new_belief": None}]},
                 "2026-08-12T18:10:00Z")
    assert out["answers"] == 1 and store.one("SELECT answer FROM asks")["answer"] == "kept"


def test_a_how_decision_by_an_agent_still_flips_a_how_belief(store, knobs):
    sid = store.session("s", "live", "s")["id"]
    e1 = store.add_entry(sid, "2026-05-04T13:00:00Z", "agent", "chat", "[backup agent] backups at 02:00")
    _dream(store, knobs, sid, {"facts": [{"fact": "The backup agent set backups at 02:00.", "entries": [f"s{e1}"],
                                          "said_by": "agent", "kind": "decision", "held": [],
                                          "new_belief": {"belief": "Backups run at 02:00", "about": "how"}}]}, "2026-05-04T13:01:00Z")
    b = store.one("SELECT id FROM beliefs")["id"]
    e2 = store.add_entry(sid, "2026-06-02T15:10:00Z", "agent", "chat", "[backup agent] moved backups to 03:00")
    out = _dream(store, knobs, sid, {"facts": [{"fact": "The backup agent moved backups to 03:00.", "entries": [f"s{e2}"],
                                                "said_by": "agent", "kind": "decision", "held": [], "new_belief": None}],
                                     "changes": [{"belief": f"b{b}", "now": "Backups run at 03:00 (the backup agent moved them)",
                                                  "entries": [f"s{e2}"]}]}, "2026-06-02T15:11:00Z")
    assert out["changes"] == 1 and out["asks"] == 0
    assert store.one("SELECT text FROM beliefs WHERE id = ?", b)["text"].startswith("Backups run at 03:00")
    assert store.one("SELECT text FROM belief_texts WHERE belief = ?", b)["text"] == "Backups run at 02:00"
    assert store.one("SELECT COUNT(*) AS n FROM beliefs")["n"] == 1      # one belief that changed, not two opposites


def test_an_exception_does_not_count_against_the_standing_belief_and_comes_back_with_it(store, knobs):
    knobs["search"]["model"] = "graph"
    sid = store.session("s", "live", "s")["id"]
    e1 = store.add_entry(sid, "2026-05-19T19:00:00Z", "bake", "chat", "I like dark mode on everything.")
    _dream(store, knobs, sid, {"facts": [{"fact": "Bake likes dark mode on everything.", "entries": [f"s{e1}"],
                                          "said_by": "bake", "kind": "statement", "held": [],
                                          "new_belief": {"belief": "Bake likes dark mode on everything", "about": "bake",
                                                         "about_things": ["dark mode"]}}]}, "2026-05-19T19:10:00Z")
    dark = store.one("SELECT id FROM beliefs")["id"]
    e2 = store.add_entry(sid, "2026-08-20T17:04:00Z", "bake", "chat", "Let's try light mode on the kitchen tablet for a bit.")
    _dream(store, knobs, sid, {"facts": [{"fact": "Bake asked for light mode on the kitchen tablet for a bit.",
                                          "entries": [f"s{e2}"], "said_by": "bake", "kind": "decision",
                                          "held": [{"belief": f"b{dark}", "stance": "against"}],
                                          "new_belief": {"belief": "Light mode on the kitchen tablet for a bit", "about": "bake",
                                                         "about_things": ["kitchen tablet", "dark mode"],
                                                         "for_now_beside": f"b{dark}"}}]}, "2026-08-20T17:10:00Z")
    tablet = store.one("SELECT id FROM beliefs WHERE id != ?", dark)["id"]
    assert standing.of(store, knobs, dark)["direction"] == 1 and not any(l["stance"] < 0 for l in store.links(dark))
    r = recall.recall(store, knobs, "What theme is the kitchen tablet using?", "2026-08-21T00:00:00Z")
    ids = [b.id for b in r.beliefs]
    assert ids[:2] == [tablet, dark]
    # settled as lapsed: the edge ends, the standing belief is as it was
    store.add_entry(sid, "2026-09-01T10:00:00Z", "bake", "chat", "Put the tablet back to dark.")
    _dream(store, knobs, sid, {"facts": [], "settle": [{"belief": f"b{tablet}", "how": "lapsed"}]}, "2026-09-01T10:10:00Z")
    assert store.one("SELECT ended FROM edges WHERE rel = 'for_now'")["ended"] == "lapsed"


def test_a_pin_is_never_silent(store, knobs, monkeypatch):
    sid = store.session("s", "live", "s")["id"]
    s = store.one("SELECT * FROM sessions WHERE id = ?", sid)

    def fake_call(store_, knobs_, purpose, prompt, **kw):
        e = store_.one("SELECT MAX(id) AS id FROM entries")["id"]
        pin.pin(store_, knobs_, "The old NAS is gone and it is not coming back.", e)
        return {"result": "Understood.", "session_id": "cc"}
    monkeypatch.setattr(model, "call", fake_call)
    answer, _ = turn.say(store, knobs, s, "The old NAS is gone and it is not coming back.", "bake", "2026-08-20T17:02:00Z")
    assert answer.endswith('(Pinned: "The old NAS is gone and it is not coming back.")')
    assert store.one("SELECT pin_note FROM turns")["pin_note"] == "added"
