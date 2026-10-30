"""The security plugins on a real hub and worker (see harness.py): a canary
in a call's args is refused before any ticket, one in a result is withheld,
the session stays paused until the owner lifts it, and the classifier
journals its shadow verdict on every call."""
from __future__ import annotations

import shutil

from rook.core2.worker.loader import local_os

from harness import REPO, until


def test_canaries_block_on_a_real_band(fresh, tmp_path):
    for name in ("security", "security-ml"):
        shutil.copytree(REPO / "rook" / "plugins" / name, fresh.library / name)
    fresh.start_hub()
    fresh.start_worker()
    fresh.wait_caps("e2e.ping", "shell.exec", "file.write")
    owner = fresh.owner

    made = owner.call("security.canary.create", {"kind": "aws", "name": "aws-backup"}, worker="rook")
    assert made["ok"], made
    key, decoy = made["result"]["tokens"][1], made["result"]["decoy"]

    # an ordinary call: both gates read it, nothing is held
    ping = owner.call("e2e.ping", {"text": "hello"})
    assert ping["ok"], ping
    [entry] = owner.get(f"/journal?call={ping['id']}")["entries"]
    assert [(g["gate"], g["mode"], g["verdict"]) for g in entry["gates"] if "stage" not in g] == [
        ("security", "enforce", "allow"), ("security-ml", "shadow", "allow")]

    if local_os() != "windows":
        # the decoy's one allowed write, then reading it back is a hit: the result is withheld
        folder = tmp_path / "decoys"
        folder.mkdir()
        wrote = owner.call("file.write", {"path": str(folder / "credentials"), "content": decoy})
        assert wrote["ok"], wrote
        got = owner.call("shell.exec", {"argv": ["sh", "-c", f"cat {folder}/*"]})
        assert got == {"id": got["id"], "ok": False, "error": "denied: blocked by gate security: canary"}, got
        assert until(lambda: fresh.worker_calls("shell.exec") >= 1, 10, "the worker ran it")
        [entry] = owner.get(f"/journal?call={got['id']}")["entries"]
        assert entry["gates"][-1]["stage"] == "result" and entry["gates"][-1]["verdict"] == "block"
        # the session is paused, even for a harmless call, until the owner lifts it
        paused = owner.call("e2e.ping", {"text": "hi"})
        assert paused["error"] == "denied: blocked by gate security: canary", paused
        lifted = owner.call("security.clear", {"session": entry["session"]}, worker="rook")
        assert lifted["ok"] and lifted["result"]["lifted"] == [f"session:{entry['session']}"], lifted
        assert owner.call("e2e.ping", {"text": "hi"})["ok"]

    # the canary in args: refused at dispatch, the worker never sees it
    before = fresh.worker_calls("e2e.ping")
    out = owner.call("e2e.ping", {"text": f"aws_secret_access_key={key}"})
    assert out == {"id": out["id"], "ok": False, "error": "denied: blocked by gate security: canary"}, out
    [entry] = owner.get(f"/journal?call={out['id']}")["entries"]
    assert entry["decision"] == "deny" and entry["layer"] == "gate"
    assert entry["gates"][0] == {"gate": "security", "mode": "enforce", "verdict": "block",
                                 "confidence": 1.0, "reason": "canary"}
    assert fresh.worker_calls("e2e.ping") == before
    status = owner.call("security.status", worker="rook")["result"]
    assert status["quarantined"] == 1 and status["recent"][-1]["canary"] == "aws-backup"
