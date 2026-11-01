"""Worker events reach the hub (gate R2) on a real 0.5 band (hub, worker and
relay as processes, see harness.py): a text arriving on the worker
(``e2e.sms.received``, emitted the way the phone's sms plugin does) fires an
owner's job on the hub, whose call reaches the worker in a tainted session,
and shows on a live page."""
from __future__ import annotations

import json

from harness import REPO, until
from test_dashboard_e2e import Browser

PLUGINS_DIR = REPO / "rook" / "plugins"
SCRIPT = ("def main(job):\n"
          "    p = job.payload\n"
          "    pong = job.call('e2e.ping', {'text': 'from ' + p['from']}, on=p['worker'], why='answer a text')\n"
          "    return {'text': pong['text'], 'person': p['person'], 'worker': p['worker']}\n")


def _ok(out: dict):
    assert out.get("ok"), out
    return out["result"]


def test_a_worker_event_fires_a_job_and_shows_on_a_page(fresh):
    b = fresh
    (b.library / "jobs").symlink_to(PLUGINS_DIR / "jobs", target_is_directory=True)
    cfg = json.loads((b.hub_dir / "hub.yaml").read_text())
    cfg["settings"] = {"job": {"tick": "100ms"}}
    (b.hub_dir / "hub.yaml").write_text(json.dumps(cfg, indent=1))
    b.start_hub()
    b.start_worker()
    b.wait_caps("e2e.receive")

    # a live page that hears e2e.* events, open in a signed-in browser
    _ok(b.owner.call("page.create", {"name": "texts", "title": "Texts", "note": "Texts as they arrive.",
                                      "html": "<p>texts</p>", "events": ["e2e.*"]}, worker="rook"))
    web = Browser(b.url)
    web.sign_in(b.owner_token)
    opened = web.post("/web/api/open", {"page": "page/texts"})
    assert opened["ok"], opened

    def poll(since=None):
        q = f"/web/api/events?token={opened['token']}" + (f"&since={since}" if since is not None else "")
        return json.loads(web.request("GET", q)[2])
    seq = poll()["seq"]

    job = _ok(b.owner.call("job.create", {
        "name": "on-text", "trigger": {"on": "e2e.sms.received", "filter": {"body": "ping me"}},
        "script": SCRIPT}, worker="rook"))
    _ok(b.owner.call("e2e.receive", {"from": "+15550100", "body": "not this one"}))
    _ok(b.owner.call("e2e.receive", {"from": "+15550123", "body": "ping me"}))

    runs = until(lambda: [r for r in _ok(b.owner.call("job.run.list", {"where": {"job": job["id"]}},
                                                      worker="rook")) if r["state"] != "running"],
                 30, "the job's run")
    assert len(runs) == 1, runs           # the filter let only the second text through
    run = runs[0]
    assert (run["trigger"], run["state"]) == ("event", "ok"), run
    assert run["result"] == {"text": "from +15550123", "person": "person:bake", "worker": "e2e-worker"}
    assert run["event"]["name"] == "e2e.sms.received" and run["event"]["taints"] is True
    assert run["event"]["payload"] == {"from": "+15550123", "body": "ping me", "person": "person:bake",
                                       "worker": "e2e-worker"}
    entry = b.owner.get("/journal?cap=e2e.ping&limit=1")["entries"][0]
    assert entry["why"] == "job on-text: answer a text" and entry["person"] == "person:bake"

    def texts():
        got = [e for e in poll(seq)["events"] if e["event"] == "e2e.sms.received"]
        return got if len(got) == 2 else None
    got = until(texts, 20, "both texts on the page")
    assert [e["payload"]["body"] for e in got] == ["not this one", "ping me"]
    assert got[0]["payload"]["worker"] == "e2e-worker" and got[0]["payload"]["person"] == "person:bake"
