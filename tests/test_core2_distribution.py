"""Plugin distribution: hub prepare/sign/push (rook.core2.hub.distribute) and
worker verify/install/check/rollback (rook.core2.worker.install), end to end
over the in-memory transport. uv is mocked; the one network test talks to a
local HTTP server with a hard timeout."""

from __future__ import annotations

import asyncio
import hashlib
import http.server
import io
import json
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest
import yaml

pytest.importorskip("nacl")
pytest.importorskip("jsonschema")
from nacl.signing import SigningKey  # noqa: E402

from rook.core2 import wire  # noqa: E402
from rook.core2.hub import distribute as d  # noqa: E402
from rook.core2.worker import install as w  # noqa: E402

HUB_SK = SigningKey(b"\x01" * 32)
HUB_ID = "a1b2c3d4e5f60718"
WID = "3f0a" * 8
ENV = d.WorkerEnv(worker_id=WID, os="linux", python="3.11", packages={"httpx": "0.28.1"})
NOW = 1793300000


def run(coro, timeout=10):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def make_plugin(root: Path, name="echo", version="1.0.0", *, extra="", check=True,
                files: dict | None = None) -> Path:
    folder = root / f"src-{name}-{version}"
    folder.mkdir(parents=True)
    text = f"""manifest: 1
plugin: {name}
version: {version}
caps:
  ping:
    doc: Say hi.
    risk: read
    scope: self
    action: {{ exec: {{ argv: [echo, hi] }} }}
{"check: [" + name + ".ping]" if check else ""}
{extra}"""
    (folder / "plugin.yaml").write_text(text)
    for rel, body in (files or {}).items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(body)
    return folder


class FakeHost:
    """Loads by reading plugin.yaml; ``fail_check`` = {(cap, version)}."""

    def __init__(self, fail_check=(), fail_load=(), hang=()):
        self.loaded: dict[str, tuple[str, Path]] = {}
        self.fail_check, self.fail_load, self.hang = set(fail_check), set(fail_load), set(hang)
        self.calls: list[tuple[str, str]] = []

    async def load(self, folder):
        data = yaml.safe_load((Path(folder) / "plugin.yaml").read_text())
        if (data["plugin"], data["version"]) in self.fail_load:
            raise RuntimeError("script won't import")
        self.loaded[data["plugin"]] = (data["version"], Path(folder))

    async def unload(self, name):
        self.loaded.pop(name)

    async def call(self, cap, args, caller):
        version = self.loaded[cap.split(".")[0]][0]
        self.calls.append((cap, version))
        assert caller.why and caller.agent == "system:install"
        if (cap, version) in self.hang:
            await asyncio.sleep(3600)
        if (cap, version) in self.fail_check:
            raise RuntimeError("device not answering")
        return {"ok": True}


def worker(tmp_path, host=None, **kw):
    host = host or FakeHost()
    keys = {wire.key_id(bytes(HUB_SK.verify_key)): wire.pub_text(HUB_SK)}
    inst = w.Installer(tmp_path / "plugins", worker_id=WID, host=host,
                       keys=lambda hub: keys if hub == HUB_ID else {},
                       approves=lambda hub: hub == HUB_ID, **kw)
    return inst, host


def no_uv(reqs, env):
    assert not reqs
    return []


def ship(folder, inst, env=ENV, **kw):
    transport = d.MemoryTransport({WID: inst})
    kw.setdefault("resolve", no_uv)
    return run(d.install(folder, env, transport, signer=HUB_SK, hub=HUB_ID, **kw))


def bundle_of(folder, tmp_path, env=ENV, **kw) -> tuple[d.Plan, Path]:
    kw.setdefault("resolve", no_uv)
    plan = d.prepare(folder, env, signer=HUB_SK, hub=HUB_ID, now=NOW, **kw)
    return plan, d.write_bundle(plan, tmp_path / "b.tar")


def rewrite(bundle: Path, change) -> Path:
    """Copy a bundle, letting ``change(name, data) -> (name, data) | None`` edit members."""
    out = bundle.with_name("evil.tar")
    with tarfile.open(bundle) as src, tarfile.open(out, "w") as dst:
        for m in src.getmembers():
            got = change(m.name, src.extractfile(m).read())
            if got is None:
                continue
            info = tarfile.TarInfo(got[0])
            info.size = len(got[1])
            dst.addfile(info, io.BytesIO(got[1]))
    return out


# -- happy path, versions, rollback --------------------------------------------------

def test_install_end_to_end(tmp_path):
    inst, host = worker(tmp_path)
    seeded = []
    folder = make_plugin(tmp_path, files={"docs/manual.md": "# hi"},
                         extra="docs: { manual: docs/manual.md }")
    report = ship(folder, inst, seed=seeded.append)
    assert report["ok"], report
    assert report["checks"] == ["echo.ping"] and host.calls == [("echo.ping", "1.0.0")]
    dest = tmp_path / "plugins" / "echo" / "1.0.0"
    assert host.loaded["echo"] == ("1.0.0", dest)
    assert (dest / "docs" / "manual.md").read_text() == "# hi"
    sig = json.loads((dest / "plugin.sig").read_text())
    assert sig["typ"] == "rook-plugin" and sig["worker"] == WID and sig["hub"] == HUB_ID
    assert wire.verify_sig(wire.pub_text(HUB_SK), sig)
    assert seeded == [{"from": "echo@1.0.0", "folder": str(folder.resolve()),
                       "sections": {"docs": {"manual": "docs/manual.md"}}}]
    assert inst.state("echo") == {"current": "1.0.0", "previous": None}


def test_update_keeps_previous_and_prunes_older(tmp_path):
    inst, host = worker(tmp_path)
    for v in ("1.0.0", "1.1.0", "1.2.0"):
        assert ship(make_plugin(tmp_path, version=v), inst)["ok"]
    assert inst.state("echo") == {"current": "1.2.0", "previous": "1.1.0"}
    assert sorted(p.name for p in (tmp_path / "plugins" / "echo").iterdir() if p.is_dir()) == ["1.1.0", "1.2.0"]
    back = run(inst.rollback("echo"))
    assert back["ok"] and back["version"] == "1.1.0"
    assert host.loaded["echo"][0] == "1.1.0"
    assert inst.state("echo") == {"current": "1.1.0", "previous": "1.2.0"}


def test_failed_check_rolls_back_to_previous(tmp_path):
    inst, host = worker(tmp_path, FakeHost(fail_check={("echo.ping", "2.0.0")}))
    assert ship(make_plugin(tmp_path, version="1.0.0"), inst)["ok"]
    report = ship(make_plugin(tmp_path, version="2.0.0"), inst)
    assert not report["ok"] and report["rolled_back"]
    assert "check echo.ping failed: device not answering" in report["reason"]
    assert host.loaded["echo"][0] == "1.0.0"
    assert inst.state("echo") == {"current": "1.0.0", "previous": None}
    assert not (tmp_path / "plugins" / "echo" / "2.0.0").exists()


def test_failed_check_on_first_install_unloads(tmp_path):
    inst, host = worker(tmp_path, FakeHost(fail_check={("echo.ping", "1.0.0")}))
    report = ship(make_plugin(tmp_path), inst)
    assert not report["ok"] and report["rolled_back"] and "echo" not in host.loaded
    assert inst.state("echo")["current"] is None


def test_hung_check_times_out_and_rolls_back(tmp_path):
    inst, host = worker(tmp_path, FakeHost(hang={("echo.ping", "1.0.0")}), check_timeout=0.2)
    report = ship(make_plugin(tmp_path), inst)
    assert not report["ok"] and "timed out" in report["reason"] and "echo" not in host.loaded


def test_load_failure_leaves_running_version(tmp_path):
    inst, host = worker(tmp_path, FakeHost(fail_load={("echo", "2.0.0")}))
    assert ship(make_plugin(tmp_path), inst)["ok"]
    report = ship(make_plugin(tmp_path, version="2.0.0"), inst)
    assert not report["ok"] and "load failed: script won't import" in report["reason"]
    assert host.loaded["echo"][0] == "1.0.0" and inst.state("echo")["current"] == "1.0.0"
    assert not (tmp_path / "plugins" / "echo" / "2.0.0").exists()


def test_same_version_same_files_is_a_noop_and_different_files_refused(tmp_path):
    inst, host = worker(tmp_path)
    folder = make_plugin(tmp_path)
    assert ship(folder, inst)["ok"]
    again = ship(folder, inst)
    assert again["ok"] and again["unchanged"] and len(host.calls) == 1
    (folder / "README.md").write_text("changed")
    report = ship(folder, inst)
    assert not report["ok"] and "bump the version" in report["reason"]


def test_same_version_repairs_a_damaged_folder(tmp_path):
    inst, host = worker(tmp_path)
    folder = make_plugin(tmp_path)
    assert ship(folder, inst)["ok"]
    dest = tmp_path / "plugins" / "echo" / "1.0.0"
    (dest / "plugin.yaml").write_text("tampered")
    (dest / "stray.py").write_text("import os")
    assert "echo" not in inst.installed()[0]
    report = ship(folder, inst)
    assert report["ok"] and not report.get("unchanged") and len(host.calls) == 2
    assert not (dest / "stray.py").exists() and "echo" in inst.installed()[0]


def test_hub_push_back_to_previous_version_reuses_its_folder(tmp_path):
    inst, host = worker(tmp_path)
    v1 = make_plugin(tmp_path, version="1.0.0")
    assert ship(v1, inst)["ok"] and ship(make_plugin(tmp_path, version="1.1.0"), inst)["ok"]
    assert ship(v1, inst)["ok"]
    assert inst.state("echo") == {"current": "1.0.0", "previous": "1.1.0"}


def test_rollback_without_previous(tmp_path):
    inst, _ = worker(tmp_path)
    assert ship(make_plugin(tmp_path), inst)["ok"]
    assert not run(inst.rollback("echo"))["ok"]


def test_installed_rechecks_hashes_and_trust(tmp_path):
    inst, _ = worker(tmp_path)
    assert ship(make_plugin(tmp_path), inst)["ok"]
    assert ship(make_plugin(tmp_path, name="other"), inst)["ok"]
    folders, skipped = inst.installed()
    assert set(folders) == {"echo", "other"} and not skipped
    (folders["other"] / "plugin.yaml").write_text("tampered")
    folders, skipped = inst.installed()
    assert set(folders) == {"echo"} and "files changed" in skipped["other"]
    inst.approves = lambda hub: False
    folders, skipped = inst.installed()
    assert not folders and "no longer approves" in skipped["echo"]


# -- the worker refuses bad bundles before writing anything ---------------------------

def _nothing_written(tmp_path):
    root = tmp_path / "plugins"
    return not root.exists() or not any(p.name != "state.json" and not p.name.startswith(".")
                                        for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("change, reason", [
    (lambda n, b: None if n == "plugin.sig" else (n, b), "unsigned bundle"),
    (lambda n, b: (n, b.replace(b"[echo, hi]", b"[rm, -rf]")) if n == "plugin.yaml" else (n, b), "does not match its signed hash"),
    (lambda n, b: (n, b) if n != "plugin.sig" else (n, b), None),
])
def test_tampered_bundles_refused(tmp_path, change, reason):
    inst, host = worker(tmp_path)
    _, bundle = bundle_of(make_plugin(tmp_path), tmp_path)
    report = run(inst.receive(rewrite(bundle, change)))
    if reason is None:
        assert report["ok"]
        return
    assert not report["ok"] and reason in report["reason"] and not host.loaded
    assert _nothing_written(tmp_path)


def test_extra_and_missing_files_refused(tmp_path):
    inst, host = worker(tmp_path)
    _, bundle = bundle_of(make_plugin(tmp_path, files={"scripts/x.py": "x = 1"}), tmp_path)
    added = rewrite(bundle, lambda n, b: (n, b))
    with tarfile.open(added, "a") as tar:
        info = tarfile.TarInfo("scripts/evil.py")
        info.size = 4
        tar.addfile(info, io.BytesIO(b"pass"))
    report = run(inst.receive(added))
    assert not report["ok"] and "unsigned file scripts/evil.py" in report["reason"]
    dropped = rewrite(bundle, lambda n, b: None if n == "scripts/x.py" else (n, b))
    report = run(inst.receive(dropped))
    assert not report["ok"] and "missing file scripts/x.py" in report["reason"]
    assert _nothing_written(tmp_path) and not host.loaded


def _resign(sig: dict, sk=HUB_SK, **fields) -> bytes:
    body = {k: v for k, v in sig.items() if k not in ("kid", "sig")}
    return json.dumps(wire.sign(sk, {**body, **fields})).encode()


@pytest.mark.parametrize("edit, reason", [
    (lambda s: _resign(s, SigningKey(b"\x02" * 32)), "may approve plugins here"),
    (lambda s: _resign(s, hub="ffffffffffffffff"), "may approve plugins here"),
    (lambda s: _resign(s, worker="0" * 32), "signed for worker"),
    (lambda s: json.dumps({**s, "version": "9.9.9"}).encode(), "bad signature"),
    (lambda s: _resign(s, python=["--index-url=http://evil"]), "malformed: python"),
    (lambda s: _resign(s, files={**s["files"], "../escape": "0" * 64}), "malformed: files"),
    (lambda s: b"not json", "not JSON"),
])
def test_bad_signatures_refused(tmp_path, edit, reason):
    inst, host = worker(tmp_path)
    _, bundle = bundle_of(make_plugin(tmp_path), tmp_path)
    evil = rewrite(bundle, lambda n, b: (n, edit(json.loads(b))) if n == "plugin.sig" else (n, b))
    report = run(inst.receive(evil))
    assert not report["ok"] and reason in report["reason"], report
    assert _nothing_written(tmp_path) and not host.loaded


def test_signed_plugin_yaml_must_match_signature(tmp_path):
    inst, _ = worker(tmp_path)
    plan, bundle = bundle_of(make_plugin(tmp_path), tmp_path)
    evil = rewrite(bundle, lambda n, b: (n, _resign(json.loads(b), plugin="other")) if n == "plugin.sig" else (n, b))
    report = run(inst.receive(evil))
    assert not report["ok"] and "plugin.yaml is not other@1.0.0" in report["reason"]
    assert not (tmp_path / "plugins" / "other" / "1.0.0").exists()


@pytest.mark.parametrize("name", ["../x", "/abs", "a/../../b", "C:/x", "a\\b"])
def test_unsafe_paths_refused(tmp_path, name):
    inst, _ = worker(tmp_path)
    _, bundle = bundle_of(make_plugin(tmp_path), tmp_path)
    evil = rewrite(bundle, lambda n, b: (name, b) if n == "plugin.yaml" else (n, b))
    report = run(inst.receive(evil))
    assert not report["ok"] and "bad or repeated path" in report["reason"]
    assert not (tmp_path / "x").exists() and not Path("/abs").exists()


def test_symlink_and_case_duplicates_refused(tmp_path):
    inst, _ = worker(tmp_path)
    _, bundle = bundle_of(make_plugin(tmp_path), tmp_path)
    link = rewrite(bundle, lambda n, b: (n, b))
    with tarfile.open(link, "a") as tar:
        info = tarfile.TarInfo("evil")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
    assert "non-file entry: evil" in run(inst.receive(link))["reason"]
    dup = rewrite(bundle, lambda n, b: (n, b))
    with tarfile.open(dup, "a") as tar:
        info = tarfile.TarInfo("PLUGIN.yaml")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    assert "bad or repeated path: PLUGIN.yaml" in run(inst.receive(dup))["reason"]


def test_not_a_tar(tmp_path):
    inst, _ = worker(tmp_path)
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"\x00garbage" * 10)
    assert "not a plugin bundle" in run(inst.receive(junk))["reason"]
    _, bundle = bundle_of(make_plugin(tmp_path), tmp_path)
    cut = tmp_path / "cut.tar"
    cut.write_bytes(bundle.read_bytes()[:700])
    report = run(inst.receive(cut))
    assert not report["ok"] and "not a plugin bundle" in report["reason"]


def test_transport_failure_is_a_report(tmp_path):
    class Broken:
        async def push(self, worker_id, bundle):
            raise ConnectionError("drop timed out")
    report = run(d.install(make_plugin(tmp_path), ENV, Broken(), signer=HUB_SK, hub=HUB_ID, resolve=no_uv))
    assert not report["ok"] and "push failed: drop timed out" in report["reason"]


# -- hub-side refusals -------------------------------------------------------------------

def test_hub_refuses_invalid_builtin_platform_and_breaking(tmp_path):
    bad = make_plugin(tmp_path, name="bad", extra="caps2: oops\n")
    bad.joinpath("plugin.yaml").write_text("manifest: 1\nplugin: Bad\nversion: 1\n")
    with pytest.raises(d.Refused, match="invalid manifest"):
        d.prepare(bad, ENV, signer=HUB_SK, hub=HUB_ID)
    core = Path(__file__).resolve().parents[1] / "conformance" / "manifest" / "valid" / "core.yaml"
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "plugin.yaml").write_text(core.read_text())
    with pytest.raises(d.Refused, match="builtin"):
        d.prepare(tmp_path / "core", ENV, signer=HUB_SK, hub=HUB_ID)
    win = make_plugin(tmp_path, name="win", extra="platforms: [windows]")
    with pytest.raises(d.Refused, match="runs on windows, not linux"):
        d.prepare(win, ENV, signer=HUB_SK, hub=HUB_ID, resolve=no_uv)
    v1 = make_plugin(tmp_path, version="1.0.0")
    v11 = make_plugin(tmp_path, version="1.1.0", check=False,
                      extra="")  # same caps: fine
    old = yaml.safe_load((v1 / "plugin.yaml").read_text())
    assert d.prepare(v11, ENV, signer=HUB_SK, hub=HUB_ID, current=old, resolve=no_uv).sig
    gone = tmp_path / "gone"
    gone.mkdir()
    (gone / "plugin.yaml").write_text("manifest: 1\nplugin: echo\nversion: 1.2.0\n")
    with pytest.raises(d.Refused, match="major version bump"):
        d.prepare(gone, ENV, signer=HUB_SK, hub=HUB_ID, current=old, resolve=no_uv)


def test_install_reports_refusals_and_change_record(tmp_path):
    inst, host = worker(tmp_path)
    folder = make_plugin(tmp_path, files={"scripts/stats.py": "def collect(ctx): return 1"})
    seen = []

    def record(plan):
        seen.append((plan.plugin, plan.trust))
        raise RuntimeError("needs a person")

    report = ship(folder, inst, change_record=record)
    assert not report["ok"] and "change record refused: needs a person" in report["reason"]
    assert seen == [("echo", "code")] and not host.loaded
    assert ship(folder, inst, change_record=lambda plan: None)["ok"]
    unreachable = run(d.install(folder, d.WorkerEnv("9" * 32, "linux", "3.11"), d.MemoryTransport(),
                                signer=HUB_SK, hub=HUB_ID, resolve=no_uv))
    assert not unreachable["ok"] and "not reachable" in unreachable["reason"]


def test_hub_only_plugin_has_no_worker_part(tmp_path):
    folder = make_plugin(tmp_path, check=False, extra="runs_on: hub")
    plan = d.prepare(folder, ENV, signer=HUB_SK, hub=HUB_ID)
    assert plan.sig is None and plan.hub_runs and plan.trust == "config"
    transport = d.MemoryTransport()
    assert run(d.push(plan, transport)) == {"ok": True, "plugin": "echo", "version": "1.0.0", "pushed": False}
    assert transport.sent == []


def test_trust_class_is_derived(tmp_path):
    assert d.prepare(make_plugin(tmp_path), None, signer=HUB_SK, hub=HUB_ID).trust == "config"
    page = make_plugin(tmp_path, name="page", files={"pages/a.html": "<p>"})
    assert d.prepare(page, None, signer=HUB_SK, hub=HUB_ID).trust == "page"


# -- python requirements with uv (mocked) ------------------------------------------------

class FakeRun:
    def __init__(self, rc=0, out="", err="", exc=None):
        self.rc, self.out, self.err, self.exc = rc, out, err, exc
        self.cmd = None
        self.files = {}

    def __call__(self, cmd, **kw):
        assert kw["timeout"] == d.UV_TIMEOUT
        self.cmd = cmd
        self.files = {"in": Path(cmd[3]).read_text(), "cons": Path(cmd[5]).read_text()}
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.rc, self.out, self.err)


def test_resolve_python_pins_only_new_packages():
    fake = FakeRun(out="anyio==4.15.1\nhttpx==0.28.1\nh11==0.16.0\n\n")
    env = d.WorkerEnv(WID, "linux", "3.12", {"httpx": "0.28.1"}, platform="aarch64-unknown-linux-gnu")
    assert d.resolve_python(["httpx>=0.27", "anyio"], env, run=fake) == ["anyio==4.15.1", "h11==0.16.0"]
    assert fake.cmd[:3] == ["uv", "pip", "compile"]
    assert fake.cmd[fake.cmd.index("--python-version") + 1] == "3.12"
    assert fake.cmd[fake.cmd.index("--python-platform") + 1] == "aarch64-unknown-linux-gnu"
    assert fake.files == {"in": "httpx>=0.27\nanyio\n", "cons": "httpx==0.28.1\n"}


def test_resolve_python_conflict_refuses_with_reason():
    err = ("Error: No solution found when resolving dependencies\n  cause: Because you require "
           "httpx>=0.27 and httpx==0.20.0, we can conclude that your requirements are unsatisfiable.")
    env = d.WorkerEnv(WID, "linux", "3.11", {"httpx": "0.20.0"})
    with pytest.raises(d.Refused, match=r"(?s)conflict on worker .*unsatisfiable"):
        d.resolve_python(["httpx>=0.27"], env, run=FakeRun(rc=1, err=err))


def test_resolve_python_refuses_options_bad_packages_and_missing_uv():
    for bad in ["--index-url=http://evil", "-e .", "x\n--extra-index-url y"]:
        with pytest.raises(d.Refused, match="not a requirement"):
            d.resolve_python([bad], ENV, run=FakeRun())
    with pytest.raises(d.Refused, match="bad package"):
        d.resolve_python(["x"], d.WorkerEnv(WID, "linux", "3.11", {"a": "1\n--evil"}), run=FakeRun())
    with pytest.raises(d.Refused, match="uv is not installed"):
        d.resolve_python(["x"], ENV, run=FakeRun(exc=FileNotFoundError()))
    with pytest.raises(d.Refused, match="took over"):
        d.resolve_python(["x"], ENV, run=FakeRun(exc=subprocess.TimeoutExpired("uv", 1)))
    with pytest.raises(d.Refused, match="would change httpx"):
        d.resolve_python(["x"], ENV, run=FakeRun(out="httpx==0.30.0\n"))
    for env in (d.WorkerEnv(WID, "linux", "--index-url=x"), d.WorkerEnv(WID, "linux", "3.11", platform="-x")):
        with pytest.raises(d.Refused, match="bad python"):
            d.resolve_python(["x"], env, run=FakeRun())
    assert d.resolve_python([], ENV, run=None) == []


def test_python_pins_are_signed_and_installed_before_load(tmp_path):
    installed = []
    inst, host = worker(tmp_path, install_python=installed.append)
    folder = make_plugin(tmp_path, extra="requires: { python: [anyio] }")
    report = ship(folder, inst, resolve=lambda reqs, env: ["anyio==4.15.1"])
    assert report["ok"] and installed == [["anyio==4.15.1"]]
    sig = json.loads((tmp_path / "plugins" / "echo" / "1.0.0" / "plugin.sig").read_text())
    assert sig["python"] == ["anyio==4.15.1"]

    def broken(pins):
        raise RuntimeError("no network")
    inst2, host2 = worker(tmp_path / "w2", install_python=broken)
    report = ship(folder, inst2, resolve=lambda reqs, env: ["anyio==4.15.1"])
    assert not report["ok"] and "no network" in report["reason"] and not host2.loaded


# -- assets ----------------------------------------------------------------------------------

def _asset_plugin(tmp_path, body: bytes, size=None):
    sha = hashlib.sha256(body).hexdigest()
    extra = f"assets: {{ model: {{ url: 'https://example.invalid/m.bin', sha256: {sha}"
    extra += f", size: {size if size is not None else len(body)} }} }}"
    return make_plugin(tmp_path, name=f"assets{len(list(tmp_path.iterdir()))}", extra=extra), sha


def test_assets_fetched_checked_cached_and_shipped(tmp_path):
    body = b"\x00model-weights" * 100
    folder, sha = _asset_plugin(tmp_path, body)
    fetched = []

    def fetch(url, dest):
        fetched.append(url)
        Path(dest).write_bytes(body)

    inst, _ = worker(tmp_path)
    cache = tmp_path / "cache"
    assert ship(folder, inst, cache=cache, fetch=fetch)["ok"]
    name = yaml.safe_load((folder / "plugin.yaml").read_text())["plugin"]
    assert (tmp_path / "plugins" / name / "1.0.0" / "assets" / "model").read_bytes() == body
    assert (cache / sha).is_file()
    d.prepare(folder, ENV, signer=HUB_SK, hub=HUB_ID, cache=cache, fetch=fetch, resolve=no_uv)
    assert len(fetched) == 1   # second prepare used the cache


def test_asset_hash_or_size_mismatch_refused(tmp_path):
    folder, _ = _asset_plugin(tmp_path, b"good")
    with pytest.raises(d.Refused, match="sha256 is"):
        d.prepare(folder, ENV, signer=HUB_SK, hub=HUB_ID, cache=tmp_path / "c", resolve=no_uv,
                  fetch=lambda url, dest: Path(dest).write_bytes(b"evil"))
    assert not list((tmp_path / "c").iterdir())
    folder, _ = _asset_plugin(tmp_path, b"good", size=99)
    with pytest.raises(d.Refused, match="4 bytes, the manifest says 99"):
        d.prepare(folder, ENV, signer=HUB_SK, hub=HUB_ID, cache=tmp_path / "c", resolve=no_uv,
                  fetch=lambda url, dest: Path(dest).write_bytes(b"good"))
    with pytest.raises(d.Refused, match="download failed"):
        d.prepare(folder, ENV, signer=HUB_SK, hub=HUB_ID, cache=tmp_path / "c", resolve=no_uv,
                  fetch=lambda url, dest: (_ for _ in ()).throw(OSError("dns")))


def test_fetch_url_refuses_non_http(tmp_path):
    with pytest.raises(d.Refused, match="http or https"):
        d.fetch_url("file:///etc/passwd", tmp_path / "x")


def test_fetch_url_over_local_http(tmp_path):
    body = b"weights" * 1000

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        d.fetch_url(f"http://127.0.0.1:{server.server_port}/m.bin", tmp_path / "m", timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        t.join(5)
    assert (tmp_path / "m").read_bytes() == body
