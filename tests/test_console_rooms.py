"""Console rooms: sanitize, store lifecycle, search, retention, and a live
end-to-end run of the real proc plugin through the real pump into the store."""

from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

from rook.band_mcp.console_rooms import ConsoleStore, sanitize
from rook.band_mcp.console_pump import ConsolePump
from rook.worker.plugins.proc import ProcPlugin


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    s = ConsoleStore(os.path.join(d, "console.db"))
    assert s.enabled
    yield s
    s.close()


# -- sanitize ---------------------------------------------------------------

def test_sanitize_strips_ansi():
    assert sanitize("\x1b[31mred\x1b[0m plain") == "red plain"
    assert sanitize("\x1b]0;title\x07after") == "after"


def test_sanitize_collapses_carriage_return_redraws():
    # A progress bar should reduce to its final state, not 4 rows of noise.
    assert sanitize("10%\r50%\r99%\r100% done\nnext") == "100% done\nnext"


@pytest.mark.parametrize("raw,gone", [
    ("API_KEY=supersecretvalue", "supersecretvalue"),
    ("password: hunter2hunter2", "hunter2hunter2"),
    ("token = abcdef123456", "abcdef123456"),
    ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "ghp_aaaa"),
    ("sk-abcdefghijklmnopqrstuvwxyz01", "sk-abcdef"),
])
def test_sanitize_redacts_secrets(raw, gone):
    assert gone not in sanitize(raw)


def test_sanitize_leaves_ordinary_output_alone():
    text = "Installing torch==2.3.0 into /opt/venv (3.4 GB)"
    assert sanitize(text) == text


# -- store lifecycle --------------------------------------------------------

def _open(store, title="set up the llama model on kaiju", worker="w1"):
    r = store.open(title=title, worker=worker, worker_name="kaiju",
                   handle="h1", cmd="bash install.sh", pty=False,
                   opened_by="agent:claude")
    assert r["ok"]
    return r["room"]


def test_open_append_freeze(store):
    rid = _open(store)
    store.append(rid, "downloading weights\n")
    store.append(rid, "done\n")
    r = store.read(rid)
    assert r["state"] == "live"
    assert [l["text"] for l in r["lines"]] == ["downloading weights", "done"]

    store.mark_closing(rid, 0)
    assert store.get(rid)["state"] == "closing"

    res = store.freeze(rid, summary="pulled 7B weights, needs CUDA 12", by="agent:claude")
    assert res["ok"] and res["state"] == "frozen"
    got = store.get(rid)
    assert got["summary"] == "pulled 7B weights, needs CUDA 12"
    assert got["exit_code"] == 0


def test_frozen_room_survives_and_stays_readable(store):
    rid = _open(store)
    store.append(rid, "the important detail\n")
    store.mark_closing(rid, 1)
    store.freeze(rid, summary="failed, missing driver")
    r = store.read(rid)
    assert r["ok"] and r["state"] == "frozen" and r["exit_code"] == 1
    assert any("the important detail" in l["text"] for l in r["lines"])


def test_read_paging_and_tail(store):
    rid = _open(store)
    for i in range(20):
        store.append(rid, f"line{i}\n")
    first = store.read(rid, limit=5)
    assert len(first["lines"]) == 5
    second = store.read(rid, since_seq=first["last_seq"], limit=5)
    assert second["lines"][0]["text"] == "line5"
    tail = store.read(rid, tail=True, limit=3)
    assert [l["text"] for l in tail["lines"]] == ["line17", "line18", "line19"]


# -- search -----------------------------------------------------------------

def test_search_finds_by_task_title(store):
    rid = _open(store, title="set up the llama model on kaiju")
    store.append(rid, "irrelevant build noise\n")
    store.mark_closing(rid, 0)
    store.freeze(rid, summary="model lives in /opt/models")

    # The question a human would actually ask, months later.
    res = store.search("set up model kaiju")
    assert res["ok"] and res["count"] == 1
    assert res["results"][0]["room"] == rid
    assert res["results"][0]["summary"] == "model lives in /opt/models"


def test_search_finds_transcript_text_with_seq(store):
    rid = _open(store)
    store.append(rid, "hello\n")
    store.append(rid, "ERROR: cudaMalloc failed\n")
    res = store.search("cudaMalloc")
    assert res["count"] == 1
    hit = res["results"][0]["hits"][0]
    # The seq lets a caller jump to the hit instead of reading everything.
    around = store.read(rid, since_seq=hit["seq"] - 1, limit=1)
    assert "cudaMalloc" in around["lines"][0]["text"]


def test_search_scopes_to_worker(store):
    a = store.open(title="nginx tuning", worker="w1", worker_name="kaiju",
                   handle="h", cmd="x", pty=False, opened_by="a")["room"]
    b = store.open(title="nginx tuning", worker="w2", worker_name="flophouse",
                   handle="h", cmd="x", pty=False, opened_by="a")["room"]
    assert store.search("nginx")["count"] == 2
    scoped = store.search("nginx", worker="kaiju")
    assert scoped["count"] == 1 and scoped["results"][0]["room"] == a


def test_search_survives_hostile_query(store):
    _open(store)
    assert store.search('broken "( AND')["ok"] in (True, False)  # must not raise
    assert store.search("")["ok"] is False


def test_search_does_not_leak_redacted_secrets(store):
    rid = _open(store)
    store.append(rid, "export API_KEY=topsecretvalue123\n")
    assert store.search("topsecretvalue123")["count"] == 0


# -- retention --------------------------------------------------------------

def test_evict_drops_oldest_frozen_only(store, monkeypatch):
    import rook.band_mcp.console_rooms as cr
    monkeypatch.setattr(cr, "MAX_ROOMS", 5)

    frozen = []
    for i in range(8):
        rid = _open(store, title=f"job {i}")
        store.append(rid, f"work {i}\n")
        store.mark_closing(rid, 0)
        store.freeze(rid, summary=f"did job {i}")
        frozen.append(rid)
    live = _open(store, title="still running")

    assert len(store.list(state="frozen", limit=100)["rooms"]) == 5
    # Oldest gone, newest kept, live untouched.
    assert store.get(frozen[0]) is None
    assert store.get(frozen[-1]) is not None
    assert store.get(live)["state"] == "live"
    # Evicted rooms leave nothing behind in the index.
    assert store.search("job 0")["count"] == 0


def test_sweep_freezes_abandoned_closing_rooms(store, monkeypatch):
    import rook.band_mcp.console_rooms as cr
    monkeypatch.setattr(cr, "CLOSING_GRACE_SECS", -1.0)
    rid = _open(store)
    store.mark_closing(rid, 0)
    assert store.sweep_closing() == 1
    assert store.get(rid)["state"] == "frozen"


# -- end to end -------------------------------------------------------------

class _FakeBand:
    """Routes client.call() straight into a real ProcPlugin, the way the band
    would once the reply came back."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.workers = {"w1": {"name": "kaiju", "caps": ["proc.start", "proc.read"]}}

    async def call(self, cap, args=None, target=None, timeout=15.0, identity=None):
        fn = {"proc.read": self.plugin._read, "proc.write": self.plugin._write,
              "proc.close": self.plugin._close}[cap]
        return {"ok": True, "from": "w1", "result": await fn(**(args or {}))}


@pytest.mark.asyncio
async def test_end_to_end_pump_drains_process_into_room(store):
    plugin = ProcPlugin()
    started = await plugin._start(
        argv=["bash", "-c",
              "echo starting; echo 'tricky $quote \"stuff\"'; sleep 0.2; "
              "echo finished; exit 3"],
        label="end to end check")
    assert started["ok"]

    rid = store.open(title="end to end check", worker="w1", worker_name="kaiju",
                     handle=started["handle"], cmd=started["cmd"], pty=False,
                     opened_by="agent:test")["room"]

    pump = ConsolePump(_FakeBand(plugin), store)
    pump.start()
    for _ in range(60):
        await asyncio.sleep(0.1)
        if store.get(rid)["state"] != "live":
            break
    await pump.stop()

    room = store.read(rid)
    text = "\n".join(l["text"] for l in room["lines"])
    assert "starting" in text
    assert 'tricky $quote "stuff"' in text     # no quoting mangling anywhere
    assert "finished" in text
    assert room["state"] == "closing"          # exited, awaiting a summary
    assert room["exit_code"] == 3

    store.freeze(rid, summary="verified the pump end to end")
    assert store.search("end to end check")["count"] == 1
    await plugin.stop()


@pytest.mark.asyncio
async def test_pump_closes_room_when_worker_goes_away(store, monkeypatch):
    import rook.band_mcp.console_pump as cp
    monkeypatch.setattr(cp, "MAX_MISSES", 2)
    monkeypatch.setattr(cp, "POLL_IDLE", 0.05)
    monkeypatch.setattr(cp, "POLL_BUSY", 0.05)

    class _DeadBand:
        workers = {}
        async def call(self, *a, **k):
            raise asyncio.TimeoutError

    rid = store.open(title="orphan", worker="w1", worker_name="gone",
                     handle="h", cmd="x", pty=False, opened_by="a")["room"]
    pump = ConsolePump(_DeadBand(), store)
    pump.start()
    for _ in range(40):
        await asyncio.sleep(0.05)
        if store.get(rid)["state"] != "live":
            break
    await pump.stop()

    # An unreachable worker must not leave the room live forever.
    assert store.get(rid)["state"] == "closing"
    assert any("unreachable" in l["text"]
               for l in store.read(rid)["lines"])


def test_typed_line_is_not_swallowed_by_a_half_finished_prompt(store):
    # A pty prompt arrives without a newline; the echo of what we type must not
    # be absorbed into it (that would merge three rows into one and lose the
    # 'in' attribution entirely).
    rid = _open(store)
    store.append(rid, "Password: ", stream="out")
    store.append(rid, "$ hunter2", stream="in", sender="agent:claude")
    store.append(rid, "accepted\n", stream="out")
    rows = [(l["stream"], l["text"]) for l in store.read(rid)["lines"]]
    assert ("in", "$ hunter2") in rows
    assert any(s == "out" and "accepted" in t for s, t in rows)


def test_partial_line_is_reassembled_across_chunks(store):
    rid = _open(store)
    store.append(rid, "Installing tor")
    store.append(rid, "ch==2.3.0 ok\nnext line\n")
    texts = [l["text"] for l in store.read(rid)["lines"]]
    assert "Installing torch==2.3.0 ok" in texts
    assert "next line" in texts


def test_trailing_partial_line_is_flushed_when_process_exits(store):
    rid = _open(store)
    store.append(rid, "no trailing newline here")
    assert store.read(rid)["lines"] == []      # still buffered
    store.mark_closing(rid, 0)
    assert any("no trailing newline here" in l["text"]
               for l in store.read(rid)["lines"])


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


def test_sanitize_keeps_crlf_lines_intact():
    # A pty terminates every line with CRLF. Collapsing redraws must not treat
    # that trailing CR as a redraw, or it erases the entire line and pty
    # transcripts come out empty.
    assert sanitize("hi world\r\nlen 14\r\n") == "hi world\nlen 14\n"
    assert sanitize("Name: ") == "Name: "


def test_sanitize_collapses_redraws_that_use_crlf_line_endings():
    # Both behaviours at once: a progress bar redrawing within a CRLF line.
    assert sanitize("10%\r50%\r100% done\r\nnext\r\n") == "100% done\nnext\n"


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
