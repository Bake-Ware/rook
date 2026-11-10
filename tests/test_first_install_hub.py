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


def pair(server):
    """Save an enrollment where the worker keeps it."""
    from rook.worker import enroll
    enroll.save({"server": server, "bands": [], "active_band": None})


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


