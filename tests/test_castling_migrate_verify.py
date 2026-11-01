"""The cutover's lossless check: migrate carries chat rooms, huddles, jobs and
change records, archives the whole copy, and ``--verify`` accounts for every
0.4 artifact (castling-switchover-decisions, "Fly it"). Also the read-only
smoke checker (castling/switchover/smoke.py)."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from castling.switchover.migrate import archive, migrate, verify
from test_castling_migrate import NOW, REPO, SECRET, _db, _hashes, make_04, make_05

CHAT = """CREATE TABLE rooms (id TEXT PRIMARY KEY, title TEXT, created REAL, last_activity REAL, participants TEXT);
CREATE TABLE messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, room_id TEXT, ts REAL, sender TEXT, text TEXT,
    mentions TEXT, expects_reply INTEGER, quiet INTEGER);
CREATE TABLE reads (identity TEXT, room_id TEXT, last_read_seq INTEGER, PRIMARY KEY (identity, room_id));
CREATE TABLE huddles(room TEXT PRIMARY KEY, scope TEXT NOT NULL, ref TEXT NOT NULL, band TEXT, title TEXT NOT NULL,
    created REAL NOT NULL, creator TEXT NOT NULL, roles TEXT, archived REAL);
CREATE TABLE huddle_posts(seq INTEGER PRIMARY KEY, room TEXT NOT NULL, sender TEXT NOT NULL, kind TEXT NOT NULL,
    data TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE huddle_questions(seq INTEGER PRIMARY KEY, room TEXT NOT NULL, asker TEXT NOT NULL, text TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'open', asked REAL NOT NULL, answer TEXT,
    answered_by TEXT, answered_label TEXT, answered REAL, answer_seq INTEGER, wake TEXT);
CREATE TABLE huddle_motions(id TEXT PRIMARY KEY, room TEXT);"""
JOBS = """CREATE TABLE jobs (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, owner TEXT NOT NULL,
    owner_info TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1, paused_reason TEXT,
    definition TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL, updated REAL NOT NULL,
    updated_by TEXT, run_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE triggers (job_id TEXT NOT NULL, idx INTEGER NOT NULL, kind TEXT NOT NULL, next_at REAL,
    done INTEGER NOT NULL DEFAULT 0, anchor TEXT, PRIMARY KEY (job_id, idx));
CREATE TABLE runs (id TEXT PRIMARY KEY, job_id TEXT, state TEXT);"""
CHANGES = """CREATE TABLE changes(id TEXT PRIMARY KEY, kind TEXT NOT NULL, category TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL, author TEXT NOT NULL, record TEXT NOT NULL, log TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT '', step INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
    ended REAL)"""
DEFINITION = {"name": "nightly-audit", "description": "Audit the LXCs.", "triggers": [{"kind": "cron", "expr": "30 23 * * *"}],
              "entry": "collect", "steps": {"collect": {"kind": "cap", "cap": "shell.exec", "worker": "soundwave"}}}


def make_full_04(root: Path) -> Path:
    """make_04's copy plus 0.4 chat, jobs and change stores (live layout)."""
    d = make_04(root)
    _db(d / "chat.db", CHAT, {
        "rooms": [{"id": "room1", "title": "General", "created": NOW, "participants": '["user:u1"]'},
                  {"id": "hud1", "title": "", "created": NOW + 1, "participants": "[]"}],
        "messages": [{"room_id": "room1", "ts": NOW + 2, "sender": "user:u1", "text": "hi there",
                      "mentions": '["agent:x"]', "expects_reply": 1},
                     {"room_id": "room1", "ts": NOW + 3, "sender": "agent:x", "text": "hello", "mentions": "[]"},
                     {"room_id": "hud1", "ts": NOW + 4, "sender": "agent:x", "text": "status soon", "mentions": "[]"}],
        "reads": [{"identity": "user:u1", "room_id": "room1", "last_read_seq": 2}],
        "huddles": [{"room": "hud1", "scope": "project", "ref": "p_x", "title": "Huddle: lab", "created": NOW + 1,
                     "creator": "agent:x", "roles": '["qa"]', "archived": NOW + 9}],
        "huddle_posts": [{"seq": 10, "room": "hud1", "sender": "agent:x", "kind": "status",
                          "data": '{"state": "done", "working_on": "the lab"}', "ts": NOW + 5},
                         {"seq": 11, "room": "hud1", "sender": "agent:x", "kind": "question",
                          "data": '{"options": ["Yes", "No"]}', "ts": NOW + 6}],
        "huddle_questions": [{"seq": 11, "room": "hud1", "asker": "agent:x", "text": "Ship it?",
                              "data": '{"options": ["Yes", "No"]}', "state": "answered", "asked": NOW + 6,
                              "answer": "Yes", "answered": NOW + 7},
                             {"seq": 12, "room": "hud1", "asker": "agent:x", "text": "Unposted ask?", "asked": NOW + 8}],
        "huddle_motions": [{"id": "mo1", "room": "hud1"}]})
    (d / "plugins" / "job").mkdir(parents=True)
    _db(d / "plugins" / "job" / "jobs.db", JOBS, {
        "jobs": [{"id": "j_1", "name": "nightly-audit", "owner": "token:x", "enabled": 1,
                  "definition": json.dumps(DEFINITION), "created": NOW, "updated": NOW},
                 {"id": "j_2", "name": "Odd Name!", "owner": "token:x", "enabled": 0, "paused_reason": "disabled",
                  "definition": '{"name": "Odd Name!"}', "created": NOW + 1, "updated": NOW + 1}],
        "triggers": [{"job_id": "j_1", "idx": 0, "kind": "cron", "next_at": NOW + 99}],
        "runs": [{"id": "r1", "job_id": "j_1", "state": "ok"}]})
    (d / "plugins" / "change").mkdir(parents=True)
    _db(d / "plugins" / "change" / "changes.db", CHANGES, {"changes": [
        {"id": "earthy-fiddle", "kind": "standard", "category": "config", "state": "kept", "author": "Claude web",
         "record": json.dumps({"what": "Rotate the log", "why": "disk", "machines": ["nas"], "plan": "logrotate -f",
                               "tests": ["df -h"], "rollback": "nothing to undo", "task": None}),
         "log": "[]", "created": NOW, "updated": NOW}]})
    (d / "plugins" / "home").mkdir(parents=True)
    (d / "plugins" / "home" / "playbook.md").write_text("# House playbook\n")
    return d


@pytest.fixture
def full04(tmp_path):
    return make_full_04(tmp_path)


def _kinds(r: dict) -> dict:
    return {k["kind"]: k for k in r["kinds"]}


def test_apply_then_verify_accounts_for_everything(tmp_path, full04):
    cfg = make_05(tmp_path)
    before = _hashes(full04)
    r = migrate(full04, cfg, apply=True)
    assert _hashes(full04) == before
    assert r["rooms"] == {"rooms": 1, "huddles": 1, "messages": 3, "posts": 3, "skipped": 0}
    assert r["jobs"]["jobs"] == 2 and r["jobs"]["renamed"] == [{"from": "Odd Name!", "to": "odd-name"}]
    assert r["changes"]["changes"] == 1
    data = cfg.parent / "data"
    # the archive: every file but the vault key, byte for byte, read-only
    arch = data / "archive" / "0.4"
    assert not (arch / "vault.key").exists()
    for rel in before:
        if rel != "vault.key":
            assert (arch / rel).is_file() and archive.sha256(arch / rel) == archive.sha256(full04 / rel), rel
            assert stat.S_IMODE((arch / rel).stat().st_mode) == 0o444
    assert set(json.loads((arch / "MANIFEST.json").read_text())) == set(before) - {"vault.key"}
    # streams: the room keeps its texts, mentions and times; the huddle is closed with its record
    ev = sqlite3.connect(data / "stream-events.db")
    streams = {json.loads(a)["from04"]: (sid, kind, closed) for sid, kind, a, closed
               in ev.execute("SELECT id, kind, attrs, closed FROM streams")}
    assert streams["room1"][1] == "room" and streams["room1"][2] is None
    assert streams["hud1"][1] == "huddle" and streams["hud1"][2] == NOW + 9
    rows = ev.execute("SELECT at, type, by, body FROM events WHERE stream=? ORDER BY seq",
                      (streams["room1"][0],)).fetchall()
    assert [(at, by, json.loads(b)["text"]) for at, _, by, b in rows] == [
        (NOW + 2, "user:u1", "hi there"), (NOW + 3, "agent:x", "hello")]
    assert json.loads(rows[0][3])["mentions"] == ["agent:x"]
    posts = [json.loads(b) for (b,) in ev.execute("SELECT body FROM events WHERE stream=? AND type='status'",
                                                    (streams["hud1"][0],))]
    assert posts[-1] == {"state": "closed"}                                           # closed at its 0.4 time
    assert [(p["kind04"], p["text"]) for p in posts[:-1]] == [
        ("status", "the lab"), ("question", "Ship it?"), ("question", "Unposted ask?")]
    assert posts[1]["data04"]["answer"] == "Yes"
    rec = sqlite3.connect(data / "records.db")
    rec.row_factory = sqlite3.Row
    h = rec.execute('SELECT * FROM "r:huddle.huddle"').fetchone()
    assert (h["title"], h["state"], h["stream"], json.loads(h["roles"])) == (
        "Huddle: lab", "closed", streams["hud1"][0], ["qa"])
    jobs = {j["name"]: j for j in rec.execute('SELECT * FROM "r:job.job"')}
    j = jobs["nightly-audit"]
    assert j["enabled"] in (0, False, "false") and json.loads(j["trigger"]) == {"manual": True}
    assert json.loads(j["vars"])["definition04"] == DEFINITION and j["person"] == "bake"
    c = rec.execute('SELECT * FROM "r:change.change"').fetchone()
    assert (c["what"], c["why"], json.loads(c["workers"])) == ("Rotate the log", "disk", ["nas"])
    assert "kept" in c["outcome"] and c["created"] == NOW
    rec.close()
    ev.close()

    v = verify.verify(full04, cfg)
    assert v["ok"], verify.render(v)
    k = _kinds(v)
    assert k["chat messages -> text events"]["moved"] == 3
    assert k["huddle posts and questions -> status events"]["moved"] == 3
    assert k["merge votes (motions, rounds, ballots, artifacts, cards)"]["left"] == 1
    assert k["jobs (disabled, 0.4 definition kept)"]["moved"] == 2
    assert k["job runs and history"]["left"] == 1
    assert k["secrets (names + value hashes)"]["moved"] == 2
    assert k["files (whole copy, byte for byte in archive/0.4)"]["left"] == 1         # vault.key
    assert all(x["why"] for x in v["kinds"] if x["left"])                              # every left item says why
    text = verify.render(v)
    assert "VERIFIED" in text and SECRET not in text and SECRET not in json.dumps(v)

    again = migrate(full04, cfg, apply=True)                                           # a re-run adds nothing
    assert again["rooms"]["skipped"] == 2 and again["jobs"]["skipped"] == 2 and again["changes"]["skipped"] == 1
    assert again["archive"]["written"] == 0
    assert verify.verify(full04, cfg)["ok"]


def test_verify_finds_what_did_not_survive(tmp_path, full04):
    cfg = make_05(tmp_path)
    migrate(full04, cfg, apply=True)
    data = cfg.parent / "data"
    ev = sqlite3.connect(data / "stream-events.db")
    ev.execute("DELETE FROM events WHERE type='text' AND by='agent:x' AND stream IN "
               "(SELECT id FROM streams WHERE kind='room')")
    ev.commit()
    ev.close()
    playbook = data / "archive" / "0.4" / "plugins" / "home" / "playbook.md"
    playbook.chmod(0o644)
    playbook.write_text("changed\n")
    from rook.core2.hub.vault import Vault
    Vault(data).unlock().set("pikvm", "a-different-value", by="test")
    rec = sqlite3.connect(data / "records.db")
    rec.execute('DELETE FROM "r:job.job" WHERE name=?', ("odd-name",))
    rec.commit()
    rec.close()
    v = verify.verify(full04, cfg)
    k = _kinds(v)
    assert not v["ok"]
    assert k["chat messages -> text events"]["missing"] == 1
    assert k["files (whole copy, byte for byte in archive/0.4)"]["examples"] == ["plugins/home/playbook.md"]
    assert k["secrets (names + value hashes)"]["examples"] == ["pikvm"]
    assert k["jobs (disabled, 0.4 definition kept)"]["examples"] == ["Odd Name!"]
    out = verify.render(v)
    assert "NOT VERIFIED" in out and "a-different-value" not in out and "kvm-pass-123" not in out


def test_an_unknown_04_table_fails_verify(tmp_path, full04):
    db = sqlite3.connect(full04 / "chat.db")
    db.execute("CREATE TABLE new_thing (x)")
    db.commit()
    db.close()
    cfg = make_05(tmp_path)
    migrate(full04, cfg, apply=True)
    v = verify.verify(full04, cfg)
    assert not v["ok"] and _kinds(v)["unknown 0.4 tables"]["examples"] == ["chat.db:new_thing"]


def test_verify_before_the_move_finds_everything_missing(tmp_path, full04):
    cfg = make_05(tmp_path)
    migrate(full04, cfg)                                                   # a dry run writes nothing
    v = verify.verify(full04, cfg)
    assert not v["ok"] and _kinds(v)["chat rooms and huddles -> streams"]["missing"] == 2
    assert _kinds(v)["secrets (names + value hashes)"]["missing"] == 2
    assert not (cfg.parent / "data" / "vault.db").exists()                    # verify wrote nothing


def test_verify_command_line(tmp_path, full04):
    cfg = make_05(tmp_path)
    run = [sys.executable, "-m", "castling.switchover.migrate", "--from", str(full04), "--to", str(cfg)]
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    p = subprocess.run(run + ["--verify"], capture_output=True, text=True, timeout=120, cwd=REPO, env=env)
    assert p.returncode == 1 and "NOT VERIFIED" in p.stdout
    p = subprocess.run(run + ["--apply"], capture_output=True, text=True, timeout=120, cwd=REPO, env=env)
    assert p.returncode == 0, p.stderr
    p = subprocess.run(run + ["--verify", "--json"], capture_output=True, text=True, timeout=120, cwd=REPO, env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout)["ok"] is True and SECRET not in p.stdout
    p = subprocess.run(run + ["--verify", "--apply"], capture_output=True, text=True, timeout=120, cwd=REPO, env=env)
    assert p.returncode == 2


# -- the smoke checker -------------------------------------------------------------------------

def _smoke():
    spec = importlib.util.spec_from_file_location("switchover_smoke", REPO / "castling/switchover/smoke.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeDoor:
    """A hub door that pages like the real one: record lists by offset (active
    and archived apart), stream.list by ``next``, stream.read by cursor."""

    def __init__(self, records: dict, workers: list, streams=(), events=None, archived=None):
        self.records, self.workers, self.calls = records, workers, []
        self.archived = archived or {}
        self.streams, self.events = list(streams), events or {}

    def request(self, method, path, body=None):
        return {"workers": self.workers}

    def call(self, cap, args, worker="rook"):
        self.calls.append((worker, cap, args))
        if cap == "worker.ping":
            return {"ok": worker in [w["name"] for w in self.workers]}
        if cap == "secret.list":
            return {"ok": True, "result": {"secrets": [{"name": f"s{i}"} for i in range(self.records.get(cap, 0))]}}
        if cap == "stream.list":
            rows = [st for st in self.streams if st["kind"] == args["kind"]]
            o, n = args.get("offset", 0), args["limit"]
            return {"ok": True, "result": {"streams": rows[o:o + n], "next": o + n if o + n < len(rows) else None}}
        if cap == "stream.read":
            evs = [e for e in self.events.get(args["stream"], ()) if not args.get("types") or e["type"] in args["types"]]
            evs = [e for e in evs if e["seq"] > args.get("after", 0)][:args.get("limit", 200)]
            return {"ok": True, "result": {"events": evs, "next": evs[-1]["seq"] if evs else args.get("after", 0)}}
        total = (self.archived if args.get("archived") else self.records).get(cap, 0)
        o, n = args.get("offset", 0), args.get("limit", 50)
        return {"ok": True, "result": [{"id": f"r{i}"} for i in range(o, min(total, o + n))]}


def test_smoke_reads_every_moved_store_in_full():
    mod = _smoke()
    streams = [{"id": f"room{i}", "kind": "room", "title": "Room"} for i in range(250)] + \
              [{"id": "h1", "kind": "huddle", "title": "Huddle"}]
    events = {"room0": [{"seq": i, "type": "text"} for i in range(1, 1500)],
              "h1": [{"seq": 1, "type": "status"}, {"seq": 2, "type": "text"}]}
    moved = {"knowledge pages": 1203, "tasks": 3, "change records": 4, "secrets (names + value hashes)": 2,
             "chat rooms and huddles -> streams": 251, "chat messages -> text events": 1500,
             "huddle posts and questions -> status events": 1}
    door = FakeDoor({"knowledge.list": 1000, "task.list": 3, "change.list": 3, "secret.list": 2},
                    [{"name": "cachyrig", "online": True}], streams, events, archived={"knowledge.list": 203})
    res = mod.run(door, ["cachyrig"], moved)
    assert any(ok and line.startswith("worker cachyrig: online, ping ok") for ok, line in res)
    failed = [line for ok, line in res if not ok]
    assert failed == ["change.list (all pages, active + archived): 3 (verify moved 4)"], failed
    assert any(ok and line == "knowledge.list (all pages, active + archived): 1203 (verify moved 1203)"
               for ok, line in res)
    assert any(ok and line.startswith("rooms + huddles as streams: 251") for ok, line in res)
    assert any(ok and line.startswith("text events in those streams: 1500") for ok, line in res)
    assert {cap for _, cap, _ in door.calls} <= {"worker.ping", "secret.list", "stream.list", "stream.read"} | \
        {cap for cap, _ in mod.RECORDS}                                              # read-only
    res = mod.run(FakeDoor({}, []), ["kaiju"], {})
    assert any(not ok and "kaiju" in line for ok, line in res)


def test_smoke_sends_a_user_agent(monkeypatch):
    mod = _smoke()
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        seen.update(req.headers)
        return Resp()
    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    mod.Door("https://hub.example", "t").request("GET", "/workers")
    assert seen.get("User-agent", "").startswith("rook-cutover-smoke/")


# -- the copy ----------------------------------------------------------------------------------

def _copy04():
    spec = importlib.util.spec_from_file_location("switchover_copy04", REPO / "castling/switchover/copy04.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_copy04_takes_a_consistent_copy_without_touching_the_hub(tmp_path):
    live = tmp_path / "live"
    (live / "updates" / "worker" / "1244").mkdir(parents=True)
    (live / "updates" / "worker" / "1244" / "band-worker.pyz").write_bytes(b"x" * 100)
    (live / "updates" / "worker" / "1244" / "band-worker.json").write_text("{}")
    (live / "trust.json").write_text('{"entries": []}')
    hub = sqlite3.connect(live / "journal.db")                 # a running hub: WAL, rows not checkpointed
    hub.execute("PRAGMA journal_mode=WAL")
    hub.execute("PRAGMA wal_autocheckpoint=0")
    hub.execute("CREATE TABLE calls (seq INTEGER PRIMARY KEY, cap TEXT)")
    hub.executemany("INSERT INTO calls (cap) VALUES (?)", [("shell.exec",)] * 5)
    hub.commit()
    assert (live / "journal.db-wal").stat().st_size > 0
    dest = tmp_path / "copy"
    info = _copy04().copy(str(live), str(dest))
    hub.close()
    assert sorted(info["files"]) == ["journal.db", "trust.json", "updates/worker/1244/band-worker.json"]
    assert info["not_copied"] == [{"path": "updates/worker/1244/band-worker.pyz", "size": 100}]
    db = sqlite3.connect(f"file:{dest / 'journal.db'}?mode=ro&immutable=1", uri=True)
    assert db.execute("SELECT count(*) FROM calls").fetchone()[0] == 5
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    db.close()
    assert not list(dest.rglob("*-wal")) and not list(dest.rglob("*-shm"))
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert json.loads((dest / "COPY-INFO.json").read_text())["files"]["trust.json"]["sha256"]
    stopped = tmp_path / "stopped"                                # a stopped hub: nothing beside its files
    stopped.mkdir()
    (stopped / "journal.db").write_bytes((dest / "journal.db").read_bytes())
    db = sqlite3.connect(stopped / "journal.db")
    db.execute("PRAGMA journal_mode=WAL")
    db.close()
    assert sorted(p.name for p in stopped.iterdir()) == ["journal.db"]
    _copy04().copy(str(stopped), str(tmp_path / "copy2"))
    assert sorted(p.name for p in stopped.iterdir()) == ["journal.db"]
    with pytest.raises(SystemExit):
        _copy04().copy(str(live), str(dest))                      # never over an earlier copy
    with pytest.raises(SystemExit):
        _copy04().copy(str(live), str(live / "inside"))
    from castling.switchover.migrate.source import Source
    Source(dest).close()                                          # migrate takes it as a copy


# -- names follow the manifests (drift #33: singular namespaces) -----------------------------

def test_the_move_follows_singular_plugin_names(tmp_path, full04, monkeypatch):
    """With #353's names (plugin job, record job; plugin agent) the move and
    verify use them: no plural name is spelled in the tool."""
    import shutil

    import castling.switchover.migrate as mig
    from castling.switchover.migrate import names
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    for p in names.PLUGINS.iterdir():
        if p.is_dir() and p.name not in ("jobs", "agents"):
            (plugins / p.name).symlink_to(p, target_is_directory=True)
    for folder, old, new, rec_old, rec_new in (("jobs", "jobs", "job", "jobs", "job"),
                                               ("agents", "agents", "agent", None, None)):
        shutil.copytree(names.PLUGINS / folder, plugins / folder)
        y = plugins / folder / "plugin.yaml"
        text = y.read_text().replace(f"\nplugin: {old}\n", f"\nplugin: {new}\n")
        if rec_old:
            text = text.replace(f"\n  {rec_old}:\n    fields:", f"\n  {rec_new}:\n    fields:") \
                       .replace(f"to: {rec_old},", f"to: {rec_new},")
        y.write_text(text)
    monkeypatch.setattr(names, "PLUGINS", plugins)
    monkeypatch.setattr(mig, "PLUGINS", plugins)
    monkeypatch.setattr(mig, "AGENTS", plugins / "agents")
    names.manifest.cache_clear()
    try:
        assert (names.record("jobs"), names.cap("jobs", "list"), names.record("agents", "memory")) == (
            "job.job", "job.list", "agent.memory")
        cfg = make_05(tmp_path)
        migrate(full04, cfg, apply=True)
        rec = sqlite3.connect(cfg.parent / "data" / "records.db")
        tables = {r[0] for r in rec.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rec.close()
        assert {"r:job.job", "r:agent.memory"} <= tables
        assert not {"r:jobs.jobs", "r:agents.memory", "r:agents.note"} & tables
        v = verify.verify(full04, cfg)
        assert v["ok"], verify.render(v)
        assert _kinds(v)["jobs (disabled, 0.4 definition kept)"]["moved"] == 2
        assert _kinds(v)["house agent memory (active -> memory or notepad)"]["moved"] == 4
        assert "job.list" in [c for c, _ in _smoke().RECORDS]          # the smoke checks name the same caps
    finally:
        names.manifest.cache_clear()
