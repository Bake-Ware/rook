"""What scripts/install-hub.sh prints when the hub is up: the first thing a
new self-hoster reads, so plain steps first and no jargon in them."""

import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install-hub.sh"


def _done_section() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("# ---- done")
    end = text.rindex("\n}\n\nmain \"$@\"")
    return text[start:end]


def _render(**env) -> str:
    base = {"SECRETS_ENV": "/var/lib/rook/secrets.env", "HUB_ENV": "/var/lib/rook/hub.env",
            "LAYOUT": "merged", "PUBLIC_HOST": "192.0.2.20", "DASHBOARD_PORT": "7005",
            "MCP_PORT": "off", "RELAY_PORT": "7474", "DOMAIN": "http://192.0.2.20:7005",
            "USE_SYSTEMD": "1", "MODE": "system", "ROOK_REPO": "Bake-Ware/rook",
            "DATA_DIR": "/var/lib/rook", "SVC_USER": "rook", "PATH": "/usr/bin:/bin"}
    script = ('read_kv() { echo "correct-horse-7"; }\n' + _done_section())
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**base, **env})
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_hub_done_message_is_plain_steps_first():
    out = _render()
    assert "[rook] Your Rook hub is running." in out
    assert "Password    correct-horse-7" in out
    # The desktop app's hub wizard reads these two labels (installer::parse_summary).
    assert re.search(r"^  Dashboard   http://192\.0\.2\.20:7005$", out, re.M)
    steps = re.findall(r"^  (\d)\. ", out, re.M)
    assert steps == ["1", "2", "3"]
    assert 'Go to "Install a worker" and click "Get a pairing code".' in out
    first = out[:out.index("Connecting AI agents")]
    # Nothing a first-time reader has to decode before the next steps.
    for jargon in ("MCP", "bearer", "relay", "UDP", "--hub", "any username", "systemctl"):
        assert jargon not in first, jargon
    assert "TCP port 7005" in out
    assert "Address       http://192.0.2.20:7005/mcp" in out
    assert "UDP 7474" in out[out.index("Looking after the hub"):]


def test_hub_done_message_without_systemd_and_with_the_old_mcp_port():
    out = _render(USE_SYSTEMD="0", LAYOUT="split", MCP_PORT="8765")
    assert "TCP ports 7005 and 8765" in out
    assert "after a reboot, run this installer again" in out
    assert "http://192.0.2.20:8765/mcp" in out


@pytest.mark.parametrize("mode", ["system", "user"])
def test_hub_done_message_status_command(mode):
    out = _render(MODE=mode)
    want = "sudo systemctl status" if mode == "system" else "systemctl --user status"
    assert want in out
