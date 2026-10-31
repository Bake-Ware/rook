"""castling/switchover/import_04.py: the one-off 0.4 workbench import (repo
tooling for the switchover, not shipped)."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("jsonschema")

from rook.kit.ctx import Caller  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "knowledge_v2.db"


@pytest.fixture
def importer():
    """import_04.py, loaded from its file (it is a script, not a module)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("import_04", REPO / "castling" / "switchover" / "import_04.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _source_copy(tmp_path) -> Path:
    """The fixture plus 0.4 whiteboard lines for one task, and a sessions db
    holding that task's handoff."""
    src = tmp_path / "knowledge.db"
    shutil.copy(FIXTURE, src)
    db = sqlite3.connect(src)
    db.executescript((REPO / "rook" / "hub" / "plugins" / "knowledge" /
                      "migrations" / "004_whiteboard.sql").read_text())
    task = "t_2f8df454186f408f905df45a8d6897de"
    for seq, (kind, text, closed) in enumerate([("goal", "Service answers", None), ("open", "check logs", None),
                                                ("open", "old item", 1.0), ("call", "shell.exec ok", None),
                                                ("decision", "restart, don't rebuild", None)], 1):
        db.execute("INSERT INTO whiteboard(band,task,claim,actor,kind,text,first,ts,closed) VALUES(?,?,?,?,?,?,?,?,?)",
                   ("band0001", task, "cl_x", "claude", kind, text, seq, seq, closed))
    db.execute("INSERT INTO events(band,record,actor,action,ts,data) VALUES(?,?,?,?,?,?)",
               ("band0001", task, "codex.codex.gpu-box", "note", 7.0, '{"text": "Logs look clean."}'))
    db.commit()
    db.close()
    sessions = sqlite3.connect(tmp_path / "sessions.db")
    sessions.execute("CREATE TABLE handoffs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, thread_id TEXT, ts REAL,"
                     " author TEXT, status TEXT, goal TEXT, state TEXT, decisions TEXT, next_steps TEXT,"
                     " artifacts TEXT, supersedes TEXT, transcript_ref TEXT)")
    sessions.execute("INSERT INTO handoffs(id,thread_id,ts,author,status,goal,state,decisions,next_steps,artifacts)"
                     " VALUES('h1','thread-0001',5,'claude','active','Restart it','Paused mid-way','[]',"
                     " '[\"run the restart\"]','[]')")
    sessions.commit()
    sessions.close()
    return src


def test_import_04_is_read_only_and_maps_everything(tmp_path, importer, capsys):
    src = _source_copy(tmp_path)
    before = src.read_bytes()
    rec = tmp_path / "records.db"
    args = ["--from", str(src), "--records", str(rec), "--band", "b1", "--handoffs", str(tmp_path / "sessions.db")]
    assert importer.main(args) == 0
    assert src.read_bytes() == before and not Path(f"{src}-wal").exists()
    store = importer.open_store(str(rec))
    me = Caller(agent="agent:x", band="b1")
    try:
        pages = {p["slug"]: p for archived in (False, True)
                 for p in store.type("knowledge.knowledge").list(me, archived=archived)}
        assert sorted(pages) == ["lab-note", "runbooks", "runbooks-2", "service-port", "service-port-moved"]
        moved, old = pages["service-port-moved"], pages["service-port"]
        assert old["archived"] and old["parent"] == pages["runbooks"]["id"]
        assert (moved["kind"], moved["creator"], moved["created"]) == ("fact", "codex.codex.gpu-box", 1790795838.769712)
        kinds = [(s["kind"], s["ref"]) for s in moved["sources"]]
        assert ("page", "service-port") in kinds and ("journal", "call-0001") in kinds
        assert ("url", "https://example.com/x") not in kinds          # retracted in 0.4
        assert any(s["kind"] == "user" and s["ref"] == "human:operator" for s in moved["sources"])
        project = store.type("project.project").list(me)[0]
        assert (project["slug"], project["concepts"]) == ("knowledge-service", ["shared-memory"])
        assert store.type("concept.concept").list(me)[0]["slug"] == "shared-memory"
        tasks = {t["title"]: t for t in store.type("task.task").list(me)}
        assert {t: (x["state"], x["project"]) for t, x in tasks.items()} == {
            "Restart test service": ("paused", project["id"]), "Write the runbook": ("done", project["id"]),
            "Soak the new build": ("in_progress", project["id"])}
        assert tasks["Write the runbook"]["outcome"] == "Runbook written."
        assert tasks["Write the runbook"]["links"][0]["ref"] == "abc1234"
        assert all(not t["claims"] for t in tasks.values())
        said = store.type("task.comment").list(me, where={"task": tasks["Restart test service"]["id"]})
        assert [(c["text"], c["creator"], c["created"]) for c in said] == \
            [("Logs look clean.", "codex.codex.gpu-box", 7.0)]
        board = store.type("whiteboard.board").list(me, where={"task": tasks["Restart test service"]["id"]})[0]
        assert board["text"] == ("Handoff from claude:\nGoal: Restart it\nState: Paused mid-way\nNext:\n- run the restart"
                                 "\n\nGoal: Service answers\n\nDecisions:\n- restart, don't rebuild\n\nOpen:\n- check logs")
    finally:
        store.close()
    report = capsys.readouterr().out
    assert '"renamed"' in report and "runbooks-2" in report
    # a second run finds everything and adds nothing
    assert importer.main(args) == 0
    assert '"imported": {}' in capsys.readouterr().out
    store = importer.open_store(str(rec))
    try:
        assert len(store.type("task.task").list(me)) == 3
        assert len(store.type("whiteboard.board").list(me)) == 1
        assert len(store.type("task.comment").list(me)) == 1
    finally:
        store.close()


def test_import_04_dry_run_and_one_band(tmp_path, importer, capsys):
    rec = tmp_path / "records.db"
    assert importer.main(["--from", str(FIXTURE), "--records", str(rec), "--band", "b1", "--dry-run"]) == 0
    assert not rec.exists() and '"dry_run": true' in capsys.readouterr().out
    other = sqlite3.connect(FIXTURE.as_uri() + "?mode=ro", uri=True).execute("SELECT DISTINCT band FROM records WHERE slug='lab-note'").fetchone()[0]
    assert importer.main(["--from", str(FIXTURE), "--records", str(rec), "--band", "b1", "--from-band", other]) == 0
    out = capsys.readouterr().out
    assert '"renamed": []' in out
    wal = tmp_path / "live.db"
    shutil.copy(FIXTURE, wal)
    Path(f"{wal}-wal").write_bytes(b"")
    assert importer.main(["--from", str(wal), "--records", str(rec), "--band", "b1"]) == 2
    assert ".backup" in capsys.readouterr().err
    bad = tmp_path / "empty.db"
    sqlite3.connect(bad).close()
    assert importer.main(["--from", str(bad), "--records", str(rec), "--band", "b1"]) == 2
