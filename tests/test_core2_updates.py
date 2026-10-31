"""Core releases and updates (docs/castling-updates.md): the release format,
the worker's boot selector and updater, and the hub's update.* rollout."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

nacl = pytest.importorskip("nacl.signing")

from rook.core2 import CORE_VERSION, release, wire  # noqa: E402
from rook.core2.hub import updates  # noqa: E402
from rook.core2.worker import boot  # noqa: E402
from rook.core2.worker.update import Updater, UpdateError, unmet  # noqa: E402
from rook.kit.ctx import KitError  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
OP = nacl.SigningKey(bytes(range(32)))
OTHER = nacl.SigningKey(bytes(range(1, 33)))
HUB = "19c7d7856f775f9a"
KEYS = {wire.key_id(bytes(OP.verify_key)): wire.pub_text(OP)}


def source(tmp: Path, body: str = "X = 1\n", name: str = "src") -> Path:
    src = tmp / name
    (src / "rook" / "core2").mkdir(parents=True)
    (src / "rook" / "__init__.py").write_text("")
    (src / "rook" / "core2" / "thing.py").write_text(body)
    (src / "rook" / "core2" / "__pycache__").mkdir()
    (src / "rook" / "core2" / "__pycache__" / "thing.cpython-312.pyc").write_bytes(b"junk")
    (src / "pyproject.toml").write_text('[project]\nname = "rook"\nversion = "0.5.0"\ndependencies = ["pyyaml>=6"]\n')
    return src


def built(tmp: Path, version: str | None = "0.5.1", signer=OP, **kw) -> tuple[Path, dict]:
    src = source(tmp, **kw)
    dest = tmp / f"rel-{version}-{kw.get('name', 'src')}.tar.gz"
    return dest, release.build(src, dest, signer=signer, hub=HUB, version=version, now=1793300000)


# -- the release format ----------------------------------------------------------------

def test_a_release_holds_the_package_files_signed_once_for_every_worker(tmp_path):
    tar, sig = built(tmp_path)
    assert sig["typ"] == "rook-release" and sig["version"] == "0.5.1" and sig["core"] == CORE_VERSION
    assert set(sig["files"]) == {"rook/__init__.py", "rook/core2/thing.py"}      # no caches
    assert sig["requires"] == ["pyyaml>=6"] and "worker" not in sig
    assert release.check_sig(sig, KEYS) is None
    assert release.read_sig(tar) == sig
    release.extract(tar, sig, tmp_path / "out")
    assert (tmp_path / "out" / "rook" / "core2" / "thing.py").read_text() == "X = 1\n"
    assert boot.intact(tmp_path / "out") is None


def test_the_default_version_is_the_code_digest(tmp_path):
    a = release.plan(source(tmp_path, name="a"))[0]
    b = release.plan(source(tmp_path, name="b"))[0]
    c = release.plan(source(tmp_path, "X = 2\n", name="c"))[0]
    assert a == b and a != c and a.startswith("0.5.0+")
    with pytest.raises(release.ReleaseError, match="semver"):
        release.plan(source(tmp_path, name="d"), "v1")


def test_a_symlink_or_missing_package_is_refused(tmp_path):
    src = source(tmp_path)
    (src / "rook" / "link.py").symlink_to(src / "rook" / "__init__.py")
    with pytest.raises(release.ReleaseError, match="symlink"):
        release.plan(src)
    with pytest.raises(release.ReleaseError, match="no rook/"):
        release.plan(tmp_path / "nowhere")


@pytest.mark.parametrize("change, why", [
    (lambda s: {**s, "version": "0.5.2"}, "bad signature"),
    (lambda s: {**s, "typ": "rook-plugin"}, "malformed"),
    (lambda s: {**s, "files": {"../evil.py": "0" * 64}}, "malformed: files"),
    (lambda s: {**s, "files": {"rook/x.py": "0" * 64}}, "malformed: files"),       # no rook/__init__.py
    (lambda s: {**s, "requires": ["--index-url=http://x"]}, "malformed: requires"),
    (lambda s: wire.sign(OP, {**{k: v for k, v in s.items() if k not in ("kid", "sig")}, "core": "3.0"}),
     "speaks core 3.0"),
    (lambda s: wire.sign(OTHER, {k: v for k, v in s.items() if k not in ("kid", "sig")}), "not signed by"),
])
def test_a_release_sig_that_does_not_verify_is_refused(tmp_path, change, why):
    _, sig = built(tmp_path)
    assert why in release.check_sig(change(sig), KEYS)


def _retar(src: Path, dest: Path, edit) -> None:
    with tarfile.open(src, "r:gz") as a, tarfile.open(dest, "w:gz") as b:
        for m in a:
            data = a.extractfile(m).read()
            for m2, d2 in edit(m, data):
                b.addfile(m2, __import__("io").BytesIO(d2))


def test_a_tampered_tarball_is_refused_and_leaves_nothing(tmp_path):
    tar, sig = built(tmp_path)

    def swap(m, data):
        if m.name == "rook/core2/thing.py":
            data = b"X = 666\n"
            m.size = len(data)
        return [(m, data)]

    def extra(m, data):
        out = [(m, data)]
        if m.name == "rook/__init__.py":
            n = tarfile.TarInfo("rook/evil.py")
            n.size = 3
            out.append((n, b"x=1"))
        return out

    for edit, why in ((swap, "does not match"), (extra, "unexpected entry"),
                      (lambda m, d: [] if m.name == "rook/core2/thing.py" else [(m, d)], "missing")):
        bad = tmp_path / "bad.tar.gz"
        _retar(tar, bad, edit)
        with pytest.raises(release.ReleaseError, match=why):
            release.extract(bad, sig, tmp_path / "x")
        assert not (tmp_path / "x").exists()


# -- the boot selector ------------------------------------------------------------------

def installed(root: Path, tmp: Path, version: str, body: str = "X = 1\n") -> None:
    tar, sig = built(tmp, version, name=f"s-{version}", body=body)
    release.extract(tar, sig, root / version)


def test_a_trial_counts_its_starts_and_is_dropped_after_max_boots(tmp_path):
    root = tmp_path / "core"
    installed(root, tmp_path, "0.5.1")
    boot.write_state(root, {"current": None, "previous": None, "trial": {"version": "0.5.1", "boots": 0},
                            "failed": None})
    for n in range(1, boot.MAX_BOOTS + 1):
        assert boot.choose(root) == "0.5.1"
        assert boot.read_state(root)["trial"]["boots"] == n
        assert boot.choose(root, hop="0.5.1") == "0.5.1"            # our own exec: not another start
        assert boot.read_state(root)["trial"]["boots"] == n
    assert boot.choose(root) is None                                # back to the base
    s = boot.read_state(root)
    assert s["trial"] is None and s["failed"]["version"] == "0.5.1" and "did not come up" in s["failed"]["reason"]


def test_a_damaged_release_is_not_run(tmp_path):
    root = tmp_path / "core"
    installed(root, tmp_path, "0.5.1")
    installed(root, tmp_path, "0.5.2", body="X = 2\n")
    boot.write_state(root, {"current": "0.5.2", "previous": "0.5.1", "trial": None, "failed": None})
    assert boot.choose(root) == "0.5.2"
    (root / "0.5.2" / "rook" / "core2" / "thing.py").write_text("X = 'edited'\n")
    assert boot.choose(root) == "0.5.1"                              # previous, still intact
    (root / "0.5.1" / "rook" / "extra.py").write_text("")
    assert boot.choose(root) is None
    boot.write_state(root, {"current": None, "previous": None, "trial": {"version": "0.5.2", "boots": 0},
                            "failed": None})
    assert boot.choose(root) is None and "changed since install" in boot.read_state(root)["failed"]["reason"]


def test_select_execs_into_the_release_and_puts_pythonpath_back(tmp_path, monkeypatch):
    root = tmp_path / "data" / "core"
    installed(root, tmp_path, "0.5.1")
    boot.write_state(root, {"current": "0.5.1", "previous": None, "trial": None, "failed": None})
    monkeypatch.setenv("PYTHONPATH", "/base")
    monkeypatch.delenv(boot.HOP, raising=False)
    monkeypatch.delenv(boot.SAVED_PATH, raising=False)
    calls = []
    monkeypatch.setattr(boot, "running_folder", lambda: None)
    assert boot.select(tmp_path / "data", execv=lambda exe, argv: calls.append((exe, argv))) == "0.5.1"
    assert calls and calls[0][0] == sys.executable
    assert os.environ["PYTHONPATH"] == f"{(root / '0.5.1').resolve()}{os.pathsep}/base"
    assert os.environ[boot.HOP] == "0.5.1" and os.environ[boot.SAVED_PATH] == "/base"
    # the new process: running from the release, the hop marker set
    monkeypatch.setattr(boot, "running_folder", lambda: (root / "0.5.1").resolve())
    calls.clear()
    assert boot.select(tmp_path / "data", execv=lambda *a: calls.append(a)) == "0.5.1"
    assert not calls and os.environ["PYTHONPATH"] == "/base"
    assert boot.HOP not in os.environ and boot.SAVED_PATH not in os.environ


def test_select_does_not_loop_when_the_switch_does_not_take(tmp_path, monkeypatch):
    root = tmp_path / "data" / "core"
    installed(root, tmp_path, "0.5.1")
    boot.write_state(root, {"current": "0.5.1", "previous": None, "trial": None, "failed": None})
    monkeypatch.setenv(boot.HOP, "0.5.1")
    monkeypatch.setattr(boot, "running_folder", lambda: None)
    monkeypatch.setattr(boot, "running", lambda: None)
    calls = []
    assert boot.select(tmp_path / "data", execv=lambda *a: calls.append(a)) is None and not calls


# -- the worker's updater ----------------------------------------------------------------

def updater(tmp: Path, running=None, check=None, installs=None) -> Updater:
    return Updater(tmp / "core", keys=lambda hub: KEYS if hub == HUB else {}, running=running,
                   install_python=(installs.append if installs is not None else lambda pins: None),
                   check=check or (lambda folder, cfg: None))


def run(coro):
    return asyncio.run(coro)


def test_receive_makes_a_trial_then_confirm_makes_it_current(tmp_path):
    tar, _ = built(tmp_path)
    up = updater(tmp_path)
    report = run(up.receive(tar))
    assert report == {"version": "0.5.1", "from": None, "hub": HUB, "restarting": True}
    s = up.state()
    assert s["trial"]["version"] == "0.5.1" and s["current"] is None
    with pytest.raises(UpdateError, match="already waiting"):
        run(up.receive(tar))
    # the restarted process runs the trial
    after = updater(tmp_path, running="0.5.1")
    assert after.in_trial() and after.confirm()
    s = after.state()
    assert s["current"] == "0.5.1" and s["previous"] is None and s["trial"] is None
    assert after.status()["kept"] == ["0.5.1"]


def test_a_failed_smoke_check_refuses_and_removes_the_release(tmp_path):
    tar, _ = built(tmp_path)

    def bad(folder, cfg):
        raise UpdateError("smoke check failed: ImportError")
    up = updater(tmp_path, check=bad)
    with pytest.raises(UpdateError, match="smoke check failed"):
        run(up.receive(tar))
    assert up.state()["trial"] is None and not (tmp_path / "core" / "0.5.1").exists()


def test_receive_refuses_other_hubs_the_running_version_and_reused_versions(tmp_path):
    other, _ = built(tmp_path, signer=OTHER, name="o")
    with pytest.raises(UpdateError, match="signature refused"):
        run(updater(tmp_path).receive(other))
    tar, _ = built(tmp_path, name="a")
    with pytest.raises(UpdateError, match="already running"):
        run(updater(tmp_path, running="0.5.1").receive(tar))
    up = updater(tmp_path, running="0.5.0")
    run(up.receive(tar))
    up.abandon("test")
    changed, _ = built(tmp_path, body="X = 2\n", name="b")
    with pytest.raises(UpdateError, match="bump the version"):
        run(up.receive(changed))


def test_unmet_requirements_are_installed_first(tmp_path):
    assert unmet(["pyyaml>=6", "pyyaml>=999", "surely-not-installed-xyz", 'thing; sys_platform == "nope"']) == \
        ["pyyaml>=999", "surely-not-installed-xyz"]
    src = source(tmp_path)
    (src / "pyproject.toml").write_text('[project]\nname = "rook"\nversion = "0.5.0"\n'
                                        'dependencies = ["pyyaml>=6", "surely-not-installed-xyz>=1"]\n')
    tar = tmp_path / "r.tar.gz"
    release.build(src, tar, signer=OP, hub=HUB, version="0.5.1")
    pins: list = []
    run(updater(tmp_path, installs=pins).receive(tar))
    assert pins == [["surely-not-installed-xyz>=1"]]


def test_abandon_and_rollback(tmp_path):
    up = updater(tmp_path)
    run(up.receive(built(tmp_path, "0.5.1", name="a")[0]))
    trial = updater(tmp_path, running="0.5.1")
    trial.confirm()
    run(trial.receive(built(tmp_path, "0.5.2", name="b", body="X = 2\n")[0]))
    trial2 = updater(tmp_path, running="0.5.2")
    trial2.abandon("no contact with the home hub within 120s")
    s = trial2.state()
    assert s["current"] == "0.5.1" and s["trial"] is None and s["failed"]["version"] == "0.5.2"
    assert boot.choose(tmp_path / "core") == "0.5.1"
    # by hand (worker.update {version}): back to the installed core, and forward again
    assert updater(tmp_path, running="0.5.1").switch("installed") == \
        {"version": None, "from": "0.5.1", "restarting": True}
    assert boot.choose(tmp_path / "core") is None
    with pytest.raises(UpdateError, match="already on installed"):
        updater(tmp_path).switch("installed")
    with pytest.raises(UpdateError, match=r"0.5.2 isn't kept here \(kept: 0.5.1, installed\)"):
        updater(tmp_path).switch("0.5.2")
    assert updater(tmp_path).switch("0.5.1")["version"] == "0.5.1"
    with pytest.raises(UpdateError, match="isn't kept"):
        updater(tmp_path / "empty").switch("0.5.1")


def test_refuse_running_falls_back_to_the_installed_core(tmp_path):
    up = updater(tmp_path)
    run(up.receive(built(tmp_path)[0]))
    t = updater(tmp_path, running="0.5.1")
    t.confirm()
    t.refuse_running("hub x no longer approves code on this machine")
    s = t.state()
    assert s["current"] is None and s["failed"]["version"] == "0.5.1"


def test_the_smoke_check_runs_the_releases_own_code(tmp_path):
    """A release of this checkout, unpacked, runs ``check`` from its folder."""
    from rook.core2.worker.update import smoke
    tar = tmp_path / "rel.tar.gz"
    sig = release.build(REPO, tar, signer=OP, hub=HUB, version="0.5.1-test.1")
    release.extract(tar, sig, tmp_path / "core" / "0.5.1-test.1")
    cfg = tmp_path / "worker.yaml"
    cfg.write_text(json.dumps({"data": "./data", "anchor": "./anchor.json", "name": "smoke"}))
    smoke(tmp_path / "core" / "0.5.1-test.1", cfg)
    # a release whose worker can't import fails it
    broken = tmp_path / "core" / "0.5.1-test.1" / "rook" / "core2" / "worker" / "runtime.py"
    broken.write_text("raise ImportError('broken on purpose')\n")
    with pytest.raises(UpdateError, match="broken on purpose"):
        smoke(tmp_path / "core" / "0.5.1-test.1", cfg)


def test_check_from_the_installed_core_names_no_release(tmp_path):
    cfg = tmp_path / "worker.yaml"
    cfg.write_text(json.dumps({"data": "./data", "anchor": "./anchor.json", "name": "smoke"}))
    r = subprocess.run([sys.executable, "-m", "rook.core2.worker", "--config", str(cfg), "check"],
                       capture_output=True, text=True, timeout=120, cwd=REPO)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout) == {"ok": True, "folder": None, "version": None}


# -- the hub's rollout ---------------------------------------------------------------------

class FakeHub:
    """What Updates uses of the hub: known workers, their caps, Drop, call_as."""

    def __init__(self, data: Path, workers: dict):
        self.data, self.op, self.hub_id, self.node = data, OP, HUB, "hubnode"
        self.plugins = SimpleNamespace(plugins={"change": object()})
        self.changes: list = []          # change records the rollouts left (Hub.note_change)
        self.known = {n: SimpleNamespace(id=n, name=n, version="0.5.0", online=True, band="b") for n in workers}
        self.dispatcher = SimpleNamespace(workers={n: SimpleNamespace(caps={"worker.update": {}} if up else {})
                                                   for n, up in workers.items()})
        self.transport = SimpleNamespace(offer=lambda band, blob, to=None: {"id": "d", "to": to})
        self.fail: dict = {}            # worker -> how it fails: "refuse" | "rollback"
        self.calls: list = []
        self.cores = {n: {"running": None, "trial": None, "failed": None} for n in workers}

    async def note_change(self, who, record):
        if "change" not in self.plugins.plugins:
            return None
        self.changes.append((who.person, record))
        return f"c{len(self.changes)}"

    async def call_as(self, who, cap, args, target=None, **kw):
        self.calls.append((cap, target, who.person))
        if cap == "worker.update":
            if self.fail.get(target) == "held":
                return {"ok": False, "held": True, "hold": "h_1", "error": "held: sacred machine (hold h_1)"}
            if self.fail.get(target) == "refuse":
                return {"ok": False, "error": "update refused: smoke check failed"}
            self._version = args
            how = self.fail.get(target)
            self.cores[target] = ({"running": None, "trial": None,
                                   "failed": {"version": self._target, "reason": "did not come up"}}
                                  if how == "rollback" else {"running": self._target, "trial": None, "failed": None})
            return {"ok": True, "result": {"restarting": True}}
        if cap == "worker.status":
            return {"ok": True, "result": {"release": self.cores[target]}}
        raise AssertionError(cap)


def ctx(owner=True):
    return SimpleNamespace(caller=SimpleNamespace(person="bake", agent=None, owner=owner))


async def finish(up: updates.Updates):
    await asyncio.wait_for(up.task, 10)


def make(tmp_path, monkeypatch, workers):
    monkeypatch.setattr(updates, "POLL", 0.01)
    hub = FakeHub(tmp_path / "hub", workers)
    return hub, updates.Updates(hub)


def test_a_release_is_signed_once(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {})
    src = source(tmp_path)

    async def go():
        a = await up._prepare(ctx(), version="0.5.1", source=str(src))
        b = await up._prepare(ctx(), version="0.5.1", source=str(src))
        (src / "rook" / "core2" / "thing.py").write_text("X = 2\n")
        with pytest.raises(KitError, match="other code"):
            await up._prepare(ctx(), version="0.5.1", source=str(src))
        return a, b
    a, b = asyncio.run(go())
    assert a["new"] and not b["new"] and a["sha256"] == b["sha256"] and a["files"] == 2
    sig = release.read_sig(tmp_path / "hub" / "releases" / "0.5.1.tar.gz")
    assert release.check_sig(sig, KEYS) is None and sig["hub"] == HUB


def test_rollout_goes_canary_first_then_batches_and_skips_what_it_cannot_update(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {"a": True, "b": True, "c": True, "d": True, "phone": False})
    hub.known["d"].online = False
    hub._target = "0.5.1"

    async def go():
        started = await up.rollout(ctx(), "0.5.1", source=str(source(tmp_path)), canary=["c"])
        await finish(up)
        return started, await up.status(ctx()), await up.history(ctx())
    started, status, history = asyncio.run(go())
    assert started["canary"] == ["c"] and started["rest"] == ["a", "b"] and started["signed"]
    assert set(started["skipped"]) == {"d", "phone"}
    assert [t for cap, t, _ in hub.calls if cap == "worker.update"][0] == "c"
    assert all(p == "bake" for _, _, p in hub.calls)                     # as the person who started it
    assert [(cap, t) for cap, t, _ in hub.calls if cap == "worker.update"][1:] == \
        [("worker.update", "a"), ("worker.update", "b")]
    # one change record for the whole rollout, as the person, written when it ended
    [(by, rec)] = hub.changes
    assert by == "bake" and rec["category"] == "update" and rec["workers"] == ["c", "a", "b"]
    assert rec["outcome"] == "done: updated c, a, b" and status["rollout"]["change"] == "c1"
    assert status["rollout"]["status"] == "done"
    assert {w["name"]: w["state"] for w in status["rollout"]["workers"]} == \
        {"c": "updated", "a": "updated", "b": "updated", "d": "skipped", "phone": "skipped"}
    events = [e["event"] for e in history["entries"]]
    assert events[0] == "rollout.done" and events[-1] == "prepared" and events.count("worker.updated") == 3


def test_a_failing_canary_stops_the_rollout(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {"a": True, "b": True, "c": True})
    hub._target = "0.5.1"
    hub.fail["a"] = "rollback"

    async def go():
        await up.rollout(ctx(), "0.5.1", source=str(source(tmp_path)))
        await finish(up)
        return await up.status(ctx())
    st = asyncio.run(go())["rollout"]
    states = {w["name"]: (w["state"], w.get("reason")) for w in st["workers"]}
    assert st["status"] == "failed"
    assert states["a"] == ("failed", "rolled back on the worker: did not come up")
    assert states["b"][0] == states["c"][0] == "not started"
    assert [t for cap, t, _ in hub.calls if cap == "worker.update"] == ["a"]
    assert hub.changes[0][1]["outcome"] == "failed: updated none; a failed: rolled back on the worker: did not come up"


def test_a_refused_update_and_a_worker_that_never_comes_back_fail(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {"a": True, "b": True})
    hub._target = "0.5.1"
    hub.fail["a"] = "refuse"

    async def go():
        await up.rollout(ctx(), "0.5.1", source=str(source(tmp_path)), workers=["a"])
        await finish(up)
        first = (await up.status(ctx()))["rollout"]
        hub.fail.clear()
        hub._target = "0.4.9"            # the worker comes back on something else
        await up.rollout(ctx(), "0.5.1", workers=["b"], gate=0)
        await finish(up)
        return first, (await up.status(ctx()))["rollout"]
    first, second = asyncio.run(go())
    assert first["workers"][0]["reason"] == "update refused: smoke check failed"
    assert second["status"] == "failed" and "not back on 0.5.1" in second["workers"][0]["reason"]


def test_rollout_refusals(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {"a": True})

    async def go():
        with pytest.raises(KitError, match="no release"):
            await up.rollout(ctx(), "9.9.9")
        src = str(source(tmp_path))
        with pytest.raises(KitError, match="only an owner"):
            await up.rollout(ctx(owner=False), "0.5.1", source=src)
        await up._prepare(ctx(), version="0.5.1", source=src)
        with pytest.raises(KitError, match="unknown worker"):
            await up.rollout(ctx(), "0.5.1", canary=["nope"])
        hub.known["a"].version = "0.5.1"
        with pytest.raises(KitError, match="already on 0.5.1"):
            await up.rollout(ctx(), "0.5.1")
    asyncio.run(go())


def test_a_rollout_needs_no_change_plugin(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {"a": True})
    hub._target = "0.5.1"
    hub.plugins.plugins.clear()

    async def go():
        await up.rollout(ctx(), "0.5.1", source=str(source(tmp_path)))
        await finish(up)
        return (await up.status(ctx()))["rollout"]
    ro = asyncio.run(go())
    assert ro["status"] == "done" and ro["change"] is None and not hub.changes
    assert [t for cap, t, _ in hub.calls if cap == "worker.update"] == ["a"]


def test_a_held_update_stops_the_rollout(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {"a": True, "b": True})
    hub._target = "0.5.1"
    hub.fail["a"] = "held"

    async def go():
        await up.rollout(ctx(), "0.5.1", source=str(source(tmp_path)))
        await finish(up)
        return (await up.status(ctx()))["rollout"]
    ro = asyncio.run(go())
    assert ro["status"] == "failed" and "waits for a person (hold h_1)" in ro["workers"][0]["reason"]
    assert ro["workers"][1]["state"] == "not started"


def test_a_rollout_cut_short_by_a_hub_restart_is_recorded(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {"a": True})
    up.rollout_state = {"id": "r1", "version": "0.5.1", "status": "running", "workers": []}
    up._save_rollout()
    again = updates.Updates(hub)
    assert again.rollout_state["status"] == "interrupted"
    assert asyncio.run(again.history(ctx()))["entries"][0]["event"] == "rollout.interrupted"


def test_old_releases_are_pruned(tmp_path, monkeypatch):
    hub, up = make(tmp_path, monkeypatch, {})
    src = source(tmp_path)

    async def go():
        for i in range(updates.KEEP + 2):
            (src / "rook" / "core2" / "thing.py").write_text(f"X = {i}\n")
            await up._prepare(ctx(), version=f"0.5.{i}", source=str(src))
            await asyncio.sleep(0.01)
    asyncio.run(go())
    assert sorted(p.name for p in (tmp_path / "hub" / "releases").glob("*.tar.gz")) == \
        [f"0.5.{i}.tar.gz" for i in range(2, updates.KEEP + 2)]


# -- the desktop app's update check (R10) -------------------------------------------------

def test_the_desktop_feed_lists_the_apps_files_and_caches(monkeypatch):
    monkeypatch.setattr(updates, "_feed", {"at": 0.0, "body": None})
    asked = []

    def fetch():
        asked.append(1)
        return {"tag_name": "v0.5.0", "assets": [
            {"name": "rook-desktop-0.3.0-x64-setup.exe",
             "browser_download_url": "https://github.com/Bake-Ware/rook/releases/download/v0.5.0/rook-desktop-0.3.0-x64-setup.exe"},
            {"name": "rook-worker.apk", "browser_download_url": "https://example/apk"}]}
    got = updates.desktop_feed(fetch, now=1000.0)
    assert got["release"] == "v0.5.0" and [d["name"] for d in got["downloads"]] == ["rook-desktop-0.3.0-x64-setup.exe"]
    assert updates.desktop_feed(fetch, now=2000.0) == got and len(asked) == 1     # cached

    def down():
        raise OSError("offline")
    assert updates.desktop_feed(down, now=1000.0 + updates.FEED_SECS + 1) == got   # the last good answer
    monkeypatch.setattr(updates, "_feed", {"at": 0.0, "body": None})
    assert updates.desktop_feed(down, now=5.0) == {"ok": True, "release": "", "downloads": []}
    assert updates.desktop_feed(fetch, now=5.0 + updates.FEED_RETRY + 1)["downloads"]   # retried soon after a failure


def test_a_garbled_state_never_keeps_the_worker_from_starting(tmp_path):
    root = tmp_path / "core"
    installed(root, tmp_path, "0.5.1")
    boot.write_state(root, {"current": None, "previous": None, "trial": {"version": "0.5.1", "boots": "x"},
                            "failed": None})
    assert boot.choose(root) is None and boot.read_state(root)["failed"]["version"] == "0.5.1"
    (root / boot.STATE).write_text("{not json")
    assert boot.choose(root) is None
    boot.write_state(root, {"current": "../../etc", "previous": None, "trial": "x", "failed": None})
    assert boot.choose(root) is None
