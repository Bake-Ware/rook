import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
import urllib.error

import pytest

from rook.cli import band_tui as tui


@pytest.fixture
def quiet_curses(monkeypatch):
    for name in ("curs_set", "start_color", "use_default_colors", "init_pair", "set_escdelay"):
        monkeypatch.setattr(tui.curses, name, lambda *args: None)


class Screen:
    def __init__(self, keys):
        self.keys = iter(keys)

    def getmaxyx(self): return (35, 140)
    def nodelay(self, value): pass
    def timeout(self, value): pass
    def getch(self): return next(self.keys)


def test_navigation_and_quit_while_server_read_is_blocked(quiet_curses):
    release = threading.Event()
    started = threading.Event()
    def slow():
        started.set()
        release.wait(5)
        return {"workers": [], "chats": []}
    band = SimpleNamespace(overview=slow, call=Mock(side_effect=AssertionError("No per-worker calls from the UI")))
    ui = tui.UI(band, "test")
    ui._all_rows = ui.rows = [{"worker_id": "one"}, {"worker_id": "two"}]
    positions = []
    def draw(scr):
        assert started.wait(.5)
        positions.append(ui.sel)
    ui.draw = draw
    before = time.monotonic()
    try:
        ui.loop(Screen([ord("j"), ord("k"), ord("q")]))
        assert time.monotonic() - before < .5
        assert positions == [0, 1, 0]
        band.call.assert_not_called()
    finally:
        release.set()


def test_chat_typing_and_escape_while_poll_is_blocked(quiet_curses):
    release = threading.Event()
    def slow(*args, **kwargs):
        release.wait(5)
        return {"ok": True, "result": {"messages": []}}
    ui = tui.UI(SimpleNamespace(overview=lambda: {"workers": [], "chats": []}, call=slow), "test")
    typed = []
    ui._chat_draw = lambda scr, active, side, sel, messages, inp: typed.append(inp)
    before = time.monotonic()
    try:
        ui._chat_ui(Screen([*map(ord, "hello"), 27]), "one", "one", "room")
        assert typed[-1] == "hello" and time.monotonic() - before < .5
    finally:
        ui.close()
        release.set()


def test_single_overview_request_and_legacy_fallback(monkeypatch):
    band = tui.BandHTTP("https://example.com", "test", "test")
    request = Mock(return_value={"workers": [{"worker_id": "one", "last_seen_age_secs": 1}], "chats": []})
    monkeypatch.setattr(band, "_req", request)
    assert band.overview()["workers"][0]["age"] == 1
    request.assert_called_once_with("/api/band/overview", timeout=8)
    request.reset_mock()
    request.side_effect = [urllib.error.HTTPError("url", 404, "missing", {}, None), [], []]
    assert band.overview() == {"workers": [], "chats": []}
    assert band.overview() == {"workers": [], "chats": []}
    assert [args.args[0] for args in request.call_args_list] == ["/api/band/overview", "/api/band/workers", "/api/band/workers"]


def test_refresh_preserves_selection_and_last_success_on_error():
    from concurrent.futures import Future
    ui = tui.UI(SimpleNamespace(overview=Mock()), "test")
    rows = [{"worker_id": "one", "name": "one"}, {"worker_id": "two", "name": "two"}]
    ui._all_rows = ui.rows = rows
    ui.sel = 1
    future = Future(); future.set_result({"workers": list(reversed(rows)), "chats": []})
    ui._roster_reads.jobs["roster"] = future
    ui._tick()
    assert ui.cur()["worker_id"] == "two" and ui.sel == 0
    failed = Future(); failed.set_exception(TimeoutError())
    ui._roster_reads.jobs["roster"] = failed
    ui._tick()
    assert ui.cur()["worker_id"] == "two" and "cached" in ui.status
    ui.close()


def test_discarded_room_reply_cannot_replace_new_room():
    reads = tui._Background(2)
    release = threading.Event()
    started = threading.Event()
    def old_room():
        started.set()
        release.wait(5)
        return "old room"
    try:
        reads.submit("poll", old_room)
        assert started.wait(.5)
        reads.discard("poll")
        reads.submit("poll", lambda: "new room")
        deadline = time.monotonic() + .5
        result = None
        while result is None and time.monotonic() < deadline:
            result = reads.take("poll")
            time.sleep(.001)
        assert result.result() == "new room"
        release.set()
        assert reads.take("poll") is None
    finally:
        reads.close()
        release.set()
