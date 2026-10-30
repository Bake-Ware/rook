"""rook.kit.ctx: the call context and the Python contract."""

import pytest

from rook.kit.ctx import Caller, Ctx, FileResult, KitError, allowed, invoke, load_run

MANIFEST = {
    "plugin": "tv",
    "uses": ["notify.send", "knowledge.*"],
    "settings": {
        "host": {"default": "10.0.0.5"},
        "client_key": {"secret": True},
        "port": {"type": "int"},
    },
    "emits": {"tv.power.changed": {}},
    "records": {"show": {"fields": {"title": {}}}},
}

BAKE = Caller(person="bake", agent="claude", session="s1", band="b1", why="testing")


class FakeDispatch:
    def __init__(self, result="ok", is_async=False):
        self.calls, self.result, self.is_async = [], result, is_async

    def __call__(self, cap, args, caller):
        self.calls.append((cap, args, caller))
        if self.is_async:
            async def res():
                return self.result
            return res()
        return self.result


def test_identity_and_settings():
    ctx = Ctx(MANIFEST, BAKE, settings={"client_key": "tv-key", "port": 3000},
              resolve_secret=lambda name: f"<{name}>")
    assert (ctx.person, ctx.agent, ctx.session, ctx.band, ctx.why) == ("bake", "claude", "s1", "b1", "testing")
    assert ctx.settings["host"] == "10.0.0.5"          # manifest default
    assert ctx.settings["port"] == 3000
    assert ctx.settings["client_key"] == "<tv-key>"    # vault name resolved per read
    assert set(ctx.settings) == {"host", "client_key", "port"}
    with pytest.raises(KeyError):
        ctx.settings["nope"]


def test_secret_without_vault_and_setting_writes():
    stored = []
    ctx = Ctx(MANIFEST, BAKE, settings={"client_key": "tv-key"},
              store_setting=lambda k, v, secret: stored.append((k, v, secret)))
    with pytest.raises(KitError, match="no vault"):
        ctx.settings["client_key"]
    ctx.settings.set("client_key", "s3cret")
    ctx.settings.set("host", "10.0.0.9")
    assert stored == [("client_key", "s3cret", True), ("host", "10.0.0.9", False)]
    assert ctx.settings["host"] == "10.0.0.9"
    with pytest.raises(KitError, match="not declared"):
        ctx.settings.set("other", 1)


def test_uses_globs():
    assert allowed("tv", [], "tv.power.set")            # own namespace
    assert not allowed("tv", [], "tvx.power.set")
    assert allowed("tv", ["knowledge.*"], "knowledge.page.get")
    assert not allowed("tv", ["knowledge.*"], "shell.exec")


def test_call_sync_and_refusal():
    d = FakeDispatch()
    ctx = Ctx(MANIFEST, BAKE, dispatch=d)
    assert ctx.call("notify.send", text="hi") == "ok"
    cap, args, caller = d.calls[0]
    assert (cap, args) == ("notify.send", {"text": "hi"})
    assert caller.person == "bake" and caller.plugin == "tv"   # still the caller's rights
    with pytest.raises(KitError, match="uses"):
        ctx.call("shell.exec", argv=["rm"])
    assert len(d.calls) == 1


@pytest.mark.asyncio
async def test_call_from_async_and_thread():
    d = FakeDispatch(result=42, is_async=True)
    ctx = Ctx(MANIFEST, BAKE, dispatch=d)

    async def afunc(ctx, n):
        return await ctx.call("notify.send", n=n)

    def sfunc(ctx, n):
        return ctx.call("knowledge.page.get", n=n)      # blocks on the host loop

    assert await invoke(afunc, ctx, {"n": 1}) == 42
    assert await invoke(sfunc, ctx, {"n": 2}) == 42
    assert [c[1] for c in d.calls] == [{"n": 1}, {"n": 2}]


def test_emit_declared_only():
    seen = []
    ctx = Ctx(MANIFEST, BAKE, emit=lambda e, p, src: seen.append((e, p, src)))
    ctx.emit("tv.power.changed", {"on": True})
    ctx.emit("tv.show.created", {"id": "x"})            # record auto event
    with pytest.raises(KitError, match="emits"):
        ctx.emit("tv.secret.thing")
    assert seen == [("tv.power.changed", {"on": True}, "tv"), ("tv.show.created", {"id": "x"}, "tv")]


@pytest.mark.asyncio
async def test_data_tmp_and_file(tmp_path):
    ctx = Ctx(MANIFEST, BAKE, data_dir=tmp_path / "data")
    assert ctx.data.is_dir()

    def shoot(ctx):
        p = ctx.tmp / "s.png"
        p.write_bytes(b"png")
        return ctx.file(p, mime="image/png"), ctx.tmp

    res, tmp = await invoke(shoot, ctx)
    assert isinstance(res, FileResult) and res.name == "s.png" and res.mime == "image/png"
    assert not tmp.exists()                              # tmp removed after the call
    assert ctx.file(data=b"x", name="a.bin").data == b"x"
    with pytest.raises(KitError):
        ctx.file(tmp_path / "missing")
    with pytest.raises(KitError):
        ctx.file()


def test_error_records_and_stream_unavailable():
    ctx = Ctx(MANIFEST, BAKE)
    err = ctx.Error("device busy", retry=True)
    assert isinstance(err, KitError) and err.retry and str(err) == "device busy"
    with pytest.raises(KitError, match="hub"):
        ctx.records
    with pytest.raises(KitError, match="streams"):
        ctx.stream(types=["bytes"])
    with pytest.raises(KitError, match="data dir"):
        ctx.data


def test_stream_delegates():
    opened = []

    class Streams:
        def open(self, **kw):
            opened.append(kw)
            return "st_1"

    ctx = Ctx(MANIFEST, BAKE, streams=Streams())
    assert ctx.stream(types=["status"], durable=False) == "st_1"
    assert opened[0]["plugin"] == "tv" and opened[0]["types"] == ["status"]


def test_load_run(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.py").write_text("def f(ctx, a):\n    return a * 2\n")
    assert load_run(tmp_path, "scripts/x.py:f")(None, 3) == 6
    (tmp_path / "scripts" / "x.py").write_text("def f(ctx, a):\n    return a * 3\n")
    assert load_run(tmp_path, "scripts/x.py:f")(None, 3) == 9   # reload sees new code
    mods = {}
    f = load_run(tmp_path, "scripts/x.py:f", mods)
    assert load_run(tmp_path, "scripts/x.py:f", mods) is f       # one load shares the module
    for bad in ("scripts/x.py", "scripts/x.py:g", "../x.py:f", "scripts/none.py:f"):
        with pytest.raises(KitError):
            load_run(tmp_path, bad)
