#!/usr/bin/env python3
"""Set up a Rook remote over USB: Wi-Fi, then enroll it with the hub.

    python3 firmware-remote/scripts/remote-enroll.py --hub https://rook.example.com \\
        [--port /dev/ttyACM0] [--name remote] [--ssid NAME --password PASS]

Reads the remote's public key over serial and asks the hub to join
(POST /enroll/ask); the remote then waits on the hub's Workers page until a
person lets it in. The hub's config bundle goes to the remote over the
cable, never over the air.
The remote then joins the band by itself. Standard library plus pyserial.
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

import serial

UA = "rook-remote-enroll/0.1"      # a hub behind Cloudflare refuses urllib's default


def port_default():
    found = sorted(glob.glob("/dev/serial/by-id/*Espressif*")) or sorted(glob.glob("/dev/ttyACM*"))
    if not found:
        sys.exit("no remote found on USB; pass --port")
    return found[0]


class Remote:
    def __init__(self, port):
        self.s = serial.Serial(port, 115200, timeout=0.5)
        self.s.dtr = True
        time.sleep(0.3)
        self.s.reset_input_buffer()

    def cmd(self, obj, wait=15.0):
        data = (json.dumps(obj) + "\n").encode()
        for i in range(0, len(data), 64):        # small writes: the device reads as it goes
            self.s.write(data[i:i + 64])
            time.sleep(0.01)
        end = time.time() + wait
        while time.time() < end:
            line = self.s.readline().decode(errors="replace").strip()
            if line.startswith("@ "):
                return json.loads(line[2:])
        raise SystemExit(f"the remote didn't answer {obj.get('cmd')}")


def post(url, path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {"ok": False, **json.loads(e.read() or b"{}")}
        except ValueError:
            return {"ok": False, "error": f"HTTP {e.code}"}


def key_id(pub_text):
    import base64
    raw = base64.b64decode(pub_text.split(":", 1)[1] if pub_text.startswith("ed25519:") else pub_text)
    return hashlib.sha256(raw).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hub", required=True, help="the hub's https address")
    ap.add_argument("--port")
    ap.add_argument("--name", help="worker name (default: what the remote has, else 'remote')")
    ap.add_argument("--ssid")
    ap.add_argument("--password", default="")
    ap.add_argument("--force", action="store_true", help="enroll again even if already enrolled")
    ap.add_argument("--bundle", help="resend a bundle saved by an earlier run that the remote didn't take")
    a = ap.parse_args()
    url = a.hub.rstrip("/")
    if not url.startswith("https://"):
        sys.exit("--hub must be https: the band key travels in the answer")

    r = Remote(a.port or port_default())
    if a.ssid:
        r.cmd({"cmd": "wifi", "ssid": a.ssid, "pass": a.password})
        print(f"wifi: {a.ssid} saved")
    if a.bundle:
        send(r, a.name or "remote", json.load(open(a.bundle)))
        return
    info = r.cmd({"cmd": "key"})
    name = a.name or info.get("name") or "remote"
    if info.get("enrolled") and not a.force:
        print(f"already enrolled as worker {info.get('worker_id')}; --force to enroll again")
        return
    start = post(url, "/enroll/ask", {"name": name, "key": info["key"]})
    if not start.get("ok") or not isinstance(start.get("ticket"), str):
        sys.exit(f"the hub refused: {start.get('error')}")
    hub = start.get("hub")
    print(f"{name} is waiting: let it in on the Workers page of hub {hub}", flush=True)
    r.cmd({"cmd": "show", "text": f"Waiting to join.\nLet '{name}' in on\nthe hub's Workers\npage."})
    deadline = time.time() + 900
    while True:
        got = post(url, "/enroll/poll", {"ticket": start["ticket"]})
        if str(got.get("error", "")).startswith("HTTP 5"):   # the hub or its tunnel blinked
            time.sleep(3)
            continue
        if not got.get("pending"):
            break
        if time.time() > deadline:
            sys.exit("nobody let the remote in in time")
        time.sleep(min(max(got.get("interval") or 2, 1), 10))
    if not got.get("ok"):
        r.cmd({"cmd": "show", "text": "Enrollment refused", "seconds": 10})
        sys.exit(f"the hub refused: {got.get('error')}")
    b = got["bundle"]
    if key_id(b["hub"]["root"]) != b["hub"]["hub_id"] or (hub and b["hub"]["hub_id"] != hub):
        sys.exit("the bundle's hub doesn't match its key or the hub that gave the code: refused")
    if hashlib.sha256(b["band"]["psk"].encode()).hexdigest()[:32] != b["band"]["id"]:
        sys.exit("the bundle's band key is for another band: refused")
    if not b["band"].get("relay"):
        sys.exit("the hub gives devices no UDP relay (hub.yaml enroll.relay); the remote needs one")
    bundle = {"worker_id": b["worker_id"], "owner": b.get("owner"),
              "hub": {"hub_id": b["hub"]["hub_id"], "root": b["hub"]["root"], "url": b["hub"].get("url", url)},
              "band": {"id": b["band"]["id"], "psk": b["band"]["psk"], "relay": b["band"]["relay"]}}
    # The hub hands a bundle out once: keep it (owner-only) until the remote has it.
    saved = os.path.expanduser("~/.cache/rook-remote-bundle.json")
    os.makedirs(os.path.dirname(saved), exist_ok=True)
    with os.fdopen(os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(bundle, f)
    send(r, name, bundle)
    os.remove(saved)


def send(r, name, bundle):
    try:
        out = r.cmd({"cmd": "enroll", "name": name, "bundle": bundle})
    except SystemExit:
        sys.exit("the remote didn't take the bundle; it is kept in ~/.cache/rook-remote-bundle.json: "
                 "rerun with --bundle ~/.cache/rook-remote-bundle.json")
    if not out.get("ok"):
        sys.exit(f"the remote refused the bundle: {out.get('error')}")
    print(f"enrolled {name} as worker {bundle['worker_id']} (owner {bundle.get('owner')}); relay {bundle['band']['relay']}")
    r.cmd({"cmd": "show", "text": "", "seconds": 0})
    r.cmd({"cmd": "reboot"})
    print("rebooting into the band")


if __name__ == "__main__":
    main()
