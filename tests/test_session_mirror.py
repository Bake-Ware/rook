"""Session mirror: the spool the Claude Code mod writes, read by the worker (sessions.mirror)."""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest

from rook.core.facts import NodeFacts
from rook.core.host import PluginHost
from rook.core.plugin import Candidate, Plugin, capability
from rook.worker import session_mirror as spool
from rook.worker import termwire
from rook.worker.plugins import terminals
from rook.worker.plugins.session_mirror import SessionMirrorPlugin
from rook.worker.plugins.terminals import TerminalsPlugin

SID = "634b5e51-0000-4000-8000-000000000001"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


def ev(seq, type_, **fields):
    return {"v": 1, "seq": seq, "ts": 1_700_000_000 + seq, "type": type_, **fields}


def write_chunk(home, index, events, agent="claude", native=SID, tail=""):
    folder = home / "mirror" / agent
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / spool.chunk_name(native, index)
    path.write_text("".join(json.dumps(e) + "\n" for e in events) + tail)
    return path


def test_state_dir_honours_rook_worker_home(home, monkeypatch):
    assert spool.mirror_root() == home / "mirror"
    monkeypatch.delenv("ROOK_WORKER_HOME")
    assert spool.state_dir() == spool.Path.home() / ".rook-band-worker"


def test_names_are_checked_before_they_touch_a_path(home):
    for agent, native in (("../x", SID), ("claude", "../../etc/passwd"), ("claude", "a.b"),
                          ("Claude", SID), ("claude", "")):
        with pytest.raises(ValueError):
            spool.SpoolReader(agent, native)


def test_reads_from_a_cursor_across_rotated_chunks(home):
    write_chunk(home, 0, [ev(1, "session.start", cwd="/w", pid=None), ev(2, "prompt", text="hi", **{"from": "person"})])
    write_chunk(home, 1, [ev(3, "assistant.delta", text="he"), ev(4, "assistant.done", text="hello")])
    # A rewrite caught half done: the unfinished last line does not count yet.
    write_chunk(home, 2, [ev(5, "turn.end")], tail='{"v": 1, "seq": 6, "ty')
    r = spool.SpoolReader("claude", SID)
    out = r.read(0)
    assert [e["seq"] for e in out["events"]] == [1, 2, 3, 4, 5]
    assert out["cursor"] == 5 and out["done"] is False and out["exists"]
    assert [e["seq"] for e in r.read(3)["events"]] == [4, 5]
    assert r.read(5) == {"ok": True, "events": [], "cursor": 5, "done": False, "exists": True}
    write_chunk(home, 2, [ev(5, "turn.end"), ev(6, "session.end", reason="prompt_input_exit")])
    out = r.read(5)
    assert [e["type"] for e in out["events"]] == ["session.end"] and out["done"] is True


def test_an_emptied_chunk_is_skipped_and_answers_are_bounded(home):
    write_chunk(home, 0, [])        # the mod empties the oldest chunks of a huge spool
    write_chunk(home, 1, [ev(n, "assistant.delta", text="x") for n in range(1, 21)])
    r = spool.SpoolReader("claude", SID)
    out = r.read(0, max_events=8)
    assert [e["seq"] for e in out["events"]] == list(range(1, 9)) and out["done"] is False
    assert [e["seq"] for e in r.read(out["cursor"], max_events=100)["events"]] == list(range(9, 21))


def test_a_missing_spool_is_empty_not_an_error(home):
    out = spool.SpoolReader("claude", SID).read(0)
    assert out == {"ok": True, "events": [], "cursor": 0, "done": False, "exists": False}


@pytest.mark.skipif(os.name != "posix", reason="process liveness is POSIX-only")
def test_a_session_whose_process_died_reads_as_done(home):
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    write_chunk(home, 0, [ev(1, "session.start", pid=dead.pid), ev(2, "prompt", text="x")])
    assert spool.SpoolReader("claude", SID).read(0)["done"] is True
    write_chunk(home, 0, [ev(1, "session.start", pid=os.getpid()), ev(2, "prompt", text="x")])
    assert spool.SpoolReader("claude", SID).read(0)["done"] is False


def test_cleanup_removes_week_old_closed_spools_only(home):
    now = time.time()
    old = now - 8 * 86400
    closed = write_chunk(home, 0, [ev(1, "session.start", pid=os.getpid()), ev(2, "session.end")], native="closed-1")
    rotated = write_chunk(home, 1, [ev(3, "session.end")], native="closed-1")
    live = write_chunk(home, 0, [ev(1, "session.start", pid=os.getpid())], native="live-1")
    fresh = write_chunk(home, 0, [ev(1, "session.end")], native="fresh-1")
    for p in (closed, rotated, live):
        os.utime(p, (old, old))
    assert {r["native_id"] for r in spool.spools()} == {"closed-1", "live-1", "fresh-1"}
    assert spool.cleanup(now=now) == ["claude/closed-1"]
    assert not closed.exists() and not rotated.exists()
    assert live.exists() and fresh.exists()


@pytest.mark.asyncio
async def test_mirror_cap_long_polls_until_events_arrive(home):
    p = SessionMirrorPlugin()
    write_chunk(home, 0, [ev(1, "session.start", pid=os.getpid())])
    first = await p.mirror("claude", SID)
    assert first["cursor"] == 1 and not first["done"]

    async def later():
        await asyncio.sleep(0.4)
        write_chunk(home, 0, [ev(1, "session.start", pid=os.getpid()), ev(2, "prompt", text="go")])

    task = asyncio.create_task(later())
    t0 = time.monotonic()
    out = await p.mirror("claude", SID, cursor=1, wait=10)
    await task
    assert [e["text"] for e in out["events"]] == ["go"] and out["cursor"] == 2
    assert time.monotonic() - t0 < 5
    # Nothing new: the call waits out `wait`, then answers empty.
    t0 = time.monotonic()
    out = await p.mirror("claude", SID, cursor=2, wait=0.5)
    assert out["events"] == [] and 0.4 < time.monotonic() - t0 < 3
    with pytest.raises(ValueError):
        await p.mirror("claude", "../x")


def test_two_plugins_may_share_the_sessions_namespace():
    """sessions.mirror lives beside the catalog plugin's sessions.list & co."""
    class Catalog(Plugin):
        NAMESPACE = "sessions"

        @capability("list")
        def list_(self):
            return {"ok": True, "items": []}

    host = PluginHost(facts=NodeFacts(node_id="w", name="worker-a"), check_placement=False)
    loaded = host.load([Candidate("sessions", "test:sessions", lambda: Catalog),
                        Candidate("session_mirror", "test:session_mirror", lambda: SessionMirrorPlugin)])
    assert len(loaded) == 2
    assert host.registry.has("sessions.list") and host.registry.has("sessions.mirror")


@pytest.mark.skipif(os.name != "posix", reason="PTYs are POSIX-only")
@pytest.mark.asyncio
async def test_handoff_waits_for_the_old_process_then_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_WORK_TERM_DIR", str(tmp_path / "terms"))
    fake = tmp_path / "claude"
    fake.write_text('#!/bin/sh\necho "RESUMED $*"\nsleep 5\n')
    fake.chmod(0o755)
    monkeypatch.setattr(terminals, "_binary", lambda h: str(fake))
    p = TerminalsPlugin()
    old = subprocess.Popen(["sleep", "30"])
    try:
        with pytest.raises(ValueError):
            await p.open(harness="claude", cwd=str(tmp_path), handoff_pid=old.pid)  # needs resume
        r = await p.open(harness="claude", cwd=str(tmp_path), resume="abc-123", handoff_pid=old.pid)
        assert r["waiting"] and r["running"] and r["pid"] is None
        await asyncio.sleep(0.5)
        assert p.terms[r["id"]].proc is None    # still waiting on the old process
        old.terminate()
        old.wait()
        out, cursor = b"", 0
        async with asyncio.timeout(8):
            while b"RESUMED" not in out:
                got = await p.read(r["id"], cursor=cursor, wait=1)
                out += termwire.decode(got["enc"], got["data"])
                cursor = got["next"]
        assert b"--resume abc-123" in out and b"waiting for the session" in out

        # Closed while it still waits: nothing starts.
        old2 = subprocess.Popen(["sleep", "30"])
        r2 = await p.open(harness="claude", cwd=str(tmp_path), resume="abc-456", handoff_pid=old2.pid)
        t2 = p.terms[r2["id"]]
        await p.close(r2["id"])
        assert t2.proc is None and not t2.running
        old2.kill()
        old2.wait()
    finally:
        if old.poll() is None:
            old.kill()
        await p.stop()
