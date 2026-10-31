"""The jobs plugin on a real 0.5 band (hub, worker and relay as processes,
see harness.py): a job fires on its schedule and on an event, and a script
job's calls reach the worker, each through dispatch as the job's creator."""
from __future__ import annotations

import json

from harness import REPO, until

PLUGINS_DIR = REPO / "rook" / "plugins"


def _jobs_band(b):
    (b.library / "jobs").symlink_to(PLUGINS_DIR / "jobs", target_is_directory=True)
    cfg = json.loads((b.hub_dir / "hub.yaml").read_text())
    cfg["settings"] = {"job": {"tick": "100ms", "retry_after": "100ms"}}
    (b.hub_dir / "hub.yaml").write_text(json.dumps(cfg, indent=1))
    b.start_hub()
    b.start_worker()
    b.wait_caps("e2e.ping")
    return b


def _ok(out: dict):
    assert out.get("ok"), out
    return out["result"]


def _runs(b, job: str) -> list[dict]:
    return _ok(b.owner.call("job.run.list", {"where": {"job": job}, "order": "created"}, worker="rook"))


def test_jobs_fire_on_a_schedule_and_on_an_event(fresh):
    b = _jobs_band(fresh)
    every = _ok(b.owner.call("job.create", {"name": "ping-every", "trigger": {"every": "1s"}, "call": "e2e.ping",
                                             "worker": "e2e-worker", "args": {"text": "scheduled"}}, worker="rook"))
    runs = until(lambda: [r for r in _runs(b, every["id"]) if r["state"] == "ok"][1:] and _runs(b, every["id"]),
                 30, "two scheduled runs")
    assert runs[0]["trigger"] == "every" and runs[0]["result"]["text"] == "scheduled"
    assert b.worker_calls("e2e.ping") >= 2
    entry = b.owner.get("/journal?cap=e2e.ping&limit=1")["entries"][0]
    assert entry["person"] == "person:bake" and entry["why"].startswith("job ping-every (run ")
    _ok(b.owner.call("job.update", {"id": "ping-every", "revision": every["revision"], "enabled": False},
                     worker="rook"))

    # on an event: a held call (hold.created) for e2e.wipe, and only that
    held_job = _ok(b.owner.call("job.create", {
        "name": "on-hold", "trigger": {"on": "hold.created", "filter": {"cap": "e2e.wipe"}},
        "script": "def main(job):\n"
                  "    pong = job.call('e2e.ping', {'text': 'held ' + job.payload['cap']}, on='e2e-worker',\n"
                  "                    why='say a hold is waiting')\n"
                  "    return pong['text']\n"}, worker="rook"))
    held = b.owner.call("e2e.wipe")
    assert held["held"], held
    runs = until(lambda: [r for r in _runs(b, held_job["id"]) if r["state"] != "running"], 30, "the event run")
    assert (runs[0]["trigger"], runs[0]["state"], runs[0]["result"]) == ("event", "ok", "held e2e.wipe"), runs
    assert runs[0]["event"]["payload"]["hold"] == held["hold"]
    entry = b.owner.get("/journal?cap=e2e.ping&limit=1")["entries"][0]
    assert entry["why"] == "job on-hold: say a hold is waiting" and entry["person"] == "person:bake"
    assert b.owner.post(f"/holds/{held['hold']}", {"decision": "refuse"}) == {"ok": True}
    assert b.worker_calls("e2e.wipe") == 0
