"""claude-history.resume — relaunching a stored session with Remote Control."""

from __future__ import annotations

import json
import os
import time

import pytest

from rook.worker.plugins.claude_history import ClaudeHistoryPlugin


class _FakeRegistry:
    """Stands in for the worker's registry, capturing proc.start calls."""

    def __init__(self, has_proc=True, running_handles=()):
        self.has_proc = has_proc
        self.calls = []
        self._running = list(running_handles)

    def has(self, cap):
        return self.has_proc and cap.startswith("proc.")

    async def call(self, cap, **kwargs):
        self.calls.append((cap, kwargs))
        if cap == "proc.start":
            return {"ok": True, "handle": "h1", "pid": 4242}
        if cap == "proc.list":
            return {"ok": True, "sessions": [
                {"handle": h, "running": True, "label": "claude: x", "age_secs": 5}
                for h in self._running]}
        raise AssertionError(cap)


class _FakeWorker:
    def __init__(self, registry):
        self.registry = registry


@pytest.fixture
def projects(tmp_path):
    """A ~/.claude/projects tree with one real-looking session."""
    root = tmp_path / "projects" / "-home-bake-infra"
    root.mkdir(parents=True)
    cwd = tmp_path / "infra"
    cwd.mkdir()
    sid = "8f3a1b2c-0000-4000-8000-000000000001"
    with open(root / f"{sid}.jsonl", "w") as f:
        f.write(json.dumps({"type": "user", "timestamp": "2026-08-30T10:00:00Z",
                            "cwd": str(cwd), "gitBranch": "main",
                            "message": {"role": "user",
                                        "content": "fix the nginx timeouts"}}) + "\n")
        f.write(json.dumps({"type": "ai-title",
                            "aiTitle": "fix the nginx upstream timeouts"}) + "\n")
    return {"root": str(tmp_path / "projects"), "sid": sid, "cwd": str(cwd)}


def _plugin(registry):
    p = ClaudeHistoryPlugin()
    p.bind_worker(_FakeWorker(registry))
    return p


@pytest.mark.asyncio
async def test_resume_builds_the_remote_control_command(projects, monkeypatch):
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin",
                        lambda: "/usr/bin/claude")
    reg = _FakeRegistry()
    out = await _plugin(reg)._resume(projects["sid"], path=projects["root"])

    assert out["ok"] is True
    cap, kw = reg.calls[-1]
    assert cap == "proc.start"
    assert kw["argv"] == ["/usr/bin/claude", "--resume", projects["sid"],
                          "--remote-control", "fix the nginx upstream timeouts"]
    # Remote Control starts an INTERACTIVE session, which needs a tty.
    assert kw["pty"] is True
    # It must come back up where the conversation actually lived.
    assert kw["cwd"] == projects["cwd"]
    assert out["remote_control"] is True
    assert out["handle"] == "h1" and out["pid"] == 4242


@pytest.mark.asyncio
async def test_resume_accepts_a_short_id_and_a_custom_name(projects, monkeypatch):
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin",
                        lambda: "/usr/bin/claude")
    reg = _FakeRegistry()
    out = await _plugin(reg)._resume("8f3a1b2c", path=projects["root"], name="nginx")
    assert out["ok"] is True
    assert out["session_id"] == projects["sid"]      # resolved to the full uuid
    assert reg.calls[-1][1]["argv"][-2:] == ["--remote-control", "nginx"]


@pytest.mark.asyncio
async def test_remote_control_can_be_turned_off(projects, monkeypatch):
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin",
                        lambda: "/usr/bin/claude")
    reg = _FakeRegistry()
    out = await _plugin(reg)._resume(projects["sid"], path=projects["root"],
                                     remote_control=False)
    assert "--remote-control" not in reg.calls[-1][1]["argv"]
    assert out["remote_control"] is False


@pytest.mark.asyncio
async def test_resume_refuses_a_session_that_is_already_running(projects, monkeypatch):
    # Two `claude --resume` processes on one session id would both write the
    # same transcript.
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin",
                        lambda: "/usr/bin/claude")
    reg = _FakeRegistry(running_handles=["h1"])
    p = _plugin(reg)
    first = await p._resume(projects["sid"], path=projects["root"])
    assert first["ok"] is True
    second = await p._resume(projects["sid"], path=projects["root"])
    assert second["ok"] is False
    assert "already running" in second["error"]


@pytest.mark.asyncio
async def test_resume_reports_a_missing_session(projects):
    out = await _plugin(_FakeRegistry())._resume("deadbeef", path=projects["root"])
    assert out["ok"] is False and "not found" in out["error"]


@pytest.mark.asyncio
async def test_resume_reports_a_cwd_that_no_longer_exists(projects, monkeypatch):
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin",
                        lambda: "/usr/bin/claude")
    os.rmdir(projects["cwd"])                       # repo moved away
    out = await _plugin(_FakeRegistry())._resume(projects["sid"], path=projects["root"])
    assert out["ok"] is False
    assert "no longer exists" in out["error"]
    assert "cwd=" in out["hint"]


@pytest.mark.asyncio
async def test_resume_reports_a_missing_claude_cli(projects, monkeypatch):
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin", lambda: None)
    out = await _plugin(_FakeRegistry())._resume(projects["sid"], path=projects["root"])
    assert out["ok"] is False and "claude CLI not found" in out["error"]


@pytest.mark.asyncio
async def test_resume_needs_the_proc_caps(projects):
    # An un-updated worker has no proc.* — say so instead of failing obscurely.
    out = await _plugin(_FakeRegistry(has_proc=False))._resume(
        projects["sid"], path=projects["root"])
    assert out["ok"] is False and "proc.*" in out["error"]


@pytest.mark.asyncio
async def test_resumed_lists_live_sessions_and_forgets_dead_ones(projects, monkeypatch):
    monkeypatch.setattr("rook.worker.plugins.claude_history._claude_bin",
                        lambda: "/usr/bin/claude")
    reg = _FakeRegistry(running_handles=["h1"])
    p = _plugin(reg)
    await p._resume(projects["sid"], path=projects["root"])
    listed = await p._resumed_list()
    assert [s["session_id"] for s in listed["sessions"]] == [projects["sid"]]
    assert listed["sessions"][0]["running"] is True

    reg._running = []                                # process exited
    assert (await p._resumed_list())["sessions"] == []
    # and it stops tracking it, so a later resume is allowed again
    assert p._resumed_handles == {}
