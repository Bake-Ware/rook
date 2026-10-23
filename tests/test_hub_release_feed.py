"""Hub releases carry the feed (rook/hubdeploy/feed.py): the worker bundle and
the APKs go into the signed manifest, stored once by sha256, and a deploy
installs them, carries what a release lacks, refuses what the APK gates would
not approve, and checks what the dashboard serves afterwards."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from rook.hubdeploy import cli, config as cfgmod, deploy as dp, feed as fd, manifest as mf
from rook.worker._update_verify import canonical_payload

REPO_ROOT = Path(__file__).resolve().parents[1]

# A stand-in dashboard: serves the release tree's feed files the way
# rook/remote/bootstrap.py does (bundle from rook/remote, /apk.json only when
# the gate approves the release's APK, beta from rook/remote/apk-beta).
SERVER = textwrap.dedent('''
    import http.server, json, os, pathlib, sys, urllib.parse
    REL = pathlib.Path(__file__).resolve().parents[1]
    REMOTE = REL / "rook" / "remote"
    def main():
        if (REL / "DARK").exists():
            os.environ.pop("ROOK_PUBLIC_APK_SHA256", None)
        class H(http.server.BaseHTTPRequestHandler):
            def _send(self, body, status=200):
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command == "GET":
                    self.wfile.write(body)
            def _file(self, p):
                if p.is_file():
                    return self._send(p.read_bytes())
                return self._send(b"missing", 404)
            def do_HEAD(self):
                self.do_GET()
            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path in ("/band-worker.pyz", "/band-worker.json"):
                    return self._file(REMOTE / u.path.lstrip("/"))
                if u.path in ("/apk", "/apk.json"):
                    d = REMOTE
                    if q.get("channel") == ["beta"] and os.environ.get("ROOK_BETA_APK_SHA256"):
                        d = REMOTE / "apk-beta"
                    elif not os.environ.get("ROOK_PUBLIC_APK_SHA256"):
                        return self._send(b"login", 401)
                    return self._file(d / ("rook-worker.apk" if u.path == "/apk"
                                           else "rook-worker-apk.json"))
                ver = json.loads((REL / "RELEASE.json").read_text())["version"]
                self._send(ver.encode())
            def log_message(self, *a):
                pass
        http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
    if __name__ == "__main__":
        main()
''')

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

QUIET = dict(log=lambda m: None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def key(monkeypatch, tmp_path):
    sk = SigningKey.generate()
    monkeypatch.setenv("ROOK_UPDATE_PUBKEY", base64.b64encode(bytes(sk.verify_key)).decode())
    monkeypatch.setenv("ROOK_UPDATE_KEY", str(tmp_path / "no-such-key"))
    for k in ("ROOK_PUBLIC_APK_SHA256", "ROOK_BETA_APK_SHA256", "ROOK_UPDATES_DIR",
              "ROOK_DATA_DIR", "ROOK_HUB_DEPLOY_CONFIG", "ROOK_PUBLIC_BASE"):
        monkeypatch.delenv(k, raising=False)
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


def bump(repo: Path, msg: str) -> None:
    (repo / "CHANGES").write_text(msg)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def worker_dir(d: Path, sk, build: int = 738, body: bytes = b"PYZ" * 100) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / fd.PYZ).write_bytes(body)
    m = {"schema": 1, "build": build, "version": f"{build}.lumpy.iguana", "commit": "357ead5",
         "filename": fd.PYZ, "sha256": hashlib.sha256(body).hexdigest(), "size": len(body),
         "url": "https://hub.example.com/band-worker.pyz"}
    m["sig"] = base64.b64encode(sk.sign(canonical_payload(m)).signature).decode()
    (d / fd.PYZ_MANIFEST).write_text(json.dumps(m))
    return d


def apk_dir(d: Path, code: int = 23, name: str = "0.4.19", body: bytes = b"APK" * 300) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / fd.APK).write_bytes(body)
    (d / fd.APK_MANIFEST).write_text(json.dumps(
        {"package": fd.PACKAGE, "version_code": code, "version_name": name,
         "sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}))
    return d


class Svc:
    def __init__(self, tmp_path):
        self.port = _free_port()
        self.pidfile = tmp_path / "svc.pid"
        self.helper = tmp_path / "restart_helper.py"
        self.helper.write_text(RESTART_HELPER)

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


def cfg_for(tmp_path, svc, **over) -> cfgmod.Config:
    root = tmp_path / "hub"
    link = root / "current-web"
    raw = {
        "root": str(root), "mode": "symlink", "python": sys.executable,
        "health_timeout": 15, "settle_seconds": 0.2, "deadman_minutes": 0,
        "feed_service": "web",
        "services": {"web": {
            "restart": f"{sys.executable} {svc.helper} {svc.pidfile} {link} {svc.port}",
            "modules": ["fakesvc.server"], "packages": ["fakesvc"],
            "health": [f"http://127.0.0.1:{svc.port}/"]}},
    }
    raw.update(over)
    return cfgmod.from_dict(raw, tmp_path / "hub-deploy.json")


def release(repo, tmp_path, key, **feed):
    _tb, mpath, m = mf.build_release(repo, tmp_path / "dist", signing_key=key, **feed)
    return str(mpath), m


# -- building ---------------------------------------------------------------------------

def test_release_carries_the_feed_signed_and_stored_once(repo, tmp_path, key):
    w = worker_dir(tmp_path / "w", key)
    a = apk_dir(tmp_path / "apk")
    b = apk_dir(tmp_path / "beta", 24, "0.4.20-beta.1", b"BETA" * 300)
    mpath, m = release(repo, tmp_path, key, worker=w, apk=a, apk_beta=b)
    paths = {e["path"]: e for e in m["feed"]}
    assert set(paths) == {"rook/remote/band-worker.pyz", "rook/remote/band-worker.json",
                          "rook/remote/rook-worker.apk", "rook/remote/rook-worker-apk.json",
                          "rook/remote/apk-beta/rook-worker.apk",
                          "rook/remote/apk-beta/rook-worker-apk.json"}
    assert m["feed_info"]["worker"]["build"] == 738
    assert m["feed_info"]["apk"]["version_name"] == "0.4.19"
    assert m["feed_info"]["apk_beta"]["version_code"] == 24
    for e in m["feed"]:
        assert mf.sha256_file(tmp_path / "dist" / "feed" / e["sha256"]) == e["sha256"]
    # The tarball holds only the commit: the feed rides beside it.
    import tarfile
    with tarfile.open(tmp_path / "dist" / m["filename"]) as tf:
        assert not any("rook-worker.apk" in n for n in tf.getnames())
    # The signature covers the feed: another path or file is a bad signature.
    mf.verify(m)
    tampered = json.loads(json.dumps(m))
    tampered["feed"][0]["sha256"] = "0" * 64
    with pytest.raises(mf.ManifestError, match="bad signature"):
        mf.verify(tampered)
    with pytest.raises(mf.ManifestError, match="bad feed entry"):
        mf.check_fields({**m, "feed": [{**m["feed"][0], "path": "rook/evil.py"}]})
    # A second release with the same APK stores it once.
    bump(repo, "v2")
    _m2src, m2 = release(repo, tmp_path, key, worker=w, apk=a)
    assert len(list((tmp_path / "dist" / "feed").iterdir())) == 6
    assert m2["feed_info"]["apk"] == m["feed_info"]["apk"]


def test_feed_sources_are_checked(repo, tmp_path, key):
    other = SigningKey.generate()
    with pytest.raises(mf.ManifestError, match="not signed by the trusted update key"):
        release(repo, tmp_path, key, worker=worker_dir(tmp_path / "w", other))
    w = worker_dir(tmp_path / "w2", key)
    (w / fd.PYZ).write_bytes(b"swapped")
    with pytest.raises(mf.ManifestError, match="does not match its signed manifest"):
        release(repo, tmp_path, key, worker=w)
    a = apk_dir(tmp_path / "apk")
    (a / fd.APK).write_bytes(b"other apk")
    with pytest.raises(mf.ManifestError, match="does not match its manifest"):
        release(repo, tmp_path, key, apk=a)
    a2 = apk_dir(tmp_path / "apk2")
    man = json.loads((a2 / fd.APK_MANIFEST).read_text())
    (a2 / fd.APK_MANIFEST).write_text(json.dumps({**man, "package": "com.example"}))
    with pytest.raises(mf.ManifestError, match="APK manifest"):
        release(repo, tmp_path, key, apk=a2)
    with pytest.raises(mf.ManifestError, match="no rook-worker.apk"):
        release(repo, tmp_path, key, apk=tmp_path / "nothing")


def test_build_worker_builds_the_commit_signed_with_the_release_key(tmp_path, key):
    """The real builder, on this repository's HEAD, in a throwaway worktree."""
    if subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                      capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    commit = _git(REPO_ROOT, "rev-parse", "HEAD")
    out = fd.build_worker(REPO_ROOT, commit, tmp_path / "w", key, "https://hub.example.com/")
    files, info = fd.worker_files(out, os.environ["ROOK_UPDATE_PUBKEY"])
    assert info["build"] == int(_git(REPO_ROOT, "rev-list", "--count", commit))
    m = json.loads((out / fd.PYZ_MANIFEST).read_text())
    assert m["commit"].startswith(commit[:7]) and m["url"] == "https://hub.example.com/band-worker.pyz"
    import zipfile
    with zipfile.ZipFile(out / fd.PYZ) as z:
        stamped = z.read("rook/worker/_update_pubkey.py").decode()
    assert os.environ["ROOK_UPDATE_PUBKEY"] in stamped
    assert not (tmp_path / "no-such-key").exists()          # no key created anywhere
    assert commit[:12] not in _git(REPO_ROOT, "worktree", "list")  # worktree removed


# -- deploying ---------------------------------------------------------------------------

def _tree(cfg, m) -> Path:
    return cfg.release_dir(m["version"])


def test_deploy_installs_the_feed_and_checks_what_is_served(repo, tmp_path, key, svc,
                                                            monkeypatch):
    cfg = cfg_for(tmp_path, svc)
    w, a = worker_dir(tmp_path / "w", key), apk_dir(tmp_path / "apk")
    b = apk_dir(tmp_path / "beta", 24, "0.4.20-beta.1", b"BETA" * 300)
    monkeypatch.setenv("ROOK_PUBLIC_APK_SHA256", json.loads((a / fd.APK_MANIFEST).read_text())["sha256"])
    monkeypatch.setenv("ROOK_BETA_APK_SHA256", json.loads((b / fd.APK_MANIFEST).read_text())["sha256"])
    mpath, m = release(repo, tmp_path, key, worker=w, apk=a, apk_beta=b)
    logs = []
    dp.deploy(cfg, mpath, log=logs.append)
    tree = _tree(cfg, m)
    assert fd.tree_kinds(tree) == {"worker", "apk", "apk_beta"}
    assert (tree / "rook/remote/rook-worker.apk").read_bytes() == (a / fd.APK).read_bytes()
    blob = cfg.state / "downloads" / "feed" / m["feed_info"]["apk"]["sha256"]
    assert blob.stat().st_ino == (tree / "rook/remote/rook-worker.apk").stat().st_ino
    served = next(l for l in logs if l.startswith("feed served:"))
    assert "build 738" in served and "0.4.19" in served and "0.4.20-beta.1" in served

    # The next release carries no feed: the running one's is carried over.
    bump(repo, "v2")
    m2src, m2 = release(repo, tmp_path, key)
    logs.clear()
    dp.deploy(cfg, m2src, log=logs.append)
    t2 = _tree(cfg, m2)
    assert fd.tree_kinds(t2) == {"worker", "apk", "apk_beta"}
    assert json.loads((t2 / "FEED-CARRIED.json").read_text())["apk"] == m["version"]
    assert any("carried apk" in l for l in logs)

    # Prune keeps the blobs a kept release names.
    dp.prune(cfg, keep=2, **QUIET)
    assert blob.exists()


def test_deploy_refuses_what_the_gates_would_not_approve(repo, tmp_path, key, svc, monkeypatch):
    cfg = cfg_for(tmp_path, svc)
    a = apk_dir(tmp_path / "apk")
    m1src, m1 = release(repo, tmp_path, key, apk=a)
    monkeypatch.setenv("ROOK_PUBLIC_APK_SHA256", json.loads((a / fd.APK_MANIFEST).read_text())["sha256"])
    dp.deploy(cfg, m1src, **QUIET)
    bump(repo, "v2")
    other = apk_dir(tmp_path / "apk2", 25, "0.4.20", b"NEW" * 300)
    m2src, m2 = release(repo, tmp_path, key, apk=other)
    with pytest.raises(dp.DeployError, match="ROOK_PUBLIC_APK_SHA256 .* approves neither"):
        dp.deploy(cfg, m2src, **QUIET)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m1["version"]    # nothing switched
    monkeypatch.setenv("ROOK_BETA_APK_SHA256", "f" * 64)
    with pytest.raises(dp.DeployError, match="ROOK_BETA_APK_SHA256"):
        dp.deploy(cfg, m1src, allow_downgrade=True, **QUIET)
    # A published APK in the updates dir satisfies the gate on its own.
    monkeypatch.delenv("ROOK_BETA_APK_SHA256")
    pub = apk_dir(tmp_path / "data" / "updates" / "apk", 25, "0.4.20", b"NEW" * 300)
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ROOK_PUBLIC_APK_SHA256",
                       json.loads((pub / fd.APK_MANIFEST).read_text())["sha256"])
    want = fd.expected(_tree(cfg, m1), dict(os.environ))
    assert not want["problems"] and want["apk_public"]
    # No gate at all: served behind the login, a warning.
    monkeypatch.delenv("ROOK_PUBLIC_APK_SHA256")
    monkeypatch.delenv("ROOK_DATA_DIR")
    want = fd.expected(_tree(cfg, m1), dict(os.environ))
    assert not want["problems"] and "behind the dashboard login" in want["warnings"][0]


def test_a_dark_feed_after_restart_rolls_back(repo, tmp_path, key, svc, monkeypatch):
    cfg = cfg_for(tmp_path, svc)
    a = apk_dir(tmp_path / "apk")
    monkeypatch.setenv("ROOK_PUBLIC_APK_SHA256", json.loads((a / fd.APK_MANIFEST).read_text())["sha256"])
    m1src, m1 = release(repo, tmp_path, key, apk=a)
    dp.deploy(cfg, m1src, **QUIET)
    bump(repo, "v2")
    (repo / "DARK").write_text("the new release stops serving /apk publicly")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "dark")
    m2src, m2 = release(repo, tmp_path, key, apk=a)
    with pytest.raises(dp.DeployError, match="feed went dark"):
        dp.deploy(cfg, m2src, **QUIET)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m1["version"]
    # --skip-feed-check lets it through (the operator's call).
    dp.deploy(cfg, m2src, check_feed=False, **QUIET)
    assert dp.Selector(cfg).current(cfg.services["web"]) == m2["version"]


def test_cli_live_sources_and_verify(repo, tmp_path, key, svc, monkeypatch, capsys):
    cfg = cfg_for(tmp_path, svc)
    raw = json.loads(json.dumps({
        "root": str(cfg.root), "mode": "symlink", "python": sys.executable,
        "feed_service": "web", "deadman_minutes": 0, "health_timeout": 15,
        "services": {"web": {"restart": cfg.services["web"].restart,
                             "modules": ["fakesvc.server"],
                             "health": cfg.services["web"].health}}}))
    (tmp_path / "hub-deploy.json").write_text(json.dumps(raw))
    w, a = worker_dir(tmp_path / "w", key), apk_dir(tmp_path / "apk")
    b = apk_dir(tmp_path / "beta", 24, "0.4.20-beta.1", b"BETA" * 300)
    monkeypatch.setenv("ROOK_PUBLIC_APK_SHA256", json.loads((a / fd.APK_MANIFEST).read_text())["sha256"])
    m1src, m1 = release(repo, tmp_path, key, worker=w, apk=a, apk_beta=b)
    dp.deploy(cfg, m1src, **QUIET)
    keyfile = tmp_path / "signing-key"
    keyfile.write_text(base64.b64encode(bytes(key)).decode())
    monkeypatch.setenv("ROOK_UPDATE_KEY", str(keyfile))
    bump(repo, "v2")
    monkeypatch.setenv("ROOK_BETA_APK_SHA256", json.loads((b / fd.APK_MANIFEST).read_text())["sha256"])
    out = tmp_path / "dist2"
    rc = cli.main(["--config", str(tmp_path / "hub-deploy.json"), "release", "build",
                   "--repo", str(repo), "--out", str(out), "--worker-bundle", "live"])
    assert rc == 0, capsys.readouterr().err
    m2 = json.loads((out / "rook-hub-latest.json").read_text())
    assert m2["feed_info"]["worker"]["build"] == 738
    assert m2["feed_info"]["apk"] == m1["feed_info"]["apk"]
    assert m2["feed_info"]["apk_beta"] == m1["feed_info"]["apk_beta"]
    mp = out / f"rook-hub-{m2['version']}.json"
    assert cli.main(["release", "verify", str(mp)]) == 0
    assert "rook/remote/rook-worker.apk: sha256 ok" in capsys.readouterr().out
    (out / "feed" / m2["feed_info"]["apk"]["sha256"]).write_bytes(b"x")
    assert cli.main(["release", "verify", str(mp)]) == 1
    # --worker-bundle live without a config is an error, not a silent none.
    rc = cli.main(["--config", str(tmp_path / "absent.json"), "release", "build",
                   "--repo", str(repo), "--out", str(out), "--worker-bundle", "live"])
    assert rc == 1 and "--worker-bundle live" in capsys.readouterr().err


# -- serving ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_published_apk_is_public_and_beta_comes_from_the_release(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from rook.remote import bootstrap, setup_store
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_UPDATES_DIR", str(tmp_path / "updates"))
    for k in ("ROOK_GOOGLE_CLIENT_FILE", "ROOK_PUBLIC_APK_SHA256", "ROOK_BETA_APK_SHA256"):
        monkeypatch.delenv(k, raising=False)
    setup_store.save({"band_name": "home", "band_psk": "k", "hub_public": "hub.example.com:443",
                      "pyz_domain": "rook.example.com"})
    server = bootstrap.CombinedServer(band_psk="k", web_user="operator", web_pass="pw",
                                      domain="rook.example.com")
    async with TestClient(TestServer(server._app)) as c:
        r = await c.get("/apk.json", allow_redirects=False)
        assert r.status in (302, 401)          # nothing published, no gate: login
        pub = apk_dir(tmp_path / "updates" / "apk", 25, "0.4.20")
        r = await c.get("/apk.json")
        assert r.status == 200 and (await r.json())["version_name"] == "0.4.20"
        r = await c.get("/apk")
        assert r.status == 200 and await r.read() == (pub / fd.APK).read_bytes()
        # The beta the release carries (rook/remote/apk-beta) when the gate names it.
        here = Path(bootstrap.__file__).parent / "apk-beta"
        monkeypatch.setattr(bootstrap, "__file__", str(tmp_path / "rel" / "bootstrap.py"))
        beta = apk_dir(tmp_path / "rel" / "apk-beta", 26, "0.4.21-beta.1", b"B" * 500)
        monkeypatch.setenv("ROOK_BETA_APK_SHA256",
                           json.loads((beta / fd.APK_MANIFEST).read_text())["sha256"])
        r = await c.get("/apk.json?channel=beta")
        assert r.status == 200 and (await r.json())["version_name"] == "0.4.21-beta.1"
        assert not here.exists() or here != beta
