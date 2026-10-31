"""The hub's stream index (gate R3, N28) on a real 0.5 band: two worker
processes each open a terminal (shell.open), and the hub lists both with
stream.list for the owner, finds one with stream.search, pages, sees one
close, and keeps a person who isn't in them out. A worker that restarts
resyncs and the index drops the streams it lost."""
from __future__ import annotations

import json
import sys

import yaml

from harness import until


def _ok(out: dict):
    assert out.get("ok"), out
    return out["result"]


def _second_worker(b, name: str):
    """Another worker process on the band, enrolled the same way as the first."""
    folder = b.root / name
    folder.mkdir()
    cfg = yaml.safe_load((b.worker_dir / "worker.yaml").read_text())     # enrolling added its own
    cfg = {k: v for k, v in cfg.items() if k not in ("anchor", "worker_id")}
    (folder / "worker.yaml").write_text(json.dumps({**cfg, "name": name}, indent=1))
    qr = b.owner.post("/enroll/code", {})
    assert qr.get("ok"), qr
    r = b.worker_cli("enroll", qr["qr"], cwd=folder)
    assert r.returncode == 0, r.stderr
    proc = b._spawn(name, [sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml"], folder)
    proc.wait_log("worker started")
    until(lambda: "shell.open" in (b.caps(name).get("caps") or {}), 30, f"the hub to list {name}'s caps")
    return proc, folder


def _listed(b, door=None, **args) -> list:
    return _ok((door or b.owner).call("stream.list", {"limit": 200, **args}, worker="rook"))["streams"]


def test_streams_from_two_workers_are_listed_on_the_hub(band):
    b = band
    proc2, folder2 = _second_worker(b, "e2e-worker2")
    one = _ok(b.owner.call("shell.open", {"argv": ["cat"], "pty": False, "title": "cat on one"}))["stream"]
    two = _ok(b.owner.call("shell.open", {"argv": ["cat"], "pty": False, "title": "cat on two"},
                           worker="e2e-worker2"))["stream"]

    rows = until(lambda: (r := {x["id"]: x for x in _listed(b)}) and one in r and two in r and r,
                 20, "both workers' streams in the index")
    assert (rows[one]["worker"], rows[two]["worker"]) == ("e2e-worker", "e2e-worker2")
    for sid in (one, two):
        row = rows[sid]
        assert row["kind"] == "process" and row["person"] == "person:bake" and row["online"], row
        assert row["closed"] is None and row["input"] and not row["durable"], row
    assert rows[two]["title"] == "cat on two" and rows[two]["attrs"]["band"] == b.band

    # one worker's only; search; paging
    assert [r["id"] for r in _listed(b, worker="e2e-worker2")] == [two]
    found = _ok(b.owner.call("stream.search", {"query": "ON TWO"}, worker="rook"))
    assert [r["id"] for r in found["streams"]] == [two]
    page = _ok(b.owner.call("stream.list", {"limit": 1}, worker="rook"))
    assert len(page["streams"]) == 1 and page["next"] == 1
    assert _ok(b.owner.call("stream.get", {"stream": one}, worker="rook"))["worker"] == "e2e-worker"

    # someone who isn't in them sees neither (the kid's rights reach stream.list only through e2e.*: refused)
    kid = b.kid.call("stream.list", {}, worker="rook")
    assert not kid.get("ok") or kid["result"]["streams"] == [], kid

    # activity and closing reach the index
    _ok(b.owner.call("stream.write", {"stream": two, "body": {"text": "hello\n"}}, worker="e2e-worker2"))
    until(lambda: _ok(b.owner.call("stream.get", {"stream": two}, worker="rook"))["size"] > 0, 20,
          "the stream's activity in the index")
    _ok(b.owner.call("stream.close", {"stream": one}))
    until(lambda: _ok(b.owner.call("stream.get", {"stream": one}, worker="rook"))["closed"], 20,
          "the closed stream in the index")
    assert [r["id"] for r in _listed(b, open=True)] == [two]

    # a worker that restarts lost its terminals: its resync drops them from the index
    proc2.stop()
    proc2 = b._spawn("e2e-worker2", [sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml"],
                     folder2)
    proc2.wait_log("worker started")
    until(lambda: two not in {r["id"] for r in _listed(b)}, 30, "the lost stream to leave the index")
    assert one in {r["id"] for r in _listed(b)}
