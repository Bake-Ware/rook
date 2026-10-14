"""The worker one-liners as a first-time user meets them: the Windows script
runs under `iex` in the user's own window (so it must never `exit` there),
reports the elevated run's outcome in the window they started from, keeps a
log, and both installers swap the bundle safely on a re-run."""

import os
import re
import shutil
import subprocess

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


@pytest.mark.asyncio
async def test_windows_script_is_ascii_and_never_exits_under_iex(scripts):
    for name in ("win", "win_login"):
        text = scripts[name]
        assert text.isascii(), name
        code = _code(text)
        # One script block run with `&`: `return` inside it ends the install
        # without touching the user's session, and $ErrorActionPreference etc.
        # stay local to it.
        assert code[0] == "& {" and code[-1] == "} @args", name
        assert code[1] == 'param([string]$ResultFile = "", [string]$Yes = "")'
        # The only `exit` is for a run from a FILE (the elevated relaunch or
        # automation), never reachable under `iex`.
        exits = [ln for ln in code if re.search(r"(^|[{;]\s*)exit(\s|$)", ln)]
        assert exits == ["if ($fromFile) { exit $exitCode }"], exits
        assert "$fromFile = [bool]$PSCommandPath" in text


@pytest.mark.asyncio
async def test_windows_stub_waits_for_elevated_run_and_reports_it(scripts):
    text = scripts["win"]
    stub = text[text.index("if (-not $isAdmin) {"):text.index("# ---- Elevated from here")]
    assert "-Verb RunAs -Wait -PassThru" in stub
    # Paths are quoted for profiles with spaces; the result file goes along.
    assert '"-File", "`"$tmp`"", "`"$res`""' in stub
    assert "Installing the Rook worker. This takes 2-5 minutes (longer if Python must be installed)." in text
    assert stub.count("Write-Host $startMsg") == 1
    assert 'Write-Host "[rook] Done: $($result[\'name\']) joined $joined.' in stub and DONE in stub
    assert "rook status" in stub
    assert "declined" in stub and "Full log: $logPath" in stub
    # Every way out of the stub returns (window stays open) and reports a code.
    assert stub.count("return") == stub.count("$global:LASTEXITCODE =") == 4
    # Retry fetch keeps the pairing code.
    assert f'/worker?band={scripts["code"]}&os=windows' in stub


@pytest.mark.asyncio
async def test_windows_elevated_run_logs_and_holds_failures_open(scripts):
    text = scripts["win"]
    body = text[text.index("# ---- Elevated from here"):]
    assert '$logPath = Join-Path $installDir "install.log"' in text
    assert '$installDir = Join-Path $env:USERPROFILE ".rook-band-worker"' in text
    assert "Start-Transcript -Path $logPath" in body and "Stop-Transcript" in body
    assert body.count("Write-Host $startMsg") == 1
    _ordered(body, *STEPS)
    # The result file and the "Press Enter" pause belong to the elevated child
    # only, and the pause only to a failure.
    child = body[body.index("if ($ResultFile) {"):body.index("if ($fromFile)")]
    assert '"status=$status"' in child and '"log=$logPath"' in child
    assert ('if ($exitCode -ne 0 -and $interactive) { $null = Read-Host "[rook] Press Enter to '
            'close this window" }') in child
    assert '"band=$bandName"' in child
    assert DONE in body and "FAILED (exit code $exitCode). Full log: $logPath" in body


@pytest.mark.asyncio
async def test_windows_rerun_swaps_the_bundle_after_stopping_the_worker(scripts):
    body = scripts["win"]
    assert '$pyzNew = "$pyz.download"' in body
    assert "-OutFile $pyz " not in body and not body.rstrip().endswith("-OutFile $pyz")
    _ordered(body,
             f'Invoke-WebRequest -Uri "http://{LAN}:7005/band-worker-enrollment.pyz" -OutFile $pyzNew',
             'Stop-ScheduledTask -TaskName "RookBandWorker"',
             '$_.CommandLine -like "*band-worker.pyz*"',  # the running-process sweep
             "Move-Item -Path $pyzNew -Destination $pyz -Force",
             '@($pyz, "--install-cli")',
             f"& $vpy $pyz --enroll http://{LAN}:7005 --pair-code {scripts['code']} @enrollExtra",
             "Register-ScheduledTask -TaskName")
    # A failure after the stop restarts the worker that was there before.
    assert 'if ($hadWorker) {' in body


@pytest.mark.asyncio
async def test_expired_pairing_code_can_be_replaced_mid_install(scripts):
    for name in ("win", "unix"):
        assert "Type the new 6-character code to retry" in scripts[name]
        assert f"--enroll http://{LAN}:7005 --pair-code" in scripts[name]
    # Account-login installs have no code to replace.
    for name in ("win_login", "unix_login"):
        assert "--pair-code" not in scripts[name] and "6-character" not in scripts[name]
        assert f"--enroll http://{LAN}:7005" in scripts[name]


@pytest.mark.asyncio
async def test_unix_script_progress_and_atomic_bundle_swap(scripts):
    text = scripts["unix"]
    assert "Installing the Rook worker. This takes 1-3 minutes" in text
    _ordered(text, *STEPS)
    assert "[r00k]" not in text
    # Download beside the live bundle, then rename over it; the service manager
    # (not this script) restarts the worker afterwards.
    assert '-o "$PYZ"' not in text
    _ordered(text,
             f'curl -fsSL http://{LAN}:7005/band-worker-enrollment.pyz -o "$PYZ.download"',
             'mv -f "$PYZ.download" "$PYZ"',
             '"$VPY" "$PYZ" --install-cli',
             "systemctl --user restart rook-band-worker",
             'say "Done: $WORKER_NAME joined ${BAND:-the band}. ' + DONE)
    assert "trap '" in text and "FAILED (exit code $rc)" in text


@pytest.mark.asyncio
async def test_rendered_scripts_parse(scripts):
    for name in ("unix", "unix_login"):
        run = subprocess.run(["bash", "-n"], input=scripts[name], capture_output=True, text=True)
        assert run.returncode == 0, (name, run.stderr)
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is not installed")
    for name in ("win", "win_login"):
        check = ("$e=$null;$t=$null;[void][System.Management.Automation.Language.Parser]::ParseInput("
                 "[Console]::In.ReadToEnd(),[ref]$t,[ref]$e);$e|%{$_.ToString()};exit $e.Count")
        run = subprocess.run([pwsh, "-NoProfile", "-Command", check], input=scripts[name],
                             capture_output=True, text=True, timeout=120)
        assert run.returncode == 0, (name, run.stdout, run.stderr)


# -- unattended installs: piped, --yes, cloud-init, systemd, management tools --------

def _bash_function(text, name):
    """One shell function from a rendered script, by its `name() {` line."""
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start) + 3
    return text[start:end]


@pytest.mark.asyncio
async def test_unix_prompts_only_with_a_terminal(scripts):
    text = scripts["unix"]
    # --yes / ROOK_YES and no openable /dev/tty both mean: never prompt.
    assert "-y|--yes) ROOK_YES=1 ;;" in text and "export ROOK_YES" in text
    assert "ROOK_INSTALLER=1" in text
    assert 'if { true </dev/tty; } 2>/dev/null; then HAVE_TTY=1; fi' in text
    enroll = _bash_function(text, "enroll")
    assert '</dev/tty' in enroll and '--yes </dev/null' in enroll
    assert 'while [ "$RC" -ne 0 ] && [ "$HAVE_TTY" = 1 ]; do' in text
    # Every /dev/tty read sits behind HAVE_TTY.
    for line in text.splitlines():
        if "</dev/tty" in line and "true </dev/tty" not in line:
            assert "HAVE_TTY" in line or "FRESH" in line, line
    # sudo never waits for a password nobody can type.
    assert 'SUDO="sudo -n"' in text and " sudo apt-get" not in text




@pytest.mark.asyncio
async def test_unix_tty_detection_runs_without_a_terminal(scripts, tmp_path):
    text = scripts["unix"]
    start = text.index("for arg in")
    end = text.index("# Root needs no sudo")
    probe = text[start:end].replace("export ROOK_YES", "ROOK_YES") + "echo $HAVE_TTY\n"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    # A new session has no controlling terminal: cloud-init, systemd, CI.
    for args, extra in ([], {}), (["--yes"], {}), ([], {"ROOK_YES": "1"}):
        run = subprocess.run(["setsid", "bash", "-c", probe, "bash", *args], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, env={**env, **extra})
        assert run.stdout.strip() == "0", (args, extra, run.stderr)
        assert run.stderr == ""


@pytest.mark.asyncio
async def test_windows_never_prompts_unattended(scripts):
    text = scripts["win"]
    assert "$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected" in text
    assert "$unattended = (\"$env:ROOK_YES\" -match '^(1|true|yes)$') -or ($Yes -eq \"yes\")" in text
    # Unattended without admin rights stops with an instruction, no UAC prompt.
    _ordered(text, "if (-not $isAdmin -and -not $interactive) {",
             "an unattended install needs an administrator PowerShell", "-Verb RunAs")
    # The elevated relaunch carries the choice, the name and the band (its
    # environment is fresh).
    assert 'if ($unattended) { $relaunch += "yes" } else { $relaunch += "no" }' in text
    assert '$relaunch += "`"name=$workerName`""' in text
    assert '$relaunch += "`"band=$bandArg`""' in text
    assert 'if (-not $interactive) { $enrollExtra += "--yes" }' in text
    assert "while ($rc -ne 0 -and $interactive) {" in text
    reads = [ln for ln in text.splitlines() if "Read-Host" in ln]
    assert len(reads) == 2 and all("$interactive" in ln or "$fresh" in ln for ln in reads), reads
    assert '$env:ROOK_INSTALLER = "1"' in text
    assert "if ($env:ROOK_WORKER_NAME) { $workerName = $env:ROOK_WORKER_NAME }" in text


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


@pytest.mark.asyncio
async def test_hid_setup_skips_a_headless_machine(scripts, tmp_path):
    if not shutil.which("setsid") or not __import__("sys").platform.startswith("linux"):
        pytest.skip("needs Linux")
    script, env, calls = _hid_harness(scripts["unix"], tmp_path)
    if any(__import__("glob").glob(p) for p in ("/tmp/.X11-unix/X*", "/run/user/*/wayland-*")):
        pytest.skip("this test machine has a display")
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert run.returncode == 0, run.stderr
    assert "Skipped keyboard and mouse control: this machine has no screen." in run.stdout
    assert "ROOK_HID=1" in run.stdout
    assert not calls.exists()            # nothing installed, no sudo
    # ROOK_HID=1 sets it up anyway; ROOK_HID=0 skips it even with a screen.
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**env, "ROOK_HID": "1"})
    assert "Setting up keyboard and mouse control" in run.stdout
    assert "modprobe uinput" in calls.read_text()
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**env, "ROOK_HID": "0", "DISPLAY": ":0"})
    assert "Skipped keyboard and mouse control (ROOK_HID=0)." in run.stdout


@pytest.mark.asyncio
async def test_hid_setup_runs_with_a_screen(scripts, tmp_path):
    script, env, calls = _hid_harness(scripts["unix"], tmp_path)
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**env, "WAYLAND_DISPLAY": "wayland-0"})
    assert run.returncode == 0, run.stderr
    assert "Setting up keyboard and mouse control" in run.stdout
    assert "Skipped" not in run.stdout


# -- Windows without Python ----------------------------------------------------------

@pytest.mark.asyncio
async def test_windows_python_is_pinned_verified_and_per_user(scripts):
    text = scripts["win"]
    assert '$pyVersion = "3.12.10"' in text
    assert re.search(r'"amd64" = "[0-9A-F]{64}"', text) and re.search(r'"arm64" = "[0-9A-F]{64}"', text)
    install = text[text.index("function Install-Python {"):text.index("# Run a native command")]
    _ordered(install, "Invoke-WebRequest", "Get-FileHash -Path $setup -Algorithm SHA256",
             "if ($hash -ne $pyHashes[$arch]) {",
             "Remove-Item $setupDir -Recurse -Force -ErrorAction SilentlyContinue\n        Fail ",
             "Start-Process")
    # Downloaded into a new folder of its own, not a guessable %TEMP% name.
    assert '$setupDir = Join-Path $installDir ("python-setup-" + [guid]::NewGuid()' in install
    assert "$env:TEMP" not in install
    assert '"InstallAllUsers=0"' in install and '"PrependPath=0"' in install
    assert "InstallAllUsers=1" not in text and "choco" not in text
    # winget only as a fallback, and for this user only.
    assert '"--scope", "user"' in text
    # One clear instruction when nothing worked.
    assert "Install Python 3.12 from https://www.python.org/downloads/windows/" in text
    # An old Python is not good enough.
    assert "sys.version_info >= (3, 11)" in text


def _ps_functions(text):
    start = text.index("function Test-Python(")
    end = text.index("# Run a native command")
    fail = text[text.index("function Fail("):text.index("New-Item -ItemType Directory -Force -Path $installDir")]
    return text[start:end] + fail


@pytest.mark.asyncio
async def test_windows_python_install_refuses_a_tampered_download(scripts, tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is not installed")
    funcs = _ps_functions(scripts["win"])
    harness = tmp_path / "harness.ps1"
    started = tmp_path / "started.txt"
    harness.write_text(
        '$ErrorActionPreference = "Stop"\n'
        f'$env:LOCALAPPDATA = "{tmp_path}"; $env:TEMP = "{tmp_path}"\n'
        # Stubs: functions win over cmdlets of the same name.
        'function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing) '
        'Set-Content -Path $OutFile -Value "not the real installer" }\n'
        f'function Start-Process {{ param($FilePath, [switch]$Wait, [switch]$PassThru, $ArgumentList) '
        f'Set-Content -Path "{started}" -Value ($ArgumentList -join " "); return @{{ ExitCode = 0 }} }}\n'
        f'$installDir = "{tmp_path / "install"}"\n'
        + funcs +
        'try { Install-Python | Out-Null; Write-Output "INSTALLED" } '
        'catch { Write-Output ("FAILED: " + $_.Exception.Message) }\n'
        # Now pretend the pinned hash is this file's: it runs, for this user.
        '$real = $pyHashes["amd64"]\n'
        'function Invoke-WebRequest { param($Uri, $OutFile, [switch]$UseBasicParsing) '
        'Set-Content -Path $OutFile -Value "stand-in" }\n'
        'Set-Content -Path (Join-Path $env:TEMP "probe.exe") -Value "stand-in"\n'
        '$pyHashes["amd64"] = (Get-FileHash (Join-Path $env:TEMP "probe.exe") -Algorithm SHA256).Hash\n'
        '$env:PROCESSOR_ARCHITECTURE = "AMD64"\n'
        'if (Install-Python) { Write-Output "RAN" }\n')
    run = subprocess.run([pwsh, "-NoProfile", "-File", str(harness)], capture_output=True,
                         text=True, timeout=120)
    out = run.stdout
    assert "FAILED: the downloaded Python installer did not match its known SHA-256" in out, (out, run.stderr)
    assert "RAN" in out
    args = started.read_text()
    assert "/quiet" in args and "InstallAllUsers=0" in args and "PrependPath=0" in args
    assert f"TargetDir=\"{tmp_path}" in args
    # The tampered file was deleted, unused, with the fresh folder it was
    # downloaded into (under the install dir, not a guessable %TEMP% name).
    assert not list(tmp_path.glob("rook-python-*.exe"))
    assert list((tmp_path / "install").iterdir()) == []


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


@pytest.mark.asyncio
async def test_unix_cloud_init_gets_home_and_a_user_service(scripts):
    """cloud-init and systemd units run the installer without HOME/USER and
    without a user service manager; lingering starts one so the worker is a
    real service that survives a reboot."""
    text = scripts["unix"]
    _ordered(text, '[ -n "${HOME:-}" ] || export HOME=', 'mkdir -p "$HOME/.rook-band-worker"')
    assert 'export USER="$(id -un)"' in text
    _ordered(text, "(6/6) Starting the worker", 'loginctl enable-linger "$USER"',
             'export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"',
             "elif command -v systemctl &>/dev/null && systemctl --user show-environment")


# -- root installs: whose worker is it? ---------------------------------------------

def _root_harness(text, tmp_path):
    """The installer's root check with id/getent/runuser/curl stubbed."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    stubs = {
        "id": 'case "$*" in -u) echo "${FAKE_UID:-0}" ;; -un) echo "${FAKE_USER:-root}" ;; '
              '*) /usr/bin/id "$@" ;; esac',
        "getent": 'case "$2" in alice) echo "alice:x:1001:1001::/home/alice:/bin/bash" ;; '
                  '*) exit 2 ;; esac',
        "runuser": f'echo "runuser $*" >> "{calls}"; exit "${{RUNUSER_RC:-0}}"',
        "curl": f'echo "curl $*" >> "{calls}"; while [ $# -gt 0 ]; do '
                '[ "$1" = -o ] && echo "echo child" > "$2"; shift; done; true',
        "chown": f'echo "chown $*" >> "{calls}"',
        "loginctl": f'echo "loginctl $*" >> "{calls}"',
        "apt-get": f'echo "apt-get $*" >> "{calls}"',
    }
    for name, body in stubs.items():
        (bin_dir / name).write_text("#!/bin/bash\n" + body + "\n")
        (bin_dir / name).chmod(0o755)
    start = text.index("# ---- run as root")
    block = text[start:text.index("\ninstall_curl\n", start)]
    script = ("set -e\nLOG=/dev/null\nsay() { echo \"[rook] $*\"; }\n"
              "quietly() { \"$@\" >/dev/null 2>&1; }\ninstall_curl() { :; }\n"
              f"install_python() {{ echo install_python >> \"{calls}\"; }}\nIS_TERMUX=0\n"
              + block + "\necho CONTINUED\n")
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return script, env, calls


@pytest.mark.asyncio
async def test_unix_root_install_is_refused_unless_told_whose_it_is(scripts, tmp_path):
    """cloud-init and `sudo` run the installer as root; a root worker runs
    every band command as root and, with lingering, comes back at every boot."""
    script, env, calls = _root_harness(scripts["unix"], tmp_path)
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    assert run.returncode == 1 and "CONTINUED" not in run.stdout
    assert "Not installed: this runs as root" in run.stdout
    assert "ROOK_USER=<account>" in run.stdout and "ROOK_ALLOW_ROOT=1" in run.stdout
    assert not calls.exists()
    # ROOK_ALLOW_ROOT=1: carries on, loudly.
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**env, "ROOK_ALLOW_ROOT": "1"})
    assert run.returncode == 0 and "CONTINUED" in run.stdout
    assert "WARNING: installing as root" in run.stdout
    # Not root: nothing to decide.
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**env, "FAKE_UID": "1000"})
    assert run.returncode == 0 and "CONTINUED" in run.stdout and "root" not in run.stdout
    # Bad or unknown accounts are refused before anything runs.
    for user, words in (("a b", "not an account name"), ("-x", "not an account name"),
                        ("bob", "no account named bob")):
        run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                             env={**env, "ROOK_USER": user})
        assert run.returncode == 1 and words in run.stdout, (user, run.stdout)
    assert not calls.exists()


@pytest.mark.asyncio
async def test_unix_root_install_for_rook_user_runs_as_that_account(scripts, tmp_path):
    script, env, calls = _root_harness(scripts["unix"], tmp_path)
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**env, "ROOK_USER": "alice", "ROOK_WORKER_NAME": "box1",
                              "ROOK_BAND": "home", "https_proxy": "http://192.0.2.9:3128"})
    assert run.returncode == 0, (run.stdout, run.stderr)
    assert "CONTINUED" not in run.stdout          # the root run ends here
    assert "the worker will run as alice, not as root" in run.stdout
    log = calls.read_text().splitlines()
    fetch = next(ln for ln in log if ln.startswith("curl "))
    assert f"/worker?band={scripts['code']}&os=unix" in fetch   # the same pairing code
    assert "loginctl enable-linger alice" in log
    assert any(ln.startswith("chown -R alice ") for ln in log)
    # Fetched before the slow preparation, run last.
    order = [ln.split()[0] for ln in log]
    assert order.index("curl") < order.index("loginctl") < order.index("runuser")
    runuser = next(ln for ln in log if ln.startswith("runuser "))
    assert runuser.startswith("runuser -u alice -- env -i HOME=/home/alice USER=alice "
                              "LOGNAME=alice ROOK_YES=1 ")
    for part in ("XDG_RUNTIME_DIR=/run/user/1001", "ROOK_WORKER_NAME=box1", "ROOK_BAND=home",
                 "https_proxy=http://192.0.2.9:3128"):
        assert part in runuser, part
    assert runuser.endswith("/install.sh --yes")
    # Its exit code is the install's; the folder is cleaned up.
    assert not os.path.exists(os.path.dirname(runuser.split()[-2]))
    run = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                         env={**env, "ROOK_USER": "alice", "RUNUSER_RC": "7"})
    assert run.returncode == 7


@pytest.mark.asyncio
async def test_unix_self_url_keeps_the_pairing_code(scripts):
    assert f"SELF_URL='http://{LAN}:7005/worker?band={scripts['code']}&os=unix'" in scripts["unix"]
    assert f"SELF_URL='http://{LAN}:7005/worker?login=google&os=unix'" in scripts["unix_login"]


def _probe(text, tmp_path, env_extra, uid="1000", user="pi"):
    """The installer's top (arguments, name check, HOME/USER) with a stubbed id."""
    bin_dir = tmp_path / "pbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "id").write_text(f'#!/bin/sh\ncase "$*" in -u) echo {uid} ;; -un) echo {user} ;; esac\n')
    (bin_dir / "id").chmod(0o755)
    body = text[text.index("for arg in"):text.index("# Root needs no sudo")]
    probe = body + 'echo "HOME=$HOME USER=$USER"\n'
    return subprocess.run(["setsid", "bash", "-c", probe], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True,
                          env={"PATH": f"{bin_dir}:/usr/bin:/bin", **env_extra})


@pytest.mark.asyncio
async def test_unix_home_user_and_worker_name_checks(scripts, tmp_path):
    text = scripts["unix"]
    # `su` without `-` leaves USER naming the old account: USER follows id -un.
    run = _probe(text, tmp_path, {"HOME": "/home/pi", "USER": "someone-else"})
    assert run.returncode == 0 and "HOME=/home/pi USER=pi" in run.stdout
    # No HOME and no passwd entry: /root only for root.
    run = _probe(text, tmp_path, {}, user="nosuchuser-rook")
    assert run.returncode == 1 and "HOME is not set" in run.stdout
    run = _probe(text, tmp_path, {}, uid="0", user="nosuchuser-rook")
    assert run.returncode == 0 and "HOME=/root " in run.stdout
    # A worker name goes into a command line.
    run = _probe(text, tmp_path, {"HOME": "/home/pi", "ROOK_WORKER_NAME": "my box; rm -rf ~"})
    assert run.returncode == 1 and "ROOK_WORKER_NAME may only use" in run.stdout
    run = _probe(text, tmp_path, {"HOME": "/home/pi", "ROOK_WORKER_NAME": "kitchen-pc_2.lan"})
    assert run.returncode == 0
    # A derived name (a phone model, a hostname) is made safe instead.
    line = next(ln for ln in text.splitlines() if ln.startswith('WORKER_NAME="$(printf'))
    run = subprocess.run(["bash", "-c", 'WORKER_NAME="Pixel 7 (Pro)"\n' + line + '\necho "$WORKER_NAME"'],
                         capture_output=True, text=True)
    assert run.stdout.strip() == "Pixel-7--Pro-"


@pytest.mark.asyncio
async def test_windows_refuses_system_and_odd_names(scripts):
    text = scripts["win"]
    assert "as SYSTEM from your management tool" not in text
    assert "SYSTEM shell" not in text
    _ordered(text, 'if ($workerName -notmatch \'^[A-Za-z0-9._-]+$\') {',
             '[Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq "S-1-5-18"',
             "Not installed: this is running as SYSTEM",
             "$isAdmin = ")
    # A derived name is made safe; the band name step can't fail a good install.
    assert "$workerName = $env:COMPUTERNAME -replace '[^A-Za-z0-9._-]', '-'" in text
    band = text[text.index('Start-ScheduledTask -TaskName "RookBandWorker"\n'):]
    _ordered(band, "try {", '$ErrorActionPreference = "Continue"', "--enrolled-band",
             '} catch { $bandName = "" } finally { $ErrorActionPreference = "Stop" }')


@pytest.mark.asyncio
async def test_windows_elevated_child_reads_name_and_band_from_its_arguments(scripts, tmp_path):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is not installed")
    text = scripts["win"]
    head = text[text.index("$workerName = $env:COMPUTERNAME"):text.index("$installDir = ")]
    harness = tmp_path / "args.ps1"
    harness.write_text(
        '$env:COMPUTERNAME = "PC#1"; $env:ROOK_BAND = ""\n'
        '& { param([string]$ResultFile = "", [string]$Yes = "")\n' + head +
        'Write-Output "name=$workerName band=$env:ROOK_BAND yes=$Yes" } @args\n')
    run = subprocess.run([pwsh, "-NoProfile", "-File", str(harness), "res.txt", "no",
                          "name=box1", "band=home lab"], capture_output=True, text=True, timeout=120)
    assert "name=box1 band=home lab yes=no" in run.stdout, (run.stdout, run.stderr)
    run = subprocess.run([pwsh, "-NoProfile", "-File", str(harness)], capture_output=True,
                         text=True, timeout=120)
    assert "name=PC-1 band= yes=" in run.stdout, (run.stdout, run.stderr)
