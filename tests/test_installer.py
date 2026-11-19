"""The installer, deploy/install/install.sh (brief HA4), run for real without systemd: its arguments,
the files and units it writes, a real hub it starts and a real worker it joins with a join token, and
an uninstall that removes exactly what it made.

Stand-ins on PATH take the place of what a test cannot use: `systemctl` runs a unit's ExecStart (with its
Environment and WorkingDirectory) as a background process and stops it again; `journalctl` prints that
process's output; `claude` answers --version, `auth status` and the probe turn; `uv` makes a "venv" whose
python is this test's own (Rook's packages are already there). curl, git, tar and the rest are real.
HOME and XDG_CONFIG_HOME point into the test's folder, so nothing outside it is touched."""

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import textwrap
import urllib.request

import pytest

from test_first_run import ROOT

INSTALL = ROOT / "deploy" / "install" / "install.sh"
MARK = "# Made by Rook's installer (deploy/install/install.sh) for {}; --uninstall removes it."

SYSTEMCTL = r'''#!/usr/bin/env bash
# A stand-in for systemctl --user: units run as background processes; what was asked is logged.
units=${XDG_CONFIG_HOME}/systemd/user state=$STANDIN_STATE
echo "systemctl $*" >> "$state/calls"
[ "$1" = --user ] && shift
cmd=$1; shift
quiet=0; [ "${1:-}" = --quiet ] && { quiet=1; shift; }
case $cmd in
  daemon-reload|enable) exit 0 ;;
  start)
    for u in "$@"; do
      f=$units/$u.service
      wd=$(sed -n 's/^WorkingDirectory=//p' "$f")
      exe=$(sed -n 's/^ExecStart=//p' "$f")
      envs=(); while IFS= read -r line; do envs+=("$line"); done < <(sed -n 's/^Environment=//p' "$f")
      (cd "$wd" && exec env "${envs[@]}" $exe) >> "$state/$u.out" 2>&1 &
      echo $! > "$state/$u.pid"
    done ;;
  disable|stop)
    [ "${1:-}" = --now ] && shift
    for u in "$@"; do
      [ -f "$state/$u.pid" ] && kill "$(cat "$state/$u.pid")" 2>/dev/null
      rm -f "$state/$u.pid"
    done; exit 0 ;;
  is-failed)
    u=$1; [ -f "$state/$u.pid" ] && ! kill -0 "$(cat "$state/$u.pid")" 2>/dev/null ;;
  *) exit 0 ;;
esac
'''

JOURNALCTL = r'''#!/usr/bin/env bash
# A stand-in for journalctl --user -u UNIT ...: the unit's output so far.
while [ $# -gt 0 ]; do [ "$1" = -u ] && { cat "$STANDIN_STATE/$2.out" 2>/dev/null; exit 0; }; shift; done
'''

CLAUDE = r'''#!/usr/bin/env bash
# A stand-in for Claude Code: signed in, and it answers the probe turn.
echo "claude $*" >> "$STANDIN_STATE/calls"
case "$*" in
  --version) echo "9.9.9 (Claude Code)" ;;
  "auth status") echo '{"loggedIn": true}' ;;
  -p*) cat > /dev/null; echo '{"type":"result","is_error":false,"result":"ok"}' ;;
esac
'''

UV = r'''#!/usr/bin/env bash
# A stand-in for uv: "uv venv ... DIR" makes DIR/bin/python run this test's python; "uv pip" does nothing.
echo "uv $*" >> "$STANDIN_STATE/calls"
if [ "$1" = venv ]; then
  dir=${!#}; mkdir -p "$dir/bin"
  printf '#!/bin/sh\nexec %s "$@"\n' "$STANDIN_PYTHON" > "$dir/bin/python"; chmod +x "$dir/bin/python"
fi
'''


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Box:
    """A throwaway HOME with the stand-ins on PATH."""

    def __init__(self, tmp):
        self.home, self.bin, self.state = tmp / "home", tmp / "bin", tmp / "state"
        for d in (self.home, self.bin, self.state):
            d.mkdir()
        for name, text in (("systemctl", SYSTEMCTL), ("journalctl", JOURNALCTL), ("claude", CLAUDE), ("uv", UV)):
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.units = self.home / ".config" / "systemd" / "user"
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        PATH=f"{self.bin}:{os.environ['PATH']}", STANDIN_STATE=str(self.state),
                        STANDIN_PYTHON=sys.executable)
        for name in ("DISPLAY", "WAYLAND_DISPLAY", "PYTHONPATH"):
            self.env.pop(name, None)

    def run(self, *args):
        return subprocess.run(["bash", str(INSTALL), *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)

    def stop_all(self):
        for pid in self.state.glob("*.pid"):
            try:
                os.kill(int(pid.read_text()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    def calls(self):
        path = self.state / "calls"
        return path.read_text().splitlines() if path.exists() else []


@pytest.fixture
def box(tmp_path):
    made = Box(tmp_path)
    yield made
    made.stop_all()


def test_arguments(box):
    shown = box.run("--help")
    assert shown.returncode == 0 and "--uninstall" in shown.stdout and "--http-port" in shown.stdout
    for args, words in ((["--frobnicate"], "unknown option --frobnicate"),
                        (["--name", "Bad Name"], "--name is lowercase letters, digits and -"),
                        (["--http-port", "eighty"], "--http-port is a port number"),
                        (["--dir", "relative/place"], "--dir is a full path")):
        refused = box.run(*args)
        assert refused.returncode == 2 and words in refused.stderr, (args, refused.stderr)
    bad_ai = box.run("--dir", str(box.home / "r"), "--http-port", str(free_port()), "--ai", "telepathy", "--no-browser")
    assert bad_ai.returncode == 1 and "--ai is one of claude, codex, gemini, api, local, none" in bad_ai.stderr
    nothing = box.run("--dir", str(box.home / "none-here"), "--open")
    assert nothing.returncode == 1 and "nothing installed in" in nothing.stderr


def test_install_join_and_uninstall(box):
    root, port = box.home / "rook", free_port()
    url = f"http://127.0.0.1:{port}"
    done = box.run("--dir", str(root), "--name", "rooktest", "--http-port", str(port), "--ai", "claude", "--no-browser")
    assert done.returncode == 0, (done.stdout, done.stderr)
    out = done.stdout
    for line in ("It works: Claude Code answered \"ok\".", "Code: this checkout, commit ",
                 "The hub is running.", "This machine is on the hub, and your house agent's engine (Claude Code) runs on it.",
                 "Rook starts when you log in.", f"  {url}\n"):
        assert line in out, (line, out)

    # The folder it made, the facts, and the two units with its mark.
    assert {p.name for p in root.iterdir()} == {"src", "venv", "hub", "vault.key", "worker", "install.json", "install.log"}
    facts = json.loads((root / "install.json").read_text())
    assert facts["units"] == ["rooktest-hub", "rooktest-worker"] and facts["ai"]["choice"] == "claude"
    assert facts["ai"]["claude"] == str(box.bin / "claude") and facts["http"] == f"0.0.0.0:{port} (every interface)"
    hub_unit, worker_unit = (box.units / f"rooktest-{u}.service" for u in ("hub", "worker"))
    src, py = root / "src", root / "venv" / "bin" / "python"
    assert hub_unit.read_text().splitlines()[0] == MARK.format(root)
    assert f"ExecStart={py} -m rook_hub --data {root / 'hub'} --vault-key {root / 'vault.key'} --host 0.0.0.0 " \
           f"--port {port}\n" in hub_unit.read_text()
    assert f"Environment=PYTHONPATH={src}/protocol/python:{src}/hub\n" in hub_unit.read_text()
    assert f"ExecStart={py} -m rook_worker {url} --state {root / 'worker'}\n" in worker_unit.read_text()
    assert "Restart=always" in worker_unit.read_text() and "After=rooktest-hub.service" in worker_unit.read_text()

    # A real hub, and a real worker joined with the join token: linked, owned by the system owner.
    with urllib.request.urlopen(url + "/api/setup") as answer:
        assert json.load(answer)["needed"] is True                      # nobody has claimed it
    worker_out = (box.state / "rooktest-worker.out").read_text()
    assert "Approved before as " in worker_out and "Linked to " in worker_out
    assert "--code" not in hub_unit.read_text() + worker_unit.read_text()   # the token is in no file
    calls = box.calls()
    assert "systemctl --user start rooktest-hub" in calls and "systemctl --user start rooktest-worker" in calls
    assert "systemctl --user enable rooktest-hub rooktest-worker" in calls
    assert any(c.startswith("claude -p --output-format json --permission-mode auto") for c in calls)

    # Once installed, a second install in the same folder is refused; --open opens it again.
    again = box.run("--dir", str(root), "--name", "rooktest", "--http-port", str(free_port()), "--ai", "claude")
    assert again.returncode == 1 and "Rook is installed in" in again.stderr
    opened = box.run("--dir", str(root), "--open", "--no-browser")
    assert opened.returncode == 0 and url in opened.stdout

    # A unit of the same kind that it did not make is left alone by the uninstall.
    foreign = box.units / "rooktest-other.service"
    foreign.write_text("[Service]\nExecStart=/bin/true\n")

    gone = box.run("--dir", str(root), "--name", "rooktest", "--uninstall")
    assert gone.returncode == 0 and "its units and" in gone.stdout
    assert not root.exists() and not hub_unit.exists() and not worker_unit.exists() and foreign.exists()
    assert "systemctl --user disable --now rooktest-worker" in box.calls()
    assert not list(box.state.glob("*.pid"))                            # both stopped


def test_no_engine_yet_and_uninstall_takes_only_its_own(box):
    root, port = box.home / "plain", free_port()
    done = box.run("--dir", str(root), "--name", "rookplain", "--http-port", str(port), "--ai", "codex",
                   "--no-boot", "--no-browser")
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "Setting Rook up with that is coming soon." in done.stdout and "This machine is on the hub." in done.stdout
    assert json.loads((root / "install.json").read_text())["ai"]["choice"] == "none"
    calls = box.calls()
    assert not any(c.startswith("claude") for c in calls) and not any("enable" in c for c in calls)
    assert box.run("--dir", str(root), "--name", "rookplain", "--uninstall").returncode == 0 and not root.exists()

    # A folder this installer did not make is never removed, nor are units without its mark.
    other = box.home / "someone-elses"
    other.mkdir()
    (other / "precious").write_text("keep me")
    unit = box.units / "rookplain-hub.service"
    unit.write_text(textwrap.dedent("""\
        [Service]
        ExecStart=/bin/true
        """))
    refused = box.run("--dir", str(other), "--name", "rookplain", "--uninstall")
    assert refused.returncode == 1 and "was not made by this installer" in refused.stderr
    assert (other / "precious").exists() and unit.exists()


def test_join_token_command(tmp_path):
    """`python -m rook_hub join-token` (added for the installer): one join token on a new hub, owned by
    the hub's system owner, printed and nothing else; the data directory is made as a start makes it."""
    from rook_hub import Hub
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(str(ROOT / p) for p in ("protocol/python", "hub")))
    made = subprocess.run([sys.executable, "-m", "rook_hub", "join-token", "--data", str(tmp_path / "hub"),
                           "--vault-key", str(tmp_path / "vault.key")], env=env, capture_output=True, text=True)
    assert made.returncode == 0 and made.stderr == "" and made.stdout.count("\n") == 1, made
    hub = Hub(tmp_path / "hub", vault_key_file=tmp_path / "vault.key")
    try:
        token = hub.auth.verify(made.stdout.strip(), "join")
        assert token["owner"] == hub.auth.system and token["purpose"] == "join" and token["expires_at"]
    finally:
        hub.close()
