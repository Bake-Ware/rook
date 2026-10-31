"""Core updates as real processes (docs/castling-updates.md): the hub signs
a release of this checkout, rolls it out, the worker restarts into it and
confirms it; a release that crashes, or never reaches its hub, is rolled
back by the worker itself and the rollout says why."""
from __future__ import annotations

import shutil
import sys

import pytest

from harness import HERE, REPO, until

VERSION = "0.5.1-e2e.1"


def call(band, cap, args=None, worker="e2e-worker"):
    out = band.owner.call(cap, args or {}, worker=worker)
    assert out.get("ok"), out
    return out["result"]


def release_of(band) -> dict:
    return call(band, "worker.status")["release"]


def rollout(band, source, version=VERSION, seconds=120.0) -> dict:
    started = call(band, "update.rollout", {"version": version, "source": str(source), "gate": 60}, worker="rook")
    assert started["canary"] == ["e2e-worker"] and started["version"] == version and started["signed"]
    return until(lambda: (r := call(band, "update.status", worker="rook")["rollout"])["status"] != "running" and r,
                 seconds, "the rollout to end")


def broken_copy(tmp, patch: str):
    """A copy of this checkout's package whose worker misbehaves after the
    smoke check (which only imports it)."""
    src = tmp / "broken-src"
    shutil.copytree(REPO / "rook", src / "rook", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(REPO / "conformance" / "manifest", src / "conformance" / "manifest")
    shutil.copy(REPO / "pyproject.toml", src / "pyproject.toml")
    with open(src / "rook" / "core2" / "worker" / "runtime.py", "a", encoding="utf-8") as f:
        f.write("\n\n" + patch + "\n")
    return src


def supervised(band):
    """Start the (enrolled) worker under supervise.py, as a service manager would."""
    if not (band.worker_dir / "anchor.json").exists():
        band.enroll()
    band.worker = band._spawn("worker", [sys.executable, str(HERE / "supervise.py"), sys.executable, "-m",
                                         "rook.core2.worker", "--config", "worker.yaml"], band.worker_dir)
    band.worker_id = band.worker.wait_log("worker started")["worker"]
    band.wait_caps("e2e.ping")


def test_a_release_rolls_out_restarts_the_worker_into_it_and_rolls_back_by_hand(band):
    pid = band.worker.pid
    assert release_of(band)["running"] is None
    ro = rollout(band, REPO)
    assert ro["status"] == "done", ro
    assert [(w["name"], w["state"]) for w in ro["workers"]] == [("e2e-worker", "updated")]
    rel = release_of(band)
    assert rel["running"] == rel["current"] == VERSION and rel["trial"] is None and rel["previous"] is None
    w = next(w for w in band.owner.get("/workers")["workers"] if w["name"] == "e2e-worker")
    assert w["version"] == VERSION
    assert band.worker.pid == pid                                # exec in place: no exit for a supervisor
    assert band.owner.call("e2e.ping", {"text": "on the release"})["ok"]
    entries = band.owner.get("/journal?cap=worker.update&limit=5")["entries"]
    assert entries and entries[0]["person"].endswith("bake") and entries[0]["ok"]
    change = call(band, "change.get", {"id": ro["change"]}, worker="rook")       # one record for the rollout
    assert change["category"] == "update" and change["outcome"] == "done: updated e2e-worker"
    history = [e["event"] for e in call(band, "update.history", worker="rook")["entries"]]
    assert history[:3] == ["rollout.done", "worker.updated", "rollout.started"]
    # by hand: back to the installed core
    back = call(band, "worker.update", {"version": "installed"})
    assert back["version"] is None and back["from"] == VERSION
    until(lambda: (r := band.owner.call("worker.status", {})).get("ok") and r["result"]["release"]["running"] is None,
          60, "the worker back on its installed core")
    assert release_of(band)["previous"] == VERSION


def test_the_desktop_update_check_is_served_without_a_token(band):
    import json
    import urllib.request
    with urllib.request.urlopen(band.url + "/updates/desktop.json", timeout=30) as r:
        feed = json.loads(r.read())
    assert feed["ok"] and isinstance(feed["downloads"], list)      # empty when GitHub can't be reached


@pytest.mark.parametrize("patch, reason", [
    # crashes at start: the supervisor restarts it until boot drops the trial
    ("async def _broken(self):\n    raise RuntimeError('broken on purpose')\nWorker.start = _broken",
     "did not come up"),
    # runs, but its calls never prove it: the trial clock rolls it back in-process
    ("Worker._served = lambda self, band: None", "no call from the home hub"),
])
def test_a_release_that_does_not_come_up_is_rolled_back_by_the_worker(fresh, monkeypatch, patch, reason):
    b = fresh
    b.env["ROOK_UPDATE_TRIAL_SECS"] = "6"
    b.start_hub()
    supervised(b)
    ro = rollout(b, broken_copy(b.root, patch), version="0.5.1-broken.1")
    w = ro["workers"][0]
    assert ro["status"] == "failed" and w["state"] == "failed", ro
    assert "rolled back on the worker" in w["reason"] and reason in w["reason"]
    rel = release_of(b)
    assert rel["running"] is None and rel["current"] is None and rel["failed"]["version"] == "0.5.1-broken.1"
    assert b.owner.call("e2e.ping", {"text": "back on the installed core"})["ok"]
