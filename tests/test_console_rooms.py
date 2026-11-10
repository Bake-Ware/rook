"""Console rooms: sanitize, store lifecycle, search, retention, and a live
end-to-end run of the real proc plugin through the real pump into the store."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from rook.worker.plugins.proc import ProcPlugin


# -- sanitize ---------------------------------------------------------------


# -- store lifecycle --------------------------------------------------------


# -- search -----------------------------------------------------------------


# -- retention --------------------------------------------------------------


# -- end to end -------------------------------------------------------------

class _FakeBand:
    """Routes client.call() straight into a real ProcPlugin, the way the band
    would once the reply came back."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.workers = {"w1": {"name": "gpu-box", "caps": ["proc.start", "proc.read"]}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        fn = {"proc.read": self.plugin._read, "proc.write": self.plugin._write,
              "proc.close": self.plugin._close}[cap]
        return {"ok": True, "from": "w1", "result": await fn(**(args or {}))}


@pytest.mark.asyncio
async def test_read_never_splits_a_utf8_character():
    # Chunk boundaries land on bytes, but multi-byte characters must survive
    # them intact — otherwise every boundary corrupts a character.
    plugin = ProcPlugin()
    started = await plugin._start(
        argv=["python3", "-c", "print('é' * 500)"], label="utf8 boundary")
    await asyncio.sleep(0.5)
    out, cursor = "", 0
    for _ in range(200):
        r = await plugin._read(started["handle"], cursor, max_bytes=7)
        out += r["chunk"]
        if r["next_cursor"] == cursor and r["eof"]:
            break
        cursor = r["next_cursor"]
    assert "�" not in out          # no replacement characters
    assert out.strip() == "é" * 500
    await plugin.stop()


# -- windows compatibility ---------------------------------------------------

@pytest.mark.asyncio
async def test_windows_hard_kill_does_not_name_sigkill(monkeypatch):
    """signal.SIGKILL is POSIX-only. Naming it in OUR code on a Windows worker
    raised an AttributeError that _terminate swallowed, so proc.close reported
    success, left the process running, and dropped the only handle that could
    reach it.

    Only our dispatch is simulated here: _IS_WIN is flipped and SIGKILL removed,
    and Process.kill stands in for what asyncio does on real Windows
    (TerminateProcess). asyncio's own Windows transport is not under test — on
    Linux its POSIX implementation names SIGKILL internally, which is exactly
    why the simulation has to stop at our boundary.
    """
    import signal as _sig
    import rook.worker.plugins.proc as proc_mod

    plugin = proc_mod.ProcPlugin()
    started = await plugin._start(argv=["bash", "-c", "sleep 30"], label="win sim")
    pid = started["pid"]
    sess = plugin._sessions[started["handle"]]

    def alive(p):
        try:
            os.kill(p, 0)
            return True
        except OSError:
            return False

    assert alive(pid)

    called = {"kill": False}

    def fake_terminate_process():
        called["kill"] = True
        os.kill(pid, _sig.SIGTERM)      # stands in for TerminateProcess

    monkeypatch.setattr(sess.proc, "kill", fake_terminate_process)
    monkeypatch.delattr(_sig, "SIGKILL")           # as Windows has it
    monkeypatch.setattr(proc_mod, "_IS_WIN", True)

    out = await plugin._close(started["handle"])   # must not raise
    await asyncio.sleep(0.4)

    assert called["kill"], "Windows path must go through Process.kill(), not SIGKILL"
    assert out["ok"] is True
    assert not alive(pid), "process survived proc.close on the Windows path"

    monkeypatch.undo()
    await plugin.stop()


@pytest.mark.asyncio
async def test_close_keeps_the_handle_when_it_cannot_stop_the_process(monkeypatch):
    # A close that fails must not report success, and must not drop the handle —
    # that would orphan the process with nothing left to reach it by.
    import rook.worker.plugins.proc as proc_mod

    plugin = proc_mod.ProcPlugin()
    started = await plugin._start(argv=["bash", "-c", "sleep 30"], label="stubborn")
    handle = started["handle"]

    async def _never_dies(sess, hard=False):
        return False
    monkeypatch.setattr(plugin, "_terminate", _never_dies)

    out = await plugin._close(handle)
    assert out["ok"] is False
    assert handle in plugin._sessions, "handle dropped despite a failed kill"

    monkeypatch.undo()
    await plugin._close(handle)
    await plugin.stop()


@pytest.mark.asyncio
async def test_close_on_a_live_process_reports_success_and_its_exit_code():
    # Regression: _terminate read liveness from session bookkeeping that the
    # cancelled waiter had not updated yet, so a successful kill reported
    # failure and the handle was kept forever.
    plugin = ProcPlugin()
    started = await plugin._start(argv=["bash", "-c", "sleep 30"], label="live close")
    handle = started["handle"]

    out = await plugin._close(handle)
    assert out["ok"] is True
    assert out["exit_code"] is not None      # backfilled from the process
    assert handle not in plugin._sessions
    await plugin.stop()
