"""Hub backup on a real 0.5 band (gate R5): the backup plugin's nightly job
seals the hub and stores it on the worker through backup-store, checks the
stored copy by restoring it, and the file restores a working hub on a
"fresh machine" (a new folder) with the recovery key alone."""
from __future__ import annotations

import json
import subprocess
import sys
import time

from harness import REPO, Door, until

PLUGINS_DIR = REPO / "rook" / "plugins"


def _ok(out: dict):
    assert out.get("ok"), out
    return out["result"]


def test_nightly_backup_to_a_worker_and_restore_elsewhere(fresh):
    b = fresh
    store = b.root / "store"
    (b.library / "jobs").symlink_to(PLUGINS_DIR / "jobs", target_is_directory=True)
    cfg = json.loads((b.hub_dir / "hub.yaml").read_text())
    cfg["settings"] = {"jobs": {"tick": "100ms"}, "backup": {"target": "worker:e2e-worker", "keep": 1}}
    (b.hub_dir / "hub.yaml").write_text(json.dumps(cfg, indent=1))
    w = json.loads((b.worker_dir / "worker.yaml").read_text())
    w["plugins"].append(str(PLUGINS_DIR / "backup-store"))
    w["settings"] = {"backup-store": {"folder": str(store)}}
    (b.worker_dir / "worker.yaml").write_text(json.dumps(w, indent=1))
    rk = b.hub_cli("recovery-key")
    b.start_hub()
    b.start_worker()
    b.wait_caps("backup-store.put")

    # the ready-made job, installed by the owner and started now
    templates = _ok(b.owner.call("jobs.templates", worker="rook"))
    assert any(t["template"] == "backup/nightly" and "cron" in t["trigger"] for t in templates)
    job = _ok(b.owner.call("jobs.install", {"template": "backup/nightly", "name": "backup-nightly"},
                           worker="rook"))
    run = _ok(b.owner.call("jobs.start", {"job": job["id"], "wait": True}, worker="rook"))
    assert run["state"] == "ok", run
    first = run["result"]
    assert first["target"] == "worker:e2e-worker" and first["verified"]["hub"] == b.hub_id
    assert [p.name for p in store.iterdir()] == [first["name"]]
    assert b.worker_calls("backup-store.put") >= 1 and b.worker_calls("backup-store.get") >= 1
    # every part went through dispatch as the job's creator, with a why
    puts = b.owner.get("/journal?cap=backup-store.put&limit=5")["entries"]
    assert puts and all(e["person"] == "person:bake" and e["decision"] == "allow" for e in puts)

    # a second run keeps one (keep: 1): the first is pruned on the worker
    time.sleep(1.1)                         # names have the second in them
    second = _ok(b.owner.call("backup.run", worker="rook"))
    assert second["pruned"] == [first["name"]] and [p.name for p in store.iterdir()] == [second["name"]]
    listed = _ok(b.owner.call("backup.list", worker="rook"))
    assert [x["name"] for x in listed["backups"]] == [second["name"]]
    assert _ok(b.owner.call("backup.verify", worker="rook"))["databases"] >= 2

    # the hub dies; a fresh machine gets the file off the worker and the key
    b.hub.stop()
    new = b.root / "newhub" / "hub.yaml"
    r = subprocess.run([sys.executable, "-m", "rook.core2.hub", "--config", str(new), "restore",
                        str(store / second["name"])], input=rk + "\n", capture_output=True, text=True,
                       timeout=120, env=b.env, cwd=b.root)
    assert r.returncode == 0, r.stderr
    assert (new.parent / "data" / "keys" / "root.key").read_bytes() == \
        (b.hub_dir / "data" / "keys" / "root.key").read_bytes()
    assert (new.parent / "library" / "jobs" / "plugin.yaml").is_file()      # copied, not a link
    r = subprocess.run([sys.executable, "-m", "rook.core2.hub", "--config", str(new), "recovery-key"],
                       capture_output=True, text=True, timeout=60, env=b.env, cwd=b.root)
    assert r.stdout.strip() == rk
    # the restored hub is the same hub: the owner's token works and the worker comes back to it
    hub2 = b._spawn("hub", [sys.executable, "-m", "rook.core2.hub", "--config", str(new)], new.parent)
    hub2.wait_log("hub started")
    doors = {x["door"]: x["bind"] for x in hub2.records() if x.get("msg") == "door open"}
    owner = Door(f"http://{doors['http']}", b.owner_token)
    until(lambda: any(x["name"] == "e2e-worker" and x["online"] for x in owner.get("/workers").get("workers", [])),
          60, "the worker online with the restored hub")
    assert owner.call("e2e.ping", {"text": "after restore"})["ok"]
