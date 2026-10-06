"""The phone's Workers tab roster: built from band announces the worker already receives."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

PATH = Path(__file__).parents[1] / "android/app/src/main/python/rook_android/roster.py"


def load():
    spec = importlib.util.spec_from_file_location("roster_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def announce(wid, name, **extra):
    return json.dumps({"kind": "announce", "worker_id": wid, "name": name,
                       "caps": ["a.b", "c.d"], **extra}).encode()


def test_records_announces_without_consuming_and_ignores_other_traffic():
    mod = load()
    now = [1000.0]
    r = mod.Roster("me", clock=lambda: now[0])
    assert r.observe(announce("w1", "desk", build=120, version="120.x",
                              hb={"battery": {"percent": 40, "charging": True}, "other": {"x": 1}},
                              app_release={"platform": "android", "version": "0.4.10", "code": 14, "junk": 1},
                              description="d" * 400)) is False
    assert r.observe(b"\x00\x01binary") is False
    assert r.observe(json.dumps({"id": "1", "cap": "x", "args": {"kind": "announce"}}).encode()) is False
    assert r.observe(b'{"kind": "announce"}') is False   # no worker_id
    now[0] = 1030.0
    rows = r.rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "desk" and row["caps"] == 2 and row["build"] == 120
    assert row["hb"] == {"battery": {"percent": 40, "charging": True}}
    assert row["app_release"] == {"platform": "android", "version": "0.4.10", "code": 14}
    assert len(row["description"]) == 280
    assert row["last_seen_age_secs"] == 30.0 and row["self"] is False


def test_own_row_added_once_and_stale_workers_forgotten():
    mod = load()
    now = [0.0]
    r = mod.Roster("me", clock=lambda: now[0])
    r.observe(announce("old", "gone"))
    now[0] = mod.FORGET_SECS + 1
    r.observe(announce("w2", "laptop"))
    own = {"worker_id": "me", "name": "phone", "description": "", "caps": 3, "version": "1",
           "build": 1, "app_release": {}, "hb": {}}
    rows = {x["worker_id"]: x for x in r.rows(own)}
    assert set(rows) == {"me", "w2"} and rows["me"]["self"] is True
    r.observe(announce("me", "phone", hb={"battery": {"percent": 80}}))
    rows = {x["worker_id"]: x for x in r.rows(own)}
    assert rows["me"]["hb"] == {"battery": {"percent": 80}}   # the echoed announce wins


def test_snapshot_follows_the_running_worker():
    mod = load()
    assert json.loads(mod.snapshot()) == {"running": False, "workers": []}
    handlers = []
    worker = SimpleNamespace(worker_id="me", name="phone", register_binary_handler=handlers.append,
                             metadata=SimpleNamespace(description="pocket"),
                             registry=SimpleNamespace(list=lambda: ["x.y"]), app_release={"version": "0.4.10"})
    mod.attach(worker)
    handlers[0](announce("w1", "desk"), ("peer",))
    snap = json.loads(mod.snapshot())
    assert snap["running"] is True and snap["self_id"] == "me"
    assert {w["name"] for w in snap["workers"]} == {"desk", "phone"}
    mod.detach(SimpleNamespace())          # another worker: no effect
    assert json.loads(mod.snapshot())["running"] is True
    mod.detach(worker)
    assert json.loads(mod.snapshot())["running"] is False
