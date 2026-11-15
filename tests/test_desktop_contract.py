"""What the desktop app (desktop/src-tauri/src/hub.rs, worker.rs, lib.rs)
relies on from the hub, site and worker crate. The app has no Python;
these checks catch a 0.5 change that would silently break it."""

import re
from pathlib import Path

import yaml

from rook.core2.hub.doors import http
from rook.core2.hub.doors.web import site

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "desktop/src-tauri/src"
HUB = (SRC / "hub.rs").read_text(encoding="utf-8")
WORKER = (SRC / "worker.rs").read_text(encoding="utf-8")
ENROLL = (ROOT / "worker/src/enroll.rs").read_text(encoding="utf-8")
LIB = (SRC / "lib.rs").read_text(encoding="utf-8")


def rust_const(name: str, src: str = HUB) -> str:
    m = re.search(rf'const {name}: &str = "([^"]*)";', src)
    assert m, f"{name} not found"
    return m.group(1)


def test_door_routes_the_app_uses():
    door = Path(http.__file__).read_text(encoding="utf-8")
    for route in ('("POST", "/call")', '("GET", "/holds")', '("POST", "/enroll/code")', '"/health"'):
        assert route in door, route
    for path in ('"/call"', '"/holds"', '"/enroll/code"', "/health", "/login", "/web/api/me", "/logout"):
        assert path in HUB, path


def test_site_sign_in_cookie_and_csrf():
    assert rust_const("WEB_COOKIE") == site.COOKIE
    src = Path(site.__file__).read_text(encoding="utf-8")
    assert 'path == "/login"' in src and '"X-Rook-CSRF"' in src
    assert '"X-Rook-CSRF"' in HUB


def test_page_ids_the_panel_links_to_exist():
    plugins = ROOT / "rook/plugins"
    builtin = ROOT / "rook/core2/hub/doors/web/builtin"
    for const in ("JOURNAL_PAGE",):
        plugin, page = rust_const(const).removeprefix("/#/").split("/")
        manifests = [*plugins.glob("*/plugin.yaml"), *builtin.glob("*/plugin.yaml")]
        found = [m for m in manifests if (yaml.safe_load(m.read_text()) or {}).get("plugin") == plugin]
        assert found, f"{const}: no plugin {plugin}"
        assert page in (yaml.safe_load(found[0].read_text()).get("pages") or {}), f"{const}: no page {page}"
    shell = (ROOT / "rook/core2/hub/doors/web/static/shell.js").read_text(encoding="utf-8")
    assert 'location.hash = "#/" + p.page' in shell      # the site opens #/<page id>


def test_caps_the_app_calls_exist():
    def caps(name):
        m = yaml.safe_load((ROOT / "rook/plugins" / name / "plugin.yaml").read_text())
        return m["plugin"], set(m.get("caps") or {}), set(m.get("records") or {})
    plugin, own, _ = caps("agents")
    assert plugin == "agent" and "chat.send" in own
    for cap in ('"agent.chat.send"', '"page.list"'):
        assert cap in HUB, cap
    # no workbench lists on 0.5 (castling-attention: the agent opens a page; drift #29)
    for cap in ('"agent.list"', '"agent.list"', '"task.list"', '"task.search"', '"knowledge.search"', '"project.list"'):
        assert cap not in HUB, cap
    assert rust_const("HOUSE") in (ROOT / "rook/plugins/agents/plugin.yaml").read_text()


def test_dock_markup_for_ask_about_this_page():
    js = (ROOT / "rook/plugins/agents/shell/dock.js").read_text(encoding="utf-8")
    assert 'id="dock-btn"' in js and 'id="dock-text"' in js
    assert '"dock-open"' in js
    m = re.search(r'const OPEN_DOCK_JS: &str = "(.*?)";\n', LIB, re.S)
    assert m and "dock-btn" in m.group(1) and "dock-text" in m.group(1) and "dock-open" in m.group(1)


def test_enroll_code_and_the_worker_crate():
    device = (ROOT / "rook/core2/enroll/device.py").read_text(encoding="utf-8")
    # "Add this computer" hands the code from POST /enroll/code (its answer's "code")
    # to the worker crate's enroll, which claims it.
    assert "rook_worker::enroll::enroll(" in WORKER and "Some(code)" in WORKER
    assert '"/enroll/claim"' in ENROLL and 'json!({"code": code, "name": name})' in ENROLL
    assert '"code": code' in (ROOT / "rook/core2/enroll/hub.py").read_text(encoding="utf-8")
    assert '"/enroll/claim"' in Path(http.__file__).read_text(encoding="utf-8")
    # Enrollment writes the anchor beside the config (Layout::enrolled), in both workers.
    assert 'raw.get("anchor") or "anchor.json"' in device
    assert 'unwrap_or("anchor.json")' in ENROLL and 'join("anchor.json")' in WORKER
    assert "python" not in WORKER.lower() and "python" not in (SRC / "lib.rs").read_text().lower()
