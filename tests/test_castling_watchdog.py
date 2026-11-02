"""The castling watchdog (deploy/castling-hub/watchdog.py): pages on Telegram
when the 0.5 hub stops answering, not on one blip, once per condition, with
reminders and a recovery, and never with the bot token in its output."""
from __future__ import annotations

import http.server
import importlib.util
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("castling_watchdog", REPO / "deploy" / "castling-hub" / "watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)


@pytest.fixture
def hub():
    """A tiny /health server whose answer the test sets."""
    reply = {"code": 200, "body": {"ok": True, "hub": "h1"}}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data = json.dumps(reply["body"]).encode()
            self.send_response(reply["code"])
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", reply
    srv.shutdown()


def test_health_ok_bad_and_down(hub):
    url, reply = hub
    assert wd.health(url, timeout=5) is None
    reply["code"] = 503
    assert wd.health(url, timeout=5) == "HTTP 503"
    reply.update(code=200, body={"ok": False})
    assert "said" in wd.health(url, timeout=5)
    assert wd.health("http://127.0.0.1:1", timeout=5).startswith("URLError")


def test_one_failed_check_does_not_page(hub, monkeypatch):
    url, reply = hub
    monkeypatch.setenv("ROOK_WATCHDOG_URL", url)
    monkeypatch.delenv("ROOK_WATCHDOG_SERVICES", raising=False)
    reply["code"] = 502
    state = {}
    assert wd.check(state) == {}
    assert "not answering" in wd.check(state)["health"]
    reply["code"] = 200
    assert wd.check(state) == {} and state["strikes"] == 0


def test_services_and_memory_on_the_hub(hub, monkeypatch):
    url, _ = hub
    monkeypatch.setenv("ROOK_WATCHDOG_URL", url)
    monkeypatch.setenv("ROOK_WATCHDOG_SERVICES", "rook05-hub, telesthete-hub")
    monkeypatch.setenv("ROOK_WATCHDOG_MIN_MEM_MB", "99999999")
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: SimpleNamespace(
        stdout="failed\n" if argv[-1] == "rook05-hub" else "active\n"))
    found = wd.check({})
    assert found["svc:rook05-hub"] == "rook05-hub is failed"
    assert "svc:telesthete-hub" not in found
    assert found["memory"].startswith("hub memory low")


def test_page_once_remind_recover(monkeypatch):
    sent, clock = [], [1000.0]
    monkeypatch.setattr(wd.time, "time", lambda: clock[0])
    monkeypatch.setenv("ROOK_WATCHDOG_REPEAT_MIN", "60")
    send = lambda text: sent.append(text) or True
    state = {}
    wd.notify(state, {"health": "down"}, "hub", send)
    wd.notify(state, {"health": "down"}, "hub", send)
    assert len(sent) == 1
    clock[0] += 3601
    wd.notify(state, {"health": "down"}, "hub", send)
    assert "still failing after 60 min" in sent[1]
    wd.notify(state, {}, "hub", send)
    assert "recovered: health" in sent[2] and state["active"] == {}
    wd.notify(state, {"x": "boom"}, "hub", lambda t: False)      # a failed send is retried next run
    assert "x" not in state["active"]


def test_telegram_errors_never_show_the_token(monkeypatch, capsys):
    monkeypatch.setenv("ROOK_WATCHDOG_TELEGRAM_TOKEN", "123:very-secret")
    monkeypatch.setenv("ROOK_WATCHDOG_TELEGRAM_CHAT", "42")

    def boom(req, timeout):
        raise OSError(f"failed {req.full_url}")

    monkeypatch.setattr(wd.urllib.request, "urlopen", boom)
    assert wd.telegram("hi") is False
    assert "very-secret" not in capsys.readouterr().err


def test_main_writes_state_and_pages(hub, monkeypatch, tmp_path):
    url, reply = hub
    monkeypatch.setenv("ROOK_WATCHDOG_URL", url)
    monkeypatch.delenv("ROOK_WATCHDOG_SERVICES", raising=False)
    sent = []
    monkeypatch.setattr(wd, "telegram", lambda text: sent.append(text) or True)
    reply["code"] = 500
    state = tmp_path / "state.json"
    for _ in range(2):
        assert wd.main(["--state", str(state), "--where", "test"]) == 0
    assert len(sent) == 1 and sent[0].startswith("⚠️ Castling [test]")
    assert json.loads(state.read_text())["strikes"] == 2


def test_install_script_parses():
    subprocess.run(["bash", "-n", str(REPO / "deploy" / "castling-hub" / "watchdog-install.sh")], check=True, timeout=10)
