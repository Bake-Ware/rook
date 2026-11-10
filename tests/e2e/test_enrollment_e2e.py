"""Enrollment as real processes (spec 3.7): an owner hub set up with
``hub init``, and a fresh worker that asks to join and is let in with one tap."""
from __future__ import annotations

import json
import re
import subprocess
import sys

import yaml

from harness import HERE, START_SECS, WHY, Band, Door, until


class InitHub:
    """A hub made by ``python -m rook.core2.hub init`` on the band's relay."""

    def __init__(self, band: Band, name: str, owner: str):
        self.band, self.name, self.dir = band, name, band.root / name
        argv = [sys.executable, "-m", "rook.core2.hub", "--config", str(self.dir / "hub.yaml"), "init",
                "--owner", owner, "--relay", band.relay_addr, "--bind", "127.0.0.1:0"]
        r = subprocess.run(argv, cwd=band.root, env=band.env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        out = r.stdout
        self.hub_id = re.search(r"Rook hub ([0-9a-f]{16}) set up", out)[1]
        self.token = re.search(r"shown once \(only its hash is kept\):\n  (\S+)", out)[1]
        self.recovery = re.search(r"shows it again\.\n  (\S+)", out)[1]
        cfg = yaml.safe_load((self.dir / "hub.yaml").read_text())
        cfg["log"] = {"level": "info", "format": "json"}
        (self.dir / "hub.yaml").write_text(yaml.safe_dump(cfg))

    def start(self) -> Door:
        self.proc = self.band._spawn(self.name, [sys.executable, "-m", "rook.core2.hub", "--config", "hub.yaml"],
                                     self.dir)
        bind = self.proc.wait_log("door open")["bind"]
        self.proc.wait_log("hub started")
        self.url = f"http://{bind}"
        self.door = Door(self.url, self.token)
        return self.door


def test_a_fresh_worker_is_let_in_with_one_tap(request, tmp_path):
    b = Band(tmp_path)
    request.node._e2e_band = b
    try:
        b.start_relay()
        home = InitHub(b, "home", "bake")
        assert re.fullmatch(r"([A-Z2-7]{4}-){7}[A-Z2-7]{4}", home.recovery)
        bake = home.start()
        assert bake.get("/recovery-key")["recovery_key"] == home.recovery

        # A fresh machine: nothing but a config naming its plugins.
        wdir = tmp_path / "laptop"
        wdir.mkdir()
        (wdir / "worker.yaml").write_text(json.dumps({"plugins": [str(HERE / "plugins")],
                                                      "log": {"level": "info", "format": "json"}}))
        enroll = b._spawn("enroll", [sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml",
                                     "enroll", home.url, "--name", "laptop"], wdir)
        line = until(lambda: next((ln for ln in enroll.lines if "waiting for someone" in ln), None), START_SECS,
                     "the device waiting")
        assert home.hub_id in line
        waiting = bake.call("hub.joins", worker="rook")["result"]["devices"]
        assert [d["name"] for d in waiting] == ["laptop"]
        assert bake.call("hub.join", {"request": waiting[0]["id"]}, worker="rook")["ok"]      # the Join button
        assert enroll.p.wait(START_SECS) == 0, "\n".join(enroll.lines)
        anchor = json.loads((wdir / "anchor.json").read_text())
        assert anchor["hub"] == home.hub_id

        worker = b._spawn("laptop", [sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml"], wdir)
        wid = worker.wait_log("worker started")["worker"]
        assert wid == yaml.safe_load((wdir / "worker.yaml").read_text())["worker_id"]
        until(lambda: any(w["name"] == "laptop" and w["online"] for w in bake.get("/workers")["workers"]),
              30, "the home hub to see the laptop")
        row = next(w for w in bake.get("/workers")["workers"] if w["name"] == "laptop")
        assert row["owner"] == "person:bake"
        home_ping = until(lambda: (r := bake.call("e2e.ping", {"text": "home"}, worker="laptop"))["ok"] and r,
                          30, "a first call at home")
        assert home_ping["result"]
    finally:
        b.close()
