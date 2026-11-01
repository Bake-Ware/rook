"""Helpers for the Castling 0.5 end-to-end tests: real processes on
localhost, a UDP relay (relay.py), ``python -m rook.core2.hub`` and
``python -m rook.core2.worker``, with generated keys and configs in a temp
folder, talking over the real Telesthete transport. Tests drive the hub
through its HTTP door.

Every wait has a deadline, and every process is killed at the end whatever
happened. Process logs are kept in the test's temp folder
(``<tmp>/<name>.log``) and printed when a test fails.

    python -m pytest tests/e2e -q        (rook-pytest tests/e2e -q on cachyrig)
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("nacl")
pytest.importorskip("telesthete")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.transport import band_of  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
E2E_TIMEOUT = int(os.environ.get("E2E_TIMEOUT", "150"))
START_SECS = 30.0
HTTP_SECS = 60.0
WHY = "End-to-end test of the Castling hub and worker."
PORTS = ("battery", "torrent", "shell", "file")   # real 0.5 ports the worker loads too


def until(cond, seconds: float = 20.0, what: str = "condition", every: float = 0.1):
    """Poll ``cond()`` until it returns something truthy; that value is returned."""
    deadline = time.monotonic() + seconds
    while True:
        got = cond()
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {seconds:g}s waiting for {what}")
        time.sleep(every)


class Proc:
    """A child process; its stdout and stderr lines are kept (and written to
    ``log``). JSON lines are parsed."""

    def __init__(self, name: str, argv: list, cwd: Path, env: dict, log: Path):
        self.name, self.log = name, log
        self.lines: list[str] = []
        self.p = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, bufsize=1, start_new_session=True)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self):
        with open(self.log, "a", encoding="utf-8") as f:
            for line in self.p.stdout:
                self.lines.append(line.rstrip("\n"))
                f.write(line)
                f.flush()

    def records(self) -> list[dict]:
        out = []
        for line in list(self.lines):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
        return out

    def wait_log(self, msg: str, seconds: float = START_SECS, since: int = 0) -> dict:
        def found():
            if self.p.poll() is not None:
                raise AssertionError(f"{self.name} exited ({self.p.returncode}):\n" + "\n".join(self.lines[-30:]))
            return next((r for r in self.records()[since:] if r.get("msg") == msg), None)
        return until(found, seconds, f"{self.name} to log {msg!r}")

    @property
    def pid(self) -> int:
        return self.p.pid

    def stop(self, seconds: float = 20.0) -> int | None:
        if self.p.poll() is None:
            self.p.send_signal(signal.SIGTERM)
            try:
                self.p.wait(seconds)
            except subprocess.TimeoutExpired:
                os.killpg(self.p.pid, signal.SIGKILL)
                self.p.wait(10)
        self._reader.join(5)
        return self.p.returncode

    def kill(self) -> None:
        if self.p.poll() is None:
            os.killpg(self.p.pid, signal.SIGKILL)
            self.p.wait(10)
        self._reader.join(5)


class Door:
    """The hub's HTTP door, as one token holder."""

    def __init__(self, url: str, token: str):
        self.url, self.token = url, token

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_SECS) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code, **json.loads(e.read() or b"{}")}

    def get(self, path: str) -> dict:
        return self.request("GET", path)

    def post(self, path: str, body: dict) -> dict:
        return self.request("POST", path, body)

    def call(self, cap: str, args: dict | None = None, *, worker: str = "e2e-worker", why: str | None = WHY,
             **extra) -> dict:
        body = {"worker": worker, "cap": cap, "args": args, **extra}
        if why is not None:
            body["why"] = why
        return self.post("/call", body)


class Band:
    """One relay, one hub, one worker, all generated under ``root``; the
    worker enrolls with the hub the first time it starts. The test holds the
    hub's keys (it wrote them), so it can mint tickets the way a hub would."""

    def __init__(self, root: Path):
        self.root = root
        self.procs: list[Proc] = []
        self.psk = secrets.token_hex(24)
        self.band = band_of(self.psk)
        self.root_key, self.op_key = SigningKey.generate(), SigningKey.generate()
        self.hub_id = wire.key_id(bytes(self.root_key.verify_key))
        self.env = {**os.environ, "ROOK_E2E_PSK": self.psk, "PYTHONUNBUFFERED": "1",
                    "PYTHONPATH": os.pathsep.join(p for p in (str(REPO), str(REPO.parent / "telesthete"),
                                                              os.environ.get("PYTHONPATH", "")) if p)}
        self.hub_dir, self.worker_dir = root / "hub", root / "worker"
        self.library = self.hub_dir / "library"
        self.hub = self.worker = self.relay = None
        self.relay_addr = None

    # -- set up -----------------------------------------------------------------

    def write(self) -> None:
        keys = self.hub_dir / "data" / "keys"
        keys.mkdir(parents=True, mode=0o700)
        self.library.mkdir()
        # plugin pushes and rollouts leave change records when the plugin is there
        (self.library / "changes").symlink_to(REPO / "rook" / "plugins" / "changes", target_is_directory=True)
        for name, key in (("root.key", self.root_key), ("op.key", self.op_key)):
            (keys / name).write_text(bytes(key).hex() + "\n")
            (keys / name).chmod(0o600)
        self.start_relay()
        band = [{"psk_env": "ROOK_E2E_PSK", "relay": self.relay_addr}]
        (self.hub_dir / "hub.yaml").write_text(json.dumps({
            "data": "./data",
            "library": "./library",
            "users": {"bake": {"preset": "owner"}, "kid": {"preset": "child", "grants": ["e2e.*"]}},
            "agents": {"claude-code": {"grants": ["*"], "max_risk": "admin"}},
            "doors": {"http": {"bind": "127.0.0.1:0"}, "mcp": {"bind": "127.0.0.1:0"}},
            "transport": {"bands": band},
            "log": {"level": "info", "format": "json"},
            "settings": {"hub": {"security": "on"}},     # Tests written for hub.security on (holds, gates, device-code approval); security off has its own tests.
        }, indent=1))
        self.worker_dir.mkdir()
        # No anchor and no band: enrollment (start_worker) writes those.
        (self.worker_dir / "worker.yaml").write_text(json.dumps({
            "data": "./data",
            "name": "e2e-worker",
            "plugins": [str(HERE / "plugins"), *(str(REPO / "rook" / "plugins" / p) for p in PORTS)],
            "log": {"level": "info", "format": "json"},
        }, indent=1))
        self.owner_token = self.hub_cli("token", "--person", "bake")
        self.kid_token = self.hub_cli("token", "--person", "kid")

    def start_relay(self) -> str:
        self.relay = self._spawn("relay", [sys.executable, str(HERE / "relay.py")], self.root)
        port = until(lambda: self.relay.lines and self.relay.lines[0].strip().isdigit() and self.relay.lines[0],
                     START_SECS, "the relay's port")
        self.relay_addr = f"127.0.0.1:{int(port)}"
        return self.relay_addr

    def hub_cli(self, *args: str, input: str | None = None) -> str:
        r = subprocess.run([sys.executable, "-m", "rook.core2.hub", "--config", str(self.hub_dir / "hub.yaml"),
                            *args], cwd=self.hub_dir, env=self.env, capture_output=True, text=True, timeout=60,
                           input=input if input is not None else "")
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def _spawn(self, name: str, argv: list, cwd: Path) -> Proc:
        n = sum(p.name == name for p in self.procs)
        p = Proc(name, argv, cwd, self.env, self.root / f"{name}{'-' + str(n) if n else ''}.log")
        self.procs.append(p)
        return p

    def start_hub(self) -> Proc:
        self.hub = self._spawn("hub", [sys.executable, "-m", "rook.core2.hub", "--config", "hub.yaml"],
                               self.hub_dir)
        self.hub.wait_log("hub started")
        doors = {r["door"]: r["bind"] for r in self.hub.records() if r.get("msg") == "door open"}
        self.url, self.mcp_url = f"http://{doors['http']}", f"http://{doors['mcp']}/mcp"
        self.owner = Door(self.url, self.owner_token)
        self.kid = Door(self.url, self.kid_token)
        return self.hub

    def worker_cli(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml", *args],
                              cwd=cwd or self.worker_dir, env=self.env, capture_output=True, text=True, timeout=60)

    def enroll(self) -> None:
        """Enroll the worker the way a person with the QR would: the owner
        asks the hub for a one-time code, the worker claims it."""
        qr = self.owner.post("/enroll/code", {})
        assert qr.get("ok"), qr
        r = self.worker_cli("enroll", qr["qr"])
        assert r.returncode == 0, r.stderr
        anchor = json.loads((self.worker_dir / "anchor.json").read_text())
        assert anchor == {"hub": self.hub_id, "root": wire.pub_text(self.root_key), "band": self.band}

    def start_worker(self) -> Proc:
        if not (self.worker_dir / "anchor.json").exists():
            self.enroll()
        self.worker = self._spawn("worker", [sys.executable, "-m", "rook.core2.worker", "--config", "worker.yaml"],
                                  self.worker_dir)
        started = self.worker.wait_log("worker started")
        self.worker_id = started["worker"]
        return self.worker

    def worker_calls(self, cap: str) -> int:
        """Calls of ``cap`` the worker served (its ``call`` log lines)."""
        return sum(r.get("msg") == "call" and r.get("cap") == cap for r in self.worker.records())

    def caps(self, worker: str = "e2e-worker") -> dict:
        got = self.owner.get(f"/caps?worker={worker}")
        return next(iter(got.get("workers", {}).values()), {}) if got.get("ok") else {}

    def wait_caps(self, *names: str, seconds: float = 30.0) -> dict:
        return until(lambda: (c := self.caps()) and all(n in c.get("caps", {}) for n in names) and c,
                     seconds, f"the hub to list {', '.join(names)}")

    def up(self) -> "Band":
        self.write()
        self.start_hub()
        self.start_worker()
        self.wait_caps("e2e.ping")
        return self

    def close(self) -> None:
        for p in reversed(self.procs):
            if p.name == "relay":
                p.kill()
            else:
                p.stop()

    # -- acting as a hub would (the test holds its keys) ------------------------------

    def ticket(self, cap: str, args: dict | None, call: str, **extra) -> dict:
        now = int(time.time())
        return wire.sign(self.op_key, {
            "typ": "rook-ticket", "v": 2, "hub": self.hub_id, "band": self.band, "call": call,
            "nonce": secrets.token_hex(16), "worker": self.worker_id, "cap": cap,
            "args_hash": wire.args_hash(args), "person": "person:bake", "session": "s_e2e",
            "why": WHY, "iat": now, "exp": now + 60, **extra})


def tv(folder: Path, version: str, *, broken: bool = False) -> Path:
    """The pushed fixture plugin, written into ``folder`` (the hub's library
    copy is replaced in place: a new version is just new files there)."""
    if folder.exists():
        shutil.rmtree(folder)
    (folder / "scripts").mkdir(parents=True)
    (folder / "plugin.yaml").write_text(
        f"manifest: 1\nplugin: tv\nversion: {version}\ntitle: TV\ndoc: The pushed e2e fixture.\n"
        "caps:\n  ping:\n    doc: Answer with this version.\n    risk: read\n    scope: self\n"
        "    run: scripts/main.py:ping\n"
        "check: [tv.ping]\n")
    body = ("def ping(ctx):\n    raise ctx.Error('screen not answering')\n" if broken else
            f"import os\n\n\ndef ping(ctx):\n    return {{'version': {version!r}, 'pid': os.getpid()}}\n")
    (folder / "scripts" / "main.py").write_text(body)
    return folder
