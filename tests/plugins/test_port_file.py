"""file through the worker host, on a temp directory."""

import base64
import os
import sys

import pytest

from port_helpers import ME, PLUGINS, go
from rook.core2.worker.host import CallError, Host


def run(cap, args):
    async def body():
        host = Host(os_name="linux")
        await host.load(PLUGINS / "file")
        try:
            return await host.call(cap if "." in cap else f"file.{cap}", args, ME)
        finally:
            await host.close()
    return go(body)


def test_write_then_read_text_and_bytes(tmp_path):
    f = tmp_path / "a" / "b.txt"
    with pytest.raises(CallError, match=f"not found: {f}"):
        run("write", {"path": str(f), "content": "x"})
    assert run("write", {"path": str(f), "content": "héllo\n", "create_parents": True}) == \
        {"path": str(f), "written": 7}
    run("write", {"path": str(f), "content": "more\n", "append": True})
    assert run("read", {"path": str(f)}) == {"content": "héllo\nmore\n", "size": 12, "encoding": "utf-8"}
    blob = bytes(range(256))
    run("write", {"path": str(f), "content": base64.b64encode(blob).decode(), "encoding": "base64"})
    got = run("read", {"path": str(f), "encoding": "base64"})
    assert base64.b64decode(got["content"]) == blob and got["size"] == 256


def test_read_truncates_and_reports_errors(tmp_path):
    f = tmp_path / "big"
    f.write_bytes(b"0123456789")
    got = run("read", {"path": str(f), "max_bytes": 4})
    assert got == {"content": "0123", "size": 10, "encoding": "utf-8", "truncated": True}
    with pytest.raises(CallError, match=f"not found: {tmp_path / 'nope'}"):
        run("read", {"path": str(tmp_path / "nope")})
    with pytest.raises(CallError, match="is a directory"):
        run("read", {"path": str(tmp_path)})
    with pytest.raises(CallError, match="unknown encoding: klingon"):
        run("read", {"path": str(f), "encoding": "klingon"})
    with pytest.raises(CallError, match="bad args"):
        run("read", {"path": str(f), "encoding": "../x"})


def test_write_refuses_bad_content(tmp_path):
    f = tmp_path / "x"
    with pytest.raises(CallError, match="not base64"):
        run("write", {"path": str(f), "content": "not base64!", "encoding": "base64"})
    with pytest.raises(CallError, match="can't be written as ascii"):
        run("write", {"path": str(f), "content": "é", "encoding": "ascii"})
    assert not f.exists()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs a non-root POSIX user")
def test_permission_denied_is_a_clean_error(tmp_path):
    f = tmp_path / "locked"
    f.write_text("secret")
    f.chmod(0)
    try:
        with pytest.raises(CallError, match=f"permission denied: {f}"):
            run("read", {"path": str(f)})
    finally:
        f.chmod(0o600)


def test_list(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "inner.txt").write_text("i")
    (tmp_path / ".hidden").write_text("h")
    (tmp_path / "top.txt").write_text("top")
    names = sorted(e["name"] for e in run("list", {"path": str(tmp_path)})["entries"])
    assert names == [".hidden", "d", "top.txt"]
    deep = run("list", {"path": str(tmp_path), "recursive": True, "hidden": False})["entries"]
    assert sorted(e["path"] for e in deep) == ["d", os.path.join("d", "inner.txt"), "top.txt"]
    top = next(e for e in deep if e["name"] == "top.txt")
    assert top["type"] == "file" and top["size"] == 3
    assert run("list", {"path": str(tmp_path), "limit": 1})["truncated"] is True
    with pytest.raises(CallError, match="not a directory"):
        run("list", {"path": str(tmp_path / "top.txt")})


def test_search(tmp_path):
    (tmp_path / "a.py").write_text("import os\nTODO: one\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("todo: two\n")
    (tmp_path / "bin.dat").write_bytes(b"\x00TODO")
    got = run("search", {"pattern": "todo", "path": str(tmp_path), "ignore_case": True})
    assert sorted((os.path.basename(m["path"]), m["line"], m["text"]) for m in got["matches"]) == \
        [("a.py", 2, "TODO: one"), ("b.txt", 1, "todo: two")]
    assert "truncated" not in got
    assert len(run("search", {"pattern": "o", "path": str(tmp_path), "glob": "*.py"})["matches"]) == 2
    assert run("search", {"pattern": "o", "path": str(tmp_path), "limit": 1})["truncated"] is True
    with pytest.raises(CallError, match="bad pattern"):
        run("search", {"pattern": "(", "path": str(tmp_path)})



@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_read_refuses_what_isnt_a_regular_file(tmp_path):
    # open() on a FIFO blocks forever: the call times out but its thread never ends
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(CallError, match="not a regular file"):
        run("read", {"path": str(tmp_path / "pipe")})


def test_a_runaway_pattern_never_stalls_the_worker(tmp_path, monkeypatch):
    # (a+)+$ backtracks exponentially and re holds the GIL: run in-process it
    # freezes every thread, the event loop included, past any timeout.
    import asyncio
    import time

    (tmp_path / "x.txt").write_text("a" * 27 + "!\n")      # ~15 s of backtracking

    async def body():
        host = Host(os_name="linux")
        await host.load(PLUGINS / "file")
        mod = next(iter(host.plugins["file"].modules.values()))
        mod.SEARCH_SECONDS = 1
        ticks, start = [], time.monotonic()

        async def beat():
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.05)
        t = asyncio.ensure_future(beat())
        try:
            out = await host.call("file.search", {"pattern": "(a+)+$", "path": str(tmp_path)}, ME)
        finally:
            t.cancel()
            await host.close()
        gaps = [b - a for a, b in zip(ticks, ticks[1:])]
        return out, max(gaps or [0]), time.monotonic() - start
    out, gap, took = go(body)
    assert out["timed_out"] and out["matches"] == []
    assert gap < 0.5 and took < 10


def test_search_works_without_a_runnable_interpreter(tmp_path, monkeypatch):
    # The Android app embeds Python: sys.executable can't be started there.
    (tmp_path / "a.txt").write_text("needle\n")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "no-python"))
    got = run("search", {"pattern": "need", "path": str(tmp_path)})
    assert [m["text"] for m in got["matches"]] == ["needle"]

def test_stat_and_old_name(tmp_path):
    f = tmp_path / "f"
    f.write_text("abc")
    got = run("stat", {"path": str(f)})
    assert got["exists"] is True and got["type"] == "file" and got["size"] == 3
    assert run("stat", {"path": str(tmp_path / "nope")}) == {"exists": False}
    assert run("file.stat", {"path": str(tmp_path)})["type"] == "dir"
    if sys.platform != "win32":
        (tmp_path / "link").symlink_to(tmp_path / "gone")
        assert run("stat", {"path": str(tmp_path / "link")})["type"] == "symlink"
