"""The worker one-liners as a first-time user meets them: the Windows script
runs under `iex` in the user's own window (so it must never `exit` there),
reports the elevated run's outcome in the window they started from, keeps a
log, and both installers swap the bundle safely on a re-run."""

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
STEPS = ["(1/6) Python", "(2/6) venv", "(3/6) dependencies", "(4/6) bundle",
         "(5/6) enrollment", "(6/6) service"]
DONE = "is installed and joining the band. It will appear under Workers in the dashboard within a minute."


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
        assert code[1] == 'param([string]$ResultFile = "")'
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
    assert 'Write-Host "[rook] Done: $($result[\'name\'])' in stub and DONE in stub
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
    assert 'if ($exitCode -ne 0) { $null = Read-Host "[rook] Press Enter to close this window" }' in child
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
             f"& $vpy $pyz --enroll http://{LAN}:7005 --pair-code {scripts['code']}",
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
             'echo "[rook] Done: $WORKER_NAME ' + DONE)
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
