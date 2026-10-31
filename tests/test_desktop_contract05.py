"""What the desktop app's 0.5 side (desktop/src-tauri/src/hub05.rs,
worker05.rs) relies on from the castling hub, site and worker. The app has no
Python; these checks catch a 0.5 change that would silently break it."""

import re
from pathlib import Path

import yaml

from rook.core2.hub.doors import http
from rook.core2.hub.doors.web import site

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "desktop/src-tauri/src"
HUB05 = (SRC / "hub05.rs").read_text(encoding="utf-8")
WORKER05 = (SRC / "worker05.rs").read_text(encoding="utf-8")
LIB = (SRC / "lib.rs").read_text(encoding="utf-8")


def rust_const(name: str, src: str = HUB05) -> str:
    m = re.search(rf'const {name}: &str = "([^"]*)";', src)
    assert m, f"{name} not found"
    return m.group(1)


def test_door_routes_the_app_uses():
    door = Path(http.__file__).read_text(encoding="utf-8")
    for route in ('("POST", "/call")', '("GET", "/holds")', '("POST", "/enroll/approve")', '"/health"'):
        assert route in door, route
    for path in ('"/call"', '"/holds"', '"/enroll/approve"', "/health", "/login", "/web/api/me", "/logout"):
        assert path in HUB05, path


def test_site_sign_in_cookie_and_csrf():
    assert rust_const("WEB_COOKIE") == site.COOKIE
    src = Path(site.__file__).read_text(encoding="utf-8")
    assert 'path == "/login"' in src and '"X-Rook-CSRF"' in src
    assert '"X-Rook-CSRF"' in HUB05


def test_page_ids_the_panel_links_to_exist():
    plugins = ROOT / "rook/plugins"
    builtin = ROOT / "rook/core2/hub/doors/web/builtin"
    for const in ("APPROVALS_PAGE",):
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
    assert plugin == "agents" and "chat.send" in own
    for cap in ('"agents.chat.send"', '"pages.list"'):
        assert cap in HUB05, cap
    # no workbench lists on 0.5 (castling-attention: the agent opens a page; drift #29)
    for cap in ('"agents.list"', '"agents.agent.list"', '"task.list"', '"task.search"', '"knowledge.search"', '"project.list"'):
        assert cap not in HUB05, cap
    assert rust_const("HOUSE") in (ROOT / "rook/plugins/agents/plugin.yaml").read_text()


def test_dock_markup_for_ask_about_this_page():
    shell = (ROOT / "rook/core2/hub/doors/web/static/shell.html").read_text(encoding="utf-8")
    js = (ROOT / "rook/core2/hub/doors/web/static/shell.js").read_text(encoding="utf-8")
    assert 'id="dock-btn"' in shell and 'id="dock-text"' in shell
    assert '"dock-open"' in js
    m = re.search(r'const OPEN_DOCK_JS_05: &str = "(.*?)";\n', LIB, re.S)
    assert m and "dock-btn" in m.group(1) and "dock-text" in m.group(1) and "dock-open" in m.group(1)


def test_enroll_line_and_worker_cli():
    device = (ROOT / "rook/core2/enroll/device.py").read_text(encoding="utf-8")
    # worker05::user_code reads the code out of this line.
    assert 'say(f"To add {name}, approve code {start[\'user_code\']} on hub' in device
    assert "approve code " in WORKER05
    main = (ROOT / "rook/core2/worker/__main__.py").read_text(encoding="utf-8")
    assert 'add_argument("--config"' in main and 'add_parser("enroll"' in main
    assert re.search(r'"rook\.core2\.worker", "--config"', WORKER05)
    for p in re.search(r"DESKTOP_PLUGINS: \[&str; \d\] = \[(.*?)\]", WORKER05).group(1).split(","):
        assert (ROOT / "rook/plugins" / p.strip().strip('"') / "plugin.yaml").is_file(), p
    # Enrollment writes the anchor beside the config (Layout::enrolled).
    assert 'raw.get("anchor") or "anchor.json"' in device
