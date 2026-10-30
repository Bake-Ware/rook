"""Plugin distribution, hub side: how a plugin in the hub's library gets to a
worker (castling-plugins "Distribution", castling-manifest-lifecycle).

    plan = prepare("library/deluge", env, signer=op_key, hub=hub_id, cache=assets_dir)
    report = await push(plan, transport)          # the worker installs, checks, rolls back
    if report["ok"]: seed(plan.content)

or all of it at once: ``await install(folder, env, transport, signer=..., hub=...)``.

``prepare`` validates the manifest, derives the trust class, resolves
``requires.python`` with uv against the target worker's environment (a
conflict refuses with uv's reason instead of breaking the worker), fetches
``assets`` with their sha256 checked, and signs the file-hash list for that
worker (``plugin.sig``, a ``rook-plugin`` signed object, spec 13). ``push``
builds the bundle (a tar of the plugin folder, the assets under
``assets/<name>`` and ``plugin.sig``) and hands it to a :class:`Transport`.
The worker side is :mod:`rook.core2.worker.install`.

Worker parts and hub content split per plugin, not per file: the worker's
loader validates every file the manifest names and announces the hash of
the very plugin.yaml the hub holds, so a worker gets the whole folder or
nothing. A plugin whose caps and hooks all run on the hub has no worker
part. Content sections (personas, skills, docs, ...) are seeded into hub
stores from the hub's own copy (:attr:`Plan.content`).

Agent-written plugins go through a change record (castling-changes): pass
``change_record`` to :func:`install`; it gets the :class:`Plan` before
anything is pushed and refuses the install by raising.
TODO(castling:changes): the change-record plugin supplies that callable.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol

from rook.core2 import wire
from rook.kit.manifest import ManifestError, check_bump, file_hashes, load, trust_class

SIG_TYP = "rook-plugin"
SIG_FILE = "plugin.sig"
ASSET_DIR = "assets"
CONTENT = ("personas", "skills", "docs", "concepts", "pages", "components", "jobs", "presets")
UV_TIMEOUT = 180         # seconds for one uv resolution
FETCH_TIMEOUT = 60       # seconds of socket silence before an asset download fails

# A requirement line: PEP 508 starts with a project name. Anything else in a
# requirements file (``-e``, ``--index-url``, ``-r``) is an option to uv.
_REQ = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_VERSION = re.compile(r"[A-Za-z0-9.+!_-]+")
_PYTHON = re.compile(r"[0-9]+\.[0-9]+(\.[0-9]+)?")
_PLATFORM = re.compile(r"[a-z0-9][a-z0-9_.-]*")


class Refused(Exception):
    """The install can't go ahead; the message says why, for people."""


@dataclass
class WorkerEnv:
    """What the hub knows about a target worker's Python environment.

    ``packages`` is every installed distribution (name -> version), so
    resolution never moves one; ``platform`` is uv's ``--python-platform``
    (``x86_64-unknown-linux-gnu``, ``aarch64-apple-darwin``, ...), omitted =
    the hub's own."""

    worker_id: str
    os: str
    python: str
    packages: dict[str, str] = field(default_factory=dict)
    platform: str | None = None


@dataclass
class Plan:
    plugin: str
    version: str
    trust: str                  # config | page | code (derived, never declared)
    folder: Path
    worker: str | None          # worker_id, None when nothing goes to a worker
    files: dict[str, Path]      # bundle path -> source file (folder + assets)
    python: list[str]           # pins the worker installs before loading
    sig: dict | None            # plugin.sig, None without a worker part
    content: dict               # hub content to seed (see hub_content)
    hub_runs: bool              # the hub hosts caps, hooks, routes, records or a gate


class Transport(Protocol):
    """Gets a bundle to a worker and returns the worker's install report.
    Production is Drop (TODO(castling:wiring)); tests use MemoryTransport."""

    async def push(self, worker_id: str, bundle: Path) -> dict: ...


class MemoryTransport:
    """In-process fake: ``receivers[worker_id]`` is anything with
    ``async receive(bundle_path) -> dict`` (a worker Installer)."""

    def __init__(self, receivers: dict[str, Any] | None = None):
        self.receivers = dict(receivers or {})
        self.sent: list[tuple[str, bytes]] = []

    async def push(self, worker_id: str, bundle: Path) -> dict:
        self.sent.append((worker_id, Path(bundle).read_bytes()))
        receiver = self.receivers.get(worker_id)
        if receiver is None:
            return {"ok": False, "reason": f"worker {worker_id} is not reachable"}
        return await receiver.receive(bundle)


# -- where the plugin runs ---------------------------------------------------------

def needs_worker(data: dict) -> bool:
    """A normalized manifest has a worker part: a cap or hooks that run there."""
    if any(c.get("on") == "worker" for c in data["caps"].values()):
        return True
    return data["runs_on"] == "worker" and bool(data.get("hooks"))


def hub_runs(data: dict) -> bool:
    if any(c.get("on") == "hub" for c in data["caps"].values()):
        return True
    if data.get("routes") or data.get("gate") or data.get("records"):
        return True
    return data["runs_on"] == "hub" and bool(data.get("hooks"))


def hub_content(data: dict, folder: Path) -> dict:
    """The content sections seeded into hub stores, marked ``from:
    plugin@version``; file paths in them are relative to ``folder``."""
    sections = {k: data[k] for k in CONTENT if data.get(k)}
    return {"from": f"{data['plugin']}@{data['version']}", "folder": str(folder),
            "sections": sections}


# -- python requirements -------------------------------------------------------------

def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def resolve_python(requirements: list[str], env: WorkerEnv, *, run=subprocess.run) -> list[str]:
    """Pins to add to ``env`` so ``requirements`` are met without moving any
    installed package. Raises Refused with uv's reason on a conflict."""
    if not requirements:
        return []
    for req in requirements:
        if not isinstance(req, str) or not _REQ.match(req) or "\n" in req or "\r" in req:
            raise Refused(f"requires.python: {req!r} is not a requirement")
    if not _PYTHON.fullmatch(env.python or "") or (env.platform and not _PLATFORM.fullmatch(env.platform)):
        raise Refused(f"worker {env.worker_id} reported a bad python {env.python!r} or platform {env.platform!r}")
    for name, ver in env.packages.items():
        if not _REQ.fullmatch(name) or not isinstance(ver, str) or not _VERSION.fullmatch(ver):
            raise Refused(f"worker {env.worker_id} reported a bad package: {name!r} {ver!r}")
    with tempfile.TemporaryDirectory(prefix="rook-uv-") as tmp:
        req_in = Path(tmp) / "requirements.in"
        cons = Path(tmp) / "constraints.txt"
        req_in.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        cons.write_text("".join(f"{n}=={v}\n" for n, v in sorted(env.packages.items())), encoding="utf-8")
        cmd = ["uv", "pip", "compile", str(req_in), "--constraint", str(cons),
               "--python-version", env.python, "--no-header", "--no-annotate", "--quiet"]
        if env.platform:
            cmd += ["--python-platform", env.platform]
        try:
            r = run(cmd, capture_output=True, text=True, timeout=UV_TIMEOUT)
        except FileNotFoundError:
            raise Refused("uv is not installed on the hub; it resolves plugin requirements") from None
        except subprocess.TimeoutExpired:
            raise Refused(f"uv took over {UV_TIMEOUT}s resolving {', '.join(requirements)}") from None
    if r.returncode != 0:
        why = "\n".join((r.stderr or r.stdout or "").strip().splitlines()[-12:])
        raise Refused(f"python requirements conflict on worker {env.worker_id}: {why or 'uv failed'}")
    have = {_norm(n): v for n, v in env.packages.items()}
    pins = []
    for line in r.stdout.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = _REQ.match(line)
        if not m or line.startswith("-"):
            raise Refused(f"uv printed something that isn't a pin: {line!r}")
        name = _norm(m.group(0))
        if name in have:
            if f"=={have[name]}" not in line.replace(" ", ""):
                raise Refused(f"resolving would change {name} {have[name]} on worker {env.worker_id}: {line}")
            continue
        pins.append(line)
    return pins


# -- assets ---------------------------------------------------------------------------

def fetch_url(url: str, dest: Path, *, timeout: float = FETCH_TIMEOUT) -> None:
    """Download ``url`` to ``dest``. Only http(s): the hash check makes the
    transport irrelevant to integrity, but a manifest must not read hub files."""
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise Refused(f"asset url must be http or https: {url}")
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_assets(assets: dict, cache: Path, *, fetch: Callable = fetch_url) -> dict[str, Path]:
    """Each asset as a file in ``cache`` named by its sha256, downloaded once
    and hash-checked (and size-checked when the manifest gives one)."""
    cache.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, a in sorted(assets.items()):
        path = cache / a["sha256"]
        if path.is_file() and _sha256(path) == a["sha256"]:
            out[name] = path
            continue
        part = cache / f"{a['sha256']}.part"
        try:
            fetch(a["url"], part)
            got = _sha256(part)
            if got != a["sha256"]:
                raise Refused(f"asset {name}: sha256 is {got}, the manifest says {a['sha256']}")
            if "size" in a and part.stat().st_size != a["size"]:
                raise Refused(f"asset {name}: {part.stat().st_size} bytes, the manifest says {a['size']}")
        except Refused:
            part.unlink(missing_ok=True)
            raise
        except Exception as e:
            part.unlink(missing_ok=True)
            raise Refused(f"asset {name}: download failed: {e}") from e
        part.replace(path)
        out[name] = path
    return out


# -- prepare, sign, push ----------------------------------------------------------------

def sign_files(signer, *, hub: str, worker: str, plugin: str, version: str,
               files: dict[str, str], python: list[str], now: int | None = None) -> dict:
    """plugin.sig: the hub operational key's signature over the file-hash list."""
    return wire.sign(signer, {"typ": SIG_TYP, "v": 2, "hub": hub, "worker": worker,
                              "plugin": plugin, "version": version, "files": files,
                              "python": python, "iat": int(time.time()) if now is None else now})


def prepare(folder, env: WorkerEnv | None, *, signer, hub: str, current: dict | None = None,
            cache: Path | None = None, fetch: Callable = fetch_url,
            resolve: Callable = resolve_python, now: int | None = None) -> Plan:
    """Everything up to the push, for one target worker (``env``; None for a
    hub-only plugin). ``current`` is the parsed manifest that worker runs now,
    if any: a breaking change without a major bump is refused."""
    folder = Path(folder).resolve()
    try:
        m = load(folder)
    except ManifestError as e:
        raise Refused(f"invalid manifest: {e}") from None
    except ValueError as e:      # a symlink in the folder, an unreadable file
        raise Refused(f"invalid plugin folder: {e}") from None
    data = m.data
    if data["builtin"]:
        raise Refused(f"{m.plugin} is a builtin manifest: announced by its worker, never pushed")
    if current is not None and current.get("version") != m.version:
        errors = [i for i in check_bump(current, m) if i.level == "error"]
        if errors:
            raise Refused(f"{m.plugin} {current.get('version')} -> {m.version}: "
                          + "; ".join(i.message for i in errors))
    try:
        trust = trust_class(folder)
        hashes = file_hashes(folder)
    except ValueError as e:
        raise Refused(f"invalid plugin folder: {e}") from None
    plan = Plan(m.plugin, m.version, trust, folder, None, {}, [], None,
                hub_content(data, folder), hub_runs(data))
    if env is None or not needs_worker(data):
        return plan
    platforms = data.get("platforms")
    if platforms and env.os not in platforms:
        raise Refused(f"{m.plugin} runs on {', '.join(platforms)}, not {env.os}")
    plan.python = resolve((data.get("requires") or {}).get("python") or [], env)
    files = {rel: folder / rel for rel in hashes}
    if data.get("assets"):
        if cache is None:
            raise ValueError("this plugin has assets: pass an asset cache directory")
        for name, path in fetch_assets(data["assets"], cache, fetch=fetch).items():
            rel = f"{ASSET_DIR}/{name}"
            if rel in files:
                raise Refused(f"{rel} is both a file in the folder and an asset")
            files[rel] = path
            hashes[rel] = data["assets"][name]["sha256"]
    plan.worker = env.worker_id
    plan.files = dict(sorted(files.items()))
    plan.sig = sign_files(signer, hub=hub, worker=env.worker_id, plugin=m.plugin, version=m.version,
                          files=dict(sorted(hashes.items())), python=plan.python, now=now)
    return plan


def _add(tar: tarfile.TarFile, name: str, size: int, fileobj) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, 0o644, 0
    tar.addfile(info, fileobj)


def write_bundle(plan: Plan, dest: Path) -> Path:
    """The tar the worker receives: every planned file, then plugin.sig."""
    if plan.sig is None:
        raise ValueError(f"{plan.plugin} has no worker part")
    with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as tar:
        for rel, src in plan.files.items():
            with open(src, "rb") as f:
                _add(tar, rel, src.stat().st_size, f)
        sig = json.dumps(plan.sig, sort_keys=True).encode("utf-8")
        _add(tar, SIG_FILE, len(sig), BytesIO(sig))
    return dest


async def push(plan: Plan, transport: Transport) -> dict:
    """Send the worker part and return the worker's report. A plan with no
    worker part is a no-op."""
    if plan.sig is None:
        return {"ok": True, "plugin": plan.plugin, "version": plan.version, "pushed": False}
    with tempfile.TemporaryDirectory(prefix="rook-bundle-") as tmp:
        bundle = write_bundle(plan, Path(tmp) / f"{plan.plugin}-{plan.version}.tar")
        return await transport.push(plan.worker, bundle)


async def install(folder, env: WorkerEnv | None, transport: Transport, *, signer, hub: str,
                  current: dict | None = None, cache: Path | None = None,
                  seed: Callable[[dict], Any] | None = None,
                  change_record: Callable[[Plan], Any] | None = None,
                  fetch: Callable = fetch_url, resolve: Callable = resolve_python) -> dict:
    """prepare -> change record -> push -> seed hub content once the worker
    reports success. Always returns a report; ``reason`` says why it failed."""
    try:
        plan = prepare(folder, env, signer=signer, hub=hub, current=current, cache=cache,
                       fetch=fetch, resolve=resolve)
    except Refused as e:
        return {"ok": False, "reason": str(e)}
    if change_record is not None:
        try:
            change_record(plan)
        except Exception as e:
            return {"ok": False, "plugin": plan.plugin, "version": plan.version,
                    "reason": f"change record refused: {e}"}
    try:
        report = await push(plan, transport)
    except Exception as e:
        return {"ok": False, "plugin": plan.plugin, "version": plan.version, "reason": f"push failed: {e}"}
    if report.get("ok") and seed is not None:
        seed(plan.content)
    return report
