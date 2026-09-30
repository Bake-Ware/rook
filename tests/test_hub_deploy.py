"""Hub self-deploy (rook/hubdeploy): signed releases, one selector, preflight,
backups, verify + auto-rollback, dead-man, status/prune. No hub needed: the
"services" are a tiny HTTP server in a throwaway git repo, restarted by a
helper script (symlink mode) or by a fake ``systemctl`` (systemd mode)."""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import textwrap
import time
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from rook.hubdeploy import cli, config as cfgmod, deploy as dp, manifest as mf, selector as selmod
from rook.worker._update_verify import verify_manifest as worker_verify

SERVER = textwrap.dedent('''
    import http.server, json, os, pathlib, sys
    REL = pathlib.Path(__file__).resolve().parents[1]
    def main():
        if (REL / "BROKEN").exists():
            sys.exit(3)
        ver = json.loads((REL / "RELEASE.json").read_text())["version"]
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(ver.encode())
            def log_message(self, *a):
                pass
        http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
    if __name__ == "__main__":
        main()
''')

# Restarts the fake service: kill the previous one, start a new one with
# PYTHONPATH taken from the selector (symlink, or the drop-in via env).
RESTART_HELPER = textwrap.dedent('''
    import os, signal, subprocess, sys, time
    pidfile, pythonpath, port = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        pid = int(open(pidfile).read())
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            try:
                os.kill(pid, 0); time.sleep(0.05)
            except OSError:
                break
    except (OSError, ValueError):
        pass
    env = {**os.environ, "PYTHONPATH": pythonpath}
    p = subprocess.Popen([sys.executable, "-m", "fakesvc.server", port], env=env,
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(pidfile, "w").write(str(p.pid))
''')


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def key(monkeypatch):
    sk = SigningKey.generate()
    monkeypatch.setenv("ROOK_UPDATE_PUBKEY", base64.b64encode(bytes(sk.verify_key)).decode())
    return sk


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "fakesvc").mkdir(parents=True)
    (r / "fakesvc" / "__init__.py").write_text("")
    (r / "fakesvc" / "server.py").write_text(SERVER)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "v1")
    return r


def commit(repo: Path, msg: str, files: dict[str, str | None] | None = None) -> None:
    for rel, text in (files or {}).items():
        p = repo / rel
        if text is None:
            p.unlink()
        else:
            p.write_text(text)
    (repo / "CHANGES").write_text(msg)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def build(repo, tmp_path, key):
    _tb, mpath, m = mf.build_release(repo, tmp_path / "dist", signing_key=key)
    return str(mpath), m


class Svc:
    """A fake hub service run by a restart command."""

    def __init__(self, tmp_path):
        self.port = _free_port()
        self.pidfile = tmp_path / "svc.pid"
        self.helper = tmp_path / "restart_helper.py"
        self.helper.write_text(RESTART_HELPER)

    def version(self) -> str | None:
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2) as r:
                return r.read().decode()
        except OSError:
            return None

    def stop(self):
        try:
            os.kill(int(self.pidfile.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass


@pytest.fixture
def svc(tmp_path):
    s = Svc(tmp_path)
    yield s
    s.stop()


def symlink_cfg(tmp_path, svc, **over) -> cfgmod.Config:
    root = tmp_path / "hub"
    link = root / "current-web"
    raw = {
        "root": str(root), "mode": "symlink", "python": sys.executable,
        "health_timeout": 15, "settle_seconds": 0.5, "deadman_minutes": 0,
        "databases": [str(tmp_path / "data" / "*.db")],
        "services": {"web": {
            "restart": f"{sys.executable} {svc.helper} {svc.pidfile} {link} {svc.port}",
            "modules": ["fakesvc.server"], "packages": ["fakesvc"],
            "health": [f"http://127.0.0.1:{svc.port}/"]}},
    }
    raw.update(over)
    return cfgmod.from_dict(raw, tmp_path / "hub-deploy.json")


QUIET = dict(log=lambda m: None)


# -- manifests -------------------------------------------------------------------

def test_manifest_sign_verify_and_domain_separation(repo, tmp_path, key):
    mpath, m = build(repo, tmp_path, key)
    assert mf.verify(json.loads(Path(mpath).read_text()))["version"] == m["version"]
    assert m["typ"] == "rook-hub-release" and len(m["commit"]) == 40
    assert mf.VERSION_RE.match(m["version"]) and m["version"].startswith(f"{m['build']}.")
    # A hub release is not a worker bundle manifest, and vice versa.
    assert not worker_verify(m, pubkey_b64=os.environ["ROOK_UPDATE_PUBKEY"])
    worker_sig = key.sign(mf.canonical(m)).signature
    with pytest.raises(mf.ManifestError, match="bad signature"):
        mf.verify({**m, "sig": base64.b64encode(worker_sig).decode()})
    for field, value in (("sha256", "0" * 64), ("url", "https://evil.example.com/x.tar.gz"),
                         ("build", m["build"] + 1)):
        tampered = {**m, field: value}
        if field == "build":
            tampered["version"] = f"{value}." + m["version"].split(".", 1)[1]
        with pytest.raises(mf.ManifestError, match="bad signature"):
            mf.verify(tampered)
    other = SigningKey.generate()
    with pytest.raises(mf.ManifestError, match="bad signature"):
        mf.verify(m, base64.b64encode(bytes(other.verify_key)).decode())
    with pytest.raises(mf.ManifestError, match="unsigned"):
        mf.verify({k: v for k, v in m.items() if k != "sig"})
    with pytest.raises(mf.ManifestError, match="version"):
        mf.check_fields({**m, "version": "1.abc1234"})
    with pytest.raises(mf.ManifestError, match="typ"):
        mf.check_fields({**m, "typ": "rook-worker"})


def test_verify_fails_closed_without_key(repo, tmp_path, key, monkeypatch):
    _mpath, m = build(repo, tmp_path, key)
    monkeypatch.delenv("ROOK_UPDATE_PUBKEY")
    monkeypatch.setenv("ROOK_UPDATE_KEY", str(tmp_path / "absent-key"))
    with pytest.raises(mf.ManifestError, match="no trusted public key"):
        mf.verify(m)


def test_build_archives_the_commit_not_the_worktree(repo, tmp_path, key):
    (repo / "fakesvc" / "server.py").write_text("raise SystemExit('dirty edit')\n")
    (repo / "stray.txt").write_text("untracked")
    mpath, m = build(repo, tmp_path, key)
    with tarfile.open(Path(mpath).parent / m["filename"]) as tf:
        names = tf.getnames()
        assert all(n == m["version"] or n.startswith(m["version"] + "/") for n in names)
        assert f"{m['version']}/stray.txt" not in names
        body = tf.extractfile(f"{m['version']}/fakesvc/server.py").read().decode()
    assert "dirty edit" not in body


def test_fetch_rejects_tampered_artifact(repo, tmp_path, key):
    mpath, m = build(repo, tmp_path, key)
    art = Path(mpath).parent / m["filename"]
    art.write_bytes(art.read_bytes()[:-10] + b"0123456789")
    with pytest.raises(mf.ManifestError, match="sha256"):
        mf.fetch_artifact(m, mpath, tmp_path / "dl")
    assert not (tmp_path / "dl" / m["filename"]).exists()


def test_unpack_is_fresh_and_never_overlays(repo, tmp_path, key, svc):
    cfg = symlink_cfg(tmp_path, svc)
    mpath, m = build(repo, tmp_path, key)
    tb = mf.fetch_artifact(m, mpath, cfg.state / "downloads")
    d = dp.unpack(cfg, tb, m)
    assert json.loads((d / "RELEASE.json").read_text())["sha256"] == m["sha256"]
    assert dp.unpack(cfg, tb, m) == d            # same artifact: reused
    with pytest.raises(dp.DeployError, match="not unpacked from this artifact"):
        dp.unpack(cfg, tb, {**m, "sha256": "f" * 64})
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tf:
        p = tmp_path / "x"
        p.write_text("x")
        tf.add(p, arcname="../escape")
    with pytest.raises(dp.DeployError, match="outside"):
        dp.unpack(cfg, evil, {**m, "version": "9.evil.goat"})


# -- full deploy, symlink mode ----------------------------------------------------

def test_deploy_upgrade_rollback_prune(repo, tmp_path, key, svc):
    data = tmp_path / "data"
    data.mkdir()
    db = sqlite3.connect(data / "journal.db")
    db.execute("create table t(x)")
    db.execute("insert into t values (42)")
    db.commit()
    cfg = symlink_cfg(tmp_path, svc)

    m1src, m1 = build(repo, tmp_path, key)
    r1 = dp.deploy(cfg, m1src, **QUIET)
    assert svc.version() == m1["version"]
    assert dp.Selector(cfg).current(cfg.services["web"]) == m1["version"]
    backup = Path(r1["databases"][0]["backup"])
    assert sqlite3.connect(backup).execute("select x from t").fetchone() == (42,)

    commit(repo, "v2")
    m2src, m2 = build(repo, tmp_path, key)
    dp.deploy(cfg, m2src, **QUIET)
    assert svc.version() == m2["version"]
    st = dp.status(cfg)
    assert st["services"]["web"]["selected"] == m2["version"]
    assert st["services"]["web"]["previous"] == m1["version"]
    assert [r["version"] for r in st["releases"]] == [m2["version"], m1["version"]]

    # Deploying an older build is refused unless asked for.
    with pytest.raises(dp.DeployError, match="newer than"):
        dp.deploy(cfg, m1src, **QUIET)

    dp.rollback(cfg, **QUIET)
    assert svc.version() == m1["version"]
    assert dp.status(cfg)["services"]["web"]["previous"] == m2["version"]
    events = [(e["event"], e["result"]) for e in dp.read_history(cfg)]
    assert events[-1] == ("rollback", "ok")

    dp.rollback(cfg, **QUIET)  # back to v2
    for i in (3, 4):
        commit(repo, f"v{i}")
        dp.deploy(cfg, build(repo, tmp_path, key)[0], **QUIET)
    before = [r["version"] for r in dp.status(cfg)["releases"]]
    assert len(before) == 4
    removed = dp.prune(cfg, keep=2, **QUIET)
    after = [r["version"] for r in dp.status(cfg)["releases"]]
    assert after == before[:2] and removed == before[2:]
    with pytest.raises(dp.DeployError):
        dp.prune(cfg, keep=1, **QUIET)


def test_preflight_import_failure_changes_nothing(repo, tmp_path, key, svc):
    cfg = symlink_cfg(tmp_path, svc)
    _src, m1 = build(repo, tmp_path, key)
    dp.deploy(cfg, _src, **QUIET)
    commit(repo, "broken import", {"fakesvc/extra.py": "import does_not_exist_anywhere\n"})
    src2, m2 = build(repo, tmp_path, key)
    with pytest.raises(dp.DeployError, match="does_not_exist_anywhere"):
        dp.deploy(cfg, src2, **QUIET)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m1["version"]
    assert svc.version() == m1["version"]


def test_preflight_catches_shadowing_install(repo, tmp_path, key, svc, monkeypatch):
    cfg = symlink_cfg(tmp_path, svc)
    src, m = build(repo, tmp_path, key)
    tb = mf.fetch_artifact(m, src, cfg.state / "downloads")
    dp.unpack(cfg, tb, m)
    # A module that exists outside the release (stdlib) cannot be "from the release".
    cfg.services["web"].modules = ["json"]
    with pytest.raises(dp.DeployError, match="not from the release"):
        dp.preflight(cfg, m["version"], [cfg.services["web"]], **QUIET)


def test_preflight_tests(repo, tmp_path, key, svc):
    commit(repo, "tests", {"tests_ok.py": "def test_ok():\n    assert True\n",
                           "tests_bad.py": "def test_bad():\n    assert False\n"})
    cfg = symlink_cfg(tmp_path, svc)
    src, m = build(repo, tmp_path, key)
    dp.unpack(cfg, mf.fetch_artifact(m, src, cfg.state / "downloads"), m)
    dp.preflight(cfg, m["version"], [cfg.services["web"]], ["tests_ok.py"], **QUIET)
    with pytest.raises(dp.DeployError, match="tests failed"):
        dp.preflight(cfg, m["version"], [cfg.services["web"]], ["tests_bad.py"], **QUIET)


def test_failed_health_rolls_back_automatically(repo, tmp_path, key, svc):
    cfg = symlink_cfg(tmp_path, svc, health_timeout=3)
    src1, m1 = build(repo, tmp_path, key)
    dp.deploy(cfg, src1, **QUIET)
    commit(repo, "crashes at start", {"BROKEN": "1"})
    src2, _m2 = build(repo, tmp_path, key)
    with pytest.raises(dp.DeployError, match="did not become healthy"):
        dp.deploy(cfg, src2, **QUIET)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m1["version"]
    for _ in range(50):
        if svc.version() == m1["version"]:
            break
        time.sleep(0.1)
    assert svc.version() == m1["version"]
    hist = dp.read_history(cfg)
    assert any(e.get("via") == "rollback.sh" and e.get("reason") == "failed-verify" for e in hist)
    assert hist[-1]["result"] == "failed" and hist[-1]["rolled_back"] is True
    assert dp.armed_deploys(cfg) == []


def test_deadman_rolls_back_an_unfinished_deploy(repo, tmp_path, key, svc):
    cfg = symlink_cfg(tmp_path, svc)
    src1, m1 = build(repo, tmp_path, key)
    dp.deploy(cfg, src1, **QUIET)
    cfg.health_timeout = 1
    commit(repo, "crashes at start", {"BROKEN": "1"})
    src2, m2 = build(repo, tmp_path, key)
    # The deploy "dies" without rolling back: only the dead-man is left.
    with pytest.raises(dp.DeployError):
        dp.deploy(cfg, src2, auto_rollback=False, deadman_minutes=2 / 60, **QUIET)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m2["version"]
    assert len(dp.armed_deploys(cfg)) == 1
    for _ in range(80):
        if dp.Selector(cfg).current(cfg.services["web"]) == m1["version"] and svc.version():
            break
        time.sleep(0.1)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m1["version"]
    assert svc.version() == m1["version"]
    assert dp.armed_deploys(cfg) == []
    assert any(e.get("reason") == "deadman" for e in dp.read_history(cfg))


def test_verified_deploy_disarms_deadman(repo, tmp_path, key, svc):
    cfg = symlink_cfg(tmp_path, svc)
    src1, m1 = build(repo, tmp_path, key)
    r = dp.deploy(cfg, src1, deadman_minutes=2 / 60, **QUIET)
    assert dp.armed_deploys(cfg) == []
    time.sleep(2.5)  # the sleeper would have fired by now
    rb = Path(r["rollback"]).parent
    assert not (rb / "deadman.armed").exists()
    assert dp.read_history(cfg)[-1]["result"] == "ok"
    out = subprocess.run(["/bin/sh", r["rollback"], "--deadman"], capture_output=True, text=True)
    assert "disarmed" in out.stdout
    assert svc.version() == m1["version"]


def test_only_selected_services_switch(repo, tmp_path, key, svc):
    cfg = symlink_cfg(tmp_path, svc)
    other_link = tmp_path / "hub" / "current-other"
    cfg.services["other"] = cfgmod.Service(name="other", restart="true", order=99)
    src, m = build(repo, tmp_path, key)
    dp.deploy(cfg, src, services=["web"], **QUIET)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m["version"]
    assert not other_link.exists()
    with pytest.raises(dp.DeployError, match="unknown"):
        dp.deploy(cfg, src, services=["nope"], **QUIET)


# -- systemd mode with a fake systemctl --------------------------------------------

FAKE_SYSTEMCTL = textwrap.dedent('''
    #!{python}
    # Minimal systemctl stand-in: drop-ins applied in lexical order, last wins.
    import os, re, subprocess, sys
    unit_dir = os.environ["FAKE_UNIT_DIR"]
    args = [a for a in sys.argv[1:] if a != "--user"]
    log = open(os.environ["FAKE_SYSTEMCTL_LOG"], "a")
    log.write(" ".join(args) + "\\n")
    def dropins(unit):
        d = os.path.join(unit_dir, unit + ".d")
        return sorted(os.path.join(d, f) for f in os.listdir(d)) if os.path.isdir(d) else []
    def env(unit):
        out = {{}}
        for p in dropins(unit):
            for m in re.finditer(r"^Environment=([A-Z_]+)=(\\S+)", open(p).read(), re.M):
                out[m.group(1)] = m.group(2)
        return out
    if args[0] == "daemon-reload":
        sys.exit(0)
    if args[0] == "show":
        prop, unit = args[2], args[-1]
        if prop == "Environment":
            print(" ".join(f"{{k}}={{v}}" for k, v in env(unit).items()))
        elif prop == "DropInPaths":
            print(" ".join(dropins(unit)))
        sys.exit(0)
    if args[0] == "restart":
        unit = args[1]
        e = env(unit)
        subprocess.run([sys.executable, os.environ["FAKE_HELPER"], os.environ["FAKE_PIDFILE"],
                        e.get("PYTHONPATH", "/nonexistent"), os.environ["FAKE_PORT"]], check=True)
        sys.exit(0)
    if args[0] == "is-active":
        try:
            os.kill(int(open(os.environ["FAKE_PIDFILE"]).read()), 0)
            print("active")
        except (OSError, ValueError):
            print("failed"); sys.exit(3)
        sys.exit(0)
    if args[0] == "stop":
        sys.exit(0)
    sys.exit("fake systemctl: unsupported " + " ".join(args))
''')


@pytest.fixture
def fake_systemd(tmp_path, svc, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sc = bindir / "systemctl"
    sc.write_text(FAKE_SYSTEMCTL.format(python=sys.executable).lstrip())
    sc.chmod(0o755)
    run = bindir / "systemd-run"  # dead-man must not reach the real systemd
    run.write_text("#!/bin/sh\necho 'fake systemd-run: not available' >&2\nexit 1\n")
    run.chmod(0o755)
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_UNIT_DIR", str(unit_dir))
    monkeypatch.setenv("FAKE_SYSTEMCTL_LOG", str(tmp_path / "systemctl.log"))
    monkeypatch.setenv("FAKE_HELPER", str(svc.helper))
    monkeypatch.setenv("FAKE_PIDFILE", str(svc.pidfile))
    monkeypatch.setenv("FAKE_PORT", str(svc.port))
    cfg = cfgmod.from_dict({
        "root": str(tmp_path / "hub"), "mode": "systemd", "python": sys.executable,
        "systemd": {"scope": "user", "unit_dir": str(unit_dir)},
        "health_timeout": 10, "settle_seconds": 0.3, "deadman_minutes": 0,
        "services": {"web": {"unit": "rook-web.service", "modules": ["fakesvc.server"],
                             "health": [f"tcp://127.0.0.1:{svc.port}"]}},
    })
    return cfg, unit_dir


def test_systemd_single_dropin_and_stray_overrides(repo, tmp_path, key, svc, fake_systemd):
    cfg, unit_dir = fake_systemd
    web = cfg.services["web"]
    src1, m1 = build(repo, tmp_path, key)
    dp.deploy(cfg, src1, **QUIET)
    dropin = unit_dir / "rook-web.service.d" / "90-release.conf"
    assert f"ROOK_RELEASE={m1['version']}" in dropin.read_text()
    assert svc.version() == m1["version"]
    st = dp.status(cfg)["services"]["web"]
    assert st["selected"] == st["effective"] == m1["version"] and st["strays"] == []

    # A hand-made, alphabetically later override silently re-pins an old tree.
    stray = unit_dir / "rook-web.service.d" / "95-hotfix.conf"
    stray.write_text("[Service]\nEnvironment=PYTHONPATH=/old/tree\nEnvironment=ROOK_RELEASE=1.old.tree\n")
    harmless = unit_dir / "rook-web.service.d" / "10-limits.conf"
    harmless.write_text("[Service]\nLimitNOFILE=4096\n")
    assert dp.Selector(cfg).strays(web) == [stray]
    assert dp.status(cfg)["services"]["web"]["effective"] == "1.old.tree"

    commit(repo, "v2")
    src2, m2 = build(repo, tmp_path, key)
    with pytest.raises(dp.DeployError, match="--adopt-strays"):
        dp.deploy(cfg, src2, **QUIET)
    r = dp.deploy(cfg, src2, adopt_strays=True, **QUIET)
    assert not stray.exists() and harmless.exists()
    assert svc.version() == m2["version"]
    assert dp.status(cfg)["services"]["web"]["effective"] == m2["version"]

    # The rollback script restores the exact previous state, stray included.
    subprocess.run(["/bin/sh", r["rollback"]], check=True, capture_output=True)
    assert stray.exists()
    assert f"ROOK_RELEASE={m1['version']}" in dropin.read_text()


def test_systemd_verify_catches_override_that_wins(repo, tmp_path, key, svc, fake_systemd):
    """Even if a stray appears between the check and the restart, verification
    compares the effective ROOK_RELEASE and rolls back."""
    cfg, unit_dir = fake_systemd
    src1, m1 = build(repo, tmp_path, key)
    dp.deploy(cfg, src1, **QUIET)
    commit(repo, "v2")
    src2, _ = build(repo, tmp_path, key)
    real_select = selmod.Selector.select

    def select_and_sneak(self, s, version):
        real_select(self, s, version)
        (unit_dir / "rook-web.service.d" / "99-late.conf").write_text(
            f"[Service]\nEnvironment=ROOK_RELEASE={m1['version']}\n")

    cfg.health_timeout = 2
    selmod.Selector.select = select_and_sneak
    try:
        with pytest.raises(dp.DeployError, match="effective ROOK_RELEASE"):
            dp.deploy(cfg, src2, **QUIET)
    finally:
        selmod.Selector.select = real_select
    assert dp.Selector(cfg).current(cfg.services["web"]) == m1["version"]


def test_units_text(fake_systemd):
    cfg, _ = fake_systemd
    text = dp.units_text(cfg)
    assert "ExecStart=" in text and "90-release.conf" in text
    assert "Environment=PYTHONPATH=" in text and "WantedBy=default.target" in text


# -- config, history, CLI ---------------------------------------------------------

def test_config_validation(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="root"):
        cfgmod.from_dict({"python": "p"})
    with pytest.raises(cfgmod.ConfigError, match="mode"):
        cfgmod.from_dict({"root": "/r", "python": "p", "mode": "docker"})
    with pytest.raises(cfgmod.ConfigError, match="keep"):
        cfgmod.from_dict({"root": "/r", "python": "p", "keep": 1})
    with pytest.raises(cfgmod.ConfigError, match="unknown keys"):
        cfgmod.from_dict({"root": "/r", "python": "p", "services": {"x": {"unit": "u", "bogus": 1}}})
    cfg = cfgmod.from_dict({"root": "/r", "python": "p"})
    assert [s.name for s in cfg.ordered()] == ["dashboard", "mcp"]  # MCP restarts last
    assert cfg.services["mcp"].disruptive and "rook.band_mcp" in cfg.services["mcp"].packages
    assert cfg.dropin_path(cfg.services["mcp"]) == Path(
        "/etc/systemd/system/rook-hub-mcp.service.d/90-release.conf")


def test_default_service_modules_import_from_this_tree():
    root = Path(__file__).resolve().parents[1]
    cfg = cfgmod.from_dict({"root": str(root.parent), "python": sys.executable})
    import importlib
    import pkgutil
    for s in cfg.ordered():
        for name in s.modules:
            importlib.import_module(name)
        for pkg in s.packages:
            p = importlib.import_module(pkg)
            for m in pkgutil.walk_packages(p.__path__, pkg + "."):
                if not m.name.endswith("__main__"):
                    importlib.import_module(m.name)


def test_previous_release_ignores_failed_switches():
    hist = [
        {"result": "ok", "services": {"web": {"from": None, "to": "1.a.b"}}},
        {"result": "ok", "services": {"web": {"from": "1.a.b", "to": "2.c.d"}}},
        {"result": "failed", "services": {"web": {"from": "2.c.d", "to": "3.e.f"}}},
    ]
    assert dp.previous_release(hist, "web", "2.c.d") == "1.a.b"
    assert dp.previous_release(hist, "web", "1.a.b") is None
    assert dp.previous_release(hist, "mcp", "2.c.d") is None


def test_deploy_lock_is_exclusive(tmp_path, svc):
    cfg = symlink_cfg(tmp_path, svc)
    with dp.deploy_lock(cfg):
        with pytest.raises(dp.DeployError, match="another hub deploy"):
            with dp.deploy_lock(cfg):
                pass


def test_cli_status_verify_and_disruptive_guard(repo, tmp_path, key, svc, capsys, monkeypatch):
    cfg = symlink_cfg(tmp_path, svc)
    cfg_path = tmp_path / "hub-deploy.json"
    cfg_path.write_text(json.dumps({
        "root": str(cfg.root), "mode": "symlink", "python": sys.executable,
        "health_timeout": 15, "settle_seconds": 0, "deadman_minutes": 0,
        "services": {"web": {"restart": cfg.services["web"].restart, "disruptive": True,
                             "modules": ["fakesvc.server"],
                             "health": [f"http://127.0.0.1:{svc.port}/"]}}}))
    src, m = build(repo, tmp_path, key)
    assert cli.main(["release", "verify", src]) == 0
    assert "sha256 ok" in capsys.readouterr().out
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert cli.main(["--config", str(cfg_path), "deploy", src]) == 1
    assert "--yes" in capsys.readouterr().err
    assert cli.main(["--config", str(cfg_path), "deploy", src, "--yes"]) == 0
    assert cli.main(["--config", str(cfg_path), "status"]) == 0
    out = capsys.readouterr().out
    assert f"selected={m['version']}" in out
    assert cli.main(["--config", str(cfg_path), "history"]) == 0
    assert "deploy" in capsys.readouterr().out
    assert cli.main(["--config", str(tmp_path / "missing.json"), "status"]) == 1
