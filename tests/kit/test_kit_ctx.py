"""rook.kit.ctx: the call context and the Python contract."""

import asyncio

import pytest

from rook.kit.ctx import Caller, Ctx, FileResult, KitError, allowed, invoke, load_run
from rook.streams import Streams

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


@pytest.mark.asyncio
async def test_thread_call_dispatches_on_the_host_loop():
    """Sync plugin code runs in a thread, but the dispatcher must run on the loop."""
    import threading
    loop_thread = threading.get_ident()
    seen = []

    def dispatch(cap, args, caller):
        seen.append(threading.get_ident())
        return "done"

    ctx = Ctx(MANIFEST, BAKE, dispatch=dispatch)
    assert await invoke(lambda ctx: ctx.call("notify.send"), ctx) == "done"
    assert seen == [loop_thread]


def test_call_without_a_loop():
    ctx = Ctx(MANIFEST, BAKE, dispatch=FakeDispatch())
    assert ctx.call("notify.send") == "ok"
    ctx = Ctx(MANIFEST, BAKE, dispatch=FakeDispatch(is_async=True))
    with pytest.raises(KitError, match="host loop"):
        ctx.call("notify.send")


@pytest.mark.asyncio
async def test_call_as_another_identity_on_a_target():
    seen = []

    async def dispatch(cap, args, caller, target=None):
        seen.append((cap, args, caller, target))
        return "ok"

    ctx = Ctx(MANIFEST, BAKE, dispatch=dispatch)
    job = Caller(person="pat", band="b1", why="a job")
    assert await ctx.call_as(job, "notify.send", {"text": "hi"}, target="phone") == "ok"
    assert await ctx.call_as(job, "notify.send") == "ok"
    assert seen[0] == ("notify.send", {"text": "hi"}, job, "phone")      # as given: no plugin stamped
    assert seen[1][3] is None
    with pytest.raises(KitError, match="uses"):
        ctx.call_as(job, "shell.exec")                                    # still held to uses


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
        ctx.stream()
    with pytest.raises(KitError, match="data dir"):
        ctx.data


STREAMING = {**MANIFEST, "caps": {
    "tail": {"doc": "Follow a log.", "risk": "read", "scope": "local", "run": "scripts/t.py:tail",
             "stream": {"types": ["bytes"], "input": True}},
    "chat": {"doc": "A kept chat.", "risk": "write", "scope": "local", "run": "scripts/t.py:chat",
             "stream": {"types": ["text"], "durable": True}},
    "ping": {"doc": "No stream.", "risk": "read", "scope": "local", "run": "scripts/t.py:ping"},
}}


@pytest.mark.asyncio
async def test_stream_opens_the_caps_declared_stream():
    streams = Streams()
    ctx = Ctx(STREAMING, BAKE, streams=streams, cap="tail")
    assert ctx.cap == "tv.tail"

    async def tail(ctx):
        handle = ctx.stream(title="syslog", attrs={"path": "/var/log/syslog"})
        handle.append("bytes", b"boot ok\n")
        return handle

    res = await invoke(tail, ctx)
    sid = res["stream"]
    assert list(res) == ["stream"]
    meta = streams.get(sid)
    assert (meta.kind, meta.title, meta.producer) == ("process", "syslog", "tv.tail")
    assert meta.spec.types == ("bytes",) and meta.spec.input and not meta.spec.durable
    # the band rides along: identities are per hub
    assert meta.participants == ["bake", "claude"] and meta.attrs == {"path": "/var/log/syslog", "band": "b1"}
    assert streams.attached(sid)                       # the code keeps writing after the reply
    events = (await streams.read(sid, 0))["events"]
    assert [e.type for e in events] == ["bytes"]


def test_stream_needs_a_declaration_and_a_durable_store():
    # A full cap name works the same as the relative one.
    with pytest.raises(KitError, match="tv.ping declares no stream"):
        Ctx(STREAMING, BAKE, streams=Streams(), cap="tv.ping").stream()
    with pytest.raises(KitError, match="declares no stream"):
        Ctx(STREAMING, BAKE, streams=Streams()).stream()      # a hook: no cap
    with pytest.raises(KitError, match="durable"):            # a worker has no durable store
        Ctx(STREAMING, BAKE, streams=Streams(), cap="chat").stream("room")


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


class People:
    def __init__(self):
        self.values = {"bake": {"tv": {"room": "den", "host": "ignored"}}, "kid": {"tv": {"room": "attic"}}}

    def owners(self):
        return ["bake"]

    def settings(self, person):
        return self.values.get(person, {})


PERSONAL = {**MANIFEST, "settings": {**MANIFEST["settings"], "room": {"where": "person", "default": "hall"}}}


def test_per_person_settings_and_owners():
    ctx = Ctx(PERSONAL, BAKE, settings={"port": 3000}, people=People())
    assert ctx.settings["room"] == "den"                 # the caller's own value
    assert ctx.settings["host"] == "10.0.0.5"            # a person can't override a non-person setting
    assert ctx.settings_for("kid")["room"] == "attic"
    assert ctx.settings_for("ann")["room"] == "hall" and ctx.settings_for("ann")["port"] == 3000
    assert ctx.owners() == ["bake"]
    bare = Ctx(PERSONAL, BAKE)
    assert bare.settings["room"] == "hall"
    with pytest.raises(KitError, match="per-person settings live on the hub"):
        bare.settings_for("kid")
    with pytest.raises(KitError, match="owners"):
        bare.owners()


def test_call_on_names_the_worker():
    got = []

    async def dispatch(cap, args, caller, target=None):
        got.append((cap, args, target, caller.plugin))
        return "shown"

    async def body():
        ctx = Ctx({**MANIFEST, "uses": ["notification.post"]}, BAKE, dispatch=dispatch)
        assert await ctx.call_on("pixel", "notification.post", text="hi") == "shown"
        assert await ctx.call("notification.post", text="yo") == "shown"
        with pytest.raises(KitError, match="not in its manifest's uses"):
            await ctx.call_on("pixel", "shell.exec")
        with pytest.raises(KitError, match="needs a worker"):
            ctx.call_on("", "notification.post")
        old = Ctx({**MANIFEST, "uses": ["notification.post"]}, BAKE, dispatch=lambda cap, args, caller: "x")
        with pytest.raises(KitError, match="can't call a named worker"):
            await old.call_on("pixel", "notification.post")
    asyncio.run(asyncio.wait_for(body(), 10))
    assert got == [("notification.post", {"text": "hi"}, "pixel", "tv"),
                   ("notification.post", {"text": "yo"}, None, "tv")]


def test_call_as_through_one_provider_of_a_contract():
    # A contract called through the provider the caller picks (an agent pinned
    # to one backend, a notify channel): held to uses by the contract's name;
    # the host gets the provider cap with the contract (castling-manifest-contracts).
    got = []

    async def dispatch(cap, args, caller, target=None, contract=None):
        got.append((cap, contract, target))
        return "sent"

    async def body():
        ctx = Ctx(MANIFEST, BAKE, dispatch=dispatch)
        assert await ctx.call_as(BAKE, "notify.send", {"text": "hi"}, provider="telegram.send") == "sent"
        with pytest.raises(KitError, match="uses"):
            ctx.call_as(BAKE, "telegram.send", {"text": "hi"}, provider="telegram.send")
        with pytest.raises(KitError, match="bad provider"):
            ctx.call_as(BAKE, "notify.send", {}, provider="notify.send")
        old = Ctx(MANIFEST, BAKE, dispatch=lambda cap, args, caller, target=None: "x")
        with pytest.raises(KitError, match="can't pick a contract's provider"):
            await old.call_as(BAKE, "notify.send", {}, provider="telegram.send")
    asyncio.run(asyncio.wait_for(body(), 10))
    assert got == [("telegram.send", "notify.send", None)]


def test_hub_views_of_contracts_and_agents():
    class Hub:
        def __init__(self):
            self.made, self.ended = [], []

        def settings(self, person):
            return {}

        def providers(self, contract):
            return ["telegram.send", "discord.send"] if contract == "notify.channel.send" else []

        def run_agent(self, plugin, name, grants):
            self.made.append((plugin, name, grants))

        def turn_token(self, plugin, person, name, seconds, tainted=False):
            return f"{plugin}:{person}:{name}:{seconds}:{tainted}"

        def end_turn_token(self, token):
            self.ended.append(token)

    hub = Hub()
    ctx = Ctx(MANIFEST, BAKE, people=hub)
    assert ctx.providers("notify.channel.send") == ["telegram.send", "discord.send"]
    ctx.run_agent("pm", ("*",))
    assert hub.made == [("tv", "pm", ["*"])]
    token = ctx.turn_token("pm", 60)
    assert token == "tv:bake:pm:60:False"                     # this call's person, never one the plugin picks
    ctx.end_turn_token(token)
    assert hub.ended == [token]
    bare = Ctx(MANIFEST, BAKE)
    for fn, args in ((bare.providers, ("x",)), (bare.run_agent, ("pm", [])), (bare.turn_token, ("pm", 1))):
        with pytest.raises(KitError, match="hub"):
            fn(*args)
