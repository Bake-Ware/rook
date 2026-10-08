"""Work v2: live PTY terminals (worker), hub fan-out, worklog web ops."""
import asyncio
import inspect
import json
import os
import stat
import struct
import time

import pytest
import pytest_asyncio
from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

from test_band_management import portal  # noqa: F401 — fixture
from rook.worker import termwire
from rook.worker.audit import _summarize_args
from rook.worker.plugins import terminals
from rook.worker.plugins.claude_history import ClaudeHistoryPlugin
from rook.worker.plugins.terminals import TerminalsPlugin, build_argv
from rook.remote import term_hub
from rook.remote.term_hub import TermHub, Viewer

pytestmark = pytest.mark.skipif(os.name != "posix", reason="PTYs are POSIX-only")


async def until(test, timeout=8.0):
    async with asyncio.timeout(timeout):
        while not test():
            await asyncio.sleep(0.01)


# -- wire framing ---------------------------------------------------------------

def test_termwire_picks_the_smallest_encoding_and_round_trips():
    ascii_ = b"hello world\r\n"
    assert termwire.encode(ascii_)[0] == "t"
    redraw = (b"\x1b[38;5;214m\xe2\x96\x88\x1b[0m" * 400)
    enc, data = termwire.encode(redraw)
    assert enc == "z" and len(data) < len(redraw) / 4
    binary = bytes(range(256)) * 2
    enc, data = termwire.encode(binary, ("t", "b"))
    assert enc == "b"
    for raw in (ascii_, redraw, binary, "naïve ✓".encode()):
        assert termwire.decode(*termwire.encode(raw)) == raw
    # A split multi-byte character is not valid text on its own: never 't'.
    assert termwire.encode("✓".encode()[:2])[0] == "b"


def test_termwire_refuses_a_decompression_bomb():
    enc, data = termwire.encode(b"\0" * 200_000)
    assert enc == "z"
    with pytest.raises(ValueError):
        termwire.decode(enc, data, limit=1000)


def test_audit_redacts_credential_args():
    out = _summarize_args({"mcp_token": "secret-value", "cwd": "/tmp", "password": "x", "harness": "claude"})
    assert out["mcp_token"] == "<redacted>" and out["password"] == "<redacted>"
    assert out["cwd"] == "/tmp" and out["harness"] == "claude"


# -- launch templates ------------------------------------------------------------

def test_launch_templates_keep_the_token_off_the_command_line():
    argv = build_argv("claude", "/bin/claude", model="opus", resume="abc", mcp_config="/tmp/c.json")
    assert argv == ["/bin/claude", "--resume", "abc", "--model", "opus", "--mcp-config", "/tmp/c.json"]
    argv = build_argv("codex", "/bin/codex", model="gpt", resume="r1", mcp_url="https://hub.example.com/mcp")
    assert argv[:5] == ["/bin/codex", "resume", "r1", "-m", "gpt"]
    assert 'mcp_servers.rook.url="https://hub.example.com/mcp"' in argv
    assert 'mcp_servers.rook.bearer_token_env_var="ROOK_MCP_TOKEN"' in argv
    assert build_argv("shell", "/bin/bash") == ["/bin/bash", "-l"]
    assert build_argv("hermes", "/bin/hermes", model="m") == ["/bin/hermes", "--model", "m"]


# -- worker PTY plugin -----------------------------------------------------------

async def follow(p, tid, until_text, cursor=0, timeout=10):
    out = b""
    async with asyncio.timeout(timeout):
        while until_text.encode() not in out:
            r = await p.read(tid, cursor, wait=2)
            out += termwire.decode(r["enc"], r["data"])
            cursor = r["next"]
            if r["eof"]:
                break
    return out, cursor


@pytest_asyncio.fixture
async def plugin(monkeypatch, tmp_path):
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("ROOK_WORK_TERM_DIR", str(tmp_path / "terms"))
    p = TerminalsPlugin()
    yield p
    await p.stop()


@pytest.mark.asyncio
async def test_pty_round_trip_resize_signal_and_close(plugin, tmp_path):
    r = await plugin.open(harness="shell", cwd=str(tmp_path), cols=80, rows=24)
    tid = r["id"]
    await plugin.write(tid, "stty size; tty; echo ready-$((6*7))\r")
    out, cur = await follow(plugin, tid, "ready-42")
    assert b"24 80" in out and b"/dev/" in out
    plugin.resize(tid, 100, 30)
    # Ctrl-C reaches the foreground job because the PTY is the controlling tty.
    await plugin.write(tid, "sleep 30\r")
    await asyncio.sleep(0.3)
    await plugin.write(tid, "\x03")
    await plugin.write(tid, "stty size\r")
    out, cur = await follow(plugin, tid, "30 100", cur)
    listing = plugin.list_terms()
    assert [t["id"] for t in listing["terminals"]] == [tid] and "shell" in listing["harnesses"]
    closed = await plugin.close(tid)
    assert closed["ok"] and tid not in plugin.terms


@pytest.mark.asyncio
async def test_long_poll_returns_promptly_and_times_out(plugin, tmp_path):
    tid = (await plugin.open(harness="shell", cwd=str(tmp_path)))["id"]
    _, cur = await follow(plugin, tid, "$")
    t0 = time.monotonic()
    r = await plugin.read(tid, cur, wait=0.4)
    assert r["data"] == "" and 0.35 < time.monotonic() - t0 < 2
    async def later():
        await asyncio.sleep(0.2)
        await plugin.write(tid, "echo woke\r")
    task = asyncio.create_task(later())
    t0 = time.monotonic()
    r = await plugin.read(tid, cur, wait=10)
    assert time.monotonic() - t0 < 3 and r["next"] > cur
    await task


@pytest.mark.asyncio
async def test_exit_is_reported_and_ring_is_bounded(plugin, tmp_path):
    tid = (await plugin.open(harness="shell", cwd=str(tmp_path), buffer_bytes=16384))["id"]
    await plugin.write(tid, "i=0; while [ $i -lt 3000 ]; do echo line-$i-padding-padding; i=$((i+1)); done; exit 3\r")
    t = plugin.terms[tid]
    await until(lambda: not t.running, 20)
    assert t.exit_code == 3 and len(t.buf) <= 16384
    r = await plugin.read(tid, 0)
    assert r["dropped"] > 0 and r["cursor"] == t.buf_start
    last = await plugin.read(tid, t.total)
    assert last["eof"] and last["exit_code"] == 3


@pytest.mark.asyncio
async def test_mcp_token_injection_and_cleanup(plugin, tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho "ARGS $*"\necho "TOKEN $ROOK_MCP_TOKEN SESSION $ROOK_WORK_SESSION"\n'
                    'for a; do [ -f "$a" ] && cat "$a"; done\necho; sleep 5\n')
    fake.chmod(0o755)
    monkeypatch.setattr(terminals, "_binary", lambda h: str(fake))
    r = await plugin.open(harness="claude", cwd=str(tmp_path), model="m1", session="s" * 32,
                          mcp_url="https://hub.example.com/mcp", mcp_token="tok-123")
    tid = r["id"]
    out, _ = await follow(plugin, tid, "mcpServers")
    text = out.decode()
    assert "--model m1" in text and "--mcp-config" in text and "tok-123" not in text.split("ARGS")[1].split("\n")[0]
    assert "TOKEN tok-123 SESSION " + "s" * 32 in text
    cfg = plugin.terms[tid].files[0]
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600
    await plugin.close(tid)
    assert not os.path.exists(cfg)


@pytest.mark.asyncio
async def test_open_validates_inputs(plugin, tmp_path):
    for kwargs in ({"harness": "vim"}, {"cwd": "/nonexistent/dir"}, {"cwd": "relative"},
                   {"harness": "shell", "resume": "abc"}, {"session": "bad id!"}):
        with pytest.raises(ValueError):
            await plugin.open(**{"cwd": str(tmp_path), **kwargs})
    with pytest.raises(ValueError):
        await plugin.read("missing", 0)


def test_heartbeat_advertises_harnesses(monkeypatch):
    p = TerminalsPlugin()
    monkeypatch.setattr(terminals, "available_harnesses", lambda: ["shell", "codex"])
    # commands: work.stream.open takes argv/cmd (console rooms run on it).
    assert p.heartbeat() == {"harnesses": ["shell", "codex"], "commands": 1}


def test_transcript_export_format(tmp_path):
    root = tmp_path / "projects" / "-home-user-app"
    root.mkdir(parents=True)
    sid = "8f3a1b2c-0000-4000-8000-000000000009"
    with open(root / f"{sid}.jsonl", "w") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-08-30T10:00:00Z", "cwd": "/srv/app",
                            "message": {"role": "user", "content": "add a health check"}}) + "\n")
        f.write(json.dumps({"type": "assistant", "timestamp": "2026-08-30T10:00:05Z",
                            "message": {"role": "assistant", "content": [{"type": "text", "text": "x" * 800}]}}) + "\n")
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "done"}}) + "\n")
    p = ClaudeHistoryPlugin()
    page = p._transcript(sid, max_chars=500, path=str(tmp_path / "projects"))
    assert page["format"] == "rook.transcript/1"
    assert page["session"]["agent"] == "claude" and page["session"]["cwd"] == "/srv/app"
    assert [m["role"] for m in page["messages"]] == ["user"]
    assert page["messages"][0] == {"index": 0, "role": "user", "ts": "2026-08-30T10:00:00Z", "text": "add a health check"}
    nxt = p._transcript(sid, offset=page["next_offset"], max_chars=500, path=str(tmp_path / "projects"))
    assert nxt["messages"][0]["index"] == 1 and len(nxt["messages"][0]["text"]) == 800 and "session" not in nxt
    rest = p._transcript(sid, offset=nxt["next_offset"], path=str(tmp_path / "projects"))
    assert rest["messages"][-1]["text"] == "done" and rest["next_offset"] is None


@pytest.mark.asyncio
async def test_sessions_catalog_merges_live_and_history(plugin, tmp_path):
    class Reg:
        def has(self, cap):
            return cap in ("claude-history.pull", "codex-history.pull")
        async def call(self, cap, **kw):
            agent = cap.split("-")[0]
            return {"ok": True, "total": 1, "sessions": [{"session_id": agent + "-1", "title": agent + " work",
                    "cwd": "/srv", "last_modified": 100 if agent == "claude" else 200, "active": agent == "codex"}]}
    plugin.bind_worker(type("W", (), {"registry": Reg()})())
    await plugin.open(harness="shell", cwd=str(tmp_path))
    out = await plugin.sessions(limit=10)
    assert len(out["live"]) == 1
    assert [i["session_id"] for i in out["items"]] == ["codex-1", "claude-1"]
    assert out["items"][0]["resumable"] is False and out["items"][1]["resumable"] is True
    assert (await plugin.sessions(query="claude"))["total"] == 1


# -- hub fan-out -------------------------------------------------------------------

class PtyBand:
    """A band whose one worker runs a real TerminalsPlugin in-process."""

    def __init__(self, plugin, caps=None, name="test-host"):
        self.plugin = plugin
        self.calls = []
        self.fail = set()
        own = list(plugin.caps())
        self.workers = {"host1": {"worker_id": "host1", "name": name, "band": "test",
                                  "last_seen": time.time(), "caps": own if caps is None else caps,
                                  "hb": {"work": {"harnesses": ["shell"]}}}}

    async def call(self, cap, args, target, timeout, identity=None):
        self.calls.append((cap, dict(args)))
        if cap in self.fail:
            raise asyncio.TimeoutError()
        fn = self.plugin.caps().get(cap)
        if fn is None:
            return {"ok": False, "from": target, "error": f"unknown capability: {cap}"}
        try:
            result = fn(**args)
            if inspect.isawaitable(result):
                result = await result
        except ValueError as e:
            return {"ok": False, "from": target, "error": str(e)}
        return {"ok": True, "from": target, "result": result}


async def drain(viewer, want: bytes, timeout=10):
    """Collect a viewer's output until ``want`` shows up; returns (bytes, json msgs)."""
    out, msgs = b"", []
    async with asyncio.timeout(timeout):
        while want not in out:
            kind, item = await viewer.next()
            if kind == "frame":
                out += item[8:]
            elif kind == "json":
                msgs.append(item)
                if item.get("type") == "reset":
                    out = b""
    return out, msgs


@pytest.mark.asyncio
async def test_hub_fans_out_with_one_input_holder(plugin, tmp_path):
    band = PtyBand(plugin)
    ended = []
    hub = TermHub(lambda: band, on_end=ended.append)
    tid = (await plugin.open(harness="shell", cwd=str(tmp_path)))["id"]
    try:
        s = hub.stream("host1", tid)
        a, b = Viewer("alice"), Viewer("bob")
        s.attach(a)
        s.attach(b)
        s.control(a, {"op": "input", "data": "echo one-$((1+1))\r"})
        assert s.holder == a.id
        for v in (a, b):
            await drain(v, b"one-2")
        with pytest.raises(ValueError):
            s.control(b, {"op": "input", "data": "echo nope\r"})
        s.control(b, {"op": "resize", "cols": 90, "rows": 20})   # ignored: not holder
        s.control(a, {"op": "handoff", "to": b.id})
        assert s.holder == b.id
        s.control(b, {"op": "resize", "cols": 90, "rows": 20})
        await until(lambda: plugin.terms[tid].cols == 90)
        s.control(b, {"op": "input", "data": "echo two-$((2+2))\r"})
        await drain(a, b"two-4")
        s.control(a, {"op": "take"})
        assert s.holder == a.id
        s.detach(a)
        assert s.holder is None
        # A late viewer gets the ring replayed; a reconnecting one only the tail.
        c = Viewer("carol")
        s.attach(c)
        out, msgs = await drain(c, b"two-4")
        assert b"one-2" in out and msgs[1]["type"] == "reset"
        d = Viewer("dave")
        s.attach(d, since=s.end)
        s.control(b, {"op": "input", "data": "exit\r"})
        await until(lambda: ended)
        assert ended[0] is s and not s.running and s.exit_code == 0
        kinds = []
        while not d.queue.empty():
            kinds.append((await d.next())[0])
        assert "json" in kinds
        writes = [c for c in band.calls if c[0] == "work.stream.write"]
        assert writes and all(c[1]["enc"] in ("t", "b") for c in writes)
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_slow_viewer_is_resynced_not_buffered_forever(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(term_hub, "VIEWER_MAX", 4096)
    band = PtyBand(plugin)
    hub = TermHub(lambda: band)
    tid = (await plugin.open(harness="shell", cwd=str(tmp_path)))["id"]
    try:
        s = hub.stream("host1", tid)
        slow = Viewer("slow")
        s.attach(slow)
        s.control(slow, {"op": "input", "data": "i=0; while [ $i -lt 800 ]; do echo row-$i-xxxxxxxxxxxx; i=$((i+1)); done; echo END\r"})
        await until(lambda: slow.resync, 15)
        assert slow.queued <= 4096 + term_hub.READ_BYTES
        assert hub.memory() < term_hub.RING_BYTES + 3 * 4096 + term_hub.READ_BYTES
    finally:
        await hub.stop()


@pytest.mark.asyncio
async def test_lost_worker_ends_the_stream(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr(term_hub, "MAX_MISSES", 2)
    band = PtyBand(plugin)
    ended = []
    hub = TermHub(lambda: band, on_end=ended.append)
    tid = (await plugin.open(harness="shell", cwd=str(tmp_path)))["id"]
    try:
        s = hub.stream("host1", tid)
        v = Viewer("v")
        s.attach(v)
        await until(lambda: s.primed)
        band.fail.add("work.stream.read")
        await until(lambda: ended, 15)
        assert "stopped answering" in s.lost
    finally:
        await hub.stop()


# -- worklog web -------------------------------------------------------------------

@pytest_asyncio.fixture
async def web_band(portal, plugin):  # noqa: F811
    band = PtyBand(plugin)
    portal.server._band = band
    yield band


async def index(ws, test=lambda m: True, timeout=8):
    async with asyncio.timeout(timeout):
        while True:
            m = await ws.receive_json()
            if m["type"] == "index" and test(m):
                return m


@pytest.mark.asyncio
async def test_launch_stream_and_end_through_the_web(portal, web_band, tmp_path, monkeypatch):  # noqa: F811
    p = portal
    work = p.account.work_web
    minted = []

    async def fake_token(request, user, payload):
        minted.append(payload)
        if payload["op"] == "create":
            return {"id": "tokid", "token": "secret-token", "name": payload["name"]}
        return {"ok": True}
    monkeypatch.setattr(work, "token_call", fake_token)
    async with TestClient(TestServer(p.app)) as client:
        r = await client.get("/account/work/bootstrap", headers=p.headers)
        assert (await r.json())["v2"] is True
        r = await client.get("/account/work/assets/vendor/xterm.mjs", headers=p.headers)
        assert r.status == 200 and "Terminal" in await r.text()
        ws = await client.ws_connect("/account/work/ws", headers=p.headers)
        m = await index(ws)
        assert m["hosts"][0]["term"] and m["hosts"][0]["harnesses"] == ["shell"]
        await ws.send_json({"op": "launch", "id": "launch-test-123", "csrf": p.csrf, "worker": "host1",
                            "harness": "shell", "cwd": str(tmp_path), "mcp": True})
        m = await index(ws, lambda m: any(s.get("term_running") for s in m["sessions"]))
        s = next(s for s in m["sessions"] if s.get("term_running"))
        sid = s["id"]
        assert s["harness"] == "shell" and s["term"]
        assert minted[0]["scopes"] == ["rook", "work-session:" + sid]
        opened = next(c for c in web_band.calls if c[0] == "work.stream.open")[1]
        assert opened["mcp_token"] == "secret-token" and opened["session"] == sid
        stored = json.dumps(work.store.get(sid))
        assert "secret-token" not in stored   # the secret is never persisted by the web
        # Terminal socket: binary frames carry output, JSON carries control.
        with pytest.raises(WSServerHandshakeError) as refused:
            await client.ws_connect(f"/account/work/term/{sid}", headers={**p.headers, "Origin": "https://evil.example"})
        assert refused.value.status == 403
        term = await client.ws_connect(f"/account/work/term/{sid}", headers=p.headers)
        hello = await term.receive_json()
        assert hello["type"] == "hello" and hello["running"]
        await term.send_json({"op": "input", "data": "echo web-$((3*3))\r"})   # no csrf
        assert "expired" in (await next_json(term, "error"))["error"]
        await term.send_json({"op": "input", "data": "echo web-$((3*3))\r", "csrf": p.csrf})
        out, cursor = b"", 0
        async with asyncio.timeout(10):
            while b"web-9" not in out:
                msg = await term.receive()
                if msg.type == WSMsgType.BINARY:
                    start = struct.unpack(">Q", msg.data[:8])[0]
                    assert start == cursor or not out
                    out += msg.data[8:]
                    cursor = start + len(msg.data) - 8
        await term.close()
        # Reconnect with a cursor: only new output, no reset.
        term = await client.ws_connect(f"/account/work/term/{sid}?since={cursor}", headers=p.headers)
        first = [await term.receive_json(), await term.receive_json()]
        assert all(x["type"] != "reset" for x in first)
        await term.close()
        await ws.send_json({"op": "term_close", "id": "close-test-1234", "session": sid, "csrf": p.csrf})
        await until(lambda: not work.store.get(sid).get("term_running"))
        m = await index(ws, lambda m: not any(x.get("revoke") for x in m["sessions"]) and minted[-1]["op"] == "revoke")
        assert minted[-1] == {"op": "revoke", "id": "tokid", "confirm": True}
        assert work.store.get(sid)["status"] == "closed" and not plugin_terms(web_band)
        await ws.close()


async def next_json(ws, kind, timeout=8):
    async with asyncio.timeout(timeout):
        while True:
            msg = await ws.receive()
            if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == kind:
                return json.loads(msg.data)


def plugin_terms(band):
    return [t for t in band.plugin.terms.values() if t.running]


@pytest.mark.asyncio
async def test_resume_uses_pty_when_supported_and_proc_otherwise(portal, plugin, tmp_path, monkeypatch):  # noqa: F811
    p = portal
    work = p.account.work_web
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho "RESUMED $*"\nsleep 5\n')
    fake.chmod(0o755)
    monkeypatch.setattr(terminals, "_binary", lambda h: str(fake))
    band = PtyBand(plugin)
    p.server._band = band
    sid = "a" * 32
    work.store.save(dict(id=sid, owner=p.uid, worker_id="host1", worker_name="test-host", band="test",
                         agent="claude", imported=True, source_id="8f3a1b2c-0000", cwd=str(tmp_path),
                         title="old work", status="pending", error=""))
    async with TestClient(TestServer(p.app)) as client:
        ws = await client.ws_connect("/account/work/ws", headers=p.headers)
        await ws.send_json({"op": "resume", "id": "resume-test-123", "session": sid, "csrf": p.csrf, "pty": True})
        await until(lambda: work.store.get(sid).get("term_running"))
        opened = next(c for c in band.calls if c[0] == "work.stream.open")[1]
        assert opened["resume"] == "8f3a1b2c-0000" and opened["harness"] == "claude" and "mcp_token" not in opened
        tid = work.store.get(sid)["term_id"]
        out, _ = await follow(plugin, tid, "RESUMED")
        assert b"--resume 8f3a1b2c-0000" in out
        await ws.close()
    # A build-167 worker (no work.stream.*) keeps today's proc.* resume path.
    sid2 = "b" * 32
    old = PtyBand(plugin, caps=["proc.start", "proc.read", "proc.write", "claude-history.resume"])

    async def old_call(cap, args, target, timeout, identity=None):
        old.calls.append((cap, dict(args)))
        assert cap == "claude-history.resume", cap
        return {"ok": True, "from": target, "result": {"ok": True, "handle": "h1", "note": "started"}}
    old.call = old_call
    p.server._band = old
    work.store.save(dict(id=sid2, owner=p.uid, worker_id="host1", worker_name="test-host", band="test",
                         agent="claude", imported=True, source_id="old-session", cwd=str(tmp_path),
                         title="older", status="pending", error=""))
    async with TestClient(TestServer(p.app)) as client:
        ws = await client.ws_connect("/account/work/ws", headers=p.headers)
        await ws.send_json({"op": "resume", "id": "resume-test-456", "session": sid2, "csrf": p.csrf, "pty": True})
        await until(lambda: work.store.get(sid2).get("external_handle") == "h1")
        assert [c[0] for c in old.calls] == ["claude-history.resume"]
        await ws.close()


@pytest.mark.asyncio
async def test_sweep_marks_terminals_that_ended_unwatched(portal, web_band, tmp_path):  # noqa: F811
    work = portal.account.work_web
    tid = (await web_band.plugin.open(harness="shell", cwd=str(tmp_path)))["id"]
    sid = "c" * 32
    work.store.save(dict(id=sid, owner=portal.uid, worker_id="host1", worker_name="test-host", band="test",
                         agent="shell", harness="shell", term_id=tid, term_running=True, cwd=str(tmp_path),
                         title="t", status="working", error="", mcp_token_id="tok9"))
    await web_band.plugin.write(tid, "exit 7\r")
    await until(lambda: not web_band.plugin.terms[tid].running)
    await work.sweep_terminals()
    s = work.store.get(sid)
    assert not s["term_running"] and s["term_exit"] == 7 and s["mcp_token_revoke"]
    gone = "d" * 32
    work.store.save(dict(id=gone, owner=portal.uid, worker_id="host1", worker_name="test-host", band="test",
                         agent="shell", harness="shell", term_id="nosuchterm", term_running=True,
                         title="t", status="working", error=""))
    await work.sweep_terminals()
    assert "gone" in work.store.get(gone)["term_note"]


@pytest.mark.asyncio
async def test_v2_flag_off_keeps_the_classic_view(portal, web_band, monkeypatch):  # noqa: F811
    work = portal.account.work_web
    monkeypatch.setattr(work, "v2", False)
    async with TestClient(TestServer(portal.app)) as client:
        r = await client.get("/account/work/bootstrap", headers=portal.headers)
        assert (await r.json())["v2"] is False
        ws = await client.ws_connect("/account/work/ws", headers=portal.headers)
        m = await index(ws)
        assert m["hosts"] == []
        await ws.send_json({"op": "launch", "id": "launch-test-999", "csrf": portal.csrf, "worker": "host1",
                            "harness": "shell", "cwd": "/tmp"})
        async with asyncio.timeout(5):
            while True:
                m = await ws.receive_json()
                if m["type"] == "error":
                    break
        assert "disabled" in m["error"]
        await ws.close()


def test_token_route_accepts_only_work_session_scopes():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient as StarletteClient
    from rook.band_mcp.account_tokens import build_account_token_routes
    from rook.band_mcp.tokens import TokenStore

    class Accounts:
        def session(self, token):
            return {"admin": True, "csrf": "c"} if token == "ok" else None
    store = TokenStore()
    app = Starlette(routes=build_account_token_routes(store, accounts=Accounts()))
    c = StarletteClient(app)
    c.cookies.set("rook_account", "ok")
    sid = "e" * 32
    r = c.post("/tokens/account-api", json={"op": "create", "name": "work:shell:x", "ttl": 86400,
                                              "scopes": ["rook", "work-session:" + sid], "csrf": "c"})
    assert r.status_code == 200
    entry = next(iter(store._api_tokens.values()))
    assert entry["scopes"] == ["rook", "work-session:" + sid]
    for scopes in (["admin"], ["rook", "anything"], "rook"):
        r = c.post("/tokens/account-api", json={"op": "create", "name": "x", "ttl": 86400, "scopes": scopes, "csrf": "c"})
        assert r.status_code == 400
