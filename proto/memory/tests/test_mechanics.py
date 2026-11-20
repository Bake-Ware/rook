"""The plain mechanics: the store, scoring, supersedence, reading sessions, condensing, and the dream
pass's wiring (with a stand-in for the model). Nothing here tests what a model writes."""

import json
import sys
import pathlib

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from rook_memory import dream, ingest, knobs as knobs_mod, model, recall, transcript  # noqa: E402
from rook_memory.__main__ import main  # noqa: E402
from rook_memory.store import Store  # noqa: E402


@pytest.fixture
def knobs():
    return knobs_mod.load()


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "mem")
    yield s
    s.close()


def session_file(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def user(text, at):
    return {"type": "user", "timestamp": at, "message": {"role": "user", "content": text}}


def agent(text, at, tool=None):
    content = [{"type": "text", "text": text}]
    if tool:
        content.append({"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": tool}})
    return {"type": "assistant", "timestamp": at, "message": {"role": "assistant", "content": content}}


def result(text, at):
    return {"type": "user", "timestamp": at,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]}}


def test_a_session_reads_into_entries_with_their_lines(tmp_path, knobs):
    f = session_file(tmp_path / "s.jsonl", [
        {"type": "mode"},
        user("Port the house agent.", "2026-11-19T10:00:00Z"),
        user("<system-reminder>noise</system-reminder>", "2026-11-19T10:00:01Z"),
        agent("Reading the brief.", "2026-11-19T10:00:02Z", tool="cat brief.md"),
        result("x" * 1000, "2026-11-19T10:00:03Z"),
        {"type": "user", "isMeta": True, "timestamp": "2026-11-19T10:00:04Z", "message": {"content": "meta"}},
    ])
    entries = transcript.read(f, knobs)
    assert [(e.line, e.role) for e in entries] == [(2, "user"), (4, "agent"), (4, "tool"), (5, "result")]
    assert entries[0].text == "Port the house agent."
    assert entries[2].text.startswith('Bash {"command": "cat brief.md"}')
    assert entries[3].text.startswith("x" * knobs["ingest"]["tool_result_chars"] + " [... 600 more characters]")
    assert [e.line for e in transcript.read(f, knobs, after_line=4)] == [5]
    runs = list(transcript.chunks(entries, 60))
    assert sum(len(r) for r in runs) == len(entries) and all(runs)


def test_a_superseded_belief_is_kept_marked_and_linked(store):
    b1, old = store.add_belief("Bake wants a wake word.", 0.6, 5)
    assert old is None
    b2, old = store.add_belief("Bake wants no wake word anywhere.", 0.9, 8, supersedes=b1, how="contradicts")
    assert old == b1
    row = store.one("SELECT * FROM beliefs WHERE id = ?", b1)
    assert row["text"] == "Bake wants a wake word." and row["superseded_by"] == b2 and row["how"] == "contradicts"
    assert [b["id"] for b in store.current_beliefs()] == [b2]
    # Superseding a belief that was already superseded supersedes the one standing in its place.
    b3, old = store.add_belief("Bake wants the watch as the first trigger, no wake word.", 0.9, 8,
                               supersedes=b1, how="refines")
    assert old == b2 and store.head(b1) == b3
    assert [b["id"] for b in store.current_beliefs()] == [b3]
    assert store.one("SELECT COUNT(*) AS n FROM beliefs")["n"] == 3
    # Naming a belief that does not exist supersedes nothing.
    b4, old = store.add_belief("Unrelated.", 0.5, 3, supersedes=999)
    assert old is None


def test_support_points_at_episodes_and_beliefs(store):
    sid = store.session("/tmp/x.jsonl")["id"]
    e = store.add_episode(sid, 10, 20, "2026-11-19T10:00:00Z", 7, "Bake chose sqlite.")
    b, _ = store.add_belief("Bake likes small stores.", 0.7, 6)
    c, _ = store.add_belief("Keep it small.", 0.8, 6, because=[("episode", e), ("belief", b), ("episode", e)])
    assert [(s["kind"], s["ref"]) for s in store.support_of(c)] == [("episode", e), ("belief", b)]


def test_scoring_adds_scaled_recency_importance_and_relevance(knobs):
    k = json.loads(json.dumps(knobs))
    at = "2026-11-20T00:00:00Z"
    items = [
        recall.Item("episode", 1, "the wizard installs the beta", "2026-11-19T23:00:00Z", 2),
        recall.Item("episode", 2, "the dock shows the house agent", "2026-11-10T00:00:00Z", 9),
        recall.Item("belief", 3, "Bake wants the wizard to install workers", "2026-11-15T00:00:00Z", 5,
                    {"confidence": 0.8}),
    ]
    ranked = recall.rank(items, "How does the wizard install things?", k, at)
    by = {i.id: i for i in ranked}
    assert by[1].scaled["recency"] == 1.0 and by[2].scaled["recency"] == 0.0
    assert by[2].scaled["importance"] == 1.0 and by[1].scaled["importance"] == 0.0
    assert by[2].scaled["relevance"] == 0.0 and by[2].matched == []
    assert "wizard" in by[1].matched and "install" not in by[1].matched  # words match exactly, no stemming
    assert by[1].recency == pytest.approx(0.995 ** 1)
    for i in ranked:
        assert i.score == pytest.approx(sum(i.scaled.values()))
    assert ranked[0].id == 3 and by[3].matched == ["install", "wizard"]  # the only one with both words
    # Turning a weight changes the order: importance alone puts the dock first.
    k["recall"].update(weight_recency=0, weight_relevance=0)
    assert recall.rank(items, "wizard", k, at)[0].id == 2


def test_words_keep_dotted_names_whole_and_split():
    assert recall.words("Call agent.chat.send on the hub") == ["call", "agent.chat.send", "agent", "chat", "send", "hub"]


def test_condensing_keeps_the_most_recent_sessions_scratch(store, knobs):
    E = transcript.Entry
    for n, day in ((1, "10"), (2, "12"), (3, "11")):
        sid = store.session(f"/tmp/s{n}.jsonl")["id"]
        store.add_scratch(sid, [E(1, f"2026-11-{day}T00:00:00Z", "user", f"hello {n}"),
                                E(2, f"2026-11-{day}T01:00:00Z", "agent", f"hi {n}")])
    assert store.condense_scratch(1) == 4
    left = store.q("SELECT DISTINCT session FROM scratch")
    assert [r["session"] for r in left] == [2]
    assert store.one("SELECT scratch_condensed_at FROM sessions WHERE id = 1")["scratch_condensed_at"]
    assert store.one("SELECT scratch_condensed_at FROM sessions WHERE id = 2")["scratch_condensed_at"] is None


class FakeModel:
    """Stands in for `claude -p`: answers by what the call is for, and records the prompts."""

    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    def __call__(self, store, knobs, purpose, model_name, prompt, schema=None):
        self.prompts.append((purpose, prompt))
        store.add_call(purpose, model_name, 0.1, 0.001, 100, 10, True)
        answer = self.answers(purpose, prompt)
        if isinstance(answer, Exception):
            raise answer
        return answer


def test_take_dream_and_supersede_end_to_end(tmp_path, store, knobs, monkeypatch, capsys):
    f = session_file(tmp_path / "s.jsonl", [
        user("Bake: no wake word anywhere.", "2026-11-19T10:00:00Z"),
        agent("Understood, the watch triggers.", "2026-11-19T10:01:00Z"),
    ])
    old, _ = store.add_belief("Bake wants a wake word.", 0.5, 5, at="2026-11-01T00:00:00Z")

    def answers(purpose, prompt):
        if purpose.startswith("episodes"):
            assert "L1 2026-11-19 10:00 user: Bake: no wake word anywhere." in prompt
            return {"episodes": [{"what": "Bake said no wake word anywhere.", "importance": 9, "first_line": 1, "last_line": 1},
                                 {"what": "", "importance": 3},
                                 {"what": "The agent agreed.", "importance": "x", "first_line": 99}]}
        if purpose.endswith("questions"):
            return {"questions": ["What does Bake want for voice triggers?"]}
        assert f"[b{old}] (belief, confidence 0.50) Bake wants a wake word." in prompt
        return {"beliefs": [{"belief": "Bake wants no wake word; the watch triggers.", "because": ["e1", "e2", "e77", "b9"],
                             "confidence": 0.9, "importance": 9, "supersedes": f"b{old}", "how": "contradicts"}]}

    fake = FakeModel(answers)
    monkeypatch.setattr(model, "ask", fake)
    made = ingest.take(store, knobs, f, log=lambda *_: None)
    assert len(made) == 2
    eps = store.q("SELECT * FROM episodes ORDER BY id")
    assert (eps[0]["first_line"], eps[0]["last_line"], eps[0]["importance"]) == (1, 1, 9)
    assert (eps[1]["first_line"], eps[1]["last_line"], eps[1]["importance"]) == (2, 2, 5)  # clamped, defaulted
    assert eps[0]["happened_at"] == eps[0]["last_read"] == "2026-11-19T10:00:00Z"
    assert store.undreamed_importance() == 14 and not dream.due(store, knobs)
    assert ingest.take(store, knobs, f, log=lambda *_: None) == []  # nothing new after the lines taken

    got = dream.run(store, knobs, log=lambda *_: None)
    assert got["made"] == 1 and got["superseded"] == 1
    new = store.current_beliefs()[0]
    assert new["text"] == "Bake wants no wake word; the watch triggers." and new["confidence"] == 0.9
    assert [(s["kind"], s["ref"]) for s in store.support_of(new["id"])] == [("episode", 1), ("episode", 2)]
    assert store.one("SELECT superseded_by, how FROM beliefs WHERE id = ?", old)[0:2] == (new["id"], "contradicts")
    assert store.undreamed() == []
    # The only session's scratch pad is the most recent one, so it stays.
    assert store.one("SELECT COUNT(*) AS n FROM scratch")["n"] == 2


def test_a_failed_question_call_leaves_the_episodes_for_the_next_dream(store, knobs, monkeypatch):
    sid = store.session("/tmp/x.jsonl")["id"]
    store.add_episode(sid, 1, 1, "2026-11-19T10:00:00Z", 7, "Something happened.")
    monkeypatch.setattr(model, "ask", FakeModel(lambda p, _: model.ModelError("no JSON")))
    assert dream.run(store, knobs, log=lambda *_: None)["made"] == 0
    assert len(store.undreamed()) == 1


def test_extract_finds_the_json_in_a_reply():
    assert model.extract('Here:\n```json\n{"a": [1, {"b": 2}]}\n```') == {"a": [1, {"b": 2}]}
    with pytest.raises(model.ModelError):
        model.extract("no json here")


def test_the_command_line_asks_shows_and_resets(tmp_path, capsys):
    folder = tmp_path / "mem"
    s = Store(folder)
    sid = s.session("/tmp/x.jsonl")["id"]
    s.add_scratch(sid, [transcript.Entry(3, "2026-11-19T10:00:00Z", "user", "the wizard takes 0.3.0")])
    e = s.add_episode(sid, 3, 3, "2026-11-19T10:00:00Z", 6, "The wizard takes agents version 0.3.0.")
    s.add_belief("Versions must rise for the beta to update.", 0.8, 7, because=[("episode", e)])
    s.close()
    main(["--store", str(folder), "ask", "which version does the wizard take?", "--at", "2026-11-19T12:00:00Z"])
    out = capsys.readouterr().out
    assert "[e1] episode" in out and "[b1] belief, confidence 0.80" in out and "[s1]" in out
    assert "on: version, wizard" in out
    main(["--store", str(folder), "show"])
    out = capsys.readouterr().out
    assert "== beliefs (1 standing, 0 superseded)" in out and "[e1] The wizard takes" in out
    main(["--store", str(folder), "reset"])
    assert not (folder / "memory.sqlite3").exists()
