"""rook.worker.agents (agent homes, CLI lookup) and the `rook sessions` CLI
built on the claude-history parsers."""
import json
import os
import subprocess
import sys

import pytest

from rook.worker import agents


def test_roots_follow_home_and_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert agents.claude_projects() == tmp_path / ".claude" / "projects"
    assert agents.codex_sessions() == tmp_path / ".codex" / "sessions"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "cx"))
    assert agents.codex_sessions() == tmp_path / "cx" / "sessions"
    from rook.worker.plugins import claude_history, codex_history, sessions
    assert claude_history._default_root() == agents.claude_projects()
    assert codex_history._root() == agents.codex_sessions()
    assert sessions._claude_home() == agents.claude_home()


def test_expand_and_short_id(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_TEST_DIR", str(tmp_path))
    assert agents.expand("$ROOK_TEST_DIR/x", lambda: tmp_path / "d") == tmp_path / "x"
    assert agents.expand("", lambda: tmp_path / "d") == tmp_path / "d"
    assert agents.short_id("8f3a1b2c-0000-4000") == "8f3a1b2c"
    assert agents.short_id("") == ""


def test_agent_bin_skips_the_session_shim(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("POSIX lookup")
    from rook.worker import shim
    real, fake = tmp_path / "real", tmp_path / "shimdir"
    real.mkdir()
    fake.mkdir()
    (fake / shim.MARKER_FILE).write_text("")
    for d in (real, fake):
        p = d / "codex"
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(fake), str(real)]))
    assert agents.codex_bin() == str(real / "codex")


def test_agent_bin_finds_windows_npm_cmd(tmp_path, monkeypatch):
    monkeypatch.setattr(agents, "_IS_WIN", True)
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "claude.cmd").write_text("@echo off\n")
    monkeypatch.setattr(agents, "_windows_npm_dirs", lambda: [str(tmp_path / "none"), str(npm)])
    assert agents.claude_bin() == str(npm / "claude.cmd")
    assert agents.codex_bin() is None


# -- rook sessions (cc_history) --------------------------------------------------

SID = "8f3a1b2c-0000-4000-8000-000000000001"
OTHER = "9e4b2c3d-0000-4000-8000-000000000002"


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / ".claude"
    proj = home / "projects" / "-srv-app"
    proj.mkdir(parents=True)
    lines = [
        {"type": "user", "timestamp": "2026-08-30T10:00:00Z", "cwd": "/srv/app",
         "message": {"role": "user", "content": "fix the nginx timeouts"}},
        {"type": "assistant", "timestamp": "2026-08-30T10:05:00Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "Raising proxy_read_timeout"},
             {"type": "tool_use", "name": "Edit", "input": {"file": "nginx.conf"}}]}},
        {"type": "user", "timestamp": "2026-08-30T10:06:00Z",
         "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "queue-operation"},
    ]
    (proj / f"{SID}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    sub = proj / SID / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(json.dumps(lines[0]) + "\n")
    (home / "history.jsonl").write_text(json.dumps(
        {"sessionId": OTHER, "project": "/srv/other", "timestamp": 1756548000000,
         "display": "only in history"}) + "\n")
    monkeypatch.setattr(agents, "claude_home", lambda: home)
    monkeypatch.setattr(agents, "claude_projects", lambda: home / "projects")
    return home


def test_scan_read_and_search(claude_home):
    from rook.cli import cc_history
    by_project = cc_history.scan_history()
    assert set(by_project) == {"/srv/app", "/srv/other"}
    s = by_project["/srv/app"][0]
    assert s.session_id == SID and s.subagent_count == 1 and s.message_count == 3
    assert s.first_prompt == "fix the nginx timeouts" and s.duration_str == "6m"
    assert by_project["/srv/other"][0].jsonl_path is None
    msgs = cc_history.read_session(s)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert "proxy_read_timeout" in msgs[1]["content"] and "[tool_use: Edit]" in msgs[1]["content"]
    hits = cc_history.search_sessions("PROXY_READ", by_project)
    assert [h[0].session_id for h in hits] == [SID]
    assert cc_history.search_sessions("only in", by_project)[0][0].session_id == OTHER


def test_rook_tmux_is_retired():
    out = subprocess.run([sys.executable, "-m", "rook", "tmux", "list"], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.getcwd()})
    assert out.returncode == 2 and "Sessions page" in out.stderr


def test_byte_ring_pages_by_absolute_cursor():
    from rook.worker.ring import ByteRing
    r = ByteRing(8)
    r.append(b"abcdef")
    assert r.slice(0, 4) == (b"abcd", 4, 0)
    assert r.slice(4, 100) == (b"ef", 6, 0)
    assert r.slice(6, 10) == (b"", 6, 0)
    r.append(b"ghijkl")                     # 12 written, ring keeps the last 8
    assert (r.start, r.total, len(r)) == (4, 12, 8)
    assert r.slice(1, 3) == (b"efg", 7, 3)  # reader that fell behind
    assert r.slice(99, 3) == (b"", 99, 0)
    assert r.slice(-5, 2) == (b"ef", 6, 4)


@pytest.mark.asyncio
async def test_proc_read_reports_ring_overflow():
    import asyncio
    from rook.worker.plugins.proc import PLUGIN
    p = PLUGIN()
    started = await p._start(argv=[sys.executable, "-c", "print('x' * 20000)"], buffer_bytes=4096)
    assert started["ok"]
    h = started["handle"]
    for _ in range(100):
        r = await p._read(h, 0)
        if r["eof"]:
            break
        await asyncio.sleep(0.05)
    assert r["eof"] and r["dropped"] == r["total"] - 4096 and r["next_cursor"] == r["total"]
    info = {s["handle"]: s for s in (await p._list())["sessions"]}[h]
    assert info["buffered"] == 4096 and info["first_available_cursor"] == r["dropped"]
