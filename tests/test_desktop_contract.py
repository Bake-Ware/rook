"""What the desktop app (desktop/, Rust) relies on from the hub and the hub
installer. The app has no Python; these checks catch a hub-side change that
would silently break it (a renamed route, a reworded agent note it matches,
installer output it parses)."""

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
    assert re.search(r"^  Password    \$WEB_PASS ", script, re.M)
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
