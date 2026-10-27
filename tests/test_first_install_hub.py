"""A fresh install never dials 127.0.0.1 unless a hub runs on this machine.

The worker (``rook worker`` / the bundle) and the terminal dashboard
(``rook``) used to fall back to 127.0.0.1:7474 and http://127.0.0.1:7005, so
an unpaired machine quietly tried to reach a hub that wasn't there.
"""
import http.server
import json
import sys
import threading

import pytest

from rook.cli import band_tui
from rook.worker import cli, desktop


class Dialled(Exception):
    pass


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    """A machine with no enrollment, no pushed config and no ROOK_HUB."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ROOK_WORKER_HOME", str(tmp_path / "state"))
    for name in ("ROOK_HUB", "ROOK_BAND_PSK", "ROOK_HUB_WS_URL", "ROOK_ENROLLMENT_FILE",
                 "ROOK_WEB_URL", "ROOK_WEB_USER", "ROOK_WEB_PASS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(desktop, "ensure_launcher", lambda: None)
    dialled = []

    def transport(**kw):
        dialled.append((kw["hub_host"], kw["hub_port"]))
        raise Dialled

    from rook.worker.transports import telesthete_hub
    monkeypatch.setattr(telesthete_hub, "TelestheteHubTransport", transport)
    return dialled


def run_worker(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["rook-worker", *argv])
    cli.main()


def test_unpaired_worker_without_a_local_hub_says_how_to_join(fresh, monkeypatch, capsys):
    monkeypatch.setattr(band_tui, "local_hub", lambda timeout=1.0: False)
    with pytest.raises(SystemExit) as exit_:
        run_worker(monkeypatch, "--psk", "k")
    assert exit_.value.code == 2 and fresh == []
    err = capsys.readouterr().err
    assert "rook pair <code> --hub http://<hub address>:7005" in err
    assert "127.0.0.1" not in err


def test_no_psk_no_hub_is_the_same_plain_message(fresh, monkeypatch, capsys):
    monkeypatch.setattr(band_tui, "local_hub", lambda timeout=1.0: False)
    with pytest.raises(SystemExit) as exit_:
        run_worker(monkeypatch)
    assert exit_.value.code == 2 and fresh == []
    assert "rook pair" in capsys.readouterr().err


def test_hub_on_this_machine_is_used(fresh, monkeypatch):
    monkeypatch.setattr(band_tui, "local_hub", lambda timeout=1.0: True)
    with pytest.raises(Dialled):
        run_worker(monkeypatch, "--psk", "k")
    assert fresh == [("127.0.0.1", 7474)]


@pytest.mark.parametrize("given", ["flag", "env"])
def test_named_hub_never_probes_this_machine(fresh, monkeypatch, given):
    def probe(timeout=1.0):
        raise AssertionError("probed 127.0.0.1 though a hub was named")
    monkeypatch.setattr(band_tui, "local_hub", probe)
    argv = ["--psk", "k"]
    if given == "flag":
        argv += ["--hub", "hub.example.com:7474"]
    else:
        monkeypatch.setenv("ROOK_HUB", "hub.example.com:7474")
    with pytest.raises(Dialled):
        run_worker(monkeypatch, *argv)
    assert fresh == [("hub.example.com", 7474)]


def test_ws_url_names_the_hub(fresh, monkeypatch):
    monkeypatch.setattr(band_tui, "local_hub", lambda timeout=1.0: pytest.fail("probed"))
    with pytest.raises(Dialled):
        run_worker(monkeypatch, "--psk", "k", "--ws-url", "wss://relay.example.com/band")
    assert fresh == [("relay.example.com", 443)]


def test_given_hub_and_psk_are_not_reported_as_overridden(fresh, monkeypatch, caplog):
    with pytest.raises(Dialled):
        run_worker(monkeypatch, "--psk", "k", "--hub", "hub.example.com:7474")
    assert "from saved state" not in caplog.text


# -- the terminal dashboard ---------------------------------------------------------

@pytest.fixture
def no_tty_answer(monkeypatch):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    return asked


def test_dashboard_uses_the_paired_hub_without_probing(fresh, monkeypatch, no_tty_answer):
    from rook.worker import enroll
    monkeypatch.setattr(enroll, "load", lambda: {"server": "https://rook.example.com"})
    monkeypatch.setattr(band_tui, "local_hub", lambda timeout=1.0: pytest.fail("probed"))
    assert band_tui.hub_url(None, {}) == "https://rook.example.com"
    assert no_tty_answer == []


def test_dashboard_prefers_given_and_saved_urls(fresh, monkeypatch):
    from rook.worker import enroll
    monkeypatch.setattr(enroll, "load", lambda: {"server": "https://paired.example.com"})
    assert band_tui.hub_url("http://given:7005", {"url": "http://saved"}) == "http://given:7005"
    assert band_tui.hub_url(None, {"url": "http://saved"}) == "http://saved"
    monkeypatch.setenv("ROOK_WEB_URL", "http://env")
    assert band_tui.hub_url(None, {"url": "http://saved"}) == "http://env"


def test_dashboard_uses_a_hub_on_this_machine(fresh, monkeypatch, no_tty_answer):
    monkeypatch.setattr(band_tui, "local_hub", lambda timeout=1.0: True)
    assert band_tui.hub_url(None, {}) == band_tui.LOCAL_DASHBOARD
    assert no_tty_answer == []


@pytest.mark.parametrize("typed, url", [
    ("192.168.1.10:7005", "http://192.168.1.10:7005"),
    ("rook.example.com", "https://rook.example.com"),
    ("http://rook.example.com:7005", "http://rook.example.com:7005"),
    ("", ""),
])
def test_dashboard_asks_when_nothing_is_known(fresh, monkeypatch, typed, url):
    monkeypatch.setattr(band_tui, "local_hub", lambda timeout=1.0: False)
    monkeypatch.setattr("builtins.input", lambda prompt="": typed)
    assert band_tui.hub_url(None, {}) == url


# -- finding a hub on this machine --------------------------------------------------

def serve(body, status=200):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("body, status, found", [
    (json.dumps({"status": "ok", "band_workers": 0}).encode(), 200, True),
    (json.dumps({"status": "ok"}).encode(), 200, False),   # some other service
    (b"<html>hello</html>", 200, False),
    (b"", 500, False),
])
def test_local_hub_needs_a_rook_health_answer(monkeypatch, body, status, found):
    server = serve(body, status)
    try:
        monkeypatch.setattr(band_tui, "LOCAL_DASHBOARD",
                            f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setenv("http_proxy", "http://192.0.2.1:9")  # must be bypassed
        assert band_tui.local_hub(timeout=2.0) is found
    finally:
        server.shutdown()


def test_local_hub_false_when_nothing_listens(monkeypatch):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setattr(band_tui, "LOCAL_DASHBOARD", f"http://127.0.0.1:{port}")
    assert band_tui.local_hub(timeout=1.0) is False
