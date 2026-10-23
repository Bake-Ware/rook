"""The session host for persistent agents (rook/agents, design persistent-agents.md 3.1-3.4),
against a fake claude (tests/agents_fake_claude.py); the real one is never called here."""

from __future__ import annotations

import asyncio
import contextlib
import glob
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web

from rook.agents.config import Config
from rook.agents.host import Host
from rook.agents.link import OpError

FAKE = Path(__file__).with_name("agents_fake_claude.py")
VOICE_TOKEN = "voice-token-123"
VOICE_HASH = hashlib.sha256(VOICE_TOKEN.encode()).hexdigest()


@pytest.fixture
def fx(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    claude = bindir / "claude"
    claude.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    claude.chmod(0o755)
    cfgdir = tmp_path / "claude-config"
    cfgdir.mkdir()
    user_cfg = tmp_path / "claude.json"
    user_cfg.write_text(json.dumps({"mcpServers": {
        "rea": {"type": "stdio", "command": "/opt/rea/bin/rea-mcp", "args": ["--x"]},
        "other": {"type": "http", "url": "http://127.0.0.1:1/mcp"}}}))
    log = tmp_path / "claude.log"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_DIE", str(tmp_path / "die"))
    monkeypatch.setenv("ROOK_AGENTS_LINK_TOKEN", "link-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")
    from rook.agents import voice as voice_mod
    monkeypatch.setattr(voice_mod.VoiceRoom, "now", lambda self: "T")   # turn headers' time
    return SimpleNamespace(tmp=tmp_path, claude=str(claude), cfgdir=cfgdir, log=log,
                           user_cfg=user_cfg, roles=tmp_path / "roles", die=tmp_path / "die")


def config(fx, **over) -> Config:
    d = {"roles_dir": str(fx.roles), "claude": fx.claude, "claude_config_dir": str(fx.cfgdir),
         "claude_user_config": str(fx.user_cfg), "voice_listen": "", "host": "testhost",
         "restart_backoff_s": [0.1, 0.4], "link_backoff_s": [0.05, 0.2], "max_forks": 2,
         "btw_timeout_s": 20}
    d.update(over)
    return Config.from_dict(d)


@contextlib.asynccontextmanager
async def running(fx, token="", **over):
    host = Host(config(fx, **over), token)
    events: list[dict] = []
    host.listeners.append(events.append)
    await host.start()
    try:
        yield host, events
    finally:
        await host.close()


def starts(fx) -> list[dict]:
    if not fx.log.exists():
        return []
    return [json.loads(x) for x in fx.log.read_text().splitlines() if x.strip()]


async def wait_for(pred, timeout=5.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        v = pred()
        if v:
            return v
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def find(events, kind, **match):
    for e in events:
        if e.get("kind") == kind and all(e.get(k) == v for k, v in match.items()):
            return e
    return None


async def wait_ev(events, kind, timeout=5.0, **match):
    return await wait_for(lambda: find(events, kind, **match), timeout, f"ev {kind} {match}")


def main_spec(**over):
    spec = {"model": "opus", "effort": "high", "tools": ["Read", "Grep"],
            "env": {"ROOK_TURN_TOKEN": "turn-tok"}, "partial": False, "idle_sleep_s": 0,
            "thinking": False}
    spec.update(over)
    return spec


async def configure(host, role="house", files=None, lanes=None, **extra):
    rc = {"dir_files": files if files is not None else {"CLAUDE.md": "# persona\n"},
          "lanes": lanes if lanes is not None else {"main": main_spec()}, **extra}
    return await host.handle({"t": "configure", "roles": {role: rc}})


async def turn(host, events, text, item, role="house", lane="main", timeout=5.0):
    await host.handle({"t": "send", "role": role, "lane": lane, "item": item, "text": text})
    return await wait_ev(events, "result", timeout=timeout, item=item)


# -- configure ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_configure_writes_files_safely(fx):
    async with running(fx) as (host, events):
        files = {"CLAUDE.md": "# house\n", ".claude/settings.json": "{}\n",
                 ".mcp.json": json.dumps({"mcpServers": {"rook": {"type": "http",
                                                                   "url": "https://hub/x"}}}),
                 "voice/CLAUDE.md": "# voice\n"}
        res = await configure(host, files=files, lanes={}, host_servers=["rea", "nope"],
                              memory="write")
        r = res["roles"]["house"]
        assert sorted(r["changed"]) == [".claude/settings.json", ".mcp.json", "CLAUDE.md",
                                        "voice/CLAUDE.md"]
        assert r["missing_servers"] == ["nope"]
        home = fx.roles / "house"
        assert (home.stat().st_mode & 0o777) == 0o700
        for d in ("memory", "workspace", ".rook", "voice"):
            assert (home / d).is_dir()
        mcp = json.loads((home / ".mcp.json").read_text())["mcpServers"]
        assert mcp["rook"]["url"] == "https://hub/x"
        assert mcp["rea"]["command"] == "/opt/rea/bin/rea-mcp"
        assert "other" not in mcp
        assert mcp["memory"]["args"][-2:] == ["rook.worker.rolemem", str(home)]
        assert "--read-only" not in mcp["memory"]["args"]
        assert host.status()["missing_servers"] == {"house": ["nope"]}

        # Same again: nothing changes. Memory read-only: only .mcp.json.
        again = await configure(host, files=files, lanes={}, host_servers=["rea", "nope"],
                                memory="write")
        assert again["roles"]["house"]["changed"] == []
        ro = await configure(host, files={}, lanes={}, host_servers=["rea"], memory="read")
        assert ro["roles"]["house"]["changed"] == [".mcp.json"]
        mcp = json.loads((home / ".mcp.json").read_text())["mcpServers"]
        assert "--read-only" in mcp["memory"]["args"] and "rook" in mcp

        outside = fx.tmp / "outside"
        outside.mkdir()
        (home / "link").symlink_to(outside)
        for bad in ("../escape.md", "/etc/passwd", "a/../../escape.md", ".rook/sessions.json",
                    "forks/x/CLAUDE.md", "link/evil.md", "~/x", "a\\b"):
            with pytest.raises(OpError):
                await configure(host, files={"CLAUDE.md": "changed\n", bad: "x"}, lanes={})
        # A refused batch writes nothing.
        assert (home / "CLAUDE.md").read_text() == "# house\n"
        assert not list(outside.iterdir())
        assert not (fx.roles / "escape.md").exists()
        with pytest.raises(OpError):
            await host.handle({"t": "configure", "roles": {"Bad Role": {}}})
        with pytest.raises(OpError):
            await configure(host, lanes={"main": main_spec(env={"LD_PRELOAD": "x"})})
        assert starts(fx) == []


# -- lanes -------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_result_argv_env_and_busy(fx):
    async with running(fx) as (host, events):
        res = await configure(host)
        assert res["roles"]["house"]["lanes"] == {"main": "started"}
        await wait_ev(events, "state", state="idle")
        r = await turn(host, events, "slowtool 0.3", "q1")
        assert r["ok"] and r["text"] == "done waiting"
        assert r["thinking_tokens"] == 0 and r["context_tokens"] > 0 and r["num_turns"] == 1
        assert find(events, "turn_start", item="q1")
        assert find(events, "tool", item="q1")["name"] == "wait"     # mcp__rook__ stripped
        assert find(events, "text", item="q1")["text"] == "done waiting"
        sess = find(events, "session")
        assert sess["resumed"] is False and "reset_reason" not in sess
        rec = json.loads((fx.roles / "house/.rook/sessions.json").read_text())["main"]
        assert rec["id"] == sess["session_id"] and rec["resumes"] == 0

        st = starts(fx)[0]
        argv = st["argv"]
        home = str(fx.roles / "house")
        for a, b in (("--input-format", "stream-json"), ("--output-format", "stream-json"),
                     ("--setting-sources", "project,local"),
                     ("--settings", f"{home}/.claude/settings.json"),
                     ("--mcp-config", f"{home}/.mcp.json"), ("--permission-mode", "dontAsk"),
                     ("--tools", "Read,Grep"), ("--model", "opus"), ("--effort", "high")):
            assert argv[argv.index(a) + 1] == b
        assert "--strict-mcp-config" in argv and "--verbose" in argv
        assert "--include-partial-messages" not in argv and "--resume" not in argv
        assert st["cwd"] == home
        assert st["env"] == {"MAX_THINKING_TOKENS": "0", "DISABLE_AUTOUPDATER": "1",
                             "ROOK_TURN_TOKEN": "turn-tok", "has_link_token": False,
                             "has_api_key": False}

        await host.handle({"t": "send", "role": "house", "lane": "main", "item": "q2",
                           "text": "slowtool 0.5"})
        with pytest.raises(OpError, match="busy"):
            await host.handle({"t": "send", "role": "house", "lane": "main", "item": "q3",
                               "text": "hi"})
        await wait_ev(events, "result", item="q2")
        assert not find(events, "result", item="q3")
        status = await host.handle({"t": "status", "req": 1})
        assert status["lanes"]["house/main"]["state"] == "idle"
        assert status["lanes"]["house/main"]["session_id"] == sess["session_id"]


@pytest.mark.asyncio
async def test_resume_by_id_and_reset_when_transcript_missing(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "hello", "q1")
        sid = find(events, "session")["session_id"]
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "again", "q2")
        sess = find(events, "session")
        assert sess == {**sess, "session_id": sid, "resumed": True}
        assert starts(fx)[-1]["argv"][starts(fx)[-1]["argv"].index("--resume") + 1] == sid
        assert json.loads((fx.roles / "house/.rook/sessions.json").read_text())["main"][
            "resumes"] == 1
    for p in glob.glob(str(fx.cfgdir / "projects" / "*" / f"{sid}.jsonl")):
        os.unlink(p)
    async with running(fx) as (host, events):
        n = len(starts(fx))
        await configure(host)
        await wait_for(lambda: len(starts(fx)) > n, 5, "a start")
        assert "--resume" not in starts(fx)[-1]["argv"]
        await turn(host, events, "fresh", "q3")
        sess = find(events, "session")
        assert sess["resumed"] is False and sess["session_id"] != sid
        assert sid in sess["reset_reason"] and sess["previous_session_id"] == sid
        rec = json.loads((fx.roles / "house/.rook/sessions.json").read_text())["main"]
        assert rec["id"] == sess["session_id"] and rec["resets"] == 1
        # The hub's record is used when the host has none; missing: reset, said so.
        await configure(host, role="scout", lanes={"main": main_spec(session_id="gone-1234")})
        await turn(host, events, "hi", "s1", role="scout")
        s2 = find(events, "session", role="scout")
        assert s2["resumed"] is False and "gone-1234" in s2["reset_reason"]


@pytest.mark.asyncio
async def test_crash_restarts_with_backoff_resumed(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "hello", "q1")
        sid = find(events, "session")["session_id"]
        n0 = len(starts(fx))
        fx.die.write_text("1")              # restarts fail until removed
        r = await turn(host, events, "crash now", "q2")
        assert r["ok"] is False and r["error"] == "the session restarted"
        crashed = await wait_ev(events, "state", state="crashed")
        assert "fake: boom" in crashed["error"] and "must-not-pass" not in json.dumps(events)
        await wait_for(lambda: len(starts(fx)) >= n0 + 3, 5, "three restart attempts")
        times = [s["t"] for s in starts(fx)[n0:n0 + 3]]
        gap1, gap2 = times[1] - times[0], times[2] - times[1]
        assert gap2 > gap1 * 1.4, (gap1, gap2)       # 0.2 then 0.4 (doubling)
        fx.die.unlink()
        t_ok = time.time()
        lane = host.roles["house"].lanes["main"]
        await wait_for(lambda: any(s["t"] > t_ok for s in starts(fx)) and lane.state == "idle"
                       and lane.running, 5, "recovered")
        argv = starts(fx)[-1]["argv"]
        assert argv[argv.index("--resume") + 1] == sid
        r = await turn(host, events, "after", "q3")
        assert r["ok"]


@pytest.mark.asyncio
async def test_steer_is_folded_into_the_running_turn(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await host.handle({"t": "send", "role": "house", "lane": "main", "item": "q1",
                           "text": "slowtool 0.6"})
        await wait_ev(events, "tool", item="q1")
        res = await host.handle({"t": "steer", "role": "house", "lane": "main",
                                 "text": "also check the garage"})
        assert res == {"steered": True, "item": "q1"}
        r = await wait_ev(events, "result", item="q1")
        assert r["text"] == "done waiting; folded: also check the garage"
        await asyncio.sleep(0.3)
        assert len([e for e in events if e["kind"] == "result"]) == 1
        # Idle: a steer starts its own turn.
        res = await host.handle({"t": "steer", "role": "house", "lane": "main", "text": "hey"})
        assert res["item"] == "steer"
        r = await wait_ev(events, "result", item="steer")
        assert r["text"] == "echo: hey"


@pytest.mark.asyncio
async def test_unowned_result_is_reported_as_steer(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "hello", "q1")
        lane = host.roles["house"].lanes["main"]
        # A message the lane did not start (a steer that missed the last turn).
        await lane._write_user("late steer")
        r = await wait_ev(events, "result", item="steer")
        assert r["text"] == "echo: late steer"
        assert find(events, "turn_start", item="steer")


@pytest.mark.asyncio
async def test_interrupt_ends_the_turn_and_the_session_goes_on(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await host.handle({"t": "send", "role": "house", "lane": "main", "item": "q1",
                           "text": "slowtool 10"})
        await wait_ev(events, "tool", item="q1")
        t0 = time.monotonic()
        assert await host.handle({"t": "interrupt", "role": "house", "lane": "main"}) == {
            "interrupted": True}
        r = await wait_ev(events, "result", item="q1")
        assert time.monotonic() - t0 < 2
        assert r["ok"] is False and r["error"] == "interrupted"
        r2 = await turn(host, events, "next", "q2")
        assert r2["ok"] and r2["text"] == "echo: next"
        assert await host.handle({"t": "interrupt", "role": "house", "lane": "main"}) == {
            "interrupted": False}


@pytest.mark.asyncio
async def test_compact_reports_compacted_not_a_result(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "hello", "q1")
        await turn(host, events, "hello again", "q2")
        before = host.roles["house"].lanes["main"].context_tokens
        await host.handle({"t": "compact", "role": "house", "lane": "main",
                           "instructions": "keep the decisions"})
        c = await wait_ev(events, "compacted")
        assert c["pre_tokens"] > c["post_tokens"]
        lane = host.roles["house"].lanes["main"]
        await wait_for(lambda: lane.state == "idle" and lane.turn is None, 3, "idle")
        assert lane.context_tokens == c["post_tokens"] < before
        assert [e["item"] for e in events if e["kind"] == "result"] == ["q1", "q2"]
        r = await turn(host, events, "after", "q3")
        assert r["ok"]


@pytest.mark.asyncio
async def test_idle_sleep_and_wake_on_send(fx):
    async with running(fx) as (host, events):
        await configure(host, lanes={"main": main_spec(idle_sleep_s=0.3)})
        await turn(host, events, "hello", "q1")
        sid = find(events, "session")["session_id"]
        await wait_ev(events, "state", state="sleeping", timeout=3)
        lane = host.roles["house"].lanes["main"]
        assert not lane.running
        n = len(starts(fx))
        r = await turn(host, events, "wake up", "q2")
        assert r["ok"] and len(starts(fx)) == n + 1
        argv = starts(fx)[-1]["argv"]
        assert argv[argv.index("--resume") + 1] == sid


@pytest.mark.asyncio
async def test_reload_restarts_at_idle_on_changed_files(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "hello", "q1")
        await host.handle({"t": "send", "role": "house", "lane": "main", "item": "q2",
                           "text": "slowtool 0.4"})
        n = len(starts(fx))
        res = await configure(host, files={"CLAUDE.md": "# new persona\n"})
        assert res["roles"]["house"]["lanes"]["main"] == "reload at next idle"
        assert len(starts(fx)) == n
        await wait_ev(events, "result", item="q2")
        await wait_for(lambda: len(starts(fx)) == n + 1, 5, "restart")
        assert "--resume" in starts(fx)[-1]["argv"]
        same = await configure(host, files={"CLAUDE.md": "# new persona\n"})
        assert same["roles"]["house"]["lanes"]["main"] == "unchanged"


# -- side threads ------------------------------------------------------------------

def transcript(fx, sid):
    path = glob.glob(str(fx.cfgdir / "projects" / "*" / f"{sid}.jsonl"))[0]
    return [json.loads(x) for x in Path(path).read_text().splitlines()]


@pytest.mark.asyncio
async def test_fork_from_last_completed_turn(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "hello", "q1")
        sid = find(events, "session")["session_id"]
        last_uuid = [m for m in transcript(fx, sid) if m["type"] == "assistant"][-1]["uuid"]
        await host.handle({"t": "send", "role": "house", "lane": "main", "item": "q2",
                           "text": "slowtool 3"})
        await wait_ev(events, "tool", item="q2")
        res = await host.handle({"t": "fork", "fork": "f1", "role": "house",
                                 "text": "look into the garage door",
                                 "env": {"ROOK_TURN_TOKEN": "fork-tok"}, "model": "sonnet",
                                 "at": "last_completed"})
        assert res["resume_session_at"] == last_uuid
        done = await wait_ev(events, "fork", fork="f1", state="done")
        started = find(events, "fork", fork="f1", state="started")
        assert started["session_id"] and started["session_id"] != sid
        assert done["text"] == "fork did: look into the garage door after 2 messages"
        assert done["duration_ms"] >= 0 and "usage" in done
        st = [s for s in starts(fx) if "--fork-session" in s["argv"]][0]
        a = st["argv"]
        assert a[a.index("--resume") + 1] == sid
        assert a[a.index("--resume-session-at") + 1] == last_uuid
        assert a[a.index("--model") + 1] == "sonnet" and a[a.index("--tools") + 1] == "Read,Grep"
        assert a[a.index("--mcp-config") + 1] == str(fx.roles / "house/.mcp.json")
        assert st["cwd"] == str(fx.roles / "house/forks/f1")
        assert st["env"]["ROOK_TURN_TOKEN"] == "fork-tok" and not st["env"]["has_link_token"]
        await host.handle({"t": "interrupt", "role": "house", "lane": "main"})
        await wait_ev(events, "result", item="q2")
        # Main idle, at "now": no cut.
        res = await host.handle({"t": "fork", "fork": "f2", "role": "house", "text": "x",
                                 "env": {}, "at": "last_completed"})
        assert res["resume_session_at"] is None
        await wait_ev(events, "fork", fork="f2", state="done")
        assert host.status()["forks"][0]["fork"] == "f1"


@pytest.mark.asyncio
async def test_fork_cap_cancel_failure_and_worktree(fx):
    repo = fx.tmp / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-qm", "a"], check=True)
    async with running(fx, max_forks=1) as (host, events):
        await configure(host)
        with pytest.raises(OpError, match="not started"):
            await host.handle({"t": "fork", "fork": "f0", "role": "house", "text": "x"})
        await turn(host, events, "hello", "q1")
        await host.handle({"t": "fork", "fork": "f1", "role": "house", "text": "slowfork 5"})
        await wait_ev(events, "fork", fork="f1", state="started")
        with pytest.raises(OpError, match="too many side threads"):
            await host.handle({"t": "fork", "fork": "f2", "role": "house", "text": "x"})
        assert find(events, "fork", fork="f2", state="failed")["error"] == "too many side threads"
        assert await host.handle({"t": "fork_cancel", "fork": "f1"}) == {"fork": "f1",
                                                                         "cancelled": True}
        await wait_ev(events, "fork", fork="f1", state="cancelled")
        assert not find(events, "fork", fork="f1", state="done")
        await host.handle({"t": "fork", "fork": "f3", "role": "house", "text": "failfork"})
        await wait_ev(events, "fork", fork="f3", state="failed")
        await host.handle({"t": "fork", "fork": "f4", "role": "house", "text": "code it",
                           "repo": str(repo)})
        await wait_ev(events, "fork", fork="f4", state="done")
        wt = fx.roles / "house/forks/f4"
        assert (wt / "a.txt").exists()
        branch = subprocess.run(["git", "-C", str(wt), "branch", "--show-current"],
                                capture_output=True, text=True).stdout.strip()
        assert branch == "fork/f4"


@pytest.mark.asyncio
async def test_btw_answers_without_tools_or_a_session_file(fx):
    async with running(fx) as (host, events):
        await configure(host)
        await turn(host, events, "hello", "q1")
        files_before = sorted(glob.glob(str(fx.cfgdir / "projects" / "*" / "*.jsonl")))
        res = await host.handle({"t": "btw", "req": "b1", "role": "house",
                                 "question": "what are you doing?"})
        assert res == {"started": True}
        ev = await wait_ev(events, "btw", req="b1")
        assert ev["ok"] and ev["ms"] >= 0
        assert ev["text"] == "btw answer from 2 messages: The question: what are you doing?"
        a = starts(fx)[-1]["argv"]
        assert "--no-session-persistence" in a and "--fork-session" in a
        for k, v in (("--tools", ""), ("--setting-sources", ""), ("--model", "haiku"),
                     ("--effort", "low"), ("--output-format", "json")):
            assert a[a.index(k) + 1] == v
        assert json.loads(Path(a[a.index("--mcp-config") + 1]).read_text()) == {
            "mcpServers": {}}
        assert sorted(glob.glob(str(fx.cfgdir / "projects" / "*" / "*.jsonl"))) == files_before
        assert [e["item"] for e in events if e["kind"] == "result"] == ["q1"]


# -- voice -------------------------------------------------------------------------

def voice_lanes():
    return {"main": main_spec(),
            "voice": {"model": "haiku", "effort": "low", "tools": [], "partial": True,
                      "env": {"ROOK_FLOW_TOKEN": "flow-tok"}, "thinking": False}}


async def voice_host(fx, host):
    await configure(host, files={"CLAUDE.md": "# p\n", "voice/CLAUDE.md": "# v\n"},
                    lanes=voice_lanes(), voice_token_sha256=VOICE_HASH)
    return f"ws://127.0.0.1:{host.voice_server.port}/voice/house"


async def recv_until(ws, pred, timeout=5.0):
    got = []
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            msg = await ws.receive(timeout=max(0.05, end - time.monotonic()))
        except asyncio.TimeoutError:
            break
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise AssertionError(f"socket ended: {msg}")
        obj = json.loads(msg.data)
        got.append(obj)
        if pred(obj):
            return got
    raise AssertionError(f"timed out; got {got}")


@pytest.mark.asyncio
async def test_voice_auth(fx):
    async with running(fx, voice_listen="127.0.0.1:0") as (host, events):
        url = await voice_host(fx, host)
        async with aiohttp.ClientSession() as s:
            for kw in ({"headers": {"Authorization": "Bearer wrong"}}, {},
                       {"params": {"token": "wrong"}}):
                with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                    await s.ws_connect(url, **kw)
                assert e.value.status == 403
            with pytest.raises(aiohttp.WSServerHandshakeError) as e:
                await s.ws_connect(url.replace("/house", "/nobody"),
                                   headers={"Authorization": f"Bearer {VOICE_TOKEN}"})
            assert e.value.status == 403
            async with s.ws_connect(url, params={"token": VOICE_TOKEN}) as ws:
                ready = json.loads((await ws.receive(timeout=3)).data)
                assert ready == {"t": "ready", "role": "house", "session": None}
            assert host.status()["voice_clients"] == {"house": 0}


@pytest.mark.asyncio
async def test_voice_turn_streams_deltas(fx):
    async with running(fx, voice_listen="127.0.0.1:0") as (host, events):
        url = await voice_host(fx, host)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, headers={"Authorization": f"Bearer {VOICE_TOKEN}"}) as ws:
                await ws.receive(timeout=3)
                await ws.send_json({"t": "user", "turn": "t1", "text": "hello there friend",
                                    "identity": {"owner": True, "name": "Bake"}})
                got = await recv_until(ws, lambda o: o["t"] == "end")
                deltas = [o["text"] for o in got if o["t"] == "delta"]
                assert len(deltas) > 3
                end = got[-1]
                assert end["ok"] and end["turn"] == "t1"
                assert end["text"] == "".join(deltas) == "echo: [voice | Bake | T] hello there friend"
                assert isinstance(end["first_ms"], int) and end["total_ms"] >= end["first_ms"]
        vt = find(events, "voice_turn", turn="t1")
        assert vt["text"] == "hello there friend" and vt["identity"]["name"] == "Bake"
        vr = await wait_ev(events, "voice_reply", turn="t1")
        assert vr["first_ms"] == end["first_ms"] and vr["thinking_tokens"] == 0
        assert vr["tools"] == [] and vr["lane"] == "voice"
        st = [x for x in starts(fx) if x["cwd"].endswith("/voice")][0]
        a = st["argv"]
        assert "--include-partial-messages" in a and a[a.index("--model") + 1] == "haiku"
        assert a[a.index("--tools") + 1] == ""
        assert a[a.index("--settings") + 1] == str(fx.roles / "house/voice/.claude/settings.json")
        assert st["env"]["MAX_THINKING_TOKENS"] == "0"
        assert not find(events, "result")      # voice turns are voice_reply, not results


@pytest.mark.asyncio
async def test_voice_routing_lines_never_reach_the_client(fx):
    async with running(fx, voice_listen="127.0.0.1:0") as (host, events):
        url = await voice_host(fx, host)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, params={"token": VOICE_TOKEN}) as ws:
                await ws.receive(timeout=3)
                # The fake echoes the text back word by word: the routing line is
                # split over several deltas.
                await ws.send_json({"t": "user", "turn": 7, "identity": {"name": "Bake"},
                                    "text": 'On it. <<flow {"op": "queue", "request": '
                                            '"what is on tomorrow"}>>'})
                got = await recv_until(ws, lambda o: o["t"] == "end")
                said = "".join(o["text"] for o in got if o["t"] == "delta")
                assert "<<" not in said and "flow" not in said and "queue" not in said
                assert said.strip() == got[-1]["text"] == "echo: [voice | Bake | T] On it."
                assert got[-1]["turn"] == 7                       # the client's own id type
        vr = await wait_ev(events, "voice_reply", turn=7)
        assert vr["routing"] == [{"op": "queue", "request": "what is on tomorrow"}]
        assert vr["text"] == "echo: [voice | Bake | T] On it."


@pytest.mark.asyncio
async def test_voice_brief_goes_in_when_it_changes_and_events_reach_the_client(fx):
    async with running(fx, voice_listen="127.0.0.1:0") as (host, events):
        url = await voice_host(fx, host)
        res = await host.handle({"t": "voice_context", "role": "house",
                                 "text": "Working on: the backup check.", "tz": "UTC"})
        assert res["ok"]
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, params={"token": VOICE_TOKEN}) as ws:
                await ws.receive(timeout=3)
                for turn in ("a", "b"):
                    await ws.send_json({"t": "user", "turn": turn, "text": "hi",
                                        "identity": {"name": "Bake"}, "mode": "listen"})
                    await recv_until(ws, lambda o: o["t"] == "end")
                await host.handle({"t": "voice_context", "role": "house",
                                   "text": "Not working on anything.", "tz": "UTC"})
                await ws.send_json({"t": "user", "turn": "c", "text": "and now",
                                    "identity": {"name": "Bake"}})
                await recv_until(ws, lambda o: o["t"] == "end")
                vsid = host.roles["house"].lanes["voice"].session_id
                users = [m["text"] for m in transcript(fx, vsid) if m["type"] == "user"]
                assert users[0] == ("[brief]\nWorking on: the backup check.\n\n"
                                    "[voice | Bake | active listening | T] hi")
                assert users[1] == "[voice | Bake | active listening | T] hi"   # unchanged
                assert users[2].startswith("[brief]\nNot working on anything.")
                # A mode change for the client that spoke last turn.
                res = await host.handle({"t": "voice_event", "role": "house", "event": "mode",
                                         "fields": {"mode": "dictate", "by": "voice"},
                                         "turn": "c"})
                assert res["ok"]
                got = await recv_until(ws, lambda o: o["t"] == "mode")
                assert got[-1] == {"t": "mode", "turn": "c", "mode": "dictate", "by": "voice"}
                with pytest.raises(Exception, match="event: one of"):
                    await host.handle({"t": "voice_event", "role": "house", "event": "exec",
                                       "fields": {}})


@pytest.mark.asyncio
async def test_voice_config_in_ready_and_on_change_and_controls_go_to_the_hub(fx):
    async with running(fx, voice_listen="127.0.0.1:0") as (host, events):
        cfg = {"engine": "house", "models": {"front": {"model": "claude-haiku-5-5"}}}
        await configure(host, files={"CLAUDE.md": "# p\n", "voice/CLAUDE.md": "# v\n"},
                        lanes=voice_lanes(), voice_token_sha256=VOICE_HASH, voice_config=cfg)
        url = f"ws://127.0.0.1:{host.voice_server.port}/voice/house"
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, params={"token": VOICE_TOKEN}) as ws:
                ready = json.loads((await ws.receive(timeout=3)).data)
                assert ready["engine"] == "house" and ready["models"] == cfg["models"]
                # The hub switches the voice to the local engine: every client hears it.
                await configure(host, files={"CLAUDE.md": "# p\n", "voice/CLAUDE.md": "# v\n"},
                                lanes=voice_lanes(), voice_token_sha256=VOICE_HASH,
                                voice_config={**cfg, "engine": "local"})
                got = await recv_until(ws, lambda o: o["t"] == "config")
                assert got[-1]["engine"] == "local"
                # The phone's pickers: a control goes to the hub with the identity.
                await ws.send_json({"t": "control", "op": "models_set",
                                    "args": {"back": "sonnet"},
                                    "identity": {"owner": True, "name": "Bake"}})
                ev = await wait_ev(events, "voice_control")
                assert ev["op"] == "models_set" and ev["args"] == {"back": "sonnet"}
                assert ev["identity"] == {"owner": True, "name": "Bake"}
                # ... and the hub's answer comes back to that client.
                await host.handle({"t": "voice_event", "role": "house", "event": "models",
                                   "fields": {"ok": True}, "client": ev["client"]})
                got = await recv_until(ws, lambda o: o["t"] == "models")
                assert got[-1]["ok"] is True
                await ws.send_json({"t": "control", "op": "shell", "args": {}})
                got = await recv_until(ws, lambda o: o["t"] == "error")
                assert "control: one of" in got[-1]["error"]


@pytest.mark.asyncio
async def test_voice_barge_in_interrupts_the_running_turn(fx):
    async with running(fx, voice_listen="127.0.0.1:0") as (host, events):
        url = await voice_host(fx, host)
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(url, params={"token": VOICE_TOKEN}) as ws:
                await ws.receive(timeout=3)
                t0 = time.monotonic()
                await ws.send_json({"t": "user", "turn": "t1", "text": "pause 8",
                                    "identity": {"name": "Bake"}})
                await wait_for(lambda: host.roles["house"].voice.current is not None, 3, "busy")
                await ws.send_json({"t": "user", "turn": "t2", "text": "actually stop",
                                    "identity": {"name": "Bake"}})
                got = await recv_until(ws, lambda o: o["t"] == "end" and o["turn"] == "t2")
                ends = {o["turn"]: o for o in got if o["t"] == "end"}
                assert ends["t1"]["ok"] is False and ends["t1"]["interrupted"] is True
                assert ends["t2"]["ok"] and ends["t2"]["text"].endswith("actually stop")
                assert time.monotonic() - t0 < 4
                # cancel interrupts too
                await ws.send_json({"t": "user", "turn": "t3", "text": "pause 8"})
                await wait_for(lambda: host.roles["house"].voice.current is not None, 3, "busy")
                await ws.send_json({"t": "cancel", "turn": "t3"})
                got = await recv_until(ws, lambda o: o["t"] == "end")
                assert got[-1]["turn"] == "t3" and got[-1]["ok"] is False


@pytest.mark.asyncio
async def test_voice_say_push_delivery_and_held_pushes(fx):
    async with running(fx, voice_listen="127.0.0.1:0") as (host, events):
        url = await voice_host(fx, host)
        # Nobody connected: kept for the next user turn.
        res = await host.handle({"t": "voice_say", "role": "house", "text": "the lights are off"})
        assert res["queued"]
        await asyncio.sleep(0.1)
        async with aiohttp.ClientSession() as s:
            a = await s.ws_connect(url, params={"token": VOICE_TOKEN})
            b = await s.ws_connect(url, params={"token": VOICE_TOKEN})
            await a.receive(timeout=3)
            await b.receive(timeout=3)
            await a.send_json({"t": "user", "turn": "a1", "text": "turn off the lights",
                               "identity": {"name": "Bake"}})
            got = await recv_until(a, lambda o: o["t"] == "end")
            assert got[-1]["text"] == "echo: [voice | Bake | T] turn off the lights"
            vsid = host.roles["house"].lanes["voice"].session_id
            users = [m["text"] for m in transcript(fx, vsid) if m["type"] == "user"]
            assert users[0] == ("[back end, while you were away] the lights are off\n\n"
                                "[voice | Bake | T] turn off the lights")
            await b.send_json({"t": "user", "turn": "b1", "text": "hi"})
            await recv_until(b, lambda o: o["t"] == "end")
            # For a1: goes to a (the client that asked), not the most recent (b).
            await host.handle({"t": "voice_say", "role": "house", "text": "done: lights off",
                               "turn": "a1"})
            got = await recv_until(a, lambda o: o["t"] == "end")
            assert got[0]["t"] == "push"
            assert got[-1]["turn"] == got[0]["turn"] and got[-1]["ok"]
            assert got[-1]["text"] == "echo: [back end] done: lights off"
            # No turn given: the most recently active client (b).
            await host.handle({"t": "voice_say", "role": "house", "text": "also the fan"})
            got = await recv_until(b, lambda o: o["t"] == "end")
            assert got[0]["t"] == "push" and got[-1]["text"] == "echo: [back end] also the fan"
            vr = [e for e in events if e["kind"] == "voice_reply" and e.get("push")]
            assert len(vr) == 2 and vr[0]["for_turn"] == "a1"
            await a.close()
            await b.close()


# -- the link ----------------------------------------------------------------------

class FakeHub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.down = False
        self.msgs: list[dict] = []
        self.ws: web.WebSocketResponse | None = None
        self.refused = 0
        self.replies: dict = {}

    async def handle(self, request):
        if self.down or request.headers.get("Authorization") != f"Bearer {self.token}":
            self.refused += 1
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        async for msg in ws:
            obj = json.loads(msg.data)
            self.msgs.append(obj)
            if obj.get("t") == "reply":
                self.replies[obj["req"]] = obj
        return ws

    async def request(self, obj, timeout=5.0):
        await self.ws.send_json(obj)
        return await wait_for(lambda: self.replies.get(obj["req"]), timeout, f"reply {obj}")


@pytest.mark.asyncio
async def test_link_hello_requests_reconnect_and_buffered_events(fx):
    hub = FakeHub("link-secret")
    app = web.Application()
    app.router.add_get("/home/agents-link", hub.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    host = Host(config(fx, hub_url=f"ws://127.0.0.1:{port}/home/agents-link"), "link-secret")
    task = asyncio.create_task(host.run())
    try:
        await wait_for(lambda: [m for m in hub.msgs if m["t"] == "hello"], 5, "hello")
        hello = hub.msgs[0]
        assert hello["t"] == "hello" and hello["host"] == "testhost" and hello["pid"]
        assert hello["servers"] == ["other", "rea"] and hello["lanes"] == {}
        rep = await hub.request({"t": "configure", "req": 1, "roles": {"house": {
            "dir_files": {"CLAUDE.md": "# p\n"}, "lanes": {"main": main_spec()}}}})
        assert rep["ok"] and rep["result"]["roles"]["house"]["lanes"] == {"main": "started"}
        rep = await hub.request({"t": "send", "req": 2, "role": "house", "lane": "main",
                                 "item": "q1", "text": "hello"})
        assert rep["ok"]
        await wait_for(lambda: [m for m in hub.msgs if m.get("kind") == "result"], 5, "result")
        rep = await hub.request({"t": "nonsense", "req": 3})
        assert rep["ok"] is False and "unknown" in rep["error"]
        rep = await hub.request({"t": "status", "req": 4})
        assert rep["result"]["lanes"]["house/main"]["state"] == "idle"

        # Down: events wait in the buffer and arrive, in order, after the next hello.
        hub.down = True
        await hub.ws.close()
        await wait_for(lambda: not host.link.connected.is_set(), 3, "disconnect")
        await wait_for(lambda: hub.refused >= 1, 3, "a refused reconnect")
        for i in range(3):
            host.emit("house", "main", "probe", n=i)
        n_before = len(hub.msgs)
        hub.down = False
        await wait_for(lambda: len([m for m in hub.msgs[n_before:]
                                    if m.get("kind") == "probe"]) == 3, 5, "buffered events")
        after = hub.msgs[n_before:]
        assert after[0]["t"] == "hello" and after[0]["lanes"] == {"house/main": "idle"}
        assert [m["n"] for m in after if m.get("kind") == "probe"] == [0, 1, 2]
        assert host.link.connects == 2
        assert "link-secret" not in json.dumps(hub.msgs)
    finally:
        host.request_stop()
        await asyncio.wait_for(task, 15)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_link_bad_token_keeps_retrying(fx):
    hub = FakeHub("right")
    app = web.Application()
    app.router.add_get("/home/agents-link", hub.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    host = Host(config(fx, hub_url=f"ws://127.0.0.1:{port}/home/agents-link"), "wrong")
    task = asyncio.create_task(host.run())
    try:
        await wait_for(lambda: hub.refused >= 3, 5, "retries")
        assert hub.msgs == [] and host.link.connects == 0
    finally:
        host.request_stop()
        await asyncio.wait_for(task, 15)
        await runner.cleanup()


def test_main_refuses_without_token(fx, monkeypatch, tmp_path):
    from rook.agents.__main__ import main
    cfg = tmp_path / "host.json"
    cfg.write_text(json.dumps({"hub_url": "wss://hub/home/agents-link",
                               "roles_dir": str(fx.roles)}))
    monkeypatch.delenv("ROOK_AGENTS_LINK_TOKEN")
    assert main(["--config", str(cfg)]) == 2
    cfg.write_text(json.dumps({"hub_url": "https://nope", "roles_dir": str(fx.roles)}))
    assert main(["--config", str(cfg)]) == 2
