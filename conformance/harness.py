#!/usr/bin/env python3
"""Live conformance run: a candidate worker against the Python reference hub.

    python conformance/harness.py --candidate "node examples/ports/typescript/src/worker.ts"
    python conformance/harness.py --candidate "examples/ports/rust/target/release/rook-port" \
        --hub-env /path/to/test-hub-data/test-hub.env

Without ``--hub-env`` it boots a throwaway hub with ``scripts/test-hub.sh``
(no workers, no dashboard, band risk ceiling ``write`` so band callers may
post to chat rooms) in a temp dir, and removes it afterwards. That needs the
relay binary (``TELESTHETE_HUB`` or ``telesthete-hub`` on PATH).

The candidate is started with these environment variables (the contract in
conformance/README.md):

    ROOK_RELAY          host:port of the relay (UDP)
    ROOK_PSK            band pre-shared key
    ROOK_NAME           worker name to announce
    ROOK_IDENTITY       identity to stamp on the calls it makes
    ROOK_ANCHOR         base64 root public key (verify the hub's is_hub grant)
    ROOK_ANNOUNCE_SECS  announce interval (the harness asks for 5)

The harness joins the band with the reference ``BandClient`` and checks the
candidate's announce, call handling (including fragmentation and error
replies) and a chat room round trip through the hub worker ``rook``. It exits
0 when every check passes. ``--json`` prints a machine-readable report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "test-hub.sh"
sys.path.insert(0, str(ROOT))

REQUIRED_CAPS = ("caps.describe", "conformance.echo", "conformance.add",
                 "conformance.chat_post", "conformance.chat_read")
ANNOUNCE_SECS = 5
HUB_ANNOUNCE_WAIT = 40.0   # the hub node announces every ~30 s


def read_env(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


class Report:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, check: str, status: str, detail: str = "") -> None:
        self.rows.append({"check": check, "status": status, "detail": detail})
        mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[status]
        print(f"  [{mark}] {check}{(': ' + detail) if detail else ''}", flush=True)

    def ok(self) -> bool:
        return all(r["status"] != "fail" for r in self.rows)


class Hub:
    """A running reference hub: attached (``--hub-env``) or booted here."""

    def __init__(self, env_file: str | None) -> None:
        self.env_file = env_file
        self.data: str | None = None
        self.env: dict[str, str] = {}

    def __enter__(self) -> "Hub":
        if self.env_file:
            self.env = read_env(Path(self.env_file))
        else:
            if not shutil.which("bash"):
                raise RuntimeError("bash is required to boot the test hub")
            self.data = tempfile.mkdtemp(prefix="rook-conformance-")
            base = int(os.environ.get("ROOK_IT_PORT_BASE") or random.randrange(20000, 40000, 3))
            env = {**os.environ, "PYTHON": os.environ.get("PYTHON", sys.executable)}
            r = subprocess.run(["bash", str(SCRIPT), "start", "--data", self.data,
                                "--port-base", str(base), "--workers", "0", "--no-dashboard",
                                "--no-knowledge", "--band-max-risk", "write"],
                               env=env, capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                shutil.rmtree(self.data, ignore_errors=True)
                raise RuntimeError(f"test hub failed to start:\n{r.stdout}\n{r.stderr}")
            self.env = read_env(Path(self.data) / "test-hub.env")
        secrets = Path(self.env["ROOK_IT_DATA_DIR"]) / "secrets.env"
        self.psk = read_env(secrets)["ROOK_BAND_PSK"]
        self.relay = self.env["ROOK_IT_RELAY"]
        self.root_pub = self.env.get("ROOK_IT_ROOT_PUB", "")
        self.band_write = self.env.get("ROOK_IT_BAND_MAX_RISK", "read") in ("write", "exec", "admin")
        return self

    def __exit__(self, *exc) -> None:
        if self.data:
            env = {**os.environ, "PYTHON": os.environ.get("PYTHON", sys.executable)}
            subprocess.run(["bash", str(SCRIPT), "reset", "--data", self.data], env=env,
                           capture_output=True, text=True, timeout=60)


async def raw_call(client, msg: dict, timeout: float) -> dict | None:
    """Send a hand-built request; return the first reply with its id, or None."""
    mid = msg.setdefault("id", uuid.uuid4().hex)
    fut = asyncio.get_running_loop().create_future()
    client._pending[mid] = fut
    try:
        await client.transport.send(json.dumps(msg).encode())
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        client._pending.pop(mid, None)


async def wait_for(pred, timeout: float, step: float = 0.25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        await asyncio.sleep(step)
    return None


async def run_checks(hub: Hub, candidate: list[str], name: str, identity: str,
                     report: Report, log_path: Path) -> None:
    from rook.band_mcp.client import BandClient
    host, _, port = hub.relay.rpartition(":")
    if hub.root_pub:
        os.environ["ROOK_UPDATE_PUBKEY"] = hub.root_pub  # this process's trust anchor
    client = BandClient(psk=hub.psk, hub_host=host, hub_port=int(port))
    await client.start()
    env = {**os.environ, "ROOK_RELAY": hub.relay, "ROOK_PSK": hub.psk, "ROOK_NAME": name,
           "ROOK_IDENTITY": identity, "ROOK_ANNOUNCE_SECS": str(ANNOUNCE_SECS)}
    if hub.root_pub:
        env["ROOK_ANCHOR"] = hub.root_pub
    log = open(log_path, "w")
    proc = subprocess.Popen(candidate, env=env, stdout=log, stderr=subprocess.STDOUT,
                            cwd=str(ROOT))
    try:
        await _checks(client, hub, name, identity, report)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        await client.stop()


async def _checks(client, hub: Hub, name: str, identity: str, report: Report) -> None:
    def find():
        return next((w for w in client.workers.values() if w.get("name") == name), None)

    entry = await wait_for(find, 30)
    if entry is None:
        report.add("announce: joins the band", "fail", f"no announce named {name!r} within 30 s")
        return
    report.add("announce: joins the band", "pass", f"worker_id {entry['worker_id']}")
    wid = entry["worker_id"]
    missing = [c for c in REQUIRED_CAPS if c not in entry.get("caps", [])]
    report.add("announce: lists the required caps", "fail" if missing else "pass",
               f"missing {missing}" if missing else "")
    tiers = entry.get("tiers") or {}
    bad = {k: v for k, v in tiers.items() if v not in ("r", "w", "x", "a")}
    report.add("announce: tiers map uses r/w/x/a", "fail" if bad or not tiers else "pass",
               f"bad {bad}" if bad else ("" if tiers else "no tiers"))
    report.add("announce: facts is an object", "pass" if isinstance(entry.get("facts"), dict)
               else "fail")
    first_seen = entry["last_seen"]
    again = await wait_for(lambda: (find() or {}).get("last_seen", 0) > first_seen,
                           ANNOUNCE_SECS * 2 + 3)
    report.add("announce: repeats on its interval", "pass" if again else "fail")

    async def call(cap, args=None, target=wid, timeout=10.0):
        try:
            return await client.call(cap, args=args or {}, target=target, timeout=timeout,
                                     identity="agent:conformance-harness")
        except (asyncio.TimeoutError, TimeoutError):
            return None

    value = {"s": "café ☃ \U0001F600", "n": [1, 2.5, -3], "b": True, "z": None}
    r = await call("conformance.echo", {"value": value})
    good = bool(r and r.get("ok") and r.get("result") == value and r.get("from") == wid)
    report.add("call: echo round trip", "pass" if good else "fail", json.dumps(r)[:200])

    big = {"blob": "".join(random.choice("abcdefghij") for _ in range(9000))}
    r = await call("conformance.echo", {"value": big}, timeout=15)
    report.add("call: 9 KB echo (fragmented both ways)",
               "pass" if r and r.get("ok") and r.get("result") == big else "fail",
               "" if r else "no reply")

    r = await call("conformance.add", {"a": 2, "b": 40})
    report.add("call: add returns a result", "pass" if r and r.get("result") == 42 else "fail",
               json.dumps(r)[:200])
    r = await call("conformance.add", {"a": 2})
    report.add("error: missing arg -> 'bad args: ...'",
               "pass" if r and r.get("ok") is False and str(r.get("error", "")).startswith("bad args: ")
               else "fail", json.dumps(r)[:200])
    r = await call("nope.nothing")
    report.add("error: targeted unknown cap -> 'unknown capability: <cap>'",
               "pass" if r and r.get("ok") is False and r.get("error") == "unknown capability: nope.nothing"
               else "fail", json.dumps(r)[:200])
    r = await raw_call(client, {"cap": "conformance.echo", "args": [1], "target": wid}, 10)
    report.add("error: non-object args -> 'args must be an object'",
               "pass" if r and r.get("ok") is False and r.get("error") == "args must be an object"
               else "fail", json.dumps(r)[:200])
    r = await raw_call(client, {"cap": "nope.nothing"}, 3)
    report.add("silence: open call for a cap it does not own", "pass" if r is None else "fail",
               json.dumps(r)[:200] if r else "")
    r = await raw_call(client, {"cap": "conformance.echo", "args": {}, "target": "someone-else"}, 3)
    report.add("silence: call targeted at another worker", "pass" if r is None else "fail",
               json.dumps(r)[:200] if r else "")
    r = await call("caps.describe", {"prefix": "conformance."})
    desc = (r or {}).get("result") or {}
    shape = (isinstance(desc, dict) and "conformance.echo" in desc
             and isinstance(desc["conformance.echo"].get("params"), list)
             and "doc" in desc["conformance.echo"]
             and all(k.startswith("conformance.") for k in desc))
    report.add("call: caps.describe (prefix filter, doc + params)", "pass" if shape else "fail",
               json.dumps(r)[:200])

    # -- chat room round trip through worker `rook` ---------------------------
    if not hub.band_write:
        report.add("chat: round trip through worker rook", "skip",
                   "the hub's band risk ceiling is read (start it with --band-max-risk write)")
        return

    def rook():
        # With the root key known the reference client only names a verified
        # hub "rook"; without it the hub shows up quarantined as rook~<id8>.
        for w in client.workers.values():
            n = str(w.get("name") or "")
            if hub.root_pub and n == "rook" and "is_hub" in (w.get("roles") or []):
                return w
            if not hub.root_pub and (n == "rook" or n.startswith("rook~")):
                return w
        return None
    hub_entry = await wait_for(rook, HUB_ANNOUNCE_WAIT)
    if hub_entry is None:
        report.add("chat: hub worker rook visible", "fail", "no verified rook announce")
        return
    hid = hub_entry["worker_id"]
    started = await call("chat.write", {"action": "start", "title": "conformance",
                                        "invite": ["band:" + identity]}, target=hid)
    if not (started and started.get("ok")):
        report.add("chat: harness opens a room", "fail", json.dumps(started)[:200])
        return
    room = started["result"]["room"]
    nonce = uuid.uuid4().hex[:12]
    r = await call("conformance.chat_post", {"room": room, "text": f"hello from port {nonce}"},
                   timeout=HUB_ANNOUNCE_WAIT + 15)
    res = (r or {}).get("result") or {}
    report.add("chat: candidate posts via chat.write on rook",
               "pass" if r and r.get("ok") and (res.get("hub") or {}).get("room") == room
               else "fail", json.dumps(r)[:300])
    if hub.root_pub:
        verified = (res.get("rook") or {}).get("verified")
        report.add("chat: candidate verified rook's is_hub grant", "pass" if verified is True
                   else "fail", json.dumps(res.get("rook"))[:200])
    read = await call("chat.read", {"action": "read", "room": room}, target=hid)
    msgs = ((read or {}).get("result") or {}).get("messages") or []
    mine = [m for m in msgs if nonce in m.get("text", "")]
    report.add("chat: hub stored the post as band:<identity>",
               "pass" if mine and mine[0].get("sender") == "band:" + identity else "fail",
               json.dumps(mine)[:200])
    reply_text = f"harness says hi {nonce}"
    await call("chat.write", {"action": "send", "room": room, "text": reply_text}, target=hid)
    r = await call("conformance.chat_read", {"room": room, "since_seq": 0}, timeout=20)
    got = (((r or {}).get("result") or {}).get("hub") or {}).get("messages") or []
    report.add("chat: candidate reads the room back via chat.read",
               "pass" if any(m.get("text") == reply_text for m in got) else "fail",
               json.dumps(r)[:300])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rook live conformance harness")
    ap.add_argument("--candidate", required=True, help="command that starts the candidate worker")
    ap.add_argument("--hub-env", help="test-hub.env of a running test hub (else boot one)")
    ap.add_argument("--name", help="worker name for the candidate (default: random)")
    ap.add_argument("--identity", default="agent:conformance-port")
    ap.add_argument("--log", help="where to write the candidate's output")
    ap.add_argument("--json", action="store_true", help="print a JSON report at the end")
    ns = ap.parse_args(argv)
    import logging
    logging.basicConfig(level=logging.WARNING)
    name = ns.name or f"conformance-{uuid.uuid4().hex[:6]}"
    if ns.log:
        log_path = Path(ns.log)
    else:
        with tempfile.NamedTemporaryFile(prefix="rook-candidate-", suffix=".log", delete=False) as f:
            log_path = Path(f.name)
    report = Report()
    print(f"conformance: candidate {ns.candidate!r} as {name}", flush=True)
    try:
        with Hub(ns.hub_env) as hub:
            asyncio.run(run_checks(hub, shlex.split(ns.candidate), name, ns.identity, report,
                                   log_path))
    except Exception as e:  # noqa: BLE001 - report, don't traceback
        report.add("harness", "fail", f"{type(e).__name__}: {e}")
    passed = sum(r["status"] == "pass" for r in report.rows)
    print(f"conformance: {passed}/{len(report.rows)} passed; candidate log: {log_path}")
    if ns.json:
        print(json.dumps({"ok": report.ok(), "checks": report.rows, "log": str(log_path)}))
    return 0 if report.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
