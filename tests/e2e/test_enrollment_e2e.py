"""Enrollment as real processes (spec 9, 12): an owner hub set up with
``hub init``, a fresh worker enrolled with a device code the owner approves,
then a second hub's band joined as a guest under a ceiling the home hub's
owner set, and left again."""
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


def test_a_fresh_worker_enrolls_with_a_device_code_and_joins_a_second_band_as_a_guest(request, tmp_path):
    b = Band(tmp_path)
    request.node._e2e_band = b
    try:
        b.start_relay()
        home, other = InitHub(b, "home", "bake"), InitHub(b, "other", "ann")
        assert home.hub_id != other.hub_id and re.fullmatch(r"([A-Z2-7]{4}-){7}[A-Z2-7]{4}", home.recovery)
        bake, ann = home.start(), other.start()
        assert bake.get("/recovery-key")["recovery_key"] == home.recovery

        # A fresh machine: nothing but a config naming its plugins.
        wdir = tmp_path / "laptop"
        wdir.mkdir()
        (wdir / "worker.yaml").write_text(json.dumps({"plugins": [str(HERE / "plugins")],
                                                      "log": {"level": "info", "format": "json"}}))
        enroll = b._spawn("enroll", [sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml",
                                     "enroll", home.url, "--name", "laptop"], wdir)
        line = until(lambda: next((ln for ln in enroll.lines if "approve code" in ln), None), START_SECS,
                     "the device's code")
        code = re.search(r"approve code ([A-Z]{4}-[A-Z]{4}) on hub ([0-9a-f]{16})", line)
        assert code[2] == home.hub_id
        waiting = bake.get("/enroll/pending")["devices"]
        assert [(d["user_code"], d["name"]) for d in waiting] == [(code[1], "laptop")]
        assert bake.post("/enroll/approve", {"code": code[1]})["ok"]
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

        # The other hub invites; the home hub's owner accepts with a ceiling of their own.
        invite = ann.post("/bands/invite", {"role": "guest", "ceiling": {"grants": ["e2e.*"]}})["invite"]
        joined = bake.post("/memberships", {"worker": "laptop", "invite": invite, "why": WHY,
                                            "ceiling": {"grants": ["e2e.ping"], "max_risk": "read"}})
        assert joined["ok"], joined
        entry = bake.get(f"/journal?call={joined['id']}")["entries"][0]
        assert entry["args"]["connect"]["psk"].startswith("{{secret:band-psk-")      # never the key itself
        worker.wait_log("joined band")
        guest = until(lambda: next((w for w in ann.get("/workers")["workers"] if w["name"] == "laptop"
                                    and w["online"]), None), 30, "the other hub to see the laptop")
        assert guest["id"] == wid and guest["owner"] is None
        # online comes with the announce; the caps once the hub has fetched the manifests
        until(lambda: "e2e.ping" in (ann.get("/caps?worker=laptop").get("workers", {}).get("laptop") or {})
              .get("caps", {}), 30, "the other hub to list the laptop's caps")
        pinged = ann.call("e2e.ping", {"text": "guest"}, worker="laptop")
        assert pinged["ok"], pinged
        refused = ann.call("e2e.admin", worker="laptop")
        assert not refused["ok"] and refused["error"].startswith("denied: ceiling"), refused
        assert bake.call("e2e.admin", worker="laptop")["ok"]                  # home is untouched

        # Off the band again: the worker leaves it.
        left = bake.post("/memberships", {"worker": "laptop", "band": invite["band"], "role": "removed", "why": WHY})
        assert left["ok"], left
        worker.wait_log("left band")
        assert bake.call("e2e.ping", {"text": "still home"}, worker="laptop")["ok"]
    finally:
        b.close()
