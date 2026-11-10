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

from rook.worker import termwire
from rook.worker.audit import _summarize_args
from rook.worker.plugins import terminals
from rook.worker.plugins.claude_history import ClaudeHistoryPlugin
from rook.worker.plugins.terminals import TerminalsPlugin, build_argv

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
    # Wait for the login shell's prompt: "#" for root (CI containers), else "$".
    _, cur = await follow(plugin, tid, "#" if os.geteuid() == 0 else "$")
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


# -- worklog web -------------------------------------------------------------------


async def index(ws, test=lambda m: True, timeout=8):
    async with asyncio.timeout(timeout):
        while True:
            m = await ws.receive_json()
            if m["type"] == "index" and test(m):
                return m


async def next_json(ws, kind, timeout=8):
    async with asyncio.timeout(timeout):
        while True:
            msg = await ws.receive()
            if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == kind:
                return json.loads(msg.data)


def plugin_terms(band):
    return [t for t in band.plugin.terms.values() if t.running]


