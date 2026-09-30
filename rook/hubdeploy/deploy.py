"""Hub deploy engine: unpack, preflight, back up, switch, restart, verify, roll back.

On-disk layout under ``cfg.root``::

    releases/<version>/            one clean tree per release (never overlaid)
    releases/<version>/RELEASE.json  the signed manifest it was unpacked from
    state/downloads/               fetched tarballs
    state/history.jsonl            one JSON line per deploy/rollback event
    state/deploy.lock              flock: one deploy at a time
    state/deploys/<id>/            per deploy: plan.json, selectors.json (the
                                   pre-deploy selector snapshot), db/ backups,
                                   strays/ (adopted drop-ins), rollback.sh,
                                   switched (services already switched),
                                   deadman.armed (present while armed)

The rollback script is plain ``/bin/sh``: it restores the snapshot, reloads
systemd, restarts only the services that were switched, and logs to history.
The dead-man timer runs it with ``--deadman``, which is a no-op once the deploy
is disarmed (marker removed). So a deploy that dies half-way (lost SSH, crashed
tool, wedged service) reverts on its own.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import glob
import json
import os
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import manifest as mf
from .config import Config, Service
from .selector import Selector, restart_argv, restore_script_lines, systemctl_argv


class DeployError(Exception):
    pass


def _now() -> float:
    return time.time()


def _log_default(msg: str) -> None:
    print(msg, flush=True)


def build_of(version: str | None) -> int:
    try:
        return int(str(version).split(".", 1)[0])
    except (ValueError, TypeError):
        return 0


# -- locking + history ----------------------------------------------------------

@contextlib.contextmanager
def deploy_lock(cfg: Config):
    cfg.state.mkdir(parents=True, exist_ok=True)
    fd = os.open(cfg.state / "deploy.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeployError("another hub deploy is running (state/deploy.lock is held)") from None
        yield
    finally:
        os.close(fd)


def history_path(cfg: Config) -> Path:
    return cfg.state / "history.jsonl"


def append_history(cfg: Config, event: dict) -> None:
    cfg.state.mkdir(parents=True, exist_ok=True)
    with open(history_path(cfg), "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": round(_now(), 3), **event}, sort_keys=True) + "\n")


def read_history(cfg: Config) -> list[dict]:
    try:
        lines = history_path(cfg).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for ln in lines:
        with contextlib.suppress(ValueError):
            out.append(json.loads(ln))
    return out


def previous_release(history: list[dict], service: str, current: str | None) -> str | None:
    """The release ``service`` ran before it was last switched to ``current``,
    counting only switches that completed (result ok)."""
    for ev in reversed(history):
        if ev.get("result") != "ok":
            continue
        ch = (ev.get("services") or {}).get(service)
        if ch and ch.get("to") == current and ch.get("from") and ch["from"] != current:
            return ch["from"]
    return None


# -- release dirs ----------------------------------------------------------------

def read_release(cfg: Config, version: str) -> dict | None:
    try:
        return json.loads((cfg.release_dir(version) / "RELEASE.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def unpack(cfg: Config, tarball: Path, manifest: dict) -> Path:
    """Extract into a fresh ``releases/<version>``. Never overlays an existing
    tree: the same release is reused only if it came from the same artifact."""
    version = manifest["version"]
    dest = cfg.release_dir(version)
    if dest.exists():
        rel = read_release(cfg, version)
        if rel and rel.get("sha256") == manifest["sha256"]:
            return dest
        raise DeployError(f"{dest} exists but was not unpacked from this artifact; "
                          "remove it (or prune) before deploying this release")
    cfg.releases.mkdir(parents=True, exist_ok=True)
    tmp = cfg.releases / f".unpack-{version}-{os.getpid()}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    try:
        with tarfile.open(tarball, "r:*") as tf:
            for m in tf.getmembers():
                name = m.name
                if name != version and not name.startswith(version + "/"):
                    raise DeployError(f"artifact member {m.name!r} is outside {version}/")
            if hasattr(tarfile, "data_filter"):
                tf.extractall(tmp, filter="data")
            else:  # pragma: no cover - very old 3.11
                for m in tf.getmembers():
                    if m.issym() or m.islnk() or m.name.startswith("/") or ".." in Path(m.name).parts:
                        raise DeployError(f"unsafe artifact member {m.name!r}")
                tf.extractall(tmp)
        src = tmp / version
        if not src.is_dir():
            raise DeployError(f"artifact has no top-level {version}/ directory")
        (src / "RELEASE.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.rename(src, dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


# -- preflight --------------------------------------------------------------------

_IMPORT_CHECK = r"""
import importlib, json, os, pkgutil, sys
rel = os.path.realpath(sys.argv[1])
names = [a for a in sys.argv[2:] if not a.startswith("pkg:")]
for a in sys.argv[2:]:
    if a.startswith("pkg:"):
        pkg = importlib.import_module(a[4:])
        names.append(pkg.__name__)
        names += [m.name for m in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + ".")
                  if m.name.rsplit(".", 1)[-1] != "__main__"]
out = {}
for name in names:
    try:
        mod = importlib.import_module(name)
    except BaseException as e:
        sys.exit(f"import {name} failed: {type(e).__name__}: {e}")
    f = os.path.realpath(getattr(mod, "__file__", "") or "")
    if not f.startswith(rel + os.sep):
        sys.exit(f"{name} was imported from {f}, not from the release {rel} "
                 "(an installed copy shadows it?)")
    out[name] = f
print(json.dumps({"imported": len(out)}))
"""


def _service_env(release: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONPATH"] = str(release)
    env["ROOK_RELEASE_DIR"] = str(release)
    return env


def preflight(cfg: Config, version: str, services: list[Service],
              tests: list[str] = (), log=_log_default) -> None:
    release = cfg.release_dir(version)
    if not release.is_dir():
        raise DeployError(f"release {version} is not unpacked at {release}")
    by_python: dict[str, list[str]] = {}
    for s in services:
        by_python.setdefault(cfg.python_for(s), [])
        for m in [*s.modules, *(f"pkg:{p}" for p in s.packages)]:
            if m not in by_python[cfg.python_for(s)]:
                by_python[cfg.python_for(s)].append(m)
    for python, modules in by_python.items():
        if not modules:
            continue
        log(f"preflight: importing {', '.join(modules)} with {python}")
        try:
            r = subprocess.run([python, "-c", _IMPORT_CHECK, str(release), *modules],
                               cwd=release, env=_service_env(release), capture_output=True,
                               text=True, timeout=300)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DeployError(f"preflight could not run {python}: {e}") from None
        if r.returncode != 0:
            tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-15:])
            raise DeployError(f"preflight import failed with {python}:\n{tail}")
        with contextlib.suppress(ValueError, IndexError, KeyError):
            n = json.loads(r.stdout.strip().splitlines()[-1])["imported"]
            log(f"preflight: {n} modules import cleanly from the release")
    if tests:
        python = cfg.python_for(services[0]) if services else cfg.python
        log(f"preflight: running tests {' '.join(tests)}")
        r = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests],
                           cwd=release, env=_service_env(release), capture_output=True,
                           text=True, timeout=3600)
        if r.returncode != 0:
            tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-25:])
            raise DeployError(f"preflight tests failed:\n{tail}")
        log("preflight: " + ((r.stdout or "").strip().splitlines() or ["tests passed"])[-1])


# -- DB backups -------------------------------------------------------------------

def backup_databases(cfg: Config, dest: Path, log=_log_default) -> list[dict]:
    """Consistent copies of every configured SQLite DB via the sqlite3 backup API
    (safe while the services are writing; no sqlite3 CLI needed)."""
    seen, out = set(), []
    for pattern in cfg.databases:
        for p in sorted(glob.glob(os.path.expanduser(pattern))):
            path = Path(p).resolve()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / f"{len(out):02d}-{path.name}"
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
            try:
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            except sqlite3.DatabaseError as e:
                target.unlink(missing_ok=True)
                raise DeployError(f"backup of {path} failed: {e}") from None
            finally:
                src.close()
            out.append({"source": str(path), "backup": str(target),
                        "size": target.stat().st_size})
    if out:
        log(f"backed up {len(out)} database(s) to {dest}")
        (dest / "index.json").write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return out


# -- rollback script + dead-man ---------------------------------------------------

def write_rollback_script(cfg: Config, ddir: Path, deploy_id: str, snapshot: list[dict],
                          services: list[Service], changes: dict) -> Path:
    q = shlex.quote
    marker, switched = ddir / "deadman.armed", ddir / "switched"
    back = {n: {"from": c["to"], "to": c["from"]} for n, c in changes.items()}
    event = json.dumps({"event": "rollback", "id": deploy_id, "result": "ok",
                        "services": back, "via": "rollback.sh"}, sort_keys=True)
    event = event[1:].replace("%", "%%")  # printf format, "{" re-added below
    lines = [
        "#!/bin/sh",
        f"# Roll back hub deploy {deploy_id}: restore the release selectors as they",
        "# were before it, then restart the services it had switched.",
        "#   sh rollback.sh             roll back now",
        "#   sh rollback.sh --deadman   (dead-man timer) only if still armed",
        "# Databases are NOT restored automatically; backups are in ./db/.",
        "set -u",
        f"MARKER={q(str(marker))}",
        f"SWITCHED={q(str(switched))}",
        'REASON="${ROOK_ROLLBACK_REASON:-manual}"',
        'if [ "${1:-}" = "--deadman" ]; then',
        f'  [ -e "$MARKER" ] || {{ echo "deploy {deploy_id} was disarmed; nothing to roll back"; exit 0; }}',
        '  REASON=deadman',
        "fi",
        'rm -f "$MARKER"',
        f'echo "rolling back hub deploy {deploy_id} ($REASON)"',
        *restore_script_lines(cfg, snapshot),
        "rc=0",
    ]
    for s in services:
        argv = " ".join(q(a) for a in restart_argv(cfg, s))
        lines.append(f'if [ -e "$SWITCHED" ] && grep -qxF {q(s.name)} "$SWITCHED"; then')
        lines.append(f'  echo "restarting {s.name}"; {argv} || rc=1')
        lines.append("fi")
    fmt = '{"ts": %s, "reason": "%s", ' + event + "\\n"
    lines += [
        f'printf {q(fmt)} "$(date +%s)" "$REASON" >> {q(str(history_path(cfg)))}',
        'echo "rollback finished (rc=$rc)"',
        'exit "$rc"',
    ]
    path = ddir / "rollback.sh"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _deadman_unit(deploy_id: str) -> str:
    return f"rook-hub-deadman-{deploy_id}"


def arm_deadman(cfg: Config, ddir: Path, deploy_id: str, script: Path, minutes: float,
                log=_log_default) -> dict:
    (ddir / "deadman.armed").write_text(str(_now()), encoding="utf-8")
    secs = max(1, int(minutes * 60))
    info: dict = {"seconds": secs, "armed_at": _now()}
    if cfg.mode == "systemd" and shutil.which("systemd-run"):
        unit = _deadman_unit(deploy_id)
        argv = ["systemd-run", *(["--user"] if cfg.scope == "user" else []),
                f"--unit={unit}", f"--on-active={secs}s",
                f"--description=Rook hub dead-man rollback for deploy {deploy_id}",
                "/bin/sh", str(script), "--deadman"]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            (ddir / "deadman.armed").unlink(missing_ok=True)
            raise DeployError(f"could not arm the dead-man timer: {r.stderr.strip()}")
        info.update(kind="systemd-run", unit=unit)
    else:
        p = subprocess.Popen(["/bin/sh", "-c", f"sleep {secs}; exec /bin/sh {shlex.quote(str(script))} --deadman"],
                             stdin=subprocess.DEVNULL, stdout=open(ddir / "deadman.log", "ab"),
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        info.update(kind="sleeper", pid=p.pid)
    (ddir / "deadman.json").write_text(json.dumps(info) + "\n", encoding="utf-8")
    log(f"dead-man armed: auto-rollback in {secs}s unless the deploy is verified")
    return info


def disarm_deadman(cfg: Config, ddir: Path, log=_log_default) -> None:
    (ddir / "deadman.armed").unlink(missing_ok=True)
    try:
        info = json.loads((ddir / "deadman.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if info.get("kind") == "systemd-run":
        subprocess.run(systemctl_argv(cfg, "stop", f"{info['unit']}.timer"),
                       capture_output=True, timeout=60)
    elif info.get("kind") == "sleeper" and info.get("pid"):
        pid = int(info["pid"])
        with contextlib.suppress(OSError):
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
            if str(ddir).encode() in cmd:
                os.killpg(pid, signal.SIGTERM)
    log("dead-man disarmed")


def armed_deploys(cfg: Config) -> list[str]:
    d = cfg.state / "deploys"
    if not d.is_dir():
        return []
    return sorted(p.parent.name for p in d.glob("*/deadman.armed"))


# -- restart + health -------------------------------------------------------------

def _check_url(url: str, timeout: float = 5) -> str | None:
    """None when healthy, else a short reason. http(s): any status below 500
    (a 401 from an auth-gated endpoint still proves the process serves).
    tcp://host:port: the port accepts a connection."""
    u = urllib.parse.urlparse(url)
    if u.scheme == "tcp":
        try:
            socket.create_connection((u.hostname, u.port), timeout=timeout).close()
            return None
        except OSError as e:
            return f"{url}: {e}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return None if r.status < 500 else f"{url}: HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return None if e.code < 500 else f"{url}: HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"{url}: {getattr(e, 'reason', e)}"


def unit_active(cfg: Config, svc: Service) -> str:
    try:
        r = subprocess.run(systemctl_argv(cfg, "is-active", svc.unit), capture_output=True,
                           text=True, timeout=30)
        return (r.stdout or "").strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def health_problems(cfg: Config, svc: Service, version: str, sel: Selector) -> list[str]:
    probs = []
    if svc.unit:
        st = unit_active(cfg, svc)
        if st != "active":
            probs.append(f"{svc.unit} is {st}")
        if cfg.mode == "systemd":
            eff = sel.effective(svc)
            if eff != version:
                probs.append(f"{svc.unit} effective ROOK_RELEASE is {eff}, expected {version}")
    for url in svc.health:
        p = _check_url(url)
        if p:
            probs.append(p)
    return probs


def restart_service(cfg: Config, svc: Service, log=_log_default) -> None:
    argv = restart_argv(cfg, svc)
    log(f"restarting {svc.name}: {' '.join(argv)}")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise DeployError(f"restart of {svc.name} failed: {(r.stderr or r.stdout).strip()}")


def verify_service(cfg: Config, svc: Service, version: str, sel: Selector,
                   log=_log_default) -> None:
    deadline = _now() + cfg.health_timeout
    probs = ["not checked"]
    while _now() < deadline:
        probs = health_problems(cfg, svc, version, sel)
        if not probs:
            break
        time.sleep(1)
    if probs:
        raise DeployError(f"{svc.name} did not become healthy on {version}: {'; '.join(probs)}")
    if cfg.settle_seconds:
        time.sleep(cfg.settle_seconds)
        probs = health_problems(cfg, svc, version, sel)
        if probs:
            raise DeployError(f"{svc.name} went unhealthy after start: {'; '.join(probs)}")
    log(f"{svc.name}: healthy on {version}")


# -- activation (shared by deploy and rollback) -----------------------------------

def new_deploy_id(version: str) -> str:
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{version}"


def activate(cfg: Config, plan: dict[str, str], *, kind: str = "deploy",
             restart: bool = True, parallel: bool = False, adopt_strays: bool = False,
             backup_dbs: bool = True, deadman_minutes: float | None = None,
             auto_rollback: bool = True, log=_log_default) -> dict:
    """Point each service in ``plan`` ({service: version}) at its release,
    restart and verify. On failure, run the rollback script and raise."""
    sel = Selector(cfg)
    unknown = set(plan) - set(cfg.services)
    if unknown:
        raise DeployError(f"unknown service(s): {', '.join(sorted(unknown))}")
    svcs = cfg.ordered(plan)
    for s in svcs:
        if not cfg.release_dir(plan[s.name]).is_dir():
            raise DeployError(f"release {plan[s.name]} is not unpacked")

    strays = {s.name: sel.strays(s) for s in svcs}
    bad = {n: ps for n, ps in strays.items() if ps}
    if bad:
        listing = "; ".join(f"{n}: {', '.join(map(str, ps))}" for n, ps in bad.items())
        if not adopt_strays:
            raise DeployError("other drop-ins also select a release and would override "
                              f"90-release.conf ({listing}). Re-run with --adopt-strays to "
                              "move them into this deploy's backup (rollback restores them).")
        unit_dir = cfg.systemd_unit_dir().resolve()
        foreign = [p for ps in bad.values() for p in ps
                   if unit_dir not in Path(p).resolve().parents]
        if foreign:
            raise DeployError(f"stray drop-ins outside {unit_dir} cannot be adopted: "
                              f"{', '.join(map(str, foreign))}; remove them by hand")

    version_label = sorted(set(plan.values()))[-1] if plan else "none"
    deploy_id = new_deploy_id(version_label)
    ddir = cfg.state / "deploys" / deploy_id
    ddir.mkdir(parents=True)
    snapshot, changes = [], {}
    for s in svcs:
        snapshot += sel.snapshot(s, extra=strays[s.name])
        changes[s.name] = {"from": sel.current(s), "to": plan[s.name]}
    (ddir / "selectors.json").write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")
    dbs = backup_databases(cfg, ddir / "db", log=log) if backup_dbs else []
    script = write_rollback_script(cfg, ddir, deploy_id, snapshot, svcs, changes)
    (ddir / "plan.json").write_text(json.dumps(
        {"id": deploy_id, "kind": kind, "services": changes, "restart": restart,
         "databases": dbs}, indent=2) + "\n", encoding="utf-8")
    append_history(cfg, {"event": kind, "id": deploy_id, "result": "started", "services": changes})
    log(f"{kind} {deploy_id}: " + ", ".join(f"{n} {c['from']} -> {c['to']}" for n, c in changes.items()))
    log(f"rollback plan: sh {script}")

    minutes = cfg.deadman_minutes if deadman_minutes is None else deadman_minutes
    armed = bool(restart and minutes and minutes > 0)
    if armed:
        arm_deadman(cfg, ddir, deploy_id, script, minutes, log=log)

    switched_file = ddir / "switched"
    switched_file.touch()

    def switch(s: Service) -> None:
        with open(switched_file, "a", encoding="utf-8") as f:
            f.write(s.name + "\n")
        sel.select(s, plan[s.name])

    try:
        for n, ps in bad.items():
            (ddir / "strays").mkdir(exist_ok=True)
            for p in ps:
                shutil.move(str(p), ddir / "strays" / f"{n}--{Path(p).name}")
                log(f"adopted stray drop-in {p}")
        if not restart:
            for s in svcs:
                switch(s)
            sel.reload()
        elif parallel:
            for s in svcs:
                switch(s)
            sel.reload()
            for s in svcs:
                restart_service(cfg, s, log=log)
            for s in svcs:
                verify_service(cfg, s, plan[s.name], sel, log=log)
        else:
            for s in svcs:
                switch(s)
                sel.reload()
                restart_service(cfg, s, log=log)
                verify_service(cfg, s, plan[s.name], sel, log=log)
    except (DeployError, OSError, subprocess.SubprocessError) as e:
        log(f"{kind} {deploy_id} FAILED: {e}")
        if auto_rollback:
            log("rolling back")
            r = subprocess.run(["/bin/sh", str(script)], capture_output=True, text=True,
                               env={**os.environ, "ROOK_ROLLBACK_REASON": "failed-verify"},
                               timeout=900)
            for ln in (r.stdout + r.stderr).strip().splitlines():
                log(f"  {ln}")
            if armed:
                disarm_deadman(cfg, ddir, log=log)
            append_history(cfg, {"event": kind, "id": deploy_id, "result": "failed",
                                 "error": str(e), "rolled_back": r.returncode == 0,
                                 "services": changes})
        else:
            append_history(cfg, {"event": kind, "id": deploy_id, "result": "failed",
                                 "error": str(e), "rolled_back": False, "services": changes})
            log(f"left as is; dead-man {'still armed' if armed else 'not armed'}; "
                f"roll back with: sh {script}")
        raise DeployError(str(e)) from None

    if armed:
        disarm_deadman(cfg, ddir, log=log)
    append_history(cfg, {"event": kind, "id": deploy_id, "result": "ok", "services": changes})
    log(f"{kind} {deploy_id}: done")
    return {"id": deploy_id, "services": changes, "rollback": str(script), "databases": dbs}


def deploy(cfg: Config, manifest_src: str, *, services: list[str] | None = None,
           tests: list[str] = (), pubkey: str | None = None, allow_downgrade: bool = False,
           skip_preflight: bool = False, log=_log_default, **kw) -> dict:
    with deploy_lock(cfg):
        m = mf.verify(mf.load_manifest(manifest_src), pubkey or cfg.pubkey or None)
        version = m["version"]
        log(f"manifest ok: {version} (commit {m['commit'][:12]}, signed)")
        targets = cfg.ordered(services) if services else cfg.ordered()
        if services and len(targets) != len(set(services)):
            raise DeployError(f"unknown service(s): {', '.join(sorted(set(services) - set(cfg.services)))}")
        sel = Selector(cfg)
        if not allow_downgrade:
            for s in targets:
                cur = sel.current(s)
                if cur and build_of(cur) > m["build"]:
                    raise DeployError(f"{s.name} runs {cur}, newer than {version}; "
                                      "pass --allow-downgrade (or use rollback)")
        tarball = mf.fetch_artifact(m, manifest_src, cfg.state / "downloads")
        log(f"artifact ok: {tarball.name} sha256 {m['sha256'][:16]}...")
        release = unpack(cfg, tarball, m)
        log(f"release dir: {release}")
        if not skip_preflight:
            preflight(cfg, version, targets, tests, log=log)
        return activate(cfg, {s.name: version for s in targets}, kind="deploy", log=log, **kw)


def rollback(cfg: Config, *, to: str | None = None, services: list[str] | None = None,
             log=_log_default, **kw) -> dict:
    with deploy_lock(cfg):
        sel, hist = Selector(cfg), read_history(cfg)
        targets = cfg.ordered(services) if services else cfg.ordered()
        plan = {}
        for s in targets:
            cur = sel.current(s)
            dest = to or previous_release(hist, s.name, cur)
            if not dest:
                if services:
                    raise DeployError(f"{s.name}: no previous release recorded; pass --to VERSION")
                continue
            if dest != cur:
                plan[s.name] = dest
        if not plan:
            raise DeployError("nothing to roll back (no service has a recorded previous release)")
        return activate(cfg, plan, kind="rollback", log=log, **kw)


# -- status + prune ---------------------------------------------------------------

def list_releases(cfg: Config) -> list[str]:
    if not cfg.releases.is_dir():
        return []
    vs = [p.name for p in cfg.releases.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return sorted(vs, key=lambda v: (build_of(v), v), reverse=True)


def status(cfg: Config) -> dict:
    sel, hist = Selector(cfg), read_history(cfg)
    services = {}
    for s in cfg.ordered():
        cur = sel.current(s)
        entry = {"selected": cur, "previous": previous_release(hist, s.name, cur),
                 "unit": s.unit or None}
        if s.unit:
            entry["active"] = unit_active(cfg, s)
        if cfg.mode == "systemd":
            entry["effective"] = sel.effective(s)
            entry["strays"] = [str(p) for p in sel.strays(s)]
        services[s.name] = entry
    in_use: dict[str, list[str]] = {}
    for n, e in services.items():
        for key in ("selected", "effective"):
            if e.get(key):
                in_use.setdefault(e[key], [])
                if n not in in_use[e[key]]:
                    in_use[e[key]].append(n)
    releases = []
    for v in list_releases(cfg):
        rel = read_release(cfg, v) or {}
        releases.append({"version": v, "commit": rel.get("commit"),
                         "built_at": rel.get("built_at"), "in_use_by": in_use.get(v, [])})
    return {"config": str(cfg.path), "mode": cfg.mode, "root": str(cfg.root),
            "services": services, "releases": releases, "armed": armed_deploys(cfg),
            "history": hist[-10:]}


def protected_releases(cfg: Config) -> set[str]:
    st = status(cfg)
    keep = set()
    for e in st["services"].values():
        for key in ("selected", "effective", "previous"):
            if e.get(key):
                keep.add(e[key])
    return keep


def prune(cfg: Config, keep: int | None = None, keep_deploys: int = 20,
          dry_run: bool = False, log=_log_default) -> list[str]:
    keep = cfg.keep if keep is None else keep
    if keep < 2:
        raise DeployError("keep must be at least 2")
    with deploy_lock(cfg):
        protected = protected_releases(cfg)
        removed = []
        for i, v in enumerate(list_releases(cfg)):
            if i < keep or v in protected:
                continue
            removed.append(v)
            log(f"{'would remove' if dry_run else 'removing'} release {v}")
            if not dry_run:
                shutil.rmtree(cfg.release_dir(v))
                for f in (cfg.state / "downloads").glob(f"rook-hub-{v}.*"):
                    f.unlink()
        ddirs = sorted((cfg.state / "deploys").glob("*")) if (cfg.state / "deploys").is_dir() else []
        armed = set(armed_deploys(cfg))
        old = [d for d in ddirs[:-keep_deploys] if d.name not in armed] if keep_deploys else []
        for d in old:
            log(f"{'would remove' if dry_run else 'removing'} deploy record {d.name}")
            if not dry_run:
                shutil.rmtree(d)
        return removed


def units_text(cfg: Config) -> str:
    """Generic systemd units for this config (one unit per service + its
    release drop-in), for a fresh install or to replace hand-made units."""
    from .selector import dropin_text
    out = []
    for s in cfg.ordered():
        mod = (s.modules or ["rook"])[0]
        if mod.endswith(".server") and mod.startswith("rook.band_mcp"):
            mod = "rook.band_mcp"
        out.append(f"# ---- {cfg.systemd_unit_dir() / s.unit}\n"
                   f"[Unit]\nDescription=Rook hub {s.name}\n"
                   "After=network-online.target\nWants=network-online.target\n\n"
                   "[Service]\nType=simple\n"
                   "# Settings (ROOK_*), e.g. ROOK_DATA_DIR, ROOK_BAND_PSK, ports:\n"
                   "EnvironmentFile=-/etc/rook/hub.env\n"
                   "Environment=PYTHONUNBUFFERED=1\n"
                   "# The release comes from the drop-in below: PYTHONPATH puts\n"
                   "# releases/<version> ahead of anything installed in the venv.\n"
                   f"ExecStart={cfg.python_for(s)} -m {mod}\n"
                   "Restart=on-failure\nRestartSec=3\n\n"
                   "[Install]\n"
                   f"WantedBy={'default.target' if cfg.scope == 'user' else 'multi-user.target'}\n")
        out.append(f"# ---- {cfg.dropin_path(s)}  (written by `rook hub deploy`)\n"
                   + dropin_text(cfg, "<version>"))
    return "\n".join(out)


def main_entry() -> None:  # pragma: no cover - convenience
    from .cli import main
    sys.exit(main(sys.argv[1:]))
