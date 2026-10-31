"""Load and soak test of the 0.5 sidecar on cachyrig (docs/castling-sidecar.md).

Drives the running sidecar through its doors only: the HTTP door as people
(the owner, an adult, a child) and the MCP door as agents, with plugin
push/rollback loops, hub/worker/relay restarts and relay pauses in the middle
of the traffic. Samples each process's memory, CPU, threads and file
descriptors, the journal's size and how long its chains take to verify, and
the worker's replay cache. Results: docs/castling-soak.md.

Run it with the sidecar's own Python (it has pyyaml and the installed rook):

    PY=~/.rook-castling/venv/bin/python
    $PY tests/soak/soak.py setup                  # soak users + tokens, the soak plugin pushed (restarts the hub)
    $PY tests/soak/soak.py burst --seconds 30     # top speed, briefly
    $PY tests/soak/soak.py run --minutes 45 --out DIR
    $PY tests/soak/soak.py report DIR             # the markdown summary again
    $PY tests/soak/soak.py teardown               # users, tokens and the soak plugin gone (restarts hub + worker)

It never touches the live 0.4 install: only ~/.rook-castling, its doors and
the rook-castling-* units, through the sidecar's own scripts (restart.sh).
"""
from __future__ import annotations

import argparse
import base64
import http.client
import itertools
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(os.environ.get("ROOK_CASTLING_HOME", Path.home() / ".rook-castling"))
SECRETS = ROOT / "secrets"
SCRIPTS = ROOT / "src" / "deploy" / "castling-sidecar"
HUB_CFG = ROOT / "hub" / "hub.yaml"
LIBRARY = ROOT / "hub" / "library"
HERE = Path(__file__).resolve().parent
HTTP = ("127.0.0.1", int(os.environ.get("ROOK_CASTLING_HTTP_PORT", "8750")))
MCP = ("127.0.0.1", int(os.environ.get("ROOK_CASTLING_MCP_PORT", "8751")))
WORKER = os.environ.get("ROOK_CASTLING_WORKER", os.uname().nodename.split(".")[0])
UNITS = {"relay": "rook-castling-relay", "hub": "rook-castling-hub", "worker": "rook-castling-worker"}
SOAK_USERS = {"soak-adult": "adult", "soak-kid": "child"}
V1, V2 = "1.0.0", "1.0.1"
WHY = "Soak test of the 0.5 sidecar."
TIMEOUT = 120.0            # seconds any one request may take before it counts as hung


# -- doors ---------------------------------------------------------------------

class Door:
    """One persistent HTTP/1.1 connection (per thread), reopened after any error."""

    def __init__(self, addr: tuple, token: str):
        self.addr, self.token, self.conn = addr, token, None

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        for attempt in (0, 1):
            if self.conn is None:
                self.conn = http.client.HTTPConnection(*self.addr, timeout=TIMEOUT)
            try:
                self.conn.request(method, path, body=data, headers=headers)
                r = self.conn.getresponse()
                raw = r.read()
                if r.getheader("Connection", "").lower() == "close":
                    self.close()
                return r.status, (json.loads(raw) if raw else None)
            except (ConnectionError, http.client.RemoteDisconnected, http.client.BadStatusLine) as e:
                self.close()
                if attempt or not isinstance(e, (http.client.RemoteDisconnected, BrokenPipeError,
                                                 ConnectionResetError)):
                    raise           # a stale keep-alive gets one retry; anything else counts
            except Exception:
                self.close()
                raise
        raise AssertionError("unreachable")

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


class Mcp(Door):
    _ids = itertools.count(1)

    def rpc(self, method: str, params: dict | None = None) -> dict:
        status, body = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": next(Mcp._ids), "method": method,
                                                     "params": params or {}})
        if status != 200 or not isinstance(body, dict):
            return {"ok": False, "error": f"http {status}: {str(body)[:200]}"}
        if "error" in body:
            return {"ok": False, "error": f"rpc {body['error'].get('message')}"}
        res = body["result"]
        if method != "tools/call":
            return {"ok": True, "result": res}
        return json.loads(res["content"][0]["text"])

    def tool(self, name: str, **args) -> dict:
        return self.rpc("tools/call", {"name": name, "arguments": args})


def call(door: Door, worker: str, cap: str, args: dict | None = None, why: str | None = WHY, **extra) -> dict:
    body = {"worker": worker, "cap": cap, "args": args or {}, **extra}
    if why is not None:
        body["why"] = why
    status, out = door.request("POST", "/call", body)
    return out if status == 200 and isinstance(out, dict) else {"ok": False, "error": f"http {status}: {out}"}


def get(door: Door, path: str) -> dict:
    status, out = door.request("GET", path)
    return out if status == 200 and isinstance(out, dict) else {"ok": False, "error": f"http {status}: {out}"}


# -- recording ----------------------------------------------------------------

class Recorder:
    """Every operation as one JSON line: {t, op, who, ok, ms, err?}."""

    def __init__(self, out: Path):
        self.out = out
        self.lock = threading.Lock()
        self.calls = open(out / "calls.jsonl", "a", buffering=1 << 16)
        self.events = open(out / "events.jsonl", "a", buffering=1)
        self.samples = open(out / "samples.jsonl", "a", buffering=1)
        self.count = 0

    def call(self, op: str, who: str, ok: bool, ms: float, err: str | None = None) -> None:
        rec = {"t": round(time.time(), 3), "op": op, "who": who, "ok": ok, "ms": round(ms, 2)}
        if err:
            rec["err"] = err[:300]
        with self.lock:
            self.calls.write(json.dumps(rec) + "\n")
            self.count += 1

    def event(self, kind: str, **fields) -> None:
        with self.lock:
            self.events.write(json.dumps({"t": round(time.time(), 3), "kind": kind, **fields}) + "\n")

    def sample(self, rec: dict) -> None:
        with self.lock:
            self.samples.write(json.dumps(rec) + "\n")

    def close(self) -> None:
        for f in (self.calls, self.events, self.samples):
            f.close()


def timed(rec: Recorder, op: str, who: str, fn, expect) -> dict | None:
    """Run ``fn()``; ``expect(result)`` returns None when it went as it should,
    else what was wrong (refusals the test asks for count as ok)."""
    t0 = time.monotonic()
    try:
        out = fn()
        err = expect(out)
    except Exception as e:
        out, err = None, f"{type(e).__name__}: {e}"
    rec.call(op, who, err is None, (time.monotonic() - t0) * 1000, err)
    return out


def ok(out) -> str | None:
    return None if isinstance(out, dict) and out.get("ok") is True else _err(out)


def refused(prefix: str):
    def check(out) -> str | None:
        if isinstance(out, dict) and out.get("ok") is False and str(out.get("error", "")).startswith(prefix):
            return None
        return f"expected a refusal ({prefix}): {_err(out)}"
    return check


def _err(out) -> str:
    if isinstance(out, dict):
        return str(out.get("error") or out)[:300]
    return str(out)[:300]


# -- identities and setup --------------------------------------------------------

def token(name: str) -> str:
    return (SECRETS / name).read_text().strip()


def sh(*argv, timeout: float = 180) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=timeout).stdout


def sidecar(script: str, *parts: str, timeout: float = 180) -> float:
    """Run one of the sidecar's own scripts; seconds it took."""
    t0 = time.monotonic()
    subprocess.run([str(SCRIPTS / script), *parts], check=True, capture_output=True, timeout=timeout)
    return time.monotonic() - t0


def library_plugin(version: str) -> None:
    """The soak plugin in the hub's library at ``version`` (a real folder, not
    one of install.sh's symlinks, so an upgrade leaves it)."""
    dest = LIBRARY / "soak"
    tmp = LIBRARY / ".soak.new"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(HERE / "plugin" / "soak", tmp)
    y = tmp / "plugin.yaml"
    y.write_text(y.read_text().replace(f"version: {V1}", f"version: {version}", 1))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)


def wait_online(door: Door, seconds: float = 120) -> float:
    """Until a soak.ping on the worker answers; seconds waited."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        try:
            if call(door, WORKER, "soak.ping", {"text": "up?"}).get("ok"):
                return time.monotonic() - t0
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"the worker didn't answer soak.ping within {seconds:g}s")


def setup(_args) -> int:
    import yaml
    cfg = yaml.safe_load(HUB_CFG.read_text())
    head = "".join(line for line in HUB_CFG.read_text().splitlines(True) if line.startswith("#"))
    users = cfg.setdefault("users", {})
    if any(users.get(n, {}).get("preset") != p for n, p in SOAK_USERS.items()):
        users.update({n: {"preset": p} for n, p in SOAK_USERS.items()})
        HUB_CFG.write_text(head + yaml.safe_dump(cfg, sort_keys=False))
        HUB_CFG.chmod(0o600)
    library_plugin(V1)
    print("restart hub:", round(sidecar("restart.sh", "hub"), 1), "s")
    py, mint = sys.executable, ["-m", "rook.core2.hub", "--config", str(HUB_CFG), "token"]
    for name, args in {"soak-adult.token": ["--person", "soak-adult"], "soak-kid.token": ["--person", "soak-kid"],
                       "soak-agent.token": ["--person", "soak-adult", "--agent", "claude-code"]}.items():
        if not (SECRETS / name).exists():
            fd = os.open(SECRETS / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(sh(py, *mint, *args, "--label", f"soak {name}"))
    owner = Door(HTTP, token("owner.token"))
    for _ in range(60):            # the worker reaches the restarted hub at its next keepalive
        out = owner.request("POST", "/plugins/push", {"worker": WORKER, "plugin": "soak", "why": WHY})[1]
        if out.get("ok") or "unknown worker" not in str(out.get("error")):
            break
        time.sleep(2)
    print("push soak:", json.dumps(out)[:300])
    print("soak.ping after", round(wait_online(owner), 1), "s")
    return 0


def teardown(_args) -> int:
    import yaml
    cfg = yaml.safe_load(HUB_CFG.read_text())
    head = "".join(line for line in HUB_CFG.read_text().splitlines(True) if line.startswith("#"))
    for n in SOAK_USERS:
        cfg.get("users", {}).pop(n, None)       # their tokens stop working with them
    HUB_CFG.write_text(head + yaml.safe_dump(cfg, sort_keys=False))
    HUB_CFG.chmod(0o600)
    shutil.rmtree(LIBRARY / "soak", ignore_errors=True)
    for name in ("soak-adult.token", "soak-kid.token", "soak-agent.token"):
        (SECRETS / name).unlink(missing_ok=True)
    # The installed copy goes; the running worker forgets it at its restart.
    shutil.rmtree(ROOT / "worker" / "data" / "plugins" / "soak", ignore_errors=True)
    print("restart hub + worker:", round(sidecar("restart.sh", "hub", "worker"), 1), "s")
    return 0


# -- the load --------------------------------------------------------------------

def drain(read, sid: str, want: bytes | None = None, seconds: float = 90) -> dict:
    """Read a stream from the start until it closes (or ``want`` shows up):
    {ok, bytes, dropped, closed, events}."""
    after, got, dropped, n = 0, bytearray(), 0, 0
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        out = read({"stream": sid, "after": after, "limit": 1000, "wait": 2})
        if not out.get("ok"):
            return out
        r = out["result"]
        after, dropped, n = r["next"], dropped + (r.get("dropped") or 0), n + len(r["events"])
        for e in r["events"]:
            if "b64" in e:
                got += base64.b64decode(e["b64"])
        if r.get("closed") or (want and want in got):
            return {"ok": True, "bytes": len(got), "dropped": dropped, "closed": bool(r.get("closed")),
                    "events": n, "data": bytes(got[-4096:])}
    return {"ok": False, "error": f"stream {sid} still open after {seconds:g}s ({len(got)} bytes)"}


def stream_ops(d: Door, w: str):
    read = lambda a: call(d, w, "stream.read", a)

    def big():
        # ~2.7 MB of output: more than the bounded store keeps (256 KiB), so a
        # reader that starts late sees dropped events, never an error.
        o = call(d, w, "shell.open", {"cmd": "head -c 2000000 /dev/urandom | base64", "pty": False})
        return drain(read, o["result"]["stream"]) if o.get("ok") else o

    def term():
        o = call(d, w, "shell.open", {"argv": ["sh"], "pty": True})
        if not o.get("ok"):
            return o
        sid = o["result"]["stream"]
        wr = call(d, w, "stream.write", {"stream": sid, "body": {"text": "echo soak-$((6*7))\r"}})
        got = drain(read, sid, b"soak-42", 30) if wr.get("ok") else wr
        closed = call(d, w, "stream.close", {"stream": sid})
        return got if not got.get("ok") else closed

    return [
        (1, "stream_2_7m", big, lambda o: ok(o) or (None if o["closed"] else "not closed")),
        (1, "stream_term", term, ok),
    ]


def owner_ops(d: Door, rec: Recorder, scratch: Path):
    w = WORKER
    big = 1 << 20

    def held_then_refused():
        out = call(d, w, "soak.wipe")
        if not out.get("held"):
            return out
        st, dec = d.request("POST", f"/holds/{out['hold']}", {"decision": "refuse"})
        return dec if st == 200 else {"ok": False, "error": f"http {st}"}

    def size_is(n):
        return lambda out: ok(out) or (None if len(out["result"]["data"]) == n else "wrong size")

    return [
        (30, "ping", lambda: call(d, w, "soak.ping", {"text": "hi"}), ok),
        (6, "exec", lambda: call(d, w, "shell.exec", {"argv": ["true"]}), ok),
        (2, "exec_1m", lambda: call(d, w, "shell.exec", {"cmd": f"head -c {big} /dev/zero | tr '\\0' x"}),
         lambda o: ok(o) or (None if len(o["result"]["stdout"]) == big else "short stdout")),
        (6, "blob_16k", lambda: call(d, w, "soak.blob", {"size": 16 << 10}), size_is(16 << 10)),
        (2, "blob_256k", lambda: call(d, w, "soak.blob", {"size": 256 << 10}), size_is(256 << 10)),
        (0.5, "blob_4m", lambda: call(d, w, "soak.blob", {"size": 4 << 20}), size_is(4 << 20)),
        (5, "file_stat", lambda: call(d, w, "file.stat", {"path": str(ROOT / "src" / ".castling-sha")}), ok),
        (4, "file_read", lambda: call(d, w, "file.read", {"path": str(ROOT / "src" / ".castling-sha")}), ok),
        (2, "file_write", lambda: call(d, w, "file.write", {"path": str(scratch / f"f{random.randrange(50)}.txt"),
                                                            "content": "soak " * 100}), ok),
        (3, "hub_status", lambda: call(d, "rook", "hub.status"), ok),
        (3, "task_list", lambda: call(d, "rook", "task.list"), ok),
        (1, "journal_search", lambda: call(d, "rook", "journal.search", {"cap": "soak.wipe", "limit": 20}), ok),
        (3, "view_workers", lambda: get(d, "/workers"), ok),
        (2, "view_caps", lambda: get(d, "/caps"), ok),
        (3, "view_journal", lambda: get(d, "/journal?limit=50"), ok),
        (2, "no_why", lambda: call(d, w, "soak.ping", why=None), refused("denied: why required")),
        (1, "unknown_cap", lambda: call(d, w, "soak.nope"), refused("unknown capability")),
        (1, "held_refused", held_then_refused, ok),
        (1, "sleep_2s", lambda: call(d, w, "soak.sleep", {"seconds": 2}), ok),
        (3, "hub_workers", lambda: call(d, "rook", "hub.workers"), ok),
        *stream_ops(d, w),
    ]


def adult_ops(d: Door, rec: Recorder, scratch: Path):
    w = WORKER
    return [
        (10, "ping", lambda: call(d, w, "soak.ping", {"text": "adult"}), ok),
        (3, "exec", lambda: call(d, w, "shell.exec", {"argv": ["true"]}), ok),
        (2, "file_read", lambda: call(d, w, "file.read", {"path": str(ROOT / "src" / ".castling-sha")}), ok),
        (2, "view_journal", lambda: get(d, "/journal?limit=20"), ok),
        (1, "push_refused", lambda: d.request("POST", "/plugins/push", {"worker": w, "plugin": "soak",
                                                                        "why": WHY})[1],
         refused("denied: only an owner")),
    ]


def kid_ops(d: Door, rec: Recorder, scratch: Path):
    return [
        (5, "ping_denied", lambda: call(d, WORKER, "soak.ping"), refused("denied: ")),
        (2, "exec_denied", lambda: call(d, WORKER, "shell.exec", {"argv": ["true"]}), refused("denied: ")),
        (2, "view_holds", lambda: get(d, "/holds"), ok),
    ]


def agent_ops(m: Mcp, rec: Recorder, scratch: Path):
    w = WORKER

    def session_cycle():
        s = m.tool("session", caps=["soak.*"])
        if not s.get("ok"):
            return s
        inside = m.tool("call", worker=w, cap="soak.ping", args={"text": "in"}, why=WHY, session=s["session"])
        outside = m.tool("call", worker=w, cap="shell.exec", args={"argv": ["true"]}, why=WHY,
                         session=s["session"])
        end = m.tool("session", end=s["session"])
        if not inside.get("ok"):
            return inside
        if outside.get("ok") or "denied: session" not in str(outside.get("error")):
            return {"ok": False, "error": f"outside the session: {_err(outside)}"}
        return end

    def stream_cycle():
        o = m.tool("call", worker=w, cap="shell.open", args={"argv": ["seq", "1", "50000"], "pty": False}, why=WHY)
        if not o.get("ok"):
            return o
        got = drain(lambda a: m.tool("stream", worker=w, why=WHY, **a), o["result"]["stream"])
        return got if not got.get("ok") or got["closed"] else {"ok": False, "error": "not closed"}

    return [
        (1, "mcp_stream_seq", stream_cycle, ok),
        (20, "mcp_ping", lambda: m.tool("call", worker=w, cap="soak.ping", args={"text": "agent"}, why=WHY), ok),
        (5, "mcp_exec", lambda: m.tool("call", worker=w, cap="shell.exec", args={"argv": ["uname", "-n"]},
                                       why=WHY), ok),
        (3, "mcp_blob_64k", lambda: m.tool("call", worker=w, cap="soak.blob", args={"size": 64 << 10}, why=WHY), ok),
        (3, "mcp_caps", lambda: m.tool("caps", match="soak."), ok),
        (3, "mcp_workers", lambda: m.tool("workers"), ok),
        (3, "mcp_journal", lambda: m.tool("journal", limit=20), ok),
        (2, "mcp_tools_list", lambda: m.rpc("tools/list"), ok),
        (3, "mcp_session", session_cycle, ok),
        (1, "mcp_held", lambda: m.tool("call", worker=w, cap="soak.wipe", why=WHY),
         lambda o: None if isinstance(o, dict) and o.get("held") else f"not held: {_err(o)}"),
    ]


def load_thread(name: str, who: str, make, door: Door, rec: Recorder, scratch: Path, interval: float,
                stop: threading.Event) -> None:
    ops = make(door, rec, scratch)
    weights = [o[0] for o in ops]
    rng = random.Random(f"{name}")
    while not stop.is_set():
        t0 = time.monotonic()
        _, op, fn, expect = rng.choices(ops, weights)[0]
        timed(rec, op, who, fn, expect)
        left = interval - (time.monotonic() - t0)
        if left > 0:
            stop.wait(left * rng.uniform(0.5, 1.5))
    door.close()


def refuse_agent_holds(door: Door, stop: threading.Event) -> None:
    """The agents' held calls wait for a person: the owner refuses them."""
    while not stop.wait(5):
        try:
            for h in get(door, "/holds").get("holds", []):
                if h.get("cap") == "soak.wipe":
                    door.request("POST", f"/holds/{h['hold']}", {"decision": "refuse"})
        except Exception:
            pass


def probe_thread(rec: Recorder, stop: threading.Event) -> None:
    """GET /holds every half second: it runs on the hub's event loop, so its
    latency shows any time the loop is blocked."""
    d = Door(HTTP, token("owner.token"))
    while not stop.wait(0.5):
        timed(rec, "probe_loop", "owner", lambda: get(d, "/holds"), ok)


def plugin_loop(rec: Recorder, every: float, stop: threading.Event) -> None:
    """Push the soak plugin's next version, then roll it back, again and again,
    with traffic on its caps."""
    d = Door(HTTP, token("owner.token"))
    while not stop.wait(every / 2):
        library_plugin(V2)
        out = timed(rec, "plugin_push", "owner",
                    lambda: d.request("POST", "/plugins/push", {"worker": WORKER, "plugin": "soak", "why": WHY})[1],
                    ok)
        rec.event("plugin_push", ok=bool(out and out.get("ok")), detail=_err(out) if not (out or {}).get("ok") else None)
        if stop.wait(every / 2):
            break
        out = timed(rec, "plugin_rollback", "owner",
                    lambda: call(d, WORKER, "core.plugin.rollback", {"plugin": "soak"}), ok)
        rec.event("plugin_rollback", ok=bool(out and out.get("ok")),
                  detail=_err(out) if not (out or {}).get("ok") else None)
    library_plugin(V1)


CHAOS = ["pause_relay_5", "restart_hub", "restart_relay", "restart_worker", "pause_relay_20", "restart_all"]


def chaos_loop(rec: Recorder, every: float, stop: threading.Event) -> None:
    """One disruption every ``every`` seconds, in turn; how long until a
    soak.ping goes through again."""
    d = Door(HTTP, token("owner.token"))
    i = 0
    while not stop.wait(every):
        kind = CHAOS[i % len(CHAOS)]
        i += 1
        t0 = time.time()
        try:
            if kind.startswith("pause_relay_"):
                secs = int(kind.rsplit("_", 1)[1])
                unit = UNITS["relay"]
                sh("systemctl", "--user", "kill", "--signal=STOP", unit)
                stop.wait(secs)
                sh("systemctl", "--user", "kill", "--signal=CONT", unit)
            elif kind == "restart_all":
                sidecar("restart.sh")
            else:
                sidecar("restart.sh", kind.split("_", 1)[1])
            wait_online(d, 180)
            rec.event("chaos", what=kind, ok=True, start=t0, took=round(time.time() - t0, 1))
        except Exception as e:
            rec.event("chaos", what=kind, ok=False, start=t0, error=str(e)[:300])
            if kind.startswith("pause_relay_"):
                subprocess.run(["systemctl", "--user", "kill", "--signal=CONT", UNITS["relay"]], timeout=30)


# -- sampling ----------------------------------------------------------------------

def main_pid(part: str) -> int:
    try:
        return int(sh("systemctl", "--user", "show", "-p", "MainPID", "--value", UNITS[part], timeout=10).strip())
    except Exception:
        return 0


def proc_stats(pid: int) -> dict | None:
    if not pid:
        return None
    try:
        status = Path(f"/proc/{pid}/status").read_text()
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        fds = len(os.listdir(f"/proc/{pid}/fd"))
    except (OSError, IndexError):
        return None
    field = {line.split(":")[0]: line.split(":", 1)[1].strip() for line in status.splitlines()}
    tick = os.sysconf("SC_CLK_TCK")
    return {"pid": pid, "rss_kb": int(field["VmRSS"].split()[0]), "threads": int(field["Threads"]),
            "fds": fds, "cpu_s": (int(stat[11]) + int(stat[12])) / tick}


def size(*paths: Path) -> int:
    return sum(p.stat().st_size for p in paths if p.exists())


def verify_journal(scratch: Path) -> dict:
    """Copy the live journal (SQLite backup: a consistent snapshot) and time
    a full chain check on the copy, as the hub's hourly check walks it."""
    from rook.core2.hub.journal_store import JournalStore
    copy = scratch / "journal-copy.db"
    copy.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{ROOT / 'hub' / 'data' / 'journal.db'}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(copy)
    with dst:
        src.backup(dst)
    src.close()
    rows = dst.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    dst.close()
    store = JournalStore(str(copy))
    t0 = time.monotonic()
    problems = store.check()
    took = time.monotonic() - t0
    store.close()
    for p in (copy, Path(f"{copy}-wal"), Path(f"{copy}-shm")):
        p.unlink(missing_ok=True)
    return {"rows": rows, "verify_s": round(took, 3), "problems": problems[:3]}


def sampler(rec: Recorder, scratch: Path, every: float, verify_every: float, stop: threading.Event) -> None:
    hub, worker = ROOT / "hub" / "data", ROOT / "worker" / "data"
    last_verify = 0.0
    status_door = Door(HTTP, token("owner.token"))
    while True:
        s = {"t": round(time.time(), 1), "calls": rec.count}
        for part in UNITS:
            s[part] = proc_stats(main_pid(part))
        s["journal_bytes"] = size(hub / "journal.db", hub / "journal.db-wal")
        s["records_bytes"] = size(hub / "records.db", hub / "records.db-wal")
        replay = worker / "replay"
        s["replay_bytes"] = size(replay)
        try:
            s["replay_lines"] = replay.read_bytes().count(b"\n")
        except OSError:
            s["replay_lines"] = None
        try:
            st = call(status_door, WORKER, "worker.status")
            s["worker_status"] = {k: v for k, v in (st.get("result") or {}).items()
                                  if isinstance(v, (int, float)) and not isinstance(v, bool)}
        except Exception:
            status_door.close()
        if time.monotonic() - last_verify >= verify_every:
            last_verify = time.monotonic()
            try:
                s["verify"] = verify_journal(scratch)
            except Exception as e:
                s["verify"] = {"error": f"{type(e).__name__}: {e}"}
        rec.sample(s)
        if stop.wait(every):
            return


# -- run, burst, report -------------------------------------------------------------

def start_load(rec: Recorder, scratch: Path, rate: float, stop: threading.Event) -> list[threading.Thread]:
    """Threads per identity and door; ``rate`` operations a second in all."""
    plan = [  # (name, who, ops, door, share of the rate)
        *[(f"owner{i}", "owner", owner_ops, Door(HTTP, token("owner.token")), 0.11) for i in range(4)],
        *[(f"adult{i}", "adult", adult_ops, Door(HTTP, token("soak-adult.token")), 0.06) for i in range(2)],
        ("kid0", "kid", kid_ops, Door(HTTP, token("soak-kid.token")), 0.04),
        *[(f"agent{i}", "agent:bake", agent_ops, Mcp(MCP, token("mcp.token")), 0.06) for i in range(4)],
        *[(f"agentb{i}", "agent:adult", agent_ops, Mcp(MCP, token("soak-agent.token")), 0.06) for i in range(2)],
    ]
    total = sum(p[4] for p in plan)
    threads = []
    for name, who, ops, door, share in plan:
        interval = 1.0 / (rate * share / total)
        t = threading.Thread(target=load_thread, args=(name, who, ops, door, rec, scratch, interval, stop),
                             name=name, daemon=True)
        t.start()
        threads.append(t)
    for m in [Mcp(MCP, token("mcp.token")), Mcp(MCP, token("soak-agent.token"))]:
        m.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "soak", "version": "0"}})
    return threads


def run(args) -> int:
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / "scratch"
    scratch.mkdir(exist_ok=True)
    rec = Recorder(out)
    sha = (ROOT / "src" / ".castling-sha").read_text().strip()
    rec.event("start", sha=sha, minutes=args.minutes, rate=args.rate, chaos_every=args.chaos_every,
              plugin_every=args.plugin_every)
    print(f"soak: {args.minutes} min at ~{args.rate}/s on {sha[:8]}; results in {out}", flush=True)
    stop = threading.Event()
    owner = Door(HTTP, token("owner.token"))
    library_plugin(V1)
    wait_online(owner)
    threads = [threading.Thread(target=sampler, args=(rec, scratch, args.sample_every, args.verify_every, stop),
                                daemon=True),
               threading.Thread(target=probe_thread, args=(rec, stop), daemon=True),
               threading.Thread(target=refuse_agent_holds, args=(Door(HTTP, token("owner.token")), stop),
                                daemon=True)]
    if args.plugin_every:
        threads.append(threading.Thread(target=plugin_loop, args=(rec, args.plugin_every, stop), daemon=True))
    if args.chaos_every:
        threads.append(threading.Thread(target=chaos_loop, args=(rec, args.chaos_every, stop), daemon=True))
    for t in threads:
        t.start()
    threads += start_load(rec, scratch, args.rate, stop)
    end = time.monotonic() + args.minutes * 60
    try:
        while time.monotonic() < end:
            time.sleep(min(30, max(0.1, end - time.monotonic())))
            print(f"  {time.strftime('%H:%M:%S')} {rec.count} ops", flush=True)
    except KeyboardInterrupt:
        print("interrupted: stopping", flush=True)
    finally:     # never leave the relay paused
        stop.set()
        subprocess.run(["systemctl", "--user", "kill", "--signal=CONT", UNITS["relay"]], timeout=30)
    for t in threads:
        t.join(TIMEOUT + 30)
    rec.event("end")
    # A last sample once the traffic stopped.
    done = threading.Event()
    done.set()
    sampler(rec, scratch, 0, 0, done)
    rec.close()
    print(report(out))
    return 0


def burst(args) -> int:
    """Top speed for a short while: soak.ping from ``threads`` HTTP clients."""
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorder(out)
    stop = threading.Event()
    cargs = json.loads(args.args) if args.args else {"text": "b"} if args.cap == "soak.ping" else {}

    def go(i):
        d = Door(HTTP, token("owner.token"))
        while not stop.is_set():
            timed(rec, f"burst_{args.cap}", "owner", lambda: call(d, WORKER, args.cap, cargs), ok)
        d.close()
    threads = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(args.threads)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    stop.wait(args.seconds)
    stop.set()
    for t in threads:
        t.join(TIMEOUT)
    took = time.monotonic() - t0
    rec.close()
    calls = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines()]
    good = [c for c in calls if c["ok"]]
    print(f"{len(calls)} calls in {took:.1f}s with {args.threads} threads: {len(good) / took:.1f} ok/s, "
          f"{len(calls) - len(good)} failed")
    if len(good) < len(calls):
        first = min(c["t"] for c in calls if not c["ok"]) - min(c["t"] - c["ms"] / 1000 for c in calls)
        print(f"first failure {first:.0f}s in")
    print(table(latencies(calls)))
    errs = defaultdict(int)
    for c in calls:
        if not c["ok"]:
            errs[c.get("err", "")[:80]] += 1
    for e, n in sorted(errs.items(), key=lambda x: -x[1])[:5]:
        print(f"  {n} x {e}")
    return 0


def pct(sorted_ms: list, p: float) -> float:
    """The p-th percentile (nearest rank) of an ascending list."""
    if not sorted_ms:
        return float("nan")
    k = max(0, min(len(sorted_ms) - 1, math.ceil(p / 100 * len(sorted_ms)) - 1))
    return sorted_ms[k]


def latencies(calls: list[dict]) -> dict:
    by = defaultdict(list)
    fails = defaultdict(int)
    for c in calls:
        by[c["op"]].append(c["ms"])
        if not c["ok"]:
            fails[c["op"]] += 1
    out = {}
    for op, ms in by.items():
        ms.sort()
        out[op] = {"n": len(ms), "fail": fails[op], "p50": pct(ms, 50), "p95": pct(ms, 95), "p99": pct(ms, 99),
                   "max": ms[-1]}
    return out


def table(lat: dict) -> str:
    lines = ["| op | n | failed | p50 ms | p95 ms | p99 ms | max ms |", "|---|---:|---:|---:|---:|---:|---:|"]
    for op, s in sorted(lat.items(), key=lambda x: -x[1]["n"]):
        lines.append(f"| {op} | {s['n']} | {s['fail']} | {s['p50']:.1f} | {s['p95']:.1f} | {s['p99']:.1f} "
                     f"| {s['max']:.0f} |")
    return "\n".join(lines)


def segments(samples: list[dict], part: str) -> list[list[dict]]:
    """A process's samples split at each restart (a new pid)."""
    segs, pid = [], None
    for s in samples:
        p = s.get(part)
        if not p:
            continue
        if p["pid"] != pid:
            segs.append([])
            pid = p["pid"]
        segs[-1].append({"t": s["t"], **p})
    return segs


WORKER_OPS = {"ping", "mcp_ping", "exec", "mcp_exec", "blob_16k", "file_stat", "file_read"}


def recovery(calls: list[dict], start: float) -> tuple[float | None, int]:
    """After a disruption that began at ``start``: seconds until a quick
    worker call sent after it came back ok, and how many worker calls in
    flight or sent until then failed or took over 5 s."""
    back = None
    for c in calls:
        sent = c["t"] - c["ms"] / 1000
        if c["op"] in WORKER_OPS and sent >= start and c["ok"] and c["ms"] < 2000:
            back = c["t"] if back is None else min(back, c["t"])
    hurt = sum(1 for c in calls if c["op"] in WORKER_OPS and (not c["ok"] or c["ms"] > 5000)
               and c["t"] >= start and c["t"] - c["ms"] / 1000 <= (back or start))
    return (None if back is None else back - start), hurt


def report(out: Path) -> str:
    out = Path(out)
    calls = [json.loads(line) for line in (out / "calls.jsonl").read_text().splitlines() if line.strip()]
    events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines() if line.strip()]
    samples = [json.loads(line) for line in (out / "samples.jsonl").read_text().splitlines() if line.strip()]
    if not calls:
        return "no calls"
    t0, t1 = calls[0]["t"], calls[-1]["t"]
    load = [c for c in calls if c["op"] != "probe_loop"]
    lines = [f"Calls: {len(load)} operations in {(t1 - t0) / 60:.1f} min ({len(load) / (t1 - t0):.1f}/s), "
             f"{sum(not c['ok'] for c in load)} failed; loop probe {len(calls) - len(load)}.", "",
             table(latencies(calls)), ""]
    errs = defaultdict(int)
    for c in calls:
        if not c["ok"]:
            errs[(c["op"], c.get("err", "")[:90])] += 1
    if errs:
        lines += ["Failures (op, error, count):", ""]
        lines += [f"- {op}: {e} ({n})" for (op, e), n in sorted(errs.items(), key=lambda x: -x[1])[:25]]
        lines.append("")
    lines += ["Processes (each run of a pid; RSS in MiB):", "",
              "| part | pid | minutes | RSS first | RSS last | RSS max | CPU % | threads max | fds first | fds last | fds max |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for part in UNITS:
        for seg in segments(samples, part):
            a, b = seg[0], seg[-1]
            mins = (b["t"] - a["t"]) / 60
            cpu = 100 * (b["cpu_s"] - a["cpu_s"]) / (b["t"] - a["t"]) if b["t"] > a["t"] else 0
            lines.append(f"| {part} | {a['pid']} | {mins:.1f} | {a['rss_kb'] / 1024:.1f} | {b['rss_kb'] / 1024:.1f} "
                         f"| {max(x['rss_kb'] for x in seg) / 1024:.1f} | {cpu:.1f} | {max(x['threads'] for x in seg)} "
                         f"| {a['fds']} | {b['fds']} | {max(x['fds'] for x in seg)} |")
    lines.append("")
    first, last = samples[0], samples[-1]
    lines.append(f"Journal: {first['journal_bytes'] / 1e6:.1f} MB -> {last['journal_bytes'] / 1e6:.1f} MB; "
                 f"records {first['records_bytes'] / 1e6:.1f} -> {last['records_bytes'] / 1e6:.1f} MB; "
                 f"replay cache file {first['replay_bytes'] / 1e3:.0f} -> {last['replay_bytes'] / 1e3:.0f} kB "
                 f"(max {max(s['replay_lines'] or 0 for s in samples)} lines).")
    ver = [s["verify"] for s in samples if isinstance(s.get("verify"), dict) and "rows" in s["verify"]]
    if ver:
        lines += ["", "Chain verification (a full check of a snapshot):", "",
                  "| journal rows | check s | problems |", "|---:|---:|---|"]
        lines += [f"| {v['rows']} | {v['verify_s']:.2f} | {v['problems'] or 'none'} |" for v in ver]
    chaos = [e for e in events if e["kind"] in ("chaos", "plugin_push", "plugin_rollback")]
    if chaos:
        lines += ["", "Disruptions:", ""]
        for e in chaos:
            when = time.strftime("%H:%M:%S", time.localtime(e["t"]))
            if e["kind"] == "chaos":
                back, hurt = recovery(calls, e["start"])
                when = time.strftime("%H:%M:%S", time.localtime(e["start"]))
                lines.append(f"- {when} {e['what']}: " + (
                    f"the script took {e['took']} s; worker calls ok again after "
                    f"{'never' if back is None else f'{back:.1f} s'}; {hurt} calls failed or took > 5 s"
                    if e["ok"] else f"FAILED {e['error']}"))
        pushes = [e for e in chaos if e["kind"] != "chaos"]
        bad = [e for e in pushes if not e["ok"]]
        lines.append(f"- plugin push/rollback: {len(pushes)} ({len(bad)} failed"
                     + (f": {sorted({e.get('detail') or '' for e in bad})[:3]}" if bad else "") + ")")
    text = "\n".join(lines) + "\n"
    (out / "report.md").write_text(text)
    return text


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sub.add_parser("teardown")
    r = sub.add_parser("run")
    r.add_argument("--minutes", type=float, default=45)
    r.add_argument("--rate", type=float, default=20, help="operations a second, all threads together")
    r.add_argument("--chaos-every", type=float, default=300, help="seconds between disruptions (0: none)")
    r.add_argument("--plugin-every", type=float, default=120, help="seconds per push + rollback (0: none)")
    r.add_argument("--sample-every", type=float, default=5)
    r.add_argument("--verify-every", type=float, default=300)
    r.add_argument("--out", required=True)
    b = sub.add_parser("burst")
    b.add_argument("--seconds", type=float, default=30)
    b.add_argument("--threads", type=int, default=8)
    b.add_argument("--cap", default="soak.ping")
    b.add_argument("--args", help="the cap's args as JSON (soak.ping: {\"text\": \"b\"})")
    b.add_argument("--out", required=True)
    p = sub.add_parser("report")
    p.add_argument("out")
    args = ap.parse_args(argv)
    if args.cmd == "report":
        print(report(Path(args.out)))
        return 0
    return {"setup": setup, "teardown": teardown, "run": run, "burst": burst}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
