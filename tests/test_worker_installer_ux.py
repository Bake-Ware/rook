"""The worker one-liners as a first-time user meets them: the Windows script
runs under `iex` in the user's own window (so it must never `exit` there),
reports the elevated run's outcome in the window they started from, keeps a
log, and both installers swap the bundle safely on a re-run."""

import os
import re
import shutil
import subprocess
import sys

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from rook.remote import setup_store
from rook.remote.bootstrap import CombinedServer
from rook.remote.enrollment import EnrollmentStore

LAN = "192.0.2.10"   # TEST-NET-1
# Plain words, no jargon (venv, bundle, enrollment, service) on a first run.
STEPS = ["(1/6) Checking for Python", "(2/6) Preparing a private Python environment",
         "(3/6) Installing the libraries", "(4/6) Downloading the worker",
         "(5/6) Joining the band", "(6/6) Starting the worker in the background"]
DONE = "It appears under Workers in the dashboard within a minute."


@pytest_asyncio.fixture
async def scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("ROOK_SETUP_PATH", str(tmp_path / "setup.json"))
    monkeypatch.setenv("ROOK_ENROLLMENT_DB", str(tmp_path / "enrollment.db"))
    monkeypatch.setenv("ROOK_CHAT_DB", str(tmp_path / "chat.db"))
    monkeypatch.setenv("ROOK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ROOK_PUBLIC_BASE", raising=False)
    monkeypatch.delenv("ROOK_HUB_PUBLIC_WS_URL", raising=False)
    setup_store.save({"band_name": "home", "band_psk": "piano-haste-nugget-drift-mangle",
                      "hub_public": f"{LAN}:8765", "pyz_domain": f"{LAN}:7005"})
    store = EnrollmentStore()
    store.import_config()
    server = CombinedServer(web_user="owner", web_pass="password", port=7005,
                            domain=f"{LAN}:7005", hub_public=f"{LAN}:8765")
    band = next(b for b in store.bands(secrets_visible=True) if b["is_primary"])
    code = store.issue(band["id"])["code"]
    out = {"code": code}
    async with TestClient(TestServer(server._app)) as client:
        for name, query in [("win", f"band={code}&os=windows"), ("unix", f"band={code}&os=unix"),
                            ("win_login", "login=google&os=windows"),
                            ("unix_login", "login=google&os=unix")]:
            response = await client.get("/worker?" + query)
            assert response.status == 200, await response.text()
            out[name] = await response.text()
    return out


def _code(text, comment="#"):
    """Script lines without comments or blanks (a crude strip, enough here)."""
    return [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith(comment)]


def _ordered(text, *needles):
    at = [text.index(n) for n in needles]
    assert at == sorted(at), list(zip(needles, at))


# -- unattended installs: piped, --yes, cloud-init, systemd, management tools --------

def _bash_function(text, name):
    """One shell function from a rendered script, by its `name() {` line."""
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


# -- keyboard/mouse control is skipped where there is no screen --------------------

def _hid_harness(text, tmp_path):
    """setup_hid_linux with every system command stubbed to log its call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for cmd in ("apt-get", "sudo", "ydotool", "ydotoold", "modprobe", "udevadm", "usermod",
                "tee", "loginctl"):
        (bin_dir / cmd).write_text(f'#!/bin/sh\necho "{cmd} $*" >> "{calls}"\n')
        (bin_dir / cmd).chmod(0o755)
    # A server: multi-user boot target, and no user manager to talk to.
    (bin_dir / "systemctl").write_text(
        '#!/bin/sh\ncase "$*" in get-default) echo multi-user.target ;; *) exit 1 ;; esac\n')
    (bin_dir / "systemctl").chmod(0o755)
    script = ('LOG=/dev/null\nSUDO=""\nsay() { echo "[rook] $*"; }\nquietly() { "$@" >/dev/null 2>&1; }\n'
              + _bash_function(text, "hid_headless") + _bash_function(text, "setup_hid_linux")
              + "setup_hid_linux\n")
    return script, {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "USER": "pi"}, calls


# -- Windows without Python ----------------------------------------------------------


def _ps_functions(text):
    start = text.index("function Test-Python(")
    end = text.index("# Run a native command")
    fail = text[text.index("function Fail("):text.index("New-Item -ItemType Directory -Force -Path $installDir")]
    return text[start:end] + fail


def test_unified_and_relay_installers_need_an_openable_terminal(tmp_path):
    """`[ -r /dev/tty ]` is true under cloud-init, where opening it fails and
    `set -e` then killed the install at the first prompt."""
    from rook.remote import bootstrap
    for template in (bootstrap._INSTALL_SCRIPT, bootstrap._HUB_INSTALL_SCRIPT):
        text = template.replace("__BASE__", "http://192.0.2.10:7005")
        assert "-r /dev/tty" not in text
        run = subprocess.run(["bash", "-n"], input=text, capture_output=True, text=True)
        assert run.returncode == 0, run.stderr
    line = next(ln for ln in bootstrap._INSTALL_SCRIPT.splitlines() if ln.startswith("has_tty()"))
    probe = line + "\nif has_tty; then echo yes; else echo no; fi\n"
    run = subprocess.run(["setsid", "bash", "-c", probe], stdin=subprocess.DEVNULL,
                         capture_output=True, text=True)
    assert run.stdout.strip() == "no" and run.stderr == ""


# -- root installs: whose worker is it? ---------------------------------------------

def _root_harness(text, tmp_path):
    """The installer's root check with id/getent/runuser/curl stubbed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    stubs = {
        "id": 'case "$*" in -u) echo "${FAKE_UID:-0}" ;; -un) echo "${FAKE_USER:-root}" ;; '
              '*) /usr/bin/id "$@" ;; esac',
        "getent": 'case "$2" in alice) echo "alice:x:1001:1001::${FAKE_UHOME:-/home/alice}:/bin/bash" ;; '
                  '*) exit 2 ;; esac',
        "runuser": f'echo "runuser $*" >> "{calls}"; exit "${{RUNUSER_RC:-0}}"',
        "curl": f'echo "curl $*" >> "{calls}"; while [ $# -gt 0 ]; do '
                '[ "$1" = -o ] && echo "echo child" > "$2"; shift; done; true',
        "chown": f'echo "chown $*" >> "{calls}"',
        "loginctl": f'echo "loginctl $*" >> "{calls}"',
        "apt-get": f'echo "apt-get $*" >> "{calls}"',
        "systemctl": f'echo "systemctl $*" >> "{calls}"; '
                     f'echo "xdg=$XDG_RUNTIME_DIR dbus=${{DBUS_SESSION_BUS_ADDRESS:-none}}" >> "{calls}.env"; '
                     'exit "${SYSTEMCTL_RC:-0}"',
        # A root worker process: RUNNING = there until killed, STUCK = never
        # stops; PGREP_SEQ = what each call finds ("4194305:-" = the first only, "-" = none).
        "pgrep": f'n=$(cat "{tmp_path}/pgrep.n" 2>/dev/null || echo 0); '
                 f'echo $((n+1)) > "{tmp_path}/pgrep.n"; echo "pgrep $*" >> "{calls}"; '
                 'if [ -n "${PGREP_SEQ:-}" ]; then IFS=: read -ra seq <<< "$PGREP_SEQ"; '
                 '[ "$n" -lt "${#seq[@]}" ] || n=$(( ${#seq[@]} - 1 )); '
                 '[ "${seq[$n]}" != - ] && echo "${seq[$n]}" && exit 0; exit 1; fi; '
                 'if [ -n "${STUCK:-}" ] || { [ -n "${RUNNING:-}" ] && [ "$n" = 0 ]; }; '
                 'then echo 4194305; else exit 1; fi',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text("#!/bin/bash\n" + body + "\n")
        (bin_dir / name).chmod(0o755)
    start = text.index("# ---- run as root")
    block = text[start:text.index("\ninstall_curl\n", start)]
    script = ("set -e\nLOG=/dev/null\nsay() { echo \"[rook] $*\"; }\n"
              "quietly() { \"$@\" >/dev/null 2>&1; }\ninstall_curl() { :; }\n"
              f"install_python() {{ echo install_python >> \"{calls}\"; }}\nIS_TERMUX=0\n"
              f"if [ -n \"${{FAST_SLEEP:-}}\" ]; then sleep() {{ echo \"sleep $*\" >> \"{calls}\"; }}; fi\n"
              + block + "\necho CONTINUED\n")
    (tmp_path / "root").mkdir()
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path / "root")}
    return script, env, calls


def _root_install(home, unit=True):
    (home / ".rook-band-worker").mkdir(parents=True)
    (home / ".rook-band-worker" / "band-worker.pyz").write_text("old")
    if unit:
        units = home / ".config" / "systemd" / "user"
        (units / "default.target.wants").mkdir(parents=True)
        (units / "rook-band-worker.service").write_text("[Service]\n")
        (units / "default.target.wants" / "rook-band-worker.service").symlink_to(
            units / "rook-band-worker.service")


def _probe(text, tmp_path, env_extra, uid="1000", user="pi", getent=True):
    """The installer's top (arguments, name check, HOME/USER) with a stubbed id
    (and, with ``getent=False``, no getent, as on macOS)."""
    bin_dir = tmp_path / "pbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "id").write_text(f'#!/bin/sh\ncase "$*" in -u) echo {uid} ;; -un) echo {user} ;; esac\n')
    (bin_dir / "id").chmod(0o755)
    (bin_dir / "getent").unlink(missing_ok=True)
    if not getent:
        (bin_dir / "getent").write_text("#!/bin/sh\nexit 127\n")
        (bin_dir / "getent").chmod(0o755)
    body = text[text.index("for arg in"):text.index("# Root needs no sudo")]
    probe = body + 'echo "HOME=$HOME USER=$USER"\n'
    return subprocess.run(["setsid", "bash", "-c", probe], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", **env_extra})


