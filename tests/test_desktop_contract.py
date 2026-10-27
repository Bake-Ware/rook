"""What the desktop app (desktop/, Rust) relies on from the hub and the hub
installer. The app has no Python; these checks catch a hub-side change that
would silently break it (a renamed route, a reworded agent note it matches,
installer output it parses)."""

import json
import re
from pathlib import Path

from rook.hub.plugins import home

ROOT = Path(__file__).resolve().parents[1]
HUB_RS = (ROOT / "desktop/src-tauri/src/hub.rs").read_text(encoding="utf-8")
INSTALLER_RS = (ROOT / "desktop/src-tauri/src/installer.rs").read_text(encoding="utf-8")


def rust_const(name: str, src: str = HUB_RS) -> str:
    m = re.search(rf'const {name}: &str = "([^"]*)";', src)
    assert m, f"{name} not found in hub.rs"
    return m.group(1)


def test_home_agent_notes_match():
    # The app treats these two chat notes from the home agent specially.
    assert rust_const("AGENT_UNAVAILABLE") == home.UNAVAILABLE
    assert home.BUSY.startswith(rust_const("AGENT_BUSY_PREFIX"))


def test_dashboard_routes_the_app_calls_exist():
    bootstrap = (ROOT / "rook/remote/bootstrap.py").read_text(encoding="utf-8")
    account = (ROOT / "rook/remote/account_web.py").read_text(encoding="utf-8")
    for route in ("/api/band/whoami", "/api/band/call", "/api/chat/rooms", "/api/chat/read",
                  "/api/chat/send", "/api/chat/start", "/login"):
        assert f'"{route}"' in bootstrap, route
        assert route in HUB_RS, route
    assert "'/account/login'" in account and "/account/login" in HUB_RS
    # The account login form field and cookies the app reads.
    assert 'name="form_token"' in account
    for cookie in ("rook_account", "rook_session", "rook_login_form"):
        assert cookie in HUB_RS


def test_home_ask_is_still_refused_over_the_band():
    # Why quick ask goes through chat (or MCP) and not /api/band/call: home.ask
    # is a write-risk cap, which the hub refuses over the band. If that ever
    # changes, the app could call it directly.
    src = (ROOT / "rook/hub/plugins/home/__init__.py").read_text(encoding="utf-8")
    assert '@capability("ask", risk="write"' in src


def test_installer_flags_and_summary_lines():
    script = (ROOT / "scripts/install-hub.sh").read_text(encoding="utf-8")
    for flag in ("--user)", "--yes)", "--bind)", "--no-systemd)"):
        assert flag in script, flag
    # installer::parse_summary reads these two lines of the closing banner.
    assert re.search(r"^  Dashboard   \$DOMAIN$", script, re.M)
    assert re.search(r"^  Password    \$WEB_PASS(\s|$)", script, re.M)
    assert "install-hub.sh" in INSTALLER_RS


def test_installer_records_what_the_wizard_reads():
    # installer::find_existing reads install.env (DATA_DIR, SYSTEMD, LAYOUT),
    # hub.env (ROOK_DOMAIN), secrets.env (ROOK_WEB_PASS) and expects the hub
    # service to be rook-hub (merged layout) or rook-dashboard (split).
    script = (ROOT / "scripts/install-hub.sh").read_text(encoding="utf-8")
    for line in ('echo "DATA_DIR=$DATA_DIR"', 'echo "SYSTEMD=$USE_SYSTEMD"', 'echo "LAYOUT=$LAYOUT"'):
        assert line in script, line
    assert 'HUB_ENV="$DATA_DIR/hub.env"' in script and 'SECRETS_ENV="$DATA_DIR/secrets.env"' in script
    assert "ROOK_DOMAIN=" in script and "ROOK_WEB_PASS" in script
    assert 'unit_name() { echo "rook-$1"; }' in script
    assert "echo merged; else echo split" in script
    assert 'USER_APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/rook-hub"' in script
    assert 'SYSTEM_APP_DIR="/opt/rook"' in script
    for s in ('"rook-hub"', '"rook-dashboard"', '"split"', 'SYSTEM_APP_DIR: &str = "/opt/rook"'):
        assert s in INSTALLER_RS, s


# -- desktop 0.2: the tray's dashboard sections, the Rook panel, updates ------------------

SECTIONS = json.loads((ROOT / "desktop/ui/sections.json").read_text(encoding="utf-8"))["sections"]
INDEX = (ROOT / "rook/web/index.html").read_text(encoding="utf-8")


def sidebar_groups() -> list[tuple[str, list[str]]]:
    """The dashboard sidebar as (group id, [#hash, ...]), in order."""
    out = []
    for m in re.finditer(r'<div class="nav-group" data-nav-group="([a-z-]+)">(.*?)</nav>', INDEX, re.S):
        out.append((m.group(1), re.findall(r'<a id="tab-[a-z-]+" href="#([a-z-]+)"', m.group(2))))
    return out


def test_tray_sections_match_the_dashboard_sidebar():
    # The tray's Dashboard menu and the Rook panel list the sidebar's sections
    # and pages; a page moved or added on the hub must move here too.
    groups = sidebar_groups()
    assert [g for g, _ in groups] == ["feed", "work", "intelligence", "trust", "settings"]
    assert [(s["id"], [p["hash"] for p in s["pages"]]) for s in SECTIONS] == groups
    for s in SECTIONS:
        label = re.search(rf'id="nav-label-{s["id"]}"[^>]*>([A-Z ]+)<', INDEX).group(1)
        assert s["title"].upper() == label, s["id"]
        for p in s["pages"]:
            shown = re.search(rf'<a id="tab-{p["hash"]}" href="#{p["hash"]}"[^>]*>([^<]+)<', INDEX).group(1)
            assert p["title"] == shown.replace("&amp;", "&"), p["hash"]


def test_panel_caps_are_read_only_hub_caps():
    # The Rook panel reads through /api/band/call on the hub's node, which runs
    # them as the signed-in person; they must stay read-risk.
    approvals = (ROOT / "rook/hub/plugins/approvals/__init__.py").read_text(encoding="utf-8")
    tasks = (ROOT / "rook/hub/plugins/tasks.py").read_text(encoding="utf-8")
    knowledge = (ROOT / "rook/hub/plugins/knowledge/__init__.py").read_text(encoding="utf-8")
    assert '@capability("list", risk="read"' in approvals and 'NAMESPACE = "approvals"' in approvals
    assert '@capability("read", risk="read"' in tasks and 'NAMESPACE = "task"' in tasks
    assert '@capability("read", risk="read"' in knowledge and 'NAMESPACE = "knowledge"' in knowledge
    for cap in ('"approvals.list"', '"task.read"', '"knowledge.read"'):
        assert cap in HUB_RS, cap
    from rook.hub.relay import KINDS
    for kind in KINDS:          # hub::waiting_page knows where each kind is decided
        assert f'"{kind}" =>' in HUB_RS, kind


def test_panel_routes_and_deep_links_exist():
    work_web = (ROOT / "rook/remote/work_web.py").read_text(encoding="utf-8")
    bootstrap = (ROOT / "rook/remote/bootstrap.py").read_text(encoding="utf-8")
    assert "'/account/work/sessions'" in work_web and "/account/work/sessions" in HUB_RS
    assert '"/updates/desktop.json"' in bootstrap and "/updates/desktop.json" in HUB_RS
    # Links the panel opens in the dashboard window.
    web = ROOT / "rook/web"
    assert "'#work?t='" in (web / "workviews.js").read_text(encoding="utf-8")
    assert "'#knowledge?p='" in (web / "workviews.js").read_text(encoding="utf-8")
    assert "#sessions?key=" in (web / "sessions.js").read_text(encoding="utf-8")
    for link in ("#work?t=", "#knowledge?p=", "#sessions?key="):
        assert link in HUB_RS, link


def test_ask_about_this_page_opens_the_home_dock():
    # lib.rs opens the dashboard's @home dock by clicking its launcher.
    dock = (ROOT / "rook/web/home-dock.js").read_text(encoding="utf-8")
    lib = (ROOT / "desktop/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    assert "'primary hd-launch'" in dock and "class: 'hd-panel'" in dock
    assert ".hd-launch" in lib and ".hd-panel" in lib


def test_published_desktop_files_are_what_the_update_check_offers():
    # tools/desktop_publish.py names the bundles; /updates/desktop.json offers
    # release files starting "rook-desktop" with these endings per platform.
    bootstrap = (ROOT / "rook/remote/bootstrap.py").read_text(encoding="utf-8")
    assert 'n.lower().startswith("rook-desktop")' in bootstrap
    from tools import desktop_publish as dp
    names = [dp.release_name(n, "0.2.0") for n in
             ("Rook_0.2.0_x64-setup.exe", "Rook_0.2.0_amd64.AppImage", "Rook_0.2.0_amd64.deb",
              "Rook_0.2.0_aarch64.dmg")]
    assert names == ["rook-desktop-0.2.0-x64-setup.exe", "rook-desktop-0.2.0-amd64.AppImage",
                     "rook-desktop-0.2.0-amd64.deb", "rook-desktop-0.2.0-aarch64.dmg"]
    for plat, ending in (("windows", "-setup.exe"), ("linux", ".AppImage"), ("linux", ".deb"),
                         ("macos", ".dmg")):
        assert f'"{plat}": (' in bootstrap and f'"{ending}"' in bootstrap


def test_publish_names_and_sums():
    import pytest
    from tools import desktop_publish as dp
    with pytest.raises(ValueError, match="version 0.1.0"):
        dp.release_name("Rook_0.1.0_x64-setup.exe", "0.2.0")
    with pytest.raises(ValueError, match="not a desktop bundle"):
        dp.release_name("Rook_0.2.0_x64.msi.zip", "0.2.0")
    old = "aaa  rook-0.4.0.tar.gz\nbbb  rook-desktop-0.1.0-x64-setup.exe\nccc  rook-worker.apk\n"
    assert dp.merged_sums(old, {"rook-desktop-0.2.0-amd64.deb": "ddd"}) == (
        "aaa  rook-0.4.0.tar.gz\nccc  rook-worker.apk\nddd  rook-desktop-0.2.0-amd64.deb\n")
    conf = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = (ROOT / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    assert dp.app_version() == conf["version"] and f'version = "{conf["version"]}"' in cargo
    good = {"workflowName": "desktop", "event": "push", "headBranch": "master",
            "status": "completed", "conclusion": "success"}
    assert dp.publishable(good) is None
    assert "push to master" in dp.publishable({**good, "event": "pull_request", "headBranch": "rw2/x"})
    assert "green" in dp.publishable({**good, "conclusion": "failure"})
    assert "desktop workflow" in dp.publishable({**good, "workflowName": "CI"})
    # lib.rs names the settings folder for --check by the bundle identifier.
    lib = (ROOT / "desktop/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    assert f'const IDENTIFIER: &str = "{conf["identifier"]}";' in lib
