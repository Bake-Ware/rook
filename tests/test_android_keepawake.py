"""device.keep_awake / passive keep-awake / stored-PIN unlock (Android APK plugins)
against fake Kotlin bridges, no device needed."""
import asyncio
import importlib
import inspect
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from rook.core.registry import CapabilityRegistry
from rook.worker.plugin import Plugin, capability

PY = Path(__file__).parents[1] / "android/app/src/main/python"
PIN = "482913"


class FakeKeepAwake:
    def __init__(self):
        self.calls, self.auto, self.idle = [], True, 10
        self.started, self.locked, self.woke, self.auto_unlock = True, False, False, True

    def engage(self, ctx, minutes):
        self.calls.append(("engage", minutes))
        return json.dumps({"ok": True, "on": True, "mode": "manual", "remaining_s": minutes * 60})

    def release(self, ctx):
        self.calls.append(("release",))
        return json.dumps({"ok": True, "on": False, "mode": None})

    def status(self, ctx):
        return json.dumps({"ok": True, "on": False, "unlock_pin_set": True})

    def configure(self, ctx, auto, idle):
        self.calls.append(("configure", auto, idle))
        return json.dumps({"ok": True})

    def applyPushed(self, ctx, auto, idle): self.calls.append(("pushed", auto, idle))
    def autoEnabled(self, ctx): return self.auto
    def idleMinutes(self, ctx): return self.idle
    def screenOn(self, ctx): return True
    def keyguardLocked(self, ctx): return self.locked

    def activity(self, ctx, cap):
        self.calls.append(("activity", cap))
        return json.dumps({"ok": True, "auto": self.auto, "held": True, "started": self.started,
                           "woke": self.woke, "locked": self.locked, "pin_set": True,
                           "auto_unlock": self.auto_unlock})


class FakeUnlockPin:
    """Holds the PIN the way the Kotlin class does: only tapStored ever uses it."""

    def __init__(self, pin=PIN):
        self._pin, self.results, self.taps = pin, [], []

    def isSet(self, ctx): return self._pin is not None
    def status(self, ctx): return json.dumps({"ok": True, "pin_set": self.isSet(ctx), "auto_unlock": True})
    def noteUnlockResult(self, ctx, automatic, unlocked): self.results.append((automatic, unlocked))

    def tapStored(self, ctx, x1, x2, x3, y1, y2, y3, y4, gap):
        self.taps.append((x1, x2, x3, y1, y2, y3, y4))
        return json.dumps({"ok": True, "digits": len(self._pin), "taps_ok": len(self._pin)})


class FakeHid:
    def __init__(self): self.taps = []
    def isEnabled(self): return True
    def tap(self, x, y): self.taps.append((x, y)); return True
    def swipe(self, *a): return True


class _Obj:
    """Stand-in for a Java object/class: any attribute is a no-op callable."""
    def __init__(self, **kw): self.__dict__.update(kw)
    def __call__(self, *a, **k): return _Obj()
    def __getattr__(self, name): return _Obj()


@pytest.fixture
def env(monkeypatch):
    ka, pin, hid = FakeKeepAwake(), FakeUnlockPin(), FakeHid()
    metrics = _Obj(widthPixels=1080, heightPixels=2340)
    java = {
        "systems.bake.rook.KeepAwake": ka,
        "systems.bake.rook.UnlockPin": pin,
        "systems.bake.rook.HidAccessibilityService": hid,
        "android.provider.Settings": _Obj(canDrawOverlays=lambda ctx: True),
        "android.util.DisplayMetrics": lambda: metrics,
        "java.lang.Boolean": lambda v: ("Boolean", v),
        "java.lang.Integer": lambda v: ("Integer", v),
    }
    ctx = _Obj()
    fake_ctx = SimpleNamespace(app_context=lambda: ctx, jclass=lambda n: java.get(n, _Obj()),
                               cast=lambda cls, o: _Obj())
    monkeypatch.syspath_prepend(str(PY))
    monkeypatch.setitem(sys.modules, "rook_android.androidctx", fake_ctx)
    for name in ("rook_android.unlock", "rook_android.keepawake",
                 "rook_android.plugins.device_android"):
        sys.modules.pop(name, None)
    unlock = importlib.import_module("rook_android.unlock")
    keepawake = importlib.import_module("rook_android.keepawake")
    device = importlib.import_module("rook_android.plugins.device_android")

    async def no_sleep(_s): return None
    monkeypatch.setattr(unlock.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(keepawake.asyncio, "sleep", no_sleep)
    plugin = device.AndroidDevicePlugin.__new__(device.AndroidDevicePlugin)
    return SimpleNamespace(ka=ka, pin=pin, hid=hid, unlock=unlock, keepawake=keepawake,
                           device=device, plugin=plugin, fake_ctx=fake_ctx)


def run(coro):
    return asyncio.run(coro)


# ---- device.keep_awake ----------------------------------------------------

def test_keep_awake_on_off_and_status(env):
    out = env.plugin._keep_awake(on=True, minutes=45)
    assert out["on"] and out["mode"] == "manual" and env.ka.calls[-1] == ("engage", 45)
    out = env.plugin._keep_awake(on="false")
    assert out["on"] is False and env.ka.calls[-1] == ("release",)
    assert env.plugin._keep_awake_status()["unlock_pin_set"] is True


def test_keep_awake_on_is_required(env):
    assert "on" in inspect.signature(env.plugin._keep_awake).parameters
    assert inspect.signature(env.plugin._keep_awake).parameters["on"].default is inspect.Parameter.empty


def test_keep_awake_settings_boxes_only_given_fields(env):
    env.plugin._keep_awake_settings(auto=False)
    assert env.ka.calls[-1] == ("configure", ("Boolean", False), None)
    env.plugin._keep_awake_settings(idle_minutes="15", auto="")
    assert env.ka.calls[-1] == ("configure", None, ("Integer", 15))


def test_not_android_host(env, monkeypatch):
    monkeypatch.setattr(env.device, "app_context", lambda: None)
    for out in (env.plugin._keep_awake(on=True), env.plugin._keep_awake_status(),
                env.plugin._keep_awake_settings(auto=True), env.plugin._unlock_status()):
        assert out["ok"] is False


# ---- settings exposure ----------------------------------------------------

def test_settings_declared_and_pushed_env_is_forwarded(env, monkeypatch):
    names = {s.name: s for s in env.device.AndroidDevicePlugin.SETTINGS}
    assert names["keep_awake_auto"].default is True
    assert names["keep_awake_idle_minutes"].default == 10
    assert all(not s.secret for s in names.values())
    assert env.plugin.setting_values() == {"keep_awake_auto": True, "keep_awake_idle_minutes": 10}
    run(env.plugin.start())
    assert not [c for c in env.ka.calls if c[0] == "pushed"]
    monkeypatch.setenv("ROOK_KEEP_AWAKE_AUTO", "false")
    run(env.plugin.start())
    assert env.ka.calls[-1] == ("pushed", "false", None)


def test_settings_report_shows_values_never_pin(env, monkeypatch):
    from rook.worker import wconfig
    from rook.worker.core import Worker
    monkeypatch.setattr(wconfig, "load", lambda: {})
    env.ka.auto, env.ka.idle = False, 25
    fake = SimpleNamespace(plugins=[env.plugin], enable_arg=None,
                           admin=SimpleNamespace(excluded=lambda: []))
    report = Worker._settings_report(fake)
    got = {d["key"]: d.get("value") for d in report["declared"]}
    assert got == {"device.keep_awake_auto": False, "device.keep_awake_idle_minutes": 25}
    assert PIN not in json.dumps(report)


# ---- device.unlock / unlock_status -----------------------------------------

def test_unlock_status_reports_only_a_bool(env):
    out = env.plugin._unlock_status()
    assert out == {"ok": True, "pin_set": True, "auto_unlock": True}


def test_unlock_without_pin_uses_stored_and_returns_no_digits(env):
    out = run(env.plugin._unlock())
    assert out["ok"] and out["pin"] == "stored" and out["unlocked"] is True
    assert out["digits"] == len(PIN) and "taps" not in out
    assert env.pin.taps and not env.hid.taps           # Python never tapped digits itself
    assert PIN not in json.dumps(out)
    assert env.pin.results == [(False, True)]


def test_unlock_without_stored_pin_says_set_it_in_the_app(env):
    env.pin._pin = None
    out = run(env.plugin._unlock(pin=""))
    assert out["ok"] is False and "Rook app" in out["error"]


def test_unlock_with_pin_arg_keeps_existing_behaviour(env):
    out = run(env.plugin._unlock(pin="1234"))
    assert out["ok"] and [t["d"] for t in out["taps"]] == list("1234")
    assert len(env.hid.taps) == 4 and not env.pin.taps
    assert run(env.plugin._unlock(pin="12a4"))["ok"] is False


def test_failed_automatic_unlock_is_recorded(env):
    env.ka.locked = True
    out = run(env.unlock.unlock(None, automatic=True))
    assert out["unlocked"] is False and "paused" in out["note"]
    assert env.pin.results == [(True, False)]


# ---- passive keep-awake ----------------------------------------------------

class Caps(Plugin):
    NAMESPACE = "x"

    def __init__(self): self.seen = []

    @capability("sync", scope="multi-user")
    def sync(self, text: str = "") -> dict:
        self.seen.append(text)
        return {"ok": True, "text": text}

    @capability("async_", scope="multi-user")
    async def async_(self, n: int = 1) -> dict:
        return {"ok": True, "n": n}


def _registry(caps):
    reg = CapabilityRegistry()
    for path in ("hid.type", "screenshot.capture", "device.launch", "device.unlock"):
        reg.register(path, caps.sync)
    reg.register("ui.click", caps.sync, meta=SimpleNamespace(limit=None, fields=None))
    reg.register("hid.backend", caps.sync)
    reg.register("sms.list", caps.sync)
    reg.register("ui.scroll", caps.async_)
    return reg


def test_wrap_registry_picks_screen_caps_and_keeps_signatures(env):
    caps = Caps()
    reg = _registry(caps)
    wrapped = env.keepawake.wrap_registry(reg)
    assert sorted(wrapped) == ["device.launch", "device.unlock", "hid.type",
                               "screenshot.capture", "ui.click", "ui.scroll"]
    assert env.keepawake.wrap_registry(reg) == []      # idempotent
    assert list(inspect.signature(reg.handler("ui.click")).parameters) == ["text"]
    assert reg.meta("ui.click") is not None
    assert run(reg.call("ui.click", text="Trips")) == {"ok": True, "text": "Trips"}
    assert run(reg.call("ui.scroll", n=3)) == {"ok": True, "n": 3}
    assert ("activity", "ui.click") in env.ka.calls and ("activity", "ui.scroll") in env.ka.calls
    run(reg.call("sms.list"))
    run(reg.call("hid.backend"))
    assert not [c for c in env.ka.calls if c[1] in ("sms.list", "hid.backend")]


def test_engaging_on_lock_screen_unlocks_with_stored_pin(env):
    env.ka.locked, env.ka.woke = True, True
    calls = []

    async def fake_unlock(automatic):
        calls.append(automatic)
        return {"ok": True, "unlocked": True}
    env.keepawake._unlock = fake_unlock
    state = run(env.keepawake.before("ui.click"))
    assert calls == [True] and state["unlock"] == {"unlocked": True}
    run(env.keepawake.before("device.unlock"))          # never recursive
    env.ka.auto_unlock = False                          # no PIN / paused
    run(env.keepawake.before("ui.click"))
    env.ka.locked = False
    run(env.keepawake.before("ui.click"))
    assert calls == [True]


def test_passive_off_does_not_unlock_and_bridge_errors_never_break_caps(env):
    env.ka.auto, env.ka.locked = False, True
    state = run(env.keepawake.before("ui.click"))
    assert state["auto"] is False and "unlock" not in state

    def boom(ctx, cap): raise RuntimeError("bridge down")
    env.ka.activity = boom
    caps = Caps()
    reg = _registry(caps)
    env.keepawake.wrap_registry(reg)
    assert run(reg.call("ui.click", text="ok"))["ok"] is True
