"""rook.kit.hooks: one scheduler for start/stop/status, every and on."""

import asyncio

import pytest

from rook.kit.ctx import Caller, KitError
from rook.kit.hooks import Scheduler
from rook.kit.records import RecordStore

pytestmark = pytest.mark.asyncio

SCRIPT = '''
import asyncio
LOG = []

def start(ctx):
    LOG.append(("start", ctx.caller.plugin, ctx.caller.person))

async def stop(ctx):
    LOG.append(("stop",))

def status(ctx):
    return {"connected": True}

def warm(ctx, event, payload):
    LOG.append(("warm", event, payload))

def tick(ctx):
    LOG.append(("tick",))

def boom(ctx, **kw):
    raise ctx.Error("broken")

async def slow(ctx, **kw):
    LOG.append(("slow-start",))
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        LOG.append(("slow-cancelled",))
        raise
'''


@pytest.fixture
def plugin(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "tv.py").write_text(SCRIPT)
    return tmp_path


def _log():
    import sys
    return sys.modules["rook_plugin_" + _log.dir + "_tv"].LOG


class Dispatch:
    def __init__(self):
        self.calls = []

    async def __call__(self, cap, args, caller):
        self.calls.append((cap, args, caller))
        return {"ok": True}


async def until(cond, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


async def test_lifecycle(plugin):
    _log.dir = plugin.name.replace("-", "_")
    d = Dispatch()
    s = Scheduler(dispatch=d)
    manifest = {"plugin": "tv", "uses": ["notify.*"], "hooks": {
        "start": "scripts/tv.py:start", "stop": "scripts/tv.py:stop",
        "status": "scripts/tv.py:status",
        "every": [{"interval": "20ms", "run": "scripts/tv.py:tick"},
                  {"interval": "20ms", "call": "tv.stats.custom", "args": {"fast": True}}],
        "on": [{"event": "worker.*", "run": "scripts/tv.py:warm"},
               {"event": "worker.online", "call": "notify.send", "args": {"text": "up"}}]}}
    await s.start(manifest, plugin_dir=plugin, band="b1")
    log = _log()
    assert log[0] == ("start", "tv", None)               # as the plugin, no person
    assert await s.status("tv") == {"connected": True}

    await until(lambda: log.count(("tick",)) >= 2 and len(d.calls) >= 2)
    cap, args, caller = d.calls[0]
    assert (cap, args, caller.plugin, caller.band) == ("tv.stats.custom", {"fast": True}, "tv", "b1")

    s.publish("worker.online", {"worker": "kaiju"})
    s.publish("phone.moved", {})
    await until(lambda: ("warm", "worker.online", {"worker": "kaiju"}) in log
                and any(c[0] == "notify.send" for c in d.calls))
    assert [c[1] for c in d.calls if c[0] == "notify.send"] == [{"text": "up"}]

    await s.stop("tv")
    assert log[-1] == ("stop",)
    ticks, calls = log.count(("tick",)), len(d.calls)
    await asyncio.sleep(0.08)
    s.publish("worker.online", {})
    await asyncio.sleep(0.05)
    assert log.count(("tick",)) == ticks and len(d.calls) == calls   # nothing left running
    assert s.plugins == [] and await s.status("tv") is None


async def test_errors_dont_stop_the_timer(plugin, caplog):
    s = Scheduler()
    n = {"runs": 0}

    def flaky():
        n["runs"] += 1
        raise RuntimeError("nope")

    s.every("core", 0.01, flaky)
    await until(lambda: n["runs"] >= 3)
    await s.stop("core")
    assert "nope" in caplog.text


async def test_stop_cancels_in_flight(plugin):
    _log.dir = plugin.name.replace("-", "_")
    s = Scheduler()
    await s.start({"plugin": "tv", "hooks": {"on": [{"event": "go", "run": "scripts/tv.py:slow"}]}},
                  plugin_dir=plugin)
    s.publish("go")
    log = _log()
    await until(lambda: ("slow-start",) in log)
    await s.stop("tv")
    assert ("slow-cancelled",) in log


async def test_status_error_and_failing_start(plugin):
    s = Scheduler()
    await s.start({"plugin": "tv", "hooks": {"status": {"run": "scripts/tv.py:boom"}}}, plugin_dir=plugin)
    assert await s.status("tv") == {"error": "broken"}
    with pytest.raises(KitError, match="already"):
        await s.start({"plugin": "tv"}, plugin_dir=plugin)
    await s.stop("tv")

    with pytest.raises(KitError, match="broken"):
        await s.start({"plugin": "tv", "hooks": {"start": "scripts/tv.py:boom",
                                                 "every": [{"interval": "10ms", "run": "scripts/tv.py:tick"}]}},
                      plugin_dir=plugin)
    assert s.plugins == []                                # nothing armed


async def test_call_hooks_respect_uses():
    d = Dispatch()
    s = Scheduler(dispatch=d)
    await s.start({"plugin": "tv", "hooks": {"on": [{"event": "x", "call": "shell.exec"}]}})
    s.publish("x")
    await asyncio.sleep(0.05)
    assert d.calls == []
    await s.stop("tv")


async def test_bad_hooks(plugin):
    s = Scheduler()
    with pytest.raises(KitError, match="folder"):
        await s.start({"plugin": "tv", "hooks": {"start": "scripts/tv.py:start"}})
    with pytest.raises(KitError, match="positive"):
        await s.start({"plugin": "tv", "hooks": {"every": [{"interval": 0, "call": "tv.x"}]}})
    with pytest.raises(KitError, match="run: or call:"):
        await s.start({"plugin": "tv", "hooks": {"on": [{"event": "x"}]}})
    with pytest.raises(ValueError):
        await s.start({"plugin": "tv", "hooks": {"every": [{"interval": "soon", "call": "tv.x"}]}})


async def test_records_events_reach_on_hooks_from_a_thread():
    """Records emit through the scheduler; writes from sync plugin code (a
    worker thread) still land on the loop."""
    s = Scheduler()
    got = []
    store = RecordStore(emit=s.publish)
    store.register({"plugin": "hw", "records": {"item": {"fields": {"t": {}}}}})
    await s.start({"plugin": "watcher", "hooks": {"on": [{"event": "hw.item.*", "run": "w.py:f"}]}},
                  resolve=lambda ref: (lambda ctx, event, payload: got.append(event)))
    await asyncio.to_thread(store.type("hw.item").create, Caller(person="bake"), t="x")
    await until(lambda: got == ["hw.item.created"])
    await s.close()


async def test_core_every_with_async_fn():
    s = Scheduler()
    hits = []

    async def prune():
        hits.append(1)

    s.every("records", "10ms", prune)
    await until(lambda: len(hits) >= 2)
    await s.close()
    assert s.plugins == []


async def test_may_receive_filters_events():
    got = []
    s = Scheduler(may_receive=lambda plugin, event: not event.startswith("secret."))
    await s.start({"plugin": "w", "hooks": {"on": [{"event": "*", "run": "w.py:f"}]}},
                  resolve=lambda ref: (lambda ctx, event, payload: got.append(event)))
    s.publish("secret.item.created", {"record": {"x": 1}})
    s.publish("worker.online")
    await until(lambda: got == ["worker.online"])
    await asyncio.sleep(0.03)
    assert got == ["worker.online"]
    await s.close()


async def test_on_hooks_that_ask_get_the_events_source():
    got = []
    s = Scheduler()
    funcs = {"a.py:f": lambda ctx, event, payload, source=None: got.append(("asks", event, source)),
             "a.py:g": lambda ctx, event, payload: got.append(("plain", event))}
    await s.start({"plugin": "w", "hooks": {"on": [{"event": "*", "run": "a.py:f"}, {"event": "*", "run": "a.py:g"}]}},
                  resolve=funcs.__getitem__)
    s.publish("telegram.message", {"text": "hi"}, "telegram")
    s.publish("worker.online")
    await until(lambda: len(got) == 4)
    assert sorted(got, key=str) == sorted([("asks", "telegram.message", "telegram"), ("plain", "telegram.message"),
                                           ("asks", "worker.online", None), ("plain", "worker.online")], key=str)
    await s.close()
