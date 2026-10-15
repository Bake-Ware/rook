"""Hub dashboard: web UI, worker installers, setup wizard and band controller."""

from __future__ import annotations

import asyncio
import hmac
import html as _html
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path

from aiohttp import web

from ..core.throttle import client_keys, message as throttle_message, shared as shared_throttle

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def _update_note(row: dict, pending: dict | None) -> str:
    """The Workers page's "too old" message for a row (rook/hub/update_feed.py)."""
    try:
        from ..hub.update_feed import worker_note
        return worker_note(row, pending=pending)
    except Exception:
        log.debug("update note failed", exc_info=True)
        return ""


def _versioned_assets(html: str, web_dir) -> str:
    """Append ?v=<content version> to dashboard asset URLs. Cloudflare rewrites
    asset caching to hours, so without this a deploy keeps serving browsers the
    old scripts (e.g. a new page shell calling a function the cached module
    lacks). The version changes whenever any web file changes."""
    import hashlib, re as _re
    h = hashlib.sha1()
    try:
        for f in sorted(p for p in web_dir.iterdir() if p.is_file()):
            st = f.stat()
            h.update(f"{f.name}:{st.st_size}:{st.st_mtime_ns}".encode())
    except OSError:
        return html
    ver = h.hexdigest()[:10]
    return _re.sub(r"(/account/[a-z]+/assets/[\w.-]+\.(?:js|css))(?=['\"])", rf"\1?v={ver}", html)


def public_ws_url() -> str:
    """``ROOK_HUB_PUBLIC_WS_URL`` if it is a plain ws(s):// URL, else "" (it
    is interpolated into shell and PowerShell command lines)."""
    url = os.environ.get("ROOK_HUB_PUBLIC_WS_URL", "").strip()
    if url and _re.fullmatch(r"wss?://[A-Za-z0-9.\[\]:-]+(?:/[A-Za-z0-9._~/-]*)?", url):
        return url
    if url:
        log.warning("ignoring ROOK_HUB_PUBLIC_WS_URL=%r (expected ws[s]://host[:port][/path])", url)
    return ""


class InstallerAccessLogger(web.AbstractAccessLogger):
    """Keep short-lived join codes out of application HTTP access logs."""
    def log(self, request, response, elapsed):
        self.logger.info('%s %s %s %s', request.remote, request.method,
                         request.path, response.status)

import re as _re
_HERMES_ANSI = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HERMES_BOX = "─│╱╲╳┌┐└┘├┤┬┴┼═║╔╗╚╝╠╣╦╩╬▀▄█▌▐░▒▓⚕☤┊⚡•·"
_HERMES_FOOTER = ("resume this session", "session:", "duration:", "messages:",
                  "hermes --resume")


def _wake_prompt(title: str, convo: str, note: str) -> str:
    """Brief handed to a hermes box when it's woken into a room. Hermes has
    rook tools of its own, so be explicit that the reply text IS the message —
    otherwise it reads the transcript as a task list and goes off to 'do' it
    (seen live: it created a new room and posted there instead of answering)."""
    note_line = f"\nNote from the person waking you: {note}" if note else ""
    return (
        f"You are a participant in the rook chat room '{title}'. Someone in the "
        f"room is waiting for your reply.{note_line}\n\n"
        f"Respond to the conversation below as yourself, in plain prose addressed "
        f"to the room. Whatever you write back is posted into this room for you "
        f"automatically — do NOT call any rook chat tools (rook_chat_start/send/"
        f"read/wake, chat.*), do not create rooms, and do not describe what you "
        f"did; just answer.\n\n--- conversation ---\n{convo}\n--- end ---")


def _clean_hermes_stdout(text: str) -> str:
    """Extract hermes's reply from its TUI stdout: strip ANSI + box drawing,
    keep the lines between the ``⚕ Hermes`` banner and the session footer.
    (Mirrors band_mcp's cleaner so the dashboard wake posts clean prose.)"""
    text = _HERMES_ANSI.sub("", str(text or ""))
    lines = []
    for raw in text.replace("\r", "\n").split("\n"):
        ln = "".join(c for c in raw if c not in _HERMES_BOX).strip()
        if ln:
            lines.append(ln)
    banner = next((i for i, ln in enumerate(lines) if ln.lower() == "hermes"), -1)
    if banner >= 0:
        body = []
        for ln in lines[banner + 1:]:
            if ln.lower().startswith(_HERMES_FOOTER):
                break
            body.append(ln)
        return " ".join(body).strip()
    kept = [ln for ln in lines
            if not ln.lower().startswith(("query:", "initializing agent") + _HERMES_FOOTER)]
    return " ".join(kept).strip()

PS_BOOTSTRAP = r'''
# R00K Band Worker Bootstrap (Windows)
#
# Usually run as:  iex (irm "<hub>/worker?band=CODE&os=windows")
# Unattended (an elevated shell of the person who uses this machine, from a
# management tool or CI):
#   $env:ROOK_YES = "1"; iex (irm "<hub>/worker?band=CODE&os=windows")
# It then never prompts; nor does it without an interactive console. Also
# read: ROOK_WORKER_NAME (this machine's name on the band), ROOK_BAND (which
# band, for an account sign-in with several). Not as SYSTEM: the worker runs
# as, and in the session of, the account that installs it.
#
# Everything is inside ONE script block run with `&`. Under `iex` this text
# executes in the user's own interactive session, so a top-level `exit` would
# close their window (with any error message in it) and preference variables
# set here would leak into their session. Inside the block, `return` ends the
# install and leaves the window open. `exit` is used only when this text runs
# from a FILE (the elevated relaunch below, or automation using -File), where
# it hands the exit code back to whoever launched us.
& {{
param([string]$ResultFile = "", [string]$Yes = "")
$ErrorActionPreference = "Stop"
# Suppress the progress bar: it speeds up Invoke-WebRequest a lot AND avoids a
# "reading the console output buffer" crash when the installer runs headless /
# with redirected output (automation, RMM, remote shells).
$ProgressPreference = "SilentlyContinue"

$startMsg = "[rook] Installing the Rook worker. This takes 2-5 minutes (longer if Python must be installed)."
# Prompts need a person: never under a service account, with redirected input
# (management tools, CI) or with ROOK_YES=1. The elevated relaunch gets a fresh
# environment, so the choice travels as its -Yes argument.
$unattended = ("$env:ROOK_YES" -match '^(1|true|yes)$') -or ($Yes -eq "yes")
$interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and -not $unattended
$workerName = $env:COMPUTERNAME -replace '[^A-Za-z0-9._-]', '-'
if ($env:ROOK_WORKER_NAME) {{ $workerName = $env:ROOK_WORKER_NAME }}
$bandChoice = "$env:ROOK_BAND"
# The elevated relaunch below has a fresh environment: the name and band
# come as its extra arguments.
foreach ($extra in $args) {{
    if ("$extra" -like "name=*") {{ $workerName = "$extra".Substring(5) }}
    elseif ("$extra" -like "band=*") {{ $bandChoice = "$extra".Substring(5) }}
}}
if ($bandChoice) {{ $env:ROOK_BAND = $bandChoice }}
$installDir = Join-Path $env:USERPROFILE ".rook-band-worker"
$logPath = Join-Path $installDir "install.log"
$detailsPath = Join-Path $installDir "install-details.log"
# Set only when this text runs from a file (-File); empty under iex.
$fromFile = [bool]$PSCommandPath

# The name goes into the scheduled task's command line.
if ($workerName -notmatch '^[A-Za-z0-9._-]+$') {{
    Write-Host "[rook] Not installed: the worker name '$workerName' may only use letters, digits, '.', '_' and '-' (set ROOK_WORKER_NAME)."
    $global:LASTEXITCODE = 1
    return
}}
# The worker belongs to the person at this machine: its task starts at their
# sign-in, in their session (keyboard, mouse and screenshots need one), and
# its files and the `rook` command live in their profile. SYSTEM has none of
# that (its profile is under C:\Windows\System32 and it runs in session 0).
if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -eq "S-1-5-18") {{
    Write-Host "[rook] Not installed: this is running as SYSTEM, but the worker must run as the person who uses this machine."
    Write-Host "[rook] Run the same command in their PowerShell (as administrator), or have your management tool run it as the signed-in user."
    $global:LASTEXITCODE = 1
    return
}}

# Self-elevate if not already Administrator - Register-ScheduledTask below
# needs -RunLevel Highest, which requires it. Relaunching ourselves elevated
# means the one-liner just works without the user having to know to
# right-click "Run as Administrator" first; this triggers a UAC prompt they
# have to accept once.
#
# IMPORTANT: the elevated relaunch downloads the script to a temp FILE and
# runs that with -File, rather than piping straight back through `iex (irm
# ...)` a second time. "Unsigned script downloads+evals itself over the
# network, then spawns an elevated copy of itself doing the same thing
# again" is close to a textbook privilege-escalating fileless-loader shape
# and is exactly what tripped Defender's ML heuristic (Commando.A!ml) during
# testing - the base one-liner alone is a far more common, lower-risk
# pattern (same one Chocolatey/Scoop/rustup use). Writing to disk first
# doesn't hide anything from AMSI/content scanning, it just avoids that
# specific self-propagating-elevation process-creation signature.
#
# This (non-elevated) window waits for the elevated one and then reports the
# outcome itself. The elevated run writes a small result file (status, name,
# log path) rather than relying on its exit code alone, which Start-Process
# does not always surface for an elevated child.
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and -not $interactive) {{
    # Nobody is here to accept the Windows administrator prompt.
    Write-Host "[rook] Not installed: an unattended install needs an administrator PowerShell."
    Write-Host "[rook] Run the same command from an elevated PowerShell signed in as the person who uses this machine."
    $global:LASTEXITCODE = 1
    return
}}
if (-not $isAdmin) {{
    Write-Host $startMsg
    Write-Host "[rook] Administrator rights are needed: accept the Windows UAC prompt. Progress shows in the new window; this one reports the result."
    $tmp = Join-Path $env:TEMP "rook-worker-install.ps1"
    $res = Join-Path $env:TEMP "rook-worker-install.result"
    Remove-Item $res -Force -ErrorAction SilentlyContinue
    try {{
        Invoke-WebRequest -Uri "{base}/worker?os=windows" -OutFile $tmp -UseBasicParsing
    }} catch {{
        Write-Host "[rook] ERROR: could not download the installer: $($_.Exception.Message)"
        Write-Host "[rook] Nothing was installed. Check that this machine can reach the hub, then re-run the command."
        $global:LASTEXITCODE = 1
        return
    }}
    # Quote both paths: Start-Process joins -ArgumentList with spaces, and a
    # profile path like C:\Users\Jane Doe\... would otherwise split in two.
    $relaunch = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$tmp`"", "`"$res`"")
    if ($unattended) {{ $relaunch += "yes" }} else {{ $relaunch += "no" }}
    $relaunch += "`"name=$workerName`""
    if ($bandChoice) {{
        $bandArg = $bandChoice -replace '"', ''
        $relaunch += "`"band=$bandArg`""
    }}
    try {{
        $p = Start-Process -FilePath "powershell.exe" -ArgumentList $relaunch -Verb RunAs -Wait -PassThru
    }} catch {{
        Remove-Item $tmp -ErrorAction SilentlyContinue
        Write-Host "[rook] Not installed: the administrator (UAC) prompt was declined or could not be shown."
        Write-Host "[rook] Re-run the command and choose Yes, or run it from an elevated PowerShell (right-click PowerShell -> Run as administrator)."
        $global:LASTEXITCODE = 1
        return
    }}
    Remove-Item $tmp -ErrorAction SilentlyContinue
    $result = @{{}}
    if (Test-Path $res) {{
        Get-Content $res -Encoding UTF8 | ForEach-Object {{
            $kv = $_ -split "=", 2
            if ($kv.Count -eq 2) {{ $result[$kv[0]] = $kv[1] }}
        }}
        Remove-Item $res -Force -ErrorAction SilentlyContinue
    }}
    if ($result["status"] -eq "ok") {{
        $joined = "the band"
        if ($result["band"]) {{ $joined = $result["band"] }}
        Write-Host "[rook] Done: $($result['name']) joined $joined. It appears under Workers in the dashboard within a minute."
        Write-Host "[rook] To check on it from this machine, open a new PowerShell window and run: rook status"
        $global:LASTEXITCODE = 0
        return
    }}
    $code = 1
    if ($result["code"]) {{ $code = [int]$result["code"] }}
    elseif ($p -and $p.ExitCode) {{ $code = $p.ExitCode }}
    if ($result["log"]) {{ $logPath = $result["log"] }}
    Write-Host "[rook] FAILED: the install did not finish (exit code $code)."
    if ($result["error"]) {{ Write-Host "[rook] Error: $($result['error'])" }}
    Write-Host "[rook] Full log: $logPath"
    Write-Host "[rook] Fix the problem, then re-run the same command (re-running is safe)."
    # Report the child's code without closing this window (see the top).
    $global:LASTEXITCODE = $code
    return
}}

# ---- Elevated from here: the relaunched child, or a user who opened an
# elevated PowerShell themselves. ----

# Resolve a WORKING python 3.11+ - prefer the `py` launcher, which is immune to the
# Microsoft Store "python.exe" App Execution Alias stub that Windows 10/11 put
# on PATH by default. That stub makes `Get-Command python` succeed even with
# no real Python installed, and `python --version` silently prints nothing -
# which used to make this script believe Python was already there and crash
# later at venv creation with no real interpreter behind it. An older Python
# (3.8, say) counts as missing: the worker needs 3.11.
function Test-Python([string]$Exe, [string[]]$PyArgs) {{
    try {{
        $ok = & $Exe @PyArgs -c "import sys; print(sys.version_info >= (3, 11))" 2>$null
        return ($LASTEXITCODE -eq 0 -and "$ok".Trim() -eq "True")
    }} catch {{ return $false }}
}}
function Resolve-Python {{
    if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-Python "py" @("-3"))) {{ return @{{ Exe = 'py'; Args = @('-3') }} }}
    if ((Get-Command python -ErrorAction SilentlyContinue) -and (Test-Python "python" @())) {{ return @{{ Exe = 'python'; Args = @() }} }}
    # Installed (by an earlier run of this script, or by hand) but not on
    # PATH: a PATH refresh doesn't always reach this process either.
    foreach ($v in @("312", "313", "311")) {{
        foreach ($exe in @((Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"),
                           (Join-Path $env:ProgramFiles "Python$v\python.exe"), "C:\Python$v\python.exe")) {{
            if ((Test-Path $exe) -and (Test-Python $exe @())) {{ return @{{ Exe = $exe; Args = @() }} }}
        }}
    }}
    return $null
}}

# Python from python.org, for this user only (no administrator files, no PATH
# change), checked against its published SHA-256 before it runs. The pinned
# hashes are the python.org installers' (also in winget's Python.Python.3.12
# 3.12.10 manifest). A download that does not match is deleted, never run.
$pyVersion = "3.12.10"
$pyHashes = @{{
    "amd64" = "67B5635E80EA51072B87941312D00EC8927C4DB9BA18938F7AD2D27B328B95FB"
    "arm64" = "377AC8FD478987940088E879441E702A71B53164D2A1E6F1D51FF77A7E470258"
}}
function Install-Python {{
    $arch = "amd64"
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {{ $arch = "arm64" }}
    $target = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312"
    # A new folder of our own, not a guessable name in %TEMP% that something
    # else could swap between the hash check and the run.
    $setupDir = Join-Path $installDir ("python-setup-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $setupDir | Out-Null
    $setup = Join-Path $setupDir "python-$pyVersion-$arch.exe"
    Write-Host "[rook]       Downloading Python $pyVersion from python.org..."
    try {{
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-$arch.exe" -OutFile $setup -UseBasicParsing
    }} catch {{
        Write-Host "[rook]       Could not download it ($($_.Exception.Message))."
        Remove-Item $setupDir -Recurse -Force -ErrorAction SilentlyContinue
        return $false
    }}
    $hash = (Get-FileHash -Path $setup -Algorithm SHA256).Hash
    if ($hash -ne $pyHashes[$arch]) {{
        Remove-Item $setupDir -Recurse -Force -ErrorAction SilentlyContinue
        Fail "the downloaded Python installer did not match its known SHA-256 fingerprint, so it was deleted unused. Something between this machine and python.org changed it; install Python 3.12 yourself from https://www.python.org/downloads/windows/ and run this command again."
    }}
    Write-Host "[rook]       Installing Python $pyVersion for this user (about a minute)..."
    $p = Start-Process -FilePath $setup -Wait -PassThru -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "TargetDir=`"$target`"", "PrependPath=0",
        "Include_launcher=0", "Include_test=0", "Include_doc=0", "Shortcuts=0", "AssociateFiles=0")
    Remove-Item $setupDir -Recurse -Force -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) {{ Write-Host "[rook]       The Python installer exited with code $($p.ExitCode)." }}
    return $true
}}

# Run a native command with its output routed through PowerShell so the
# transcript (install.log) keeps it; returns the exit code. Errors are
# "Continue" here because Windows PowerShell 5.1 turns redirected stderr lines
# into error records, which "Stop" would make fatal (pip warnings, notices).
#
# Successful output goes only to install-details.log beside install.log (the
# screen keeps to the numbered steps); a failure's output is shown too.
function Invoke-Logged([string]$Exe, [string[]]$Arguments) {{
    $ErrorActionPreference = "Continue"
    $lines = @(& $Exe @Arguments 2>&1 | ForEach-Object {{ "$_" }})
    $code = $LASTEXITCODE
    try {{ Add-Content -Path $detailsPath -Value (@("> $Exe $($Arguments -join ' ')") + $lines) -Encoding UTF8 }} catch {{}}
    if ($code -ne 0) {{ $lines | Select-Object -Last 20 | ForEach-Object {{ Write-Host $_ }} }}
    return $code
}}

# Abort the install with a message and an exit code (caught below).
function Fail([string]$Message, [int]$Code = 1) {{
    $err = New-Object System.Exception $Message
    $err.Data["RookExitCode"] = $Code
    throw $err
}}

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Remove-Item $detailsPath -Force -ErrorAction SilentlyContinue
$transcript = $false
try {{ Start-Transcript -Path $logPath -Force | Out-Null; $transcript = $true }} catch {{}}
$exitCode = 0
$failure = ""
$hadWorker = $false
$bandName = ""
try {{
    Write-Host $startMsg

    try {{
        Write-Host "[rook] (1/6) Checking for Python 3.11 or newer..."
        $py = Resolve-Python
        if (-not $py) {{
            Write-Host "[rook]       Python was not found (or only the Microsoft Store shortcut is there). Installing it - this is the slow part..."
            $null = Install-Python
            $py = Resolve-Python
            if (-not $py -and (Get-Command winget -ErrorAction SilentlyContinue)) {{
                Write-Host "[rook]       Trying winget instead..."
                $null = Invoke-Logged "winget" @("install", "-e", "--id", "Python.Python.3.12", "--scope", "user", "--accept-package-agreements", "--accept-source-agreements", "-h")
                $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
                $py = Resolve-Python
            }}
            if (-not $py) {{
                Write-Host "[rook] Python could not be installed automatically. Install Python 3.12 from https://www.python.org/downloads/windows/, then run this command again."
                Fail "Python 3.11 or newer is needed."
            }}
        }}
        $null = Invoke-Logged $py.Exe (@($py.Args) + @("--version"))

        # Use a dedicated venv - avoids PEP 668 and missing/broken system pip.
        Write-Host "[rook] (2/6) Preparing a private Python environment for the worker..."
        $venv = Join-Path $installDir "venv"
        $vpy = "$venv\Scripts\python.exe"
        if (-not (Test-Path $vpy)) {{
            $null = Invoke-Logged $py.Exe (@($py.Args) + @("-m", "venv", $venv))
            if (-not (Test-Path $vpy)) {{ Fail "venv creation failed - $vpy was not created." }}
        }}
        # pythonw.exe is the windowless interpreter - the worker runs with NO console window.
        $vpyw = "$venv\Scripts\pythonw.exe"
        if (-not (Test-Path $vpyw)) {{ $vpyw = $vpy }}  # fallback if pythonw is absent
        $null = Invoke-Logged $vpy @("-m", "ensurepip", "--upgrade")
        $null = Invoke-Logged $vpy @("-m", "pip", "install", "--quiet", "--upgrade", "pip")

        # Install required dependencies into the venv (prebuilt wheels - no compiler needed).
        # mss+pillow back screenshot.* (see rook/worker/plugins/screenshot.py); hid.* needs
        # nothing extra - it's stdlib ctypes SendInput. windows-curses gives the chat.open
        # receiver window a curses UI (chat falls back to a plain line client without it).
        Write-Host "[rook] (3/6) Installing the libraries it needs (the longest step)..."
        $rc = Invoke-Logged $vpy @("-m", "pip", "install", "--quiet", "pynacl", "aiohttp", "websockets", "cryptography", "mss", "pillow", "windows-curses")
        if ($rc -ne 0) {{ Fail "installing the libraries failed (pip exit code $rc) - see the output above." $rc }}

        # Download the bundle to a temp name, THEN stop the running worker and
        # swap it in. Windows refuses to overwrite a file another process has
        # open, so a re-run (upgrade, or re-pairing to another hub) used to fail
        # here while the old worker held band-worker.pyz. Stopping after the
        # download means a failed download leaves the old worker running.
        Write-Host "[rook] (4/6) Downloading the worker..."
        $pyz = Join-Path $installDir "band-worker.pyz"
        $pyzNew = "$pyz.download"
        Remove-Item $pyzNew -Force -ErrorAction SilentlyContinue
        Invoke-WebRequest -Uri "{base}/band-worker.pyz" -OutFile $pyzNew -UseBasicParsing

        # Stop the existing worker - avoids a locked bundle, duplicate processes and
        # stale worker-ids lingering on the band (each process announces a fresh id).
        if (Get-ScheduledTask -TaskName "RookBandWorker" -ErrorAction SilentlyContinue) {{ $hadWorker = $true }}
        Stop-ScheduledTask -TaskName "RookBandWorker" -ErrorAction SilentlyContinue
        Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
            Where-Object {{ $_.CommandLine -like "*band-worker.pyz*" }} |
            ForEach-Object {{ $hadWorker = $true; Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
        Start-Sleep -Seconds 1
        # A just-killed process can keep the file open for a moment; retry briefly.
        for ($i = 1; ; $i++) {{
            try {{ Move-Item -Path $pyzNew -Destination $pyz -Force; break }}
            catch {{ if ($i -ge 10) {{ throw }}; Start-Sleep -Seconds 1 }}
        }}
        $rc = Invoke-Logged $vpy @($pyz, "--install-cli")
        if ($rc -ne 0) {{ Fail "the worker bundle failed to run (exit code $rc) - see the output above." $rc }}

        # Not routed through Invoke-Logged: enrollment may be interactive (browser
        # sign-in, band choice), so it keeps the console to itself. Its output
        # fits under this step (ROOK_INSTALLER), and without a person it never
        # prompts (--yes).
        Write-Host "[rook] (5/6) Joining the band as $workerName..."
        $env:ROOK_INSTALLER = "1"
        $enrollExtra = @("--name", $workerName)
        if (-not $interactive) {{ $enrollExtra += "--yes" }}
        & $vpy $pyz {enroll_args} @enrollExtra
        $rc = $LASTEXITCODE
{enroll_retry}        if ($rc -ne 0) {{ Fail "joining the band failed (exit code $rc) - see the message above." $rc }}

        # HID backend: Windows is native - hid.* uses SendInput via user32.dll (no install).
        # The task runs at logon in the interactive session, so input injection works.
        Write-Host "[rook] (6/6) Starting the worker in the background (it starts again at every sign-in)..."

        # Register as Scheduled Task so worker restarts at every logon  (band: {band_name})
        $workerArgs = "`"$pyz`" --hub {hub_public} --ws --psk {band_psk} --name $workerName --update-url {base}/band-worker.json"
        $action = New-ScheduledTaskAction -Execute $vpyw -Argument $workerArgs
        $trigger = New-ScheduledTaskTrigger -AtLogon
        $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0
        Register-ScheduledTask -TaskName "RookBandWorker" -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force | Out-Null
        Start-ScheduledTask -TaskName "RookBandWorker"
        # The worker already runs: a stray stderr line here (which "Stop"
        # makes fatal in Windows PowerShell 5.1) must not mark it FAILED.
        try {{
            $ErrorActionPreference = "Continue"
            $bandName = "$(& $vpy $pyz --enrolled-band 2>$null | Select-Object -First 1)".Trim()
        }} catch {{ $bandName = "" }} finally {{ $ErrorActionPreference = "Stop" }}
    }} catch {{
        $exitCode = 1
        if ($_.Exception.Data.Contains("RookExitCode")) {{
            $exitCode = [int]$_.Exception.Data["RookExitCode"]
            if ($exitCode -eq 0) {{ $exitCode = 1 }}
        }} else {{
            # An unexpected error: say where, for the log.
            Write-Host $_.InvocationInfo.PositionMessage
        }}
        $failure = $_.Exception.Message -replace "[\r\n]+", " "
        Write-Host "[rook] ERROR: $failure"
        if ($hadWorker) {{
            # Don't leave a previously working machine offline.
            try {{
                Start-ScheduledTask -TaskName "RookBandWorker"
                Write-Host "[rook] Restarted the previously installed worker."
            }} catch {{}}
        }}
    }}

    if ($exitCode -eq 0) {{
        $joined = "the band"
        if ($bandName) {{ $joined = $bandName }}
        Write-Host "[rook] Done: $workerName joined $joined. It appears under Workers in the dashboard within a minute."
    }} else {{
        Write-Host "[rook] FAILED (exit code $exitCode). Full log: $logPath"
        Write-Host "[rook] Fix the problem above, then re-run the same command (re-running is safe)."
    }}
}} finally {{
    # finally: also on Ctrl+C, so an elevated session is not left transcribing.
    if ($transcript) {{ try {{ Stop-Transcript | Out-Null }} catch {{}} }}
}}
if ($ResultFile) {{
    # Read by the non-elevated window that launched us (see the top).
    $status = "failed"
    if ($exitCode -eq 0) {{ $status = "ok" }}
    try {{
        Set-Content -Path $ResultFile -Encoding UTF8 -Value @("status=$status", "code=$exitCode", "name=$workerName", "band=$bandName", "log=$logPath", "error=$failure")
    }} catch {{}}
    # Keep a failed run's window open so the error can be read; a successful
    # one closes and the launching window reports success.
    if ($exitCode -ne 0 -and $interactive) {{ $null = Read-Host "[rook] Press Enter to close this window" }}
}}
if ($fromFile) {{ exit $exitCode }}
$global:LASTEXITCODE = $exitCode
}} @args
'''

BASH_BOOTSTRAP = '''#!/bin/bash
set -e

# R00K Band Worker Bootstrap (Linux/Mac/Termux)
#
#   curl -fsSL '<hub>/worker?band=CODE' | bash                 # at a terminal
#   curl -fsSL '<hub>/worker?band=CODE' | bash -s -- --yes     # unattended
#
# Unattended runs (cloud-init, a systemd unit, CI, --yes or ROOK_YES=1) never
# prompt: a prompt needs a person at a terminal, and stdin here is the curl
# pipe, so prompts read /dev/tty only when it can be opened. Other knobs:
# ROOK_WORKER_NAME (this machine's name on the band), ROOK_BAND (which band,
# for an account sign-in with several), ROOK_HID=0|1 (keyboard/mouse control:
# skip, or set up even with no screen).
#
# As root (cloud-init, sudo) it installs nothing unless told whose worker this
# is: ROOK_USER=<account> installs it for that account (it runs as them), and
# ROOK_ALLOW_ROOT=1 runs it as root (a container, say). A root worker runs
# every band command (shell, files, terminals) as root.

for arg in "$@"; do
    case "$arg" in
        -y|--yes) ROOK_YES=1 ;;
        --no-hid) ROOK_HID=0 ;;
    esac
done
# The name goes into the service's command line: keep it to safe characters.
case "${{ROOK_WORKER_NAME:-}}" in
    *[!A-Za-z0-9._-]*)
        echo "[rook] Not installed: ROOK_WORKER_NAME may only use letters, digits, '.', '_' and '-'."
        exit 1 ;;
esac
# The worker's own enrollment output fits under step 5 when it sees this.
export ROOK_YES="${{ROOK_YES:-}}" ROOK_INSTALLER=1

HAVE_TTY=0
case "$ROOK_YES" in
    1|true|yes) ;;
    *) if {{ true </dev/tty; }} 2>/dev/null; then HAVE_TTY=1; fi ;;
esac

# cloud-init and systemd units may run us without HOME or USER, and `su`
# without `-` leaves USER naming the account it came from: USER is always
# whoever this really runs as (lingering and the input group are for them).
export USER="$(id -un)"
# Root writes only to its own home: `sudo -E` keeps the caller's HOME, and a
# root-owned install.log there would break that person's own install later.
# Only a home the system names (getent, or ~user where there is no getent:
# macOS) replaces HOME; rooted Termux keeps its own.
own_home() {{
    local h
    h="$(getent passwd "$USER" 2>/dev/null | cut -d: -f6)"
    if [ -z "$h" ]; then
        case "$USER" in *[!A-Za-z0-9._-]*|"") ;; *) h="$(eval "echo ~$USER")" ;; esac
        case "$h" in "~"*) h="" ;; esac
    fi
    echo "$h"
}}
case "${{PREFIX:-}}" in /data/data/com.termux*) ;; *)
    if [ "$(id -u)" = 0 ]; then
        _own="$(own_home)"
        [ -n "$_own" ] && export HOME="$_own"
    fi ;;
esac
[ -n "${{HOME:-}}" ] || export HOME="$(own_home)"
if [ -z "${{HOME:-}}" ]; then
    if [ "$(id -u)" = 0 ]; then export HOME=/root
    else echo "[rook] Not installed: HOME is not set. Set it to your home folder, then run the same command again."; exit 1
    fi
fi

# Root needs no sudo (a container, cloud-init); without a terminal sudo must
# fail at once instead of waiting for a password nobody can type.
SUDO=""
if [ "$(id -u)" != 0 ] && command -v sudo &>/dev/null; then
    if [ "$HAVE_TTY" = 1 ]; then SUDO="sudo"; else SUDO="sudo -n"; fi
fi

mkdir -p "$HOME/.rook-band-worker"
# Details (package managers, pip) go here; the screen gets the steps.
LOG="$HOME/.rook-band-worker/install.log"
: > "$LOG"
say() {{ echo "[rook] $*"; echo "[rook] $*" >> "$LOG"; }}
quietly() {{ "$@" >> "$LOG" 2>&1; }}

say "Installing the Rook worker. This takes 1-3 minutes (longer if Python must be installed)."
# set -e stops at the first failing step; say so plainly instead of just ending.
trap 'rc=$?; [ $rc -ne 0 ] && echo "[rook] FAILED (exit code $rc). Fix the problem above, then re-run the same command (re-running is safe). Details: $LOG"' EXIT

install_python() {{
    say "      Python was not found; installing it..."
    if command -v apt-get &>/dev/null; then
        quietly $SUDO apt-get update -qq && quietly $SUDO apt-get install -y -qq python3 python3-pip python3-venv curl
    elif command -v dnf &>/dev/null; then
        quietly $SUDO dnf install -y python3 python3-pip curl
    elif command -v pacman &>/dev/null; then
        quietly $SUDO pacman -Sy --noconfirm python python-pip curl
    elif command -v apk &>/dev/null; then
        quietly $SUDO apk add python3 py3-pip curl
    elif command -v brew &>/dev/null; then
        quietly brew install python3
    elif command -v pkg &>/dev/null; then
        quietly pkg install -y python curl
    else
        say "ERROR: no supported package manager found."
        say "Install Python 3.11 or newer, then run the same command again."
        exit 1
    fi
}}

install_curl() {{
    if ! command -v curl &>/dev/null; then
        say "      curl was not found; installing it..."
        if command -v apt-get &>/dev/null; then
            quietly $SUDO apt-get install -y -qq curl
        elif command -v dnf &>/dev/null; then
            quietly $SUDO dnf install -y curl
        elif command -v pacman &>/dev/null; then
            quietly $SUDO pacman -Sy --noconfirm curl
        elif command -v pkg &>/dev/null; then
            quietly pkg install -y curl
        fi
    fi
}}

# ---- keyboard/mouse control (Linux): ydotool + ydotoold so hid.* works ----
# Wayland needs ydotool (xdotool is X11-only); ydotool also covers X11. Mac/Windows
# use their own native backends; Termux/Android does not use this path.

# No screen to type into (a server, a container, a VM without a desktop): the
# worker's hid plugin stays off there anyway, so don't install anything.
# A desktop reached over SSH still counts: its display sockets or graphical
# boot target give it away.
hid_headless() {{
    [ -n "${{DISPLAY:-}}${{WAYLAND_DISPLAY:-}}" ] && return 1
    ls /tmp/.X11-unix/X* >/dev/null 2>&1 && return 1
    ls /run/user/*/wayland-* >/dev/null 2>&1 && return 1
    [ "$(systemctl get-default 2>/dev/null || true)" = graphical.target ] && return 1
    return 0
}}

setup_hid_linux() {{
    [ "$(uname -s)" = "Linux" ] || return 0
    case "$PREFIX" in /data/data/com.termux*) return 0 ;; esac
    case "${{ROOK_HID:-}}" in
        0) say "Skipped keyboard and mouse control (ROOK_HID=0)."; return 0 ;;
        1) ;;
        *) if hid_headless; then
               say "Skipped keyboard and mouse control: this machine has no screen."
               say "      (To set it up anyway, run the same command with ROOK_HID=1.)"
               return 0
           fi ;;
    esac
    say "Setting up keyboard and mouse control..."
    if ! command -v ydotool &>/dev/null; then
        if command -v apt-get &>/dev/null; then quietly $SUDO apt-get install -y -qq ydotool || true
        elif command -v dnf &>/dev/null; then quietly $SUDO dnf install -y ydotool || true
        elif command -v pacman &>/dev/null; then quietly $SUDO pacman -Sy --noconfirm ydotool || true
        elif command -v apk &>/dev/null; then quietly $SUDO apk add ydotool || true
        fi
    fi
    command -v ydotool &>/dev/null || {{ say "Note: keyboard and mouse control is not available (ydotool could not be installed). Everything else works."; return 0; }}

    # /dev/uinput access. Active seat sessions get an ACL automatically; the udev
    # rule + input group cover headless and post-reboot. All best-effort (sudo).
    quietly $SUDO modprobe uinput || true
    if [ ! -e /etc/udev/rules.d/99-rook-uinput.rules ]; then
        echo 'KERNEL=="uinput", MODE="0660", GROUP="input", OPTIONS+="static_node=uinput"' | $SUDO tee /etc/udev/rules.d/99-rook-uinput.rules >/dev/null 2>&1 || true
        quietly $SUDO udevadm control --reload-rules || true
        quietly $SUDO udevadm trigger /dev/uinput || true
    fi
    quietly $SUDO usermod -aG input "$USER" || true

    # ydotoold as our own user service (portable across distro unit naming).
    if command -v systemctl &>/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
        mkdir -p ~/.config/systemd/user
        cat > ~/.config/systemd/user/rook-ydotoold.service << RKYD
[Unit]
Description=ydotoold (rook HID backend daemon)

[Service]
ExecStart=$(command -v ydotoold)
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
RKYD
        quietly systemctl --user daemon-reload
        quietly systemctl --user enable --now rook-ydotoold.service || true
        if ! systemctl --user is-active --quiet rook-ydotoold.service; then
            say "Note: keyboard and mouse control starts after you sign out and back in."
        fi
    fi
}}

# ---- Termux/Android: native deps + persistence helper --------------------
IS_TERMUX=0
case "$PREFIX" in /data/data/com.termux*) IS_TERMUX=1 ;; esac

setup_termux_service() {{
    # Persist via termux-services (runit). Falls back to nohup this session if
    # runsvdir isn't supervising yet (first install before a Termux restart).
    quietly pkg install -y termux-services || true
    SVDIR="$PREFIX/var/service/rook-band-worker"
    mkdir -p "$SVDIR"
    cat > "$SVDIR/run" << TXSVC
#!$PREFIX/bin/sh
termux-wake-lock 2>/dev/null || true
exec $WORKER_CMD 2>&1
TXSVC
    chmod +x "$SVDIR/run"

    # Termux:Boot autostart (no-op unless the Termux:Boot addon is installed):
    # bring up runsvdir on boot so it supervises our service.
    mkdir -p "$HOME/.termux/boot"
    cat > "$HOME/.termux/boot/rook-band-worker" << TXBOOT
#!$PREFIX/bin/sh
termux-wake-lock 2>/dev/null || true
. $PREFIX/etc/profile.d/start-services.sh 2>/dev/null || true
TXBOOT
    chmod +x "$HOME/.termux/boot/rook-band-worker"

    if command -v sv >/dev/null 2>&1 && pgrep -x runsvdir >/dev/null 2>&1; then
        sv up rook-band-worker 2>/dev/null || true
        sleep 2
        if sv status rook-band-worker 2>/dev/null | grep -q "^run"; then
            return 0
        fi
    fi
    termux-wake-lock 2>/dev/null || true
    nohup $WORKER_CMD >> "$HOME/.rook-band-worker/worker.log" 2>&1 &
    sleep 2
    if kill -0 $! 2>/dev/null; then
        say "Tip: restart Termux once so the worker keeps running after Termux restarts."
    else
        say "ERROR: the worker stopped right after starting. Its log: ~/.rook-band-worker/worker.log"
        tail -n 20 "$HOME/.rook-band-worker/worker.log" 2>/dev/null
        exit 1
    fi
}}

if [ "$IS_TERMUX" = "1" ]; then
    quietly pkg install -y libsodium termux-api || true
    # pynacl: link Termux's prebuilt libsodium instead of compiling the bundled
    # copy, whose configure mis-detects memset_explicit on Android API < 34.
    export SODIUM_INSTALL=system
fi

# ---- run as root: whose worker is this? ------------------------------------
# A root worker runs every band command as root, and lingering (below) would
# bring it back at every boot. ROOK_USER=<account>: prepare what that account
# can't install without sudo, then run this same installer as them.
SELF_URL='{base}/worker?os=unix'
install_for_user() {{
    case "$ROOK_USER" in
        -*|*[!A-Za-z0-9._-]*) say "ERROR: ROOK_USER is not an account name: $ROOK_USER"; exit 1 ;;
    esac
    local entry
    entry="$(getent passwd "$ROOK_USER" 2>/dev/null || true)"
    if [ -z "$entry" ]; then
        say "ERROR: there is no account named $ROOK_USER here. Create it first (useradd -m $ROOK_USER), then run the same command again."
        exit 1
    fi
    if ! command -v runuser &>/dev/null; then
        say "ERROR: installing for another account needs runuser (util-linux). Run the command as $ROOK_USER instead."
        exit 1
    fi
    local uhome uid tmp
    uhome="$(echo "$entry" | cut -d: -f6)"
    uid="$(echo "$entry" | cut -d: -f3)"
    say "Installing for the account $ROOK_USER: the worker will run as $ROOK_USER, not as root."
    install_curl
    # Fetched first, while a pairing code is fresh; a private folder they own.
    tmp="$(mktemp -d)"
    if ! curl -fsSL "$SELF_URL" -o "$tmp/install.sh"; then
        rm -rf "$tmp"
        say "ERROR: could not download the installer again for $ROOK_USER (an expired pairing code? Get a new one)."
        exit 1
    fi
    chown -R "$ROOK_USER" "$tmp"
    command -v python3 &>/dev/null || command -v python &>/dev/null || install_python
    local py
    py="$(command -v python3 || command -v python)"
    if ! quietly "$py" -c "import venv, ensurepip" && command -v apt-get &>/dev/null; then
        quietly apt-get install -y -qq python3-venv || true
    fi
    # Their user service runs without a login, now and at every boot.
    command -v loginctl &>/dev/null && {{ quietly loginctl enable-linger "$ROOK_USER" || true; }}
    # An earlier root run (`sudo -E`, older installers) may have left files of
    # root's in their folder; theirs again, without following any links.
    if [ -d "$uhome/.rook-band-worker" ] && [ ! -L "$uhome/.rook-band-worker" ]; then
        chown -R -h -P "$ROOK_USER" "$uhome/.rook-band-worker" || true
    fi
    local envs=(HOME="$uhome" USER="$ROOK_USER" LOGNAME="$ROOK_USER" ROOK_YES=1
                PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                XDG_RUNTIME_DIR="/run/user/$uid")
    local v
    for v in ROOK_WORKER_NAME ROOK_BAND ROOK_HID LANG http_proxy https_proxy no_proxy \\
             HTTP_PROXY HTTPS_PROXY NO_PROXY; do
        [ -n "${{!v:-}}" ] && envs+=("$v=${{!v}}")
    done
    trap - EXIT
    local rc=0
    runuser -u "$ROOK_USER" -- env -i "${{envs[@]}}" bash "$tmp/install.sh" --yes </dev/null || rc=$?
    rm -rf "$tmp"
    if [ "$rc" -ne 0 ]; then
        root_worker_present && say "The worker already installed for root was left running."
        exit "$rc"
    fi
    # Only once the new worker is in: one machine, one worker.
    if root_worker_present; then stop_root_worker || exit 1; fi
    exit 0
}}
# A worker installed for root earlier (older installers made root-over-SSH
# installs a lingering user service, or a plain background process).
ROOT_PYZ="$HOME/.rook-band-worker/band-worker.pyz"
ROOT_UNIT="$HOME/.config/systemd/user/rook-band-worker.service"
root_worker_present() {{ [ -e "$ROOT_PYZ" ] || [ -e "$ROOT_UNIT" ]; }}
# Root's own user manager, never the caller's: `sudo -E` and `su` keep the
# caller's XDG_RUNTIME_DIR and session bus.
root_systemctl() {{
    env -u DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR=/run/user/0 systemctl --user "$@"
}}
# uid-0 processes running root's worker: pgrep, else /proc (slim images have
# no procps). Fails when neither can tell.
root_worker_pids() {{
    if command -v pgrep &>/dev/null; then
        pgrep -u 0 -f -- "$ROOT_PYZ" 2>/dev/null || true
        return 0
    fi
    [ -d /proc/self ] || return 2
    local d
    for d in /proc/[0-9]*; do
        [ -O "$d" ] || continue
        if tr '\\0' ' ' < "$d/cmdline" 2>/dev/null | grep -qF -- "$ROOT_PYZ"; then
            echo "${{d#/proc/}}"
        fi
    done
    return 0
}}
ROOT_STOP_STEPS="ps -ef | grep band-worker.pyz, then sudo kill <pid> for root's; or reboot (its service no longer starts at boot)"
stop_root_worker() {{
    say "Stopping the worker installed for root here, so this machine runs one worker..."
    local had_unit=0 manager=0 pids rc=0
    if [ -e "$ROOT_UNIT" ]; then
        had_unit=1
        if quietly root_systemctl disable --now rook-band-worker; then manager=1; fi
        # Never again at boot, even where root's user manager isn't reachable now.
        rm -f "$HOME/.config/systemd/user/default.target.wants/rook-band-worker.service"
        mv -f "$ROOT_UNIT" "$ROOT_UNIT.disabled" || true
        quietly root_systemctl daemon-reload || true
    fi
    pids="$(root_worker_pids)" || rc=$?
    if [ "$rc" -eq 0 ] && [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
        sleep 2
        pids="$(root_worker_pids)" || rc=$?
    fi
    # Root's manager wasn't reached: a unit it still has loaded (Restart=always)
    # starts the worker again after RestartSec (10 s). Look again after that.
    if [ "$rc" -eq 0 ] && [ -z "$pids" ] && [ "$had_unit" = 1 ] && [ "$manager" = 0 ]; then
        sleep 12
        pids="$(root_worker_pids)" || rc=$?
    fi
    if [ "$rc" -ne 0 ]; then
        say "WARNING: could not check whether root's worker still runs (no pgrep or /proc here). Check: $ROOT_STOP_STEPS."
        return 1
    fi
    if [ -n "$pids" ]; then
        say "ERROR: root's worker is still running (process $pids). Stop it: sudo kill $pids. If it comes back: $ROOT_STOP_STEPS."
        return 1
    fi
    say "      Done: root's worker is stopped and won't start at boot. Its files stay in $HOME/.rook-band-worker; delete that folder when you no longer need it."
}}
if [ "$(id -u)" = 0 ] && [ "$IS_TERMUX" != "1" ]; then
    if [ -n "${{ROOK_USER:-}}" ] && [ "$ROOK_USER" != root ]; then
        install_for_user
    fi
    case "${{ROOK_ALLOW_ROOT:-}}" in
        1|true|yes)
            if root_worker_present; then
                say "Updating the worker already installed for root here, in place."
            fi
            say "WARNING: installing as root (ROOK_ALLOW_ROOT=1). Every band command (shell, files, terminals) runs as root on this machine, and the worker starts again at every boot." ;;
        *)
            say "Not installed: this runs as root, so the worker would run every band command (shell, files, terminals) as root."
            if root_worker_present; then
                say "A worker is already installed for root here ($HOME/.rook-band-worker)."
                say "To update that root worker in place, set ROOK_ALLOW_ROOT=1 and run the same command."
                say "To move it to an everyday account, set ROOK_USER=<account>: once that account's worker is in, root's is stopped and disabled."
            else
                say "Install it for an everyday account instead: set ROOK_USER=<account> and run the same command (it runs as that account)."
                say "Or, if root is really what you want (a container, say), set ROOK_ALLOW_ROOT=1."
            fi
            exit 1 ;;
    esac
fi

install_curl

say "(1/6) Checking for Python 3..."
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    install_python
    if command -v python3 &>/dev/null; then
        PYTHON=python3
    elif command -v python &>/dev/null; then
        PYTHON=python
    else
        say "ERROR: Python could not be installed. Install Python 3.11 or newer, then run the same command again."
        exit 1
    fi
fi
echo "Python: $($PYTHON --version 2>&1)" >> "$LOG"

# Use a dedicated venv — sidesteps PEP 668 (externally-managed), missing system pip,
# and --user path quirks. The venv always gets its own pip via ensurepip.
VENV="$HOME/.rook-band-worker/venv"
say "(2/6) Preparing a private Python environment for the worker..."
if [ ! -x "$VENV/bin/python" ]; then
    if ! quietly $PYTHON -m venv "$VENV"; then
        # venv module missing — install it, then retry
        if command -v apt-get &>/dev/null; then quietly $SUDO apt-get install -y -qq python3-venv || true
        elif command -v pacman &>/dev/null; then quietly $SUDO pacman -Sy --noconfirm python || true
        fi
        quietly $PYTHON -m venv "$VENV" || {{ say "ERROR: could not create the Python environment. Details: $LOG"; exit 1; }}
    fi
fi
VPY="$VENV/bin/python"

# Ensure pip inside the venv (ensurepip is bundled with venv; belt-and-suspenders)
quietly "$VPY" -m ensurepip --upgrade || true
quietly "$VPY" -m pip install --quiet --upgrade pip || true

# Install required dependencies into the venv (prebuilt wheels — no compiler needed)
say "(3/6) Installing the libraries it needs (the longest step)..."
quietly "$VPY" -m pip install --quiet pynacl aiohttp websockets cryptography || {{
    say "ERROR: installing the libraries failed. Details: $LOG"
    tail -n 15 "$LOG"
    exit 1
}}

# Download band-worker bundle to a temp name, then rename it into place. The
# rename is atomic and a running worker keeps reading the old file it has
# open, so a re-run (upgrade, re-pairing) never leaves a half-written bundle
# under a live worker or after a failed download. The old worker is NOT
# stopped here: the service manager swaps it below (see the note there).
say "(4/6) Downloading the worker..."
PYZ="$HOME/.rook-band-worker/band-worker.pyz"
curl -fsSL {base}/band-worker.pyz -o "$PYZ.download"
chmod +x "$PYZ.download"
mv -f "$PYZ.download" "$PYZ"
quietly "$VPY" "$PYZ" --install-cli

WORKER_NAME="${{ROOK_WORKER_NAME:-}}"
[ -n "$WORKER_NAME" ] || WORKER_NAME="$(hostname 2>/dev/null || uname -n 2>/dev/null || cat /etc/hostname 2>/dev/null)"
[ -z "$WORKER_NAME" ] && WORKER_NAME="${{HOSTNAME:-rook-worker}}"
if [ "$IS_TERMUX" = "1" ] && [ -z "${{ROOK_WORKER_NAME:-}}" ]; then
    # Termux hostname is always "localhost" — use the device model instead.
    MODEL=$(getprop ro.product.model 2>/dev/null | tr ' ' '-')
    [ -n "$MODEL" ] && WORKER_NAME="$MODEL"
fi
# A derived name too goes into a command line: anything unusual becomes '-'.
WORKER_NAME="$(printf '%s' "$WORKER_NAME" | tr -c 'A-Za-z0-9._-' '-')"
say "(5/6) Joining the band as $WORKER_NAME..."
# With a terminal, enrollment may ask (which band); without one it never does.
enroll() {{
    if [ "$HAVE_TTY" = 1 ]; then "$VPY" "$PYZ" "$@" --name "$WORKER_NAME" </dev/tty
    else "$VPY" "$PYZ" "$@" --name "$WORKER_NAME" --yes </dev/null; fi
}}
RC=0; enroll {enroll_args} || RC=$?
{enroll_retry}[ "$RC" -eq 0 ] || {{ say "ERROR: joining the band failed (exit code $RC) - see the message above."; exit "$RC"; }}

# band: {band_name}
WORKER_CMD="$VPY $PYZ --hub {hub_public} --ws --psk {band_psk} --name $WORKER_NAME --update-url {base}/band-worker.json"

# Swap in the new worker. CRITICAL: when this installer is launched *by the
# running worker* (a band-driven update), this script shares the worker's
# process tree / systemd cgroup. Stopping the old worker from inside this
# script (e.g. `pkill`) therefore kills THIS script mid-swap, before the new
# worker starts — stranding the host. So we never kill the old worker
# in-process: we let the service manager own the stop+start (it finishes the
# restart even if this script dies), and on no-service-manager hosts we launch
# the new worker in its OWN session before reaping the old one.
say "(6/6) Starting the worker in the background..."

# cloud-init, a systemd unit or an SSH session without a login session has no
# user service manager yet. Lingering starts one now (and at every boot), so
# the worker below gets a real service instead of a process that a reboot ends.
if [ "$IS_TERMUX" != "1" ] && command -v systemctl &>/dev/null && command -v loginctl &>/dev/null \
        && ! systemctl --user show-environment >/dev/null 2>&1; then
    quietly loginctl enable-linger "$USER" || quietly $SUDO loginctl enable-linger "$USER" || true
    export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        systemctl --user show-environment >/dev/null 2>&1 && break
        sleep 1
    done
fi

if [ "$IS_TERMUX" = "1" ]; then
    setup_termux_service
    # runit owns the swap; restarting is safe even if this script is the caller.
    sv restart rook-band-worker 2>/dev/null || sv up rook-band-worker 2>/dev/null || true
elif command -v systemctl &>/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
    mkdir -p ~/.config/systemd/user
    cat > ~/.config/systemd/user/rook-band-worker.service << ROOKSVC
[Unit]
Description=Rook Band Worker
After=network-online.target rook-ydotoold.service
Wants=network-online.target rook-ydotoold.service

[Service]
ExecStart=$WORKER_CMD
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
ROOKSVC
    quietly systemctl --user daemon-reload
    quietly systemctl --user enable rook-band-worker || true
    # Enable lingering so the service survives logout/reboot (best-effort)
    quietly loginctl enable-linger "$USER" || quietly $SUDO loginctl enable-linger "$USER" || true
    # Hand the swap to systemd. `restart` stops the old instance and starts the
    # new one from the unit above — owned by the user manager, NOT this script —
    # so it completes even if stopping the old worker kills this caller.
    systemctl --user restart rook-band-worker
else
    # No service manager. Start the NEW worker in its own session FIRST (so it
    # survives even if reaping the old worker kills this script), then reap any
    # other band-worker processes except the one we just launched: only this
    # account's, running this install's bundle (as root, never other users').
    setsid bash -c "exec $WORKER_CMD >> $HOME/.rook-band-worker/worker.log 2>&1" </dev/null >/dev/null 2>&1 &
    NEWPID=$!
    sleep 2
    for pid in $(pgrep -u "$(id -u)" -f -- "$PYZ" 2>/dev/null); do
        [ "$pid" = "$NEWPID" ] && continue
        kill "$pid" 2>/dev/null || true
    done
    if kill -0 "$NEWPID" 2>/dev/null; then
        say "      No service manager here: the worker runs until the next reboot (log: ~/.rook-band-worker/worker.log). Run this command again after a reboot."
    else
        say "ERROR: the worker stopped right after starting. Its log: ~/.rook-band-worker/worker.log"
        tail -n 20 ~/.rook-band-worker/worker.log 2>/dev/null
        exit 1
    fi
fi

# Keyboard/mouse setup runs LAST, on purpose: the worker is already on the band, so a
# slow/hung/failed ydotool install (e.g. a stalled pacman) can never strand it.
# The worker detects its backend lazily on the first hid.* call, by which time
# ydotool is installed — so no restart is needed.
setup_hid_linux

BAND="$("$VPY" "$PYZ" --enrolled-band 2>/dev/null || true)"
say "Done: $WORKER_NAME joined ${{BAND:-the band}}. It appears under Workers in the dashboard within a minute."
say "To check on it from this machine, open a new terminal and run: rook status"
'''


# Enrollment retry for pairing-code installs, spliced into the scripts above
# (as {enroll_retry}; inserted verbatim, so single braces). The first attempt
# runs the code from the command the user copied; if that fails (most often
# because the 5-minute code expired while Python/pip installed), ask for the
# fresh code the dashboard is showing instead of making them start over.
_PS_ENROLL_RETRY = r'''        # Only a person can type a fresh code.
        while ($rc -ne 0 -and $interactive) {
            Write-Host "[rook] Pairing codes work once and expire after 5 minutes; the dashboard's Install page shows a fresh one."
            $fresh = ""
            try { $fresh = "$(Read-Host '[rook] Type the new 6-character code to retry, or press Enter to stop')".Trim().ToLower() } catch {}
            if (-not $fresh) { break }
            & $vpy $pyz --enroll __BASE__ --pair-code $fresh @enrollExtra
            $rc = $LASTEXITCODE
        }
'''

_BASH_ENROLL_RETRY = r'''# Only a person at a terminal can type a fresh code.
while [ "$RC" -ne 0 ] && [ "$HAVE_TTY" = 1 ]; do
    echo "[rook] Pairing codes work once and expire after 5 minutes; the dashboard's Install page shows a fresh one."
    printf '%s' "[rook] Type the new 6-character code to retry, or press Enter to stop: "
    FRESH=""; read -r FRESH </dev/tty || FRESH=""
    FRESH="$(printf '%s' "$FRESH" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')"
    [ -n "$FRESH" ] || break
    RC=0; enroll --enroll __BASE__ --pair-code "$FRESH" || RC=$?
done
'''
# `curl -fsSL https://<host>/install | bash` — one installer, selectable target.
#   interactive : prompts worker / cli / both
#   unattended  : `... | bash -s -- worker|cli|both`  (or ROOK_INSTALL=…)
# stdin is the curl pipe, so interactive prompts read from /dev/tty.
_INSTALL_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail
BASE="${ROOK_BASE:-__BASE__}"
DEST="${ROOK_BIN:-$HOME/.local/bin}"
CONF="$HOME/.config/rook/band.conf"

# A prompt needs a person at a terminal: /dev/tty must open (it exists but
# can't be opened under cloud-init, systemd or CI) and ROOK_YES must be unset.
has_tty() { [ -z "${ROOK_YES:-}" ] && { true </dev/tty; } 2>/dev/null; }

TARGET="${1:-${ROOK_INSTALL:-}}"
if [ -z "$TARGET" ]; then
  if has_tty; then
    {
      echo "[rook] what do you want to install?"
      echo "  1) worker  — make this machine a band node (controlled remotely)"
      echo "  2) cli     — the 'rook band' terminal control panel (drive the fleet)"
      echo "  3) both"
      printf "[rook] choice [1/2/3]: "
    } > /dev/tty
    read -r c < /dev/tty || true
    case "$c" in 1) TARGET=worker;; 3) TARGET=both;; *) TARGET=cli;; esac
  else
    TARGET=cli
  fi
fi
case "$TARGET" in tui|controller|dashboard) TARGET=cli;; node|agent) TARGET=worker;; esac

install_worker() {
  echo "[rook] installing band worker…"
  local JOIN_CODE="${ROOK_JOIN_CODE:-__JOIN_CODE__}"
  if [ -z "$JOIN_CODE" ] && has_tty; then
    printf '[rook] pairing code, or Enter to sign in with Google/local account: ' > /dev/tty
    read -r JOIN_CODE < /dev/tty
  fi
  if [ -z "$JOIN_CODE" ]; then
    curl -fsSL "$BASE/worker?login=google" | bash
    return
  fi
  curl -fsSL "$BASE/worker?band=$JOIN_CODE" | bash
}

ensure_python() {
  command -v python3 >/dev/null 2>&1 && return 0
  echo "[rook] python3 not found — installing…"
  local SUDO=""; [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
  if command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update -qq && $SUDO apt-get install -y -qq python3 curl
  elif command -v dnf >/dev/null 2>&1; then $SUDO dnf install -y python3 curl
  elif command -v pacman >/dev/null 2>&1; then $SUDO pacman -Sy --noconfirm python curl
  elif command -v apk >/dev/null 2>&1; then $SUDO apk add python3 curl
  elif command -v zypper >/dev/null 2>&1; then $SUDO zypper -n install python3 curl
  elif command -v brew >/dev/null 2>&1; then brew install python3
  elif command -v pkg >/dev/null 2>&1; then pkg install -y python curl
  else echo "[rook] ERROR: no supported package manager — install python3 manually and re-run" >&2; return 1; fi
  command -v python3 >/dev/null 2>&1
}

install_cli() {
  # `both`, or adding the CLI to an existing worker, must keep the launcher tied
  # to the signed bundle instead of overwriting it with a standalone copy.
  local WORKER_PY="$HOME/.rook-band-worker/venv/bin/python"
  local WORKER_BUNDLE="$HOME/.rook-band-worker/band-worker.pyz"
  if [ -x "$WORKER_PY" ] && [ -f "$WORKER_BUNDLE" ] && "$WORKER_PY" "$WORKER_BUNDLE" --help 2>/dev/null | grep -q -- --install-cli; then
    "$WORKER_PY" "$WORKER_BUNDLE" --install-cli
    return
  fi
  ensure_python || { echo "[rook] python3 is required for the CLI" >&2; return 1; }
  mkdir -p "$DEST" "$(dirname "$CONF")"
  echo "[rook] installing CLI -> $DEST/rook"
  if ! curl -fsSL "$BASE/rook.py" -o "$DEST/rook"; then
    echo "[rook] ERROR: could not download $BASE/rook.py" >&2; return 1; fi
  if ! head -1 "$DEST/rook" | grep -q python; then
    echo "[rook] ERROR: downloaded file is not the CLI" >&2; rm -f "$DEST/rook"; return 1; fi
  chmod +x "$DEST/rook"
  if grep -q '^pass=' "$CONF" 2>/dev/null; then
    echo "[rook] using saved login ($CONF)"
  elif [ -n "${ROOK_WEB_PASS:-}" ]; then
    ( umask 077; printf '# rook band connection (chmod 600)\nurl=%s\nuser=%s\npass=%s\n' "$BASE" "${ROOK_WEB_USER:-__WEB_USER__}" "$ROOK_WEB_PASS" > "$CONF" )
    chmod 600 "$CONF"; echo "[rook] saved login (from ROOK_WEB_PASS)"
  elif has_tty; then
    WU="${ROOK_WEB_USER:-__WEB_USER__}"
    printf "[rook] dashboard password (user %s): " "$WU" > /dev/tty
    read -rs WP < /dev/tty || true; printf "\n" > /dev/tty
    if [ -n "${WP:-}" ]; then
      ( umask 077; printf '# rook band connection (chmod 600)\nurl=%s\nuser=%s\npass=%s\n' "$BASE" "$WU" "$WP" > "$CONF" )
      chmod 600 "$CONF"; echo "[rook] saved login -> $CONF"
    fi
  else
    echo "[rook] no credentials set — run 'rook' later to configure"
  fi
  # Actually put $DEST on PATH (the #1 reason `rook` "isn't found" after install)
  # — append to whatever shell rc files exist, idempotently, + fish.
  case ":$PATH:" in
    *":$DEST:"*) ;;
    *)
      line="export PATH=\"$DEST:\$PATH\""
      touched=""
      for rc in "$HOME/.profile" "$HOME/.bashrc" "$HOME/.zshrc"; do
        [ "$rc" = "$HOME/.profile" ] || [ -e "$rc" ] || continue
        grep -qF "$DEST" "$rc" 2>/dev/null && continue
        printf '\n# added by rook installer\n%s\n' "$line" >> "$rc" && touched="$touched ${rc##*/}"
      done
      if command -v fish >/dev/null 2>&1; then
        fish -c "fish_add_path -g $DEST" >/dev/null 2>&1 && touched="$touched fish"
      fi
      export PATH="$DEST:$PATH"
      [ -n "$touched" ] && echo "[rook] added $DEST to PATH ($touched)"
      echo "[rook] open a NEW terminal, or run:  export PATH=\"$DEST:\$PATH\"" ;;
  esac
  hash -r 2>/dev/null || true
}

case "$TARGET" in
  worker) install_worker ;;
  cli)    install_cli ;;
  both)   install_worker; install_cli ;;
  *) echo "[rook] unknown target '$TARGET' — use: worker | cli | both" >&2; exit 2 ;;
esac
echo "[rook] done."
[ "$TARGET" != worker ] && [ -x "$DEST/rook" ] && echo "[rook] run:  rook"
'''


# `iex (irm https://<host>/install?os=windows)` — install the `rook` band CLI
# (the terminal control panel) on Windows. The Linux path is `.../install | bash`;
# this is its PowerShell equivalent. Uses .replace("__BASE__", …) rather than
# str.format so the PowerShell braces don't need doubling.
PS_CLI_BOOTSTRAP = r'''# R00K Band CLI (rook) installer - Windows
$ErrorActionPreference = "Stop"
# Suppress the progress bar (faster downloads + no console-buffer crash when run
# headless / with redirected output, e.g. automation or a remote shell).
$ProgressPreference = "SilentlyContinue"

# Resolve a WORKING python (ignore the Microsoft Store python.exe alias stub;
# prefer the `py` launcher). Mirrors the worker installer's resolver.
function Resolve-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $v = & py -3 --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$v" -match '^Python \d') { return @{ Exe = 'py'; Args = @('-3') } }
        } catch {}
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        try {
            $v = & python --version 2>&1
            if ($LASTEXITCODE -eq 0 -and "$v" -match '^Python \d') { return @{ Exe = 'python'; Args = @() } }
        } catch {}
    }
    return $null
}

$py = Resolve-Python
if (-not $py) {
    Write-Host "[rook] Python not found - installing..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements -h
    } else {
        $pyUrl = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe"
        $pyInstaller = "$env:TEMP\python_install.exe"
        Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller
        Start-Process -Wait -FilePath $pyInstaller -ArgumentList "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_launcher=1"
        Remove-Item $pyInstaller
    }
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $py = Resolve-Python
    if (-not $py) {
        $hunt = @(
            "$env:LocalAppData\Programs\Python\Python312\python.exe",
            "$env:LocalAppData\Programs\Python\Python313\python.exe",
            "C:\Python312\python.exe", "C:\Python313\python.exe"
        ) | Where-Object { Test-Path $_ }
        if ($hunt) { $py = @{ Exe = $hunt[0]; Args = @() } }
    }
    if (-not $py) { Write-Host "[rook] ERROR: no working Python; install Python 3 and re-run."; exit 1 }
}
Write-Host "[rook] Python: $(& $py.Exe @($py.Args) --version)"

# Dedicated CLI venv (independent of the worker). windows-curses gives the
# stdlib-curses TUI a backend on Windows (it isn't in the stdlib there).
$root = "$env:USERPROFILE\.rook-cli"
$venv = "$root\venv"
New-Item -ItemType Directory -Force -Path $root | Out-Null
if (-not (Test-Path "$venv\Scripts\python.exe")) {
    Write-Host "[rook] creating venv at $venv ..."
    & $py.Exe @($py.Args) -m venv "$venv"
    if (-not (Test-Path "$venv\Scripts\python.exe")) { Write-Host "[rook] ERROR: venv creation failed."; exit 1 }
}
$vpy = "$venv\Scripts\python.exe"
& $vpy -m pip install --quiet --upgrade pip 2>$null
Write-Host "[rook] installing dependency: windows-curses ..."
& $vpy -m pip install --quiet windows-curses

# Download the standalone CLI payload.
$rookpy = "$root\rook.py"
Invoke-WebRequest -Uri "__BASE__/rook.py" -OutFile $rookpy

# Launcher shim on PATH: `rook` -> venv python running the CLI.
$bin = "$root\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$launcher = "@echo off`r`n`"$vpy`" `"$rookpy`" %*`r`n"
Set-Content -Path "$bin\rook.cmd" -Value $launcher -Encoding ASCII -NoNewline

# Add the bin dir to the USER PATH (idempotent) + this session.
$userPath = [System.Environment]::GetEnvironmentVariable("PATH", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ';') -notcontains $bin) {
    [System.Environment]::SetEnvironmentVariable("PATH", ($userPath.TrimEnd(';') + ";" + $bin), "User")
    Write-Host "[rook] added $bin to your user PATH."
}
if (($env:PATH -split ';') -notcontains $bin) { $env:PATH = "$env:PATH;$bin" }

# Optional unattended connection config (env: ROOK_WEB_URL/USER/PASS).
if ($env:ROOK_WEB_PASS) {
    $cfgdir = "$env:USERPROFILE\.config\rook"
    New-Item -ItemType Directory -Force -Path $cfgdir | Out-Null
    $u = if ($env:ROOK_WEB_URL) { $env:ROOK_WEB_URL } else { "__BASE__" }
    $usr = if ($env:ROOK_WEB_USER) { $env:ROOK_WEB_USER } else { "__WEB_USER__" }
    "# rook band - dashboard connection`nurl=$u`nuser=$usr`npass=$($env:ROOK_WEB_PASS)" |
        Set-Content -Path "$cfgdir\band.conf" -Encoding UTF8
    Write-Host "[rook] saved connection config (from ROOK_WEB_PASS)."
}

Write-Host "[rook] installed. Open a NEW terminal and run:  rook"
Write-Host "[rook] (or run it now: $bin\rook.cmd)"
'''


# `curl -fsSL https://<host>/hub | bash` — stand up a telesthete-hub (the band
# relay) on this machine. A short wizard populates the vars, then it installs the
# binary + a hardened systemd unit and starts it. Non-interactive: pass `--yes`
# (or set HUB_YES=1) and override any var via the environment (HUB_BIND, …).
#
# Binary acquisition, in order: (1) prebuilt from the host for this arch, else
# (2) `cargo build` from the public source if a Rust toolchain is present.
_HUB_INSTALL_SCRIPT = r'''#!/usr/bin/env bash
set -euo pipefail
BASE="${ROOK_BASE:-__BASE__}"
SRC_REPO="${HUB_SRC_REPO:-https://github.com/Bake-Ware/telesthete}"

# Vars — an env value skips its prompt (so unattended installs just set these).
HUB_BIND="${HUB_BIND:-}"
HUB_PEER_TTL_SECS="${HUB_PEER_TTL_SECS:-}"
HUB_PRUNE_SECS="${HUB_PRUNE_SECS:-}"
RUST_LOG="${RUST_LOG:-}"
HUB_USER="${HUB_USER:-}"
PREFIX="${HUB_PREFIX:-/usr/local/bin}"
ASSUME_YES="${HUB_YES:-}"
if [ "${1:-}" = "-y" ] || [ "${1:-}" = "--yes" ]; then ASSUME_YES=1; fi

say(){ echo "[hub] $*"; }
die(){ echo "[hub] ERROR: $*" >&2; exit 1; }

SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "run as root, or install sudo"
  SUDO="sudo"
fi

# ---- dependency install (same spirit as the worker bootstrap) -----------
PKG=""
for m in apt-get dnf pacman apk zypper brew pkg; do
  command -v "$m" >/dev/null 2>&1 && { PKG="$m"; break; }
done

# ensure_cmd <command> <apt> <dnf> <pacman> <apk> <zypper> <brew> <pkg>
# Installs the right package for the detected manager only if <command> is missing.
ensure_cmd(){
  local cmd="$1"; shift
  command -v "$cmd" >/dev/null 2>&1 && return 0
  say "installing dependency: $cmd"
  case "$PKG" in
    apt-get) $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$1" ;;
    dnf)     $SUDO dnf install -y "$2" ;;
    pacman)  $SUDO pacman -Sy --noconfirm "$3" ;;
    apk)     $SUDO apk add "$4" ;;
    zypper)  $SUDO zypper -n install "$5" ;;
    brew)    brew install "$6" ;;
    pkg)     $SUDO pkg install -y "$7" ;;
    *) say "WARNING: no known package manager to install $cmd"; return 1 ;;
  esac
  command -v "$cmd" >/dev/null 2>&1
}

# curl is needed to fetch the binary — install it before anything else.
ensure_cmd curl curl curl curl curl curl curl curl || die "curl is required and could not be installed"

# ask VAR "prompt" "default" — env-provided value wins; --yes/no-tty takes default.
ask(){
  local __v="$1" __p="$2" __d="$3" __cur __in
  eval "__cur=\${$__v}"
  if [ -n "$__cur" ]; then return; fi
  # /dev/tty exists but can't be opened under cloud-init, systemd or CI.
  if [ -n "$ASSUME_YES" ] || ! { true </dev/tty; } 2>/dev/null; then eval "$__v=\$__d"; return; fi
  printf "[hub] %s [%s]: " "$__p" "$__d" > /dev/tty
  read -r __in < /dev/tty || true
  [ -z "$__in" ] && __in="$__d"
  eval "$__v=\$__in"
}

say "telesthete-hub installer — the band relay (dumb band_id forwarder, holds no keys)"
ask HUB_BIND          "UDP bind address (host:port)"    "0.0.0.0:7474"
ask HUB_PEER_TTL_SECS "peer idle eviction, seconds"     "60"
ask HUB_PRUNE_SECS    "eviction sweep interval, seconds" "10"
ask RUST_LOG          "log level (error|warn|info|debug)" "info"
ask HUB_USER          "service user"                    "telesthete"

ARCH="$(uname -m)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/telesthete-hub"

is_elf(){ head -c4 "$1" 2>/dev/null | grep -qa $'\x7fELF'; }

# Match the ELF e_machine field (offset 18, 2 bytes) to the host arch, so we
# never install a wrong-arch binary — WITHOUT executing it. (An exec-based
# check breaks on hosts that mount /tmp noexec, exactly the hardened boxes a
# relay tends to run on.) systemd's start + is-active is the real liveness gate.
elf_arch_ok(){
  command -v od >/dev/null 2>&1 || return 0   # can't inspect -> trust arch-gated download
  local m; m=$(od -An -tu2 -j18 -N2 "$1" 2>/dev/null | tr -d ' ')
  local want
  case "$ARCH" in
    x86_64|amd64)        want=62  ;;
    aarch64|arm64)       want=183 ;;
    armv7l|armv6l|arm)   want=40  ;;
    i386|i486|i586|i686) want=3   ;;
    riscv64)             want=243 ;;
    ppc64le)             want=21  ;;
    s390x)               want=22  ;;
    *) return 0 ;;                            # unknown host arch -> don't block
  esac
  [ "$m" = "$want" ]
}

acquire_prebuilt(){
  say "fetching prebuilt binary for $ARCH…"
  curl -fsSL "$BASE/telesthete-hub?arch=$ARCH" -o "$BIN" 2>/dev/null || return 1
  is_elf "$BIN" || return 1                   # a 404/HTML body is not an ELF
  elf_arch_ok "$BIN" || { say "prebuilt is not $ARCH — building from source instead"; return 1; }
  chmod +x "$BIN"
  return 0
}

acquire_cargo(){
  say "no usable prebuilt for $ARCH — building from source with cargo"
  # Build deps: git (fetch), a C compiler+linker (cargo links via cc), and the
  # Rust toolchain. Install any that are missing, package-manager permitting.
  ensure_cmd git git git git git git git git || return 1
  if ! command -v cc >/dev/null 2>&1 && ! command -v gcc >/dev/null 2>&1; then
    ensure_cmd gcc build-essential gcc base-devel build-base gcc gcc gcc || \
      ensure_cmd cc build-essential clang base-devel build-base clang clang clang || true
  fi
  ensure_cmd cargo cargo cargo rust cargo cargo rust rust || {
    say "no Rust toolchain and none installable — cannot build from source"; return 1; }
  say "building telesthete-hub (this can take a minute or two)…"
  git clone --depth 1 "$SRC_REPO" "$TMP/src" >/dev/null 2>&1 || return 1
  ( cd "$TMP/src/rust" && cargo build --release -p telesthitium ) || return 1
  [ -x "$TMP/src/rust/target/release/telesthete-hub" ] || return 1
  cp "$TMP/src/rust/target/release/telesthete-hub" "$BIN"; chmod +x "$BIN"; return 0
}

acquire_prebuilt || acquire_cargo || die "could not obtain a telesthete-hub binary for $ARCH \
(no prebuilt, and no cargo+git to build). Build it manually from $SRC_REPO — rust/, \
'cargo build --release -p telesthitium' — and re-run, or drop the binary at $PREFIX/telesthete-hub."

say "installing binary -> $PREFIX/telesthete-hub"
$SUDO install -m 0755 "$BIN" "$PREFIX/telesthete-hub"

# System service account: no home, no login shell.
if ! id "$HUB_USER" >/dev/null 2>&1; then
  say "creating system user '$HUB_USER'"
  $SUDO useradd --system --no-create-home --shell /usr/sbin/nologin "$HUB_USER" 2>/dev/null \
    || $SUDO useradd --system --no-create-home "$HUB_USER" 2>/dev/null || true
fi

# The wizard's values live in an EnvironmentFile; the unit reads them, so
# re-running or editing this file + `systemctl restart` re-tunes the hub.
say "writing /etc/telesthete-hub.env"
$SUDO tee /etc/telesthete-hub.env >/dev/null <<ENV
HUB_BIND=$HUB_BIND
HUB_PEER_TTL_SECS=$HUB_PEER_TTL_SECS
HUB_PRUNE_SECS=$HUB_PRUNE_SECS
RUST_LOG=$RUST_LOG
ENV
$SUDO chmod 0644 /etc/telesthete-hub.env

if command -v systemctl >/dev/null 2>&1; then
  say "installing hardened systemd unit"
  $SUDO tee /etc/systemd/system/telesthete-hub.service >/dev/null <<UNIT
[Unit]
Description=Telesthete Hub — discovery + relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$HUB_USER
EnvironmentFile=-/etc/telesthete-hub.env
ExecStart=$PREFIX/telesthete-hub
Restart=on-failure
RestartSec=3s

# Sandboxing
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6
RestrictNamespaces=true
LockPersonality=true
MemoryDenyWriteExecute=true
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
AmbientCapabilities=
CapabilityBoundingSet=

[Install]
WantedBy=multi-user.target
UNIT
  $SUDO systemctl daemon-reload
  $SUDO systemctl enable --now telesthete-hub >/dev/null 2>&1 || $SUDO systemctl restart telesthete-hub || true
  sleep 1
  if $SUDO systemctl is-active --quiet telesthete-hub; then
    say "telesthete-hub RUNNING on $HUB_BIND  (systemd: telesthete-hub)"
    say "logs:   journalctl -u telesthete-hub -f"
    say "tune:   edit /etc/telesthete-hub.env, then systemctl restart telesthete-hub"
  else
    say "service failed to start — recent logs:"
    $SUDO systemctl status telesthete-hub --no-pager -l 2>/dev/null | tail -20 || true
    exit 1
  fi
else
  say "no systemd here — start it yourself:"
  echo "    HUB_BIND=$HUB_BIND HUB_PEER_TTL_SECS=$HUB_PEER_TTL_SECS HUB_PRUNE_SECS=$HUB_PRUNE_SECS RUST_LOG=$RUST_LOG $PREFIX/telesthete-hub"
fi
say "done. Point workers at this host:  --hub <this-host>:${HUB_BIND##*:}"
'''


class CombinedServer:
    """Single-port HTTP server: dashboard, installers and band control."""

    def __init__(self, port: int = 7005, domain: str = "hub.example.com",
                 web_user: str = "", web_pass: str = "",
                 band_psk: str = "",
                 hub_host: str = "127.0.0.1", hub_port: int = 7474,
                 hub_public: str = "hub.example.com:443", band_name: str = "rook-band",
                 bind: str = "0.0.0.0", explicit: dict | None = None,
                 argv: list | None = None, *, enrollment=None, band=None, chat=None,
                 settings_store=None):
        """``enrollment``, ``band``, ``chat`` and ``settings_store`` are for
        ``rook hub serve`` (rook/hub/serve.py), which runs the MCP server in
        this process: its enrollment store, band client, chat store and
        settings store, shared instead of opening a second of each. A ``band`` given here is started and stopped by its
        owner, not by :meth:`start` / :meth:`stop`."""
        self.port = port
        self.bind = bind
        self.domain = domain
        self.web_user = web_user
        self.web_pass = web_pass
        self.band_psk = band_psk
        self.hub_host = hub_host
        self.hub_port = hub_port
        self.hub_public = hub_public  # host:port workers call back to (templated into installers)
        self.band_name = band_name    # cosmetic band identity, shown in dashboard + installer
        # Hidden in the setup form: a page on another site can POST to a
        # password-less loopback hub, but it cannot read this value first.
        self._setup_csrf = secrets.token_urlsafe(24)
        # Failed sign-ins, shared with the MCP side in `rook hub serve`.
        self._login_throttle = shared_throttle()

        # Domain, relay address and band label: environment/flag > Settings
        # page (settings store) > first-run setup.json > default. ``explicit``
        # names the ones the environment or command line set (settings P1);
        # without it (older callers) setup.json keeps beating the constructor.
        from . import setup_store
        from . import dashboard_settings as _ds
        _s = setup_store.load()
        if _s.get("band_psk"):
            self.band_psk = _s["band_psk"]
        self._explicit = explicit
        self._argv = list(argv or [])   # for the Settings page: which flags lock a key
        self._settings_store = settings_store if settings_store is not None else _ds.open_store()
        self._ctor_settings = {"domain": self.domain, "hub_public": self.hub_public,
                               "band_name": self.band_name}
        _vals, self._setting_sources, self._setting_conflicts = _ds.resolve(
            self._ctor_settings, explicit, _s, _ds.stored_values(self._settings_store))
        self.domain, self.hub_public, self.band_name = (
            _vals["domain"], _vals["hub_public"], _vals["band_name"])
        self._apply_domain(self.domain)
        for _c in self._setting_conflicts:
            log.warning("setting %s: %s", _c["key"], _c["note"])
        self._settings_checked = 0.0
        self._started_at = time.time()
        self._band = None  # MultiBandClient — joins the hub to track workers + invoke caps
        # Shared site chat store — same sqlite file band_mcp serves over MCP, so
        # the dashboard chat panel and MCP agents share one set of rooms.
        self._chat = None
        try:
            from ..band_mcp.chat_rooms import ChatStore
            from ..paths import data_path
            from .dashboard_settings import chat_db_path
            chat_db = chat_db_path(self._settings_store,
                                   data_path("chat.db", "/var/lib/rook-band-mcp/chat.db"))
            if chat is not None:
                chat_db = chat.path
            self._chat_db = chat_db
            self._chat = chat if chat is not None else ChatStore(chat_db)
        except Exception:
            log.warning("chat store unavailable; dashboard chat disabled", exc_info=True)
        # Reverse secret masking for the chat this process writes and shows.
        # The dashboard holds no vault: the MCP bridge masks for it
        # (mask_client.py -> /internal/mask), with the token it keeps beside
        # its stores.
        from ..paths import data_path
        from .mask_client import BridgeMask
        self._bridge = BridgeMask([
            os.environ.get("ROOK_MASK_TOKEN_FILE"),
            os.path.join(os.path.dirname(getattr(self, "_chat_db", "") or ""), "mask.token")
            if getattr(self, "_chat_db", None) else None,
            data_path("mask.token", "/var/lib/rook-band-mcp/mask.token")])
        self._bridge_mask = self._bridge.mask
        self._push_task = None  # background: push signed manifest to behind workers
        # The bundle installers download: operator-built, else built here.
        from .worker_bundle import WorkerBundle
        self._bundle = WorkerBundle(lambda: self.public_base)
        from ..hub.update_feed import WorkerFeed
        self._feed = WorkerFeed()
        self._started_at = time.time()
        from .band_overview import BandOverview
        self._overview = BandOverview(self._band_worker_rows, self._overview_call)
        # Known bands for the dashboard selector. PSKs stay server-side; the UI
        # only ever sees the band_id label (first 8 hex of SHA256(PSK)[:16]).
        from .enrollment import EnrollmentStore
        self._enrollment = enrollment if enrollment is not None else EnrollmentStore()
        self._shared_band = band
        # The environment's key only seeds an empty database (never undoes a
        # rotation made on the Bands page).
        if self._enrollment.import_config([self.band_psk], hub=self.hub_public, seed_only=True):
            log.warning("ROOK_BAND_PSK/--psk holds a key the enrollment database does not use; "
                        "ignored (it only seeds the first band)")
            self._setting_conflicts.append({
                "key": "core.band.key", "env": "ROOK_BAND_PSK",
                "note": "ROOK_BAND_PSK holds a key the enrollment database does not use; "
                        "ignored. The environment only seeds the first band."})
        self._bands = self._enrollment.bands(active_only=True, secrets_visible=True)
        self._enrollment_task = None
        self._bans = setup_store.load_bans()   # deauthed workers (by name / worker_id)
        self._band_names: dict[str, str] = {}  # band_id label -> friendly name
        self._primary_label = ""               # the configured band; not removable
        try:
            from telesthete.protocol.crypto import derive_band_id
            for _b in self._bands:
                _lbl = derive_band_id(_b["psk"]).hex()[:8]
                self._band_names.setdefault(_lbl, _b["name"])
            _primary = next((b for b in self._bands if b["is_primary"]), None)
            if _primary:
                self.band_psk = _primary["psk"]
                self._primary_label = _primary["label"]
        except Exception:
            log.warning("could not derive band labels; selector names may be blank")
        self._app = web.Application(middlewares=[self._basic_auth_middleware,
                                                 self._principal_middleware])
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/worker", self._worker_bootstrap)
        self._app.router.add_post("/enroll", self._enroll_config)
        self._app.router.add_get("/band-worker.pyz", self._band_worker_pyz)
        self._app.router.add_get("/band-worker-enrollment.pyz", self._band_worker_pyz)
        self._app.router.add_get("/band-worker.json", self._band_worker_manifest)
        # The update service's worker feed (rook/hub/update_feed.py): every
        # build it keeps, by number, and the desktop app's update check.
        self._app.router.add_get("/updates/worker/{build}/band-worker.pyz", self._feed_pyz)
        self._app.router.add_get("/updates/desktop.json", self._desktop_update)
        self._app.router.add_get("/apk", self._worker_apk)
        self._app.router.add_get("/apk.json", self._worker_apk_manifest)
        self._app.router.add_get("/install", self._installer)    # unified installer (worker | cli | both)
        # Pre-/install URL, still in users' saved one-liners: not a peer, so
        # the rook.core.compat floor doesn't retire it.
        self._app.router.add_get("/rook", self._installer)
        self._app.router.add_get("/rook.py", self._band_cli)     # the standalone CLI script itself
        self._app.router.add_get("/hub", self._hub_installer)    # stand up a telesthete-hub (band relay)
        self._app.router.add_get("/telesthete-hub", self._hub_binary)  # prebuilt hub binary (arch-gated)
        self._app.router.add_get("/api/bands", self._api_bands)
        self._app.router.add_post("/api/bands", self._api_add_band)
        self._app.router.add_post("/api/bands/psk", self._api_generate_psk)
        self._app.router.add_delete("/api/bands/{id}", self._api_remove_band)
        self._app.router.add_get("/api/band/workers", self._api_band_workers)
        self._app.router.add_get("/api/band/overview", self._api_band_overview)
        self._app.on_cleanup.append(self._close_overview)
        self._app.router.add_post("/api/band/call", self._api_band_call)
        self._app.router.add_get("/api/band/whoami", self._api_band_whoami)
        self._app.router.add_get("/api/band/bans", self._api_bans)
        self._app.router.add_post("/api/band/ban", self._api_ban)
        self._app.router.add_post("/api/band/unban", self._api_unban)
        # Chat panel — reads/writes the same site chat store band_mcp serves
        # over MCP (shared sqlite), so the dashboard mirrors the rook CLI chat.
        self._app.router.add_get("/api/chat/rooms", self._api_chat_rooms)
        self._app.router.add_get("/api/chat/read", self._api_chat_read)
        self._app.router.add_post("/api/chat/send", self._api_chat_send)
        self._app.router.add_post("/api/chat/start", self._api_chat_start)
        self._app.router.add_post("/api/chat/wake", self._api_chat_wake)
        self._app.router.add_post("/api/chat/delete", self._api_chat_delete)
        self._app.router.add_get("/api/avatars", self._api_avatars)
        self._app.router.add_get("/api/avatar", self._api_avatar)
        self._app.router.add_get("/api/presence", self._api_presence)
        # Auth routes (handled by middleware, these are just route stubs)
        async def _noop(r): return web.Response(text="")
        self._app.router.add_get("/login", _noop)
        self._app.router.add_post("/login", _noop)
        self._app.router.add_get("/logout", _noop)
        self._app.router.add_get("/setup", self._setup_page)
        self._app.router.add_post("/setup", self._setup_submit)
        self._app.router.add_get("/health", self._health)
        from .account_web import AccountWeb
        self._accounts = AccountWeb(self)
        self._accounts.install(self._app)
        # Permissions page + policy API (docs/design/permissions.md 3.10).
        self._authz = None
        from .policy_web import PolicyWeb
        PolicyWeb(self).install(self._app)
        # Approvals page API: held agent calls wait for the operator here (3.11).
        from .approvals_web import ApprovalsWeb
        ApprovalsWeb(self).install(self._app)
        self._runner: web.AppRunner | None = None
        # Seconds stop() lets open requests (long-polls, terminal sockets)
        # finish before cancelling them; aiohttp's default.
        self.shutdown_timeout = 60.0


    def _session_ok(self, request: web.Request) -> bool:
        return self._accounts.store.dashboard_session_ok(request.cookies.get("rook_session", ""))

    def _signed_in(self, location: str, request: web.Request,
                   weak: bool = False) -> web.HTTPFound:
        """Redirect that starts a new random dashboard session. ``weak`` marks
        a session whose password is a vault value, so it can't approve held
        calls (permissions 3.11)."""
        from .public_url import set_cookie
        resp = web.HTTPFound(location)
        token = self._accounts.store.dashboard_session()
        if weak:
            self._accounts.store.mark_weak_session(token)
        # No configured floor (base ""): this cookie was never Secure, and
        # requiring it from the https:// setting would lock operators out of
        # an https hub they open by its raw http://<ip>:<port>. It is Secure
        # whenever the request itself came over HTTPS (directly or via a proxy).
        set_cookie(resp, request, "", "rook_session", token, max_age=30 * 86400)
        return resp

    async def credential_is_vaulted(self, *values: str) -> "bool | None":
        """Whether any of ``values`` (a password, or ``user:pass``) is a stored
        vault secret, so agents could use it to sign in. ``None`` when the
        vault can't be reached (callers fail closed for approvals)."""
        bridge = getattr(self, "_bridge", None)
        if bridge is None:
            return None
        unknown = False
        for v in values:
            if not v:
                continue
            got = await bridge.is_secret(v)
            if got is True:
                return True
            if got is None:
                unknown = True
        return None if unknown else False

    # GET paths where a dashboard (shared password) login is carried over to
    # the operator's account session: the account pages and the JSON the
    # dashboard's account-backed views open with.
    _BRIDGE_PATHS = ("/account", "/account/bands", "/account/bands/component",
                     "/account/bands/api", "/account/component", "/account/tokens/api",
                     "/account/session", "/account/settings/api", "/account/knowledge/api",
                     "/account/guidance/api", "/account/vault/api", "/account/jobs/api",
                     "/account/plugins/api", "/account/huddle/api", "/account/ui/api",
                     "/account/manual/api")
    # Set (never Secure, holds nothing secret) on the bridge's redirect: if it
    # comes back without the account cookie, the browser dropped that cookie
    # and redirecting again would loop forever.
    _BRIDGE_MARK = "rook_bridge"

    def _bridge_failed(self, request: web.Request) -> web.Response:
        """The account cookie set one hop ago did not come back: explain
        instead of redirecting again (ERR_TOO_MANY_REDIRECTS before)."""
        from .account_web import NO_STORE, esc
        origin = self.public_base
        message = (f"Your browser did not keep the Rook sign-in cookie. This dashboard is set "
                   f"up for {origin}; open it at that address. If that address is wrong (for "
                   f"example a LAN hub reached over plain HTTP), set ROOK_DOMAIN to the "
                   f"address you use, such as http://<host>:{self.port}, and restart the dashboard.")
        if (request.headers.get("Sec-Fetch-Mode") == "navigate"
                or "text/html" in request.headers.get("Accept", "")):
            response = self._accounts.response("Sign-in cookie refused",
                                               "<p>" + esc(message) + "</p>", 400)
        else:
            response = web.json_response({"error": message}, status=400, headers=NO_STORE)
        response.del_cookie(self._BRIDGE_MARK)   # a reload tries the bridge once more
        return response

    def _check_creds(self, user: str, passwd: str) -> bool:
        """Validate login. Username is enforced only when one is configured,
        so a bare password still works for password-only setups. Constant
        time: both fields are always compared."""
        def same(given, expected) -> bool:
            return hmac.compare_digest(str(given).encode("utf-8"),
                                       str(expected).encode("utf-8"))
        pass_ok = same(passwd, self.web_pass)
        if self.web_user:
            return same(user, self.web_user) & pass_ok
        return pass_ok

    def _throttle_keys(self, request: web.Request) -> tuple:
        """The login throttle's keys for this request (rook.core.throttle)."""
        return client_keys(request.remote, request.headers)

    def _throttled_login_page(self, seconds: int) -> web.Response:
        return web.Response(text=self._login_page(throttle_message(seconds)),
                            status=429, content_type="text/html",
                            headers={"Retry-After": str(seconds), "Cache-Control": "no-store"})

    @staticmethod
    def _throttled_text(seconds: int) -> web.Response:
        return web.Response(text=throttle_message(seconds), status=429,
                            headers={"Retry-After": str(seconds), "Cache-Control": "no-store"})

    _AUTH_EXEMPT = ("/health", "/worker", "/band-worker.pyz",
                    "/band-worker-enrollment.pyz", "/band-worker.json", "/updates/worker",
                    "/updates/desktop.json", "/enroll", "/install",
                    "/rook", "/rook.py", "/hub", "/telesthete-hub")

    @classmethod
    def _auth_exempt(cls, path: str) -> bool:
        return any(path == p or path.startswith(p + "/") for p in cls._AUTH_EXEMPT)

    @web.middleware
    async def _basic_auth_middleware(self, request: web.Request, handler):
        """Password-only gate, mirroring the MCP /tokens endpoint.

        A single shared password (``web_pass``) unlocks the dashboard. Once
        entered it sets a session cookie; headless callers can pass the
        password via HTTP Basic auth (in either the user or pass field).
        Installer/health endpoints stay public. Auth is off when no password
        is configured.

        Until the band is configured every other page redirects to /setup.
        With a password, /setup itself needs the login first; without one the
        hub only starts on loopback (or with --insecure-no-auth).
        """
        import base64
        from . import setup_store

        # Account routes enforce their own sessions, CSRF and band ownership.
        # A Google/member login never unlocks the legacy global admin APIs.
        if self._accounts.handles(request.path):
            bridged = request.path in self._BRIDGE_PATHS and request.method in ("GET", "HEAD")
            signed_in = self._accounts.current(request) is not None
            if (bridged and not signed_in and self.web_pass and self._accounts.bootstrap_id
                    and self._session_ok(request)):
                if request.cookies.get(self._BRIDGE_MARK):
                    return self._bridge_failed(request)
                from .account_web import NO_STORE
                response = web.HTTPFound(str(request.rel_url), headers=NO_STORE)
                self._accounts.start_session(response, request, self._accounts.bootstrap_id)
                response.set_cookie(self._BRIDGE_MARK, "1", max_age=60, httponly=True,
                                    samesite="Lax")
                raise response
            clear_mark = signed_in and bool(request.cookies.get(self._BRIDGE_MARK))
            try:
                response = await handler(request)
            except web.HTTPException as exc:   # e.g. /account/bands redirects
                if clear_mark:
                    exc.del_cookie(self._BRIDGE_MARK)
                raise
            if clear_mark and not response.prepared:
                response.del_cookie(self._BRIDGE_MARK)
            return response

        # Always public: worker bootstrap/artifacts, health, websockets.
        is_exempt = self._auth_exempt(request.path)
        if request.path in ("/apk", "/apk.json") and os.environ.get("ROOK_PUBLIC_APK_SHA256"):
            is_exempt = True  # handler verifies the exact approved generic artifact

        if is_exempt:
            return await handler(request)

        # First-run gate: until the band has a PSK + public hub address, only
        # the setup wizard (and the login that guards it) is reachable.
        unconfigured = (not setup_store.is_configured()
                        and not any(b["is_primary"] for b in self._enrollment.bands()))

        async def admit():
            if unconfigured and request.path != "/setup":
                if "text/html" in request.headers.get("Accept", ""):
                    raise web.HTTPFound("/setup")
                return web.Response(status=503, text="rook hub not configured — open /setup in a browser")
            return await handler(request)

        if not self.web_pass:
            return await admit()

        account_user = self._accounts.current(request)
        if account_user and request.path == "/logout":
            self._accounts.store.logout(request.cookies.get("rook_account", ""))
            self._accounts.store.dashboard_logout(request.cookies.get("rook_session", ""))
            response = web.HTTPFound("/account/login")
            response.del_cookie("rook_account")
            response.del_cookie("rook_session")
            return response
        if account_user and account_user["admin"]:
            if request.path == "/login":
                raise web.HTTPFound("/")
            return await admit()
        if account_user and request.path == "/":
            raise web.HTTPFound("/account")

        # Login endpoint — username + password. A locked-out address gets
        # 429 before the password is looked at (rook.core.throttle).
        if request.path == "/login" and request.method == "POST":
            keys = self._throttle_keys(request)
            wait = self._login_throttle.retry_after(keys)
            if wait:
                return self._throttled_login_page(wait)
            try:
                data = await request.post()
                if self._check_creds(data.get("user", ""), data.get("pass", "")):
                    self._login_throttle.success(keys)
                    pw = data.get("pass", "")
                    weak = await self.credential_is_vaulted(
                        pw, f"{data.get('user', '')}:{pw}" if data.get("user") else "")
                    raise self._signed_in("/setup" if unconfigured else "/", request,
                                          weak=weak is not False)
            except web.HTTPFound:
                raise
            except Exception:
                pass
            wait = self._login_throttle.failure(keys, "dashboard login")
            if wait:
                return self._throttled_login_page(wait)
            return web.Response(text=self._login_page(
                "That password is not right." if not self.web_user
                else "That username and password are not right."), content_type="text/html")

        if request.path == "/login":
            return web.Response(text=self._login_page(), content_type="text/html")

        if request.path == "/logout":
            self._accounts.store.dashboard_logout(request.cookies.get("rook_session", ""))
            resp = web.HTTPFound("/login")
            resp.del_cookie("rook_session")
            raise resp

        if self._session_ok(request):
            return await admit()

        # Check basic auth (for API/curl): password in either field.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Basic "):
            keys = self._throttle_keys(request)
            wait = self._login_throttle.retry_after(keys)
            if wait:
                return self._throttled_text(wait)
            # Only the credential parsing is guarded — the handler runs outside
            # the try so a handler's own HTTP exceptions (404, 400, redirects)
            # reach the client instead of being swallowed into a bogus 401.
            creds_ok = False
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                user, _, passwd = decoded.partition(":")
                creds_ok = self._check_creds(user, passwd)
            except Exception:
                creds_ok = False
            if creds_ok:
                self._login_throttle.success(keys)
                # No cookie: headless callers resend Basic auth each request,
                # and minting a session per call would pile up stored grants.
                return await admit()
            wait = self._login_throttle.failure(keys, "dashboard basic-auth")
            if wait:
                return self._throttled_text(wait)

        # No valid auth — redirect to login page for browsers, 401 for API
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            raise web.HTTPFound("/login")

        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="r00k"'},
            text="Unauthorized",
        )

    def _login_page(self, error: str = "") -> str:
        """The dashboard sign-in form. A hub set up with a password only
        (the installers' default, one operator) asks for just the password:
        the username field is hidden, since any value would do. A
        configured username keeps its field."""
        from .web_pages import render
        err = f'<p class="error" role="alert">{_html.escape(error)}</p>' if error else ""
        if self.web_user:
            field = ('<label>Username <input name="user" autofocus required '
                     'autocomplete="username"></label>')
            lead, focus = "Sign in to the dashboard.", ""
        else:
            # Hidden but present, so password managers still pair the saved
            # password with this site.
            field = '<input type="hidden" name="user" value="admin" autocomplete="username">'
            lead, focus = "Enter the dashboard password to continue.", " autofocus"
        return render("login.html", error=err, user_field=field, lead=lead, pass_focus=focus)

    @staticmethod
    def _setup_suggestions(request) -> dict:
        """First-run defaults for the wizard's two addresses, from the
        address the operator opened it at: on a single-process hub workers
        reach the band on the dashboard's own port, and installers download
        from it. Only ever a suggestion shown to the signed-in operator."""
        if request is None or not request.host:
            return {}
        from .public_url import request_is_https
        https = request_is_https(request)
        host = request.host
        hub = host if _re.search(r":\d+$", host) else f"{host}:{443 if https else 80}"
        return {"hub_public": hub, "pyz_domain": f"{'https' if https else 'http'}://{host}"}

    def _setup_page_html(self, error: str = "", request=None) -> str:
        from . import setup_store
        from .web_pages import render
        s = setup_store.load()
        primary = next((b for b in self._enrollment.bands(secrets_visible=True) if b["is_primary"]), None)
        current_key = (primary["psk"] if primary["active"] else "") if primary else s.get("band_psk")
        err = f'<p class="error" role="alert">{_html.escape(error)}</p>' if error else ""
        from ..hub.plugin_profiles import setup_presets_html, stored_preset_states
        try:
            states = stored_preset_states(self._settings_store, primary["id"] if primary else None)
        except Exception:
            log.warning("Setup wizard: reading the stored plugin choices failed", exc_info=True)
            states = None
        hint = self._setup_suggestions(request)
        return render("setup.html", error=err, csrf=self._setup_csrf,
                      presets=setup_presets_html(states),
                      band_name=s.get("band_name") or self.band_name,
                      hub_public=s.get("hub_public") or hint.get("hub_public", ""),
                      pyz_domain=s.get("pyz_domain") or hint.get("pyz_domain", ""),
                      band_psk=current_key or setup_store.gen_psk())

    async def _setup_page(self, request: web.Request) -> web.Response:
        return web.Response(text=self._setup_page_html(request=request), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def _setup_submit(self, request: web.Request) -> web.Response:
        from . import setup_store
        from .accounts import csrf_ok
        data = await request.post()
        if not csrf_ok(data, self._setup_csrf):
            return web.Response(text=self._setup_page_html("The form expired. Check the values and save again.", request),
                                status=400, content_type="text/html", headers={"Cache-Control": "no-store"})
        vals = {
            "band_name": (data.get("band_name") or "").strip() or "rook-band",
            "hub_public": (data.get("hub_public") or "").strip(),
            "pyz_domain": (data.get("pyz_domain") or "").strip(),
            "band_psk": (data.get("band_psk") or "").strip(),
        }
        if not vals["hub_public"] or not vals["band_psk"] or not vals["pyz_domain"]:
            return web.Response(
                text=self._setup_page_html("Fill in both addresses and the band key.", request),
                content_type="text/html",
                headers={"Cache-Control": "no-store"},
            )
        primary = next((b for b in self._enrollment.bands(secrets_visible=True) if b["is_primary"]), None)
        if primary and (not primary["active"] or vals["band_psk"] != primary["psk"]):
            return web.Response(text=self._setup_page_html("The band key can't be changed here: use the Tokens page to replace it.", request),
                                status=400, content_type="text/html", headers={"Cache-Control": "no-store"})
        saved = setup_store.save(vals)
        # Apply live so the installer one-liners reflect the new band immediately.
        self.band_name = saved["band_name"]
        self.band_psk = saved["band_psk"]
        self.hub_public = saved["hub_public"]
        self._apply_domain(saved["pyz_domain"])
        self._enrollment.import_config(hub=self.hub_public)
        await self._sync_enrollment()
        if data.get("presets_shown"):
            self._setup_presets(data.getall("preset", []))
        log.info("Setup wizard: band '%s' configured (hub=%s)", self.band_name, self.hub_public)
        raise web.HTTPFound("/")

    def _setup_presets(self, selected: list) -> None:
        """Store the wizard's "What do you want Rook to do?" choice: the hub's
        plugin switches and the primary band's defaults for new workers
        (rook.hub.plugin_profiles; the MCP applies the hub part when the
        Plugins page next loads, or at its next start)."""
        from ..hub.plugin_profiles import PRESET_IDS, apply_presets_to_store
        try:
            primary = next((b for b in self._enrollment.bands() if b["is_primary"]), None)
            apply_presets_to_store(self._settings_store, [p for p in selected if p in PRESET_IDS],
                                   primary["id"] if primary else None, actor="human:setup",
                                   source="setup")
        except Exception:
            log.exception("Setup wizard: storing the plugin presets failed")

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app, access_log_class=InstallerAccessLogger,
                                     shutdown_timeout=self.shutdown_timeout)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bind, self.port)
        await site.start()
        log.info("Remote server on %s:%d (HTTP)", self.bind, self.port)
        if not self.web_pass:
            log.warning("dashboard password not set: anyone who can reach port %d "
                        "can control every worker", self.port)
        self._report_settings()
        # A fresh hub has no worker bundle: build it in the background so the
        # Install page works without a manual build step.
        try:
            self._bundle.ensure()
        except Exception:
            log.warning("could not start the worker bundle build", exc_info=True)

        # Join the band so the dashboard can list workers + invoke caps.
        # Best-effort: a missing/unreachable hub must not take down the
        # installer server (the bootstrap endpoints don't need the band).
        try:
            psks = self._enrollment.transport_psks()
            if self._shared_band is not None:
                # rook hub serve: the MCP side's client (and authorizer), already up.
                self._band = self._shared_band
                self._authz = getattr(self._band, "authz", None)
            else:
                from ..band_mcp.client import MultiBandClient
                self._band = MultiBandClient(psks=psks, hub_host=self.hub_host,
                                             hub_port=self.hub_port)
                self._band.authz = self._authorizer()
                await self._band.start()
                log.info("band client joined hub %s:%d (%d band(s))",
                         self.hub_host, self.hub_port, len(psks))
            self._enrollment_task = asyncio.create_task(self._watch_enrollment())
            if os.environ.get("ROOK_PUSH_UPDATES", "1") != "0":
                self._push_task = asyncio.create_task(self._push_loop())
        except Exception as e:
            log.warning("band client failed to start (%s); dashboard band view disabled", e)
            self._band = None

    def _refresh_settings(self) -> None:
        """Pick up Settings-page changes to the domain, relay address and band
        label (live), and report this process's view to the store."""
        from . import dashboard_settings as _ds
        from . import setup_store
        vals, self._setting_sources, _conflicts = _ds.resolve(
            self._ctor_settings, self._explicit if self._explicit is not None else {},
            setup_store.load(), _ds.stored_values(self._settings_store))
        self.domain, self.hub_public, self.band_name = (
            vals["domain"], vals["hub_public"], vals["band_name"])
        self._apply_domain(self.domain)

    def _apply_domain(self, value: str) -> None:
        """Split the configured domain into ``self.domain`` (host[:port], what
        installers validate and embed) and the scheme behind
        :attr:`public_base` (rook.remote.public_url)."""
        from .public_url import split_domain
        self._domain_setting = value
        self.public_scheme, self.domain = split_domain(value, self.port)

    @property
    def public_base(self) -> str:
        """``http(s)://host[:port]`` browsers and workers use to reach this
        dashboard: the one base for installers, one-liners, the account
        origin and the update manifest URL."""
        return f"{self.public_scheme}://{self.domain}"

    def _report_settings(self) -> None:
        from . import dashboard_settings as _ds
        _ds.report(self._settings_store, explicit=self._explicit,
                   sources=self._setting_sources, conflicts=self._setting_conflicts,
                   chat_db=getattr(self, "_chat_db", ""), started_at=self._started_at,
                   values={"domain": self._domain_setting, "hub_public": self.hub_public,
                           "band_name": self.band_name}, argv=self._argv)

    async def _sync_enrollment(self) -> None:
        if time.time() - self._settings_checked > 10:
            self._settings_checked = time.time()
            try:
                self._refresh_settings()
            except Exception:
                log.warning("refreshing settings failed", exc_info=True)
        bands = self._enrollment.bands(active_only=True, secrets_visible=True)
        self._bands = bands
        self._band_names = {b["label"]: b["name"] for b in bands}
        primary = next((b for b in bands if b["is_primary"]), None)
        self._primary_label = primary["label"] if primary else ""
        self.band_psk = primary["psk"] if primary else ""
        if self._band is not None:
            await self._band.sync_bands(self._enrollment.transport_psks())

    async def _watch_enrollment(self) -> None:
        while True:
            try:
                await self._sync_enrollment()
            except Exception:
                log.exception("could not sync band enrollment")
            await asyncio.sleep(2)

    async def _push_loop(self) -> None:
        """Auto-converge: push the current signed manifest to apply-capable
        workers reporting an older build. Fail-closed — no-op until a *signed*
        manifest exists, and it only targets workers advertising worker.apply."""
        pushed: dict[str, tuple] = {}      # worker_id -> (target_build, ts)
        REPUSH_SECS = 180.0                # don't re-push the same build too soon
        while True:
            try:
                await asyncio.sleep(20.0)
                if self._band is None:
                    continue
                # The update service's released build when it runs the
                # feed (new builds go out by staged rollout), else the bundle.
                manifest = self._feed_manifest()
                # Workers in a running staged rollout are the rollout's: a
                # push of the released build would race its pushes.
                busy: set = set()
                if manifest is None:
                    try:
                        manifest = json.loads(self._bundle.path("band-worker.json").read_text())
                    except Exception:
                        continue
                else:
                    from ..hub.update_feed import rollout_targets
                    busy = rollout_targets(self._feed.root)
                if not manifest.get("sig"):
                    continue  # unsigned -> workers would reject; don't bother
                target = int(manifest.get("build", 0))
                if target <= 0:
                    continue
                now = time.time()
                for wid, w in list(self._band.workers.items()):
                    if self._ban_match(w.get("name"), wid):
                        continue  # deauthed — never feed a banned node updates
                    if "worker.apply" not in (w.get("caps") or []):
                        continue  # can't receive an in-band push yet
                    if wid in busy:
                        continue  # the staged rollout is updating it
                    b = w.get("build")
                    if not isinstance(b, int) or b <= 0 or b >= target:
                        continue
                    last = pushed.get(wid)
                    if last and last[0] == target and (now - last[1]) < REPUSH_SECS:
                        continue
                    pushed[wid] = (target, now)
                    name = w.get("name", wid)
                    log.info("push update %s: build %s -> %s", name, b, target)
                    try:
                        reply = await self._band.call(
                            "worker.apply", args={"manifest": manifest},
                            target=wid, timeout=30)
                        log.info("push %s reply: %s", name, reply.get("result", reply))
                    except Exception as e:
                        log.warning("push to %s failed: %s", name, e)
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("push loop error")

    async def stop(self) -> None:
        await self._overview.close()
        if self._enrollment_task is not None:
            self._enrollment_task.cancel()
            try:
                await self._enrollment_task
            except asyncio.CancelledError:
                pass
            self._enrollment_task = None
        if self._push_task is not None:
            self._push_task.cancel()
            await asyncio.gather(self._push_task, return_exceptions=True)
            self._push_task = None
        if self._band is not None and self._band is not self._shared_band:
            try:
                await self._band.stop()
            except Exception:
                log.exception("band client stop failed")
        self._band = None
        if self._runner:
            await self._runner.cleanup()

    # -- HTTP endpoints --

    async def _index(self, request: web.Request) -> web.Response:
        ua = request.headers.get("User-Agent", "").lower()

        # Only auto-serve bootstrap at /worker, not /
        # / always shows the help page

        # Browser — serve dashboard directly
        accept = request.headers.get("Accept", "")
        if "text/html" in accept:
            try:
                index_path = WEB_DIR / "index.html"
                if index_path.exists():
                    # no-store so a dashboard redeploy is seen on the next load
                    # instead of the browser serving a stale cached page.
                    return web.Response(
                        text=_versioned_assets(index_path.read_text(encoding="utf-8"), WEB_DIR),
                        content_type="text/html",
                        headers={"Cache-Control": "no-store, must-revalidate"})
            except Exception:
                pass

        # CLI — show instructions
        base = self.public_base
        text = f"""
  R ☠ ☠ K  Band Worker Installer
  =================================

  Linux / Mac:
    curl -fsSL {base}/worker | bash

  Windows (PowerShell, on Windows only):
    iex (irm {base}/worker)

  Force a variant (if auto-detect guesses wrong):
    curl -fsSL "{base}/worker?os=unix" | bash
    iex (irm "{base}/worker?os=windows")

  Stand up a hub (band relay):
    curl -fsSL {base}/hub | bash            # wizard
    curl -fsSL {base}/hub | bash -s -- --yes # unattended

  Endpoints:
    /install          unified installer (worker | cli | both)
    /hub              telesthete-hub installer (wizard; --yes for unattended)
    /telesthete-hub   prebuilt hub binary (?arch=x86_64), else built from source
    /worker           bootstrap script (auto-detects OS; ?os=windows|unix to force)
    /band-worker.pyz  self-contained band-worker zipapp
    /health           server status
"""
        return web.Response(text=text, content_type="text/plain")

    async def _worker_bootstrap(self, request: web.Request) -> web.Response:
        from .enrollment import JoinDenied, JoinLimited
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        code = request.query.get("band", "")
        account_login = request.query.get("login") == "google" and not code
        if account_login:
            band = {"psk": "account-enrollment", "hub": self.hub_public, "name": "selected after login"}
        else:
            try:
                band = self._enrollment.redeem(code, request.remote or "unknown")
            except JoinLimited as e:
                return web.Response(text=str(e), status=429, headers={**headers, "Retry-After": "60"})
            except JoinDenied as e:
                return web.Response(text=str(e), status=403, headers=headers)
        ua = request.headers.get("User-Agent", "").lower()
        base = self.public_base
        kw = {"domain": self.domain, "base": base, "band_psk": band["psk"],
              "hub_public": band["hub"] or self.hub_public,
              "band_name": band["name"].replace("\r", " ").replace("\n", " ")}
        # These fields enter shell/PowerShell command lines. Reject unsupported
        # legacy values rather than interpolate executable syntax or normalize keys.
        if (not _re.fullmatch(r"[A-Za-z0-9_-]{1,256}", kw["band_psk"])
                or not _re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", kw["domain"])
                or not _re.fullmatch(r"[A-Za-z0-9.\[\]:-]+", kw["hub_public"])):
            return web.Response(text="band settings require correction before installation", status=400, headers=headers)
        # Explicit override wins, for determinism: /worker?os=windows|unix
        os_q = request.query.get("os", "").lower()
        if os_q in ("windows", "win"):
            want_ps = True
        elif os_q in ("unix", "linux", "mac", "macos", "darwin", "posix"):
            want_ps = False
        else:
            # PowerShell is cross-platform (pwsh on Linux/macOS), so "powershell"
            # in the UA alone is NOT enough — only serve the Windows installer
            # when the client is actually running on Windows. Everything else
            # (curl, wget, browsers, pwsh-on-Linux) gets the bash script.
            is_ps = ("powershell" in ua) or ("pwsh" in ua)
            on_windows = ("windows" in ua) or ("win32" in ua) or ("win64" in ua)
            want_ps = is_ps and on_windows
        # Downloading an installer never grants a reusable band PSK. The worker
        # generates its own key and redeems the short code (or browser approval).
        kw["enroll_args"] = f'--enroll {base}' + (f' --pair-code {code}' if code else '')
        # A pairing code lasts PAIRING_TTL (5 min) and a first install can
        # outlast it, so code-based installs offer to retry with a fresh code.
        retry = (_PS_ENROLL_RETRY if want_ps else _BASH_ENROLL_RETRY) if code else ""
        kw["enroll_retry"] = retry.replace("__BASE__", base)
        script = (PS_BOOTSTRAP if want_ps else BASH_BOOTSTRAP).format(**kw)
        script = script.replace(f'{base}/band-worker.pyz',
                                f'{base}/band-worker-enrollment.pyz')
        script = script.replace(f'--hub {kw["hub_public"]} --ws --psk {kw["band_psk"]}', '--enrolled --ws')
        # ROOK_HUB_PUBLIC_WS_URL: point new workers at the relay's native WS
        # listener instead of this process's /band bridge (core spec 2.6).
        ws_url = public_ws_url()
        if ws_url:
            script = script.replace('--enrolled --ws', f'--enrolled --ws --ws-url {ws_url}')
        script = script.replace(f' --update-url {base}/band-worker.json', '')
        # The elevated relaunch (Windows) and a root install for ROOK_USER
        # (Linux) fetch this same script again: keep the code.
        query = 'login=google' if account_login else f'band={code}'
        if want_ps:
            script = script.replace(f'{base}/worker?os=windows',
                                    f'{base}/worker?{query}&os=windows')
        else:
            script = script.replace(f"SELF_URL='{base}/worker?os=unix'",
                                    f"SELF_URL='{base}/worker?{query}&os=unix'")
        return web.Response(text=script, content_type="text/plain", headers=headers)

    async def _enroll_config(self, request: web.Request) -> web.Response:
        """Code-based config fetch for provisioning tools and native clients."""
        from .enrollment import JoinDenied, JoinLimited
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        try:
            data = await request.json()
            code = data.get("code", "") if isinstance(data, dict) else ""
        except (ValueError, TypeError):
            code = ""
        try:
            band = self._enrollment.redeem(code, request.remote or "unknown")
        except JoinLimited as e:
            return web.json_response({"error": str(e)}, status=429,
                                     headers={**headers, "Retry-After": "60"})
        except JoinDenied as e:
            return web.json_response({"error": str(e)}, status=403, headers=headers)
        return web.json_response({"band_id": band["id"], "name": band["name"],
                                  "psk": band["psk"], "hub": band["hub"] or self.hub_public,
                                  "epoch": band["epoch"]}, headers=headers)

    def _bundle_missing(self, name: str) -> web.Response:
        """No bundle to serve: 503 while the background build runs (or was
        just started), 404 with the reason when it cannot be built."""
        if not self._bundle.error:
            self._bundle.ensure()
        if self._bundle.building:
            return web.Response(status=503, headers={"Retry-After": "60", "Cache-Control": "no-store"},
                                text=f"{name} is building on this hub, retry in a minute.\n")
        why = f" ({self._bundle.error})" if self._bundle.error else ""
        return web.Response(status=404, headers={"Cache-Control": "no-store"},
                            text=f"{name} is not built yet{why}. "
                                 "Run: python3 rook/remote/build_band_worker.py\n")

    async def _band_worker_pyz(self, request: web.Request) -> web.Response:
        filename = "band-worker-enrollment.pyz" if request.path == "/band-worker-enrollment.pyz" else "band-worker.pyz"
        pyz_path = self._bundle.path(filename)
        if pyz_path is None:
            return self._bundle_missing("band-worker.pyz")
        data = await asyncio.to_thread(pyz_path.read_bytes)
        return web.Response(
            body=data,
            content_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=band-worker.pyz"},
        )

    async def _band_worker_manifest(self, request: web.Request) -> web.Response:
        """Serve the signed OTA manifest (band-worker.json) next to the pyz.
        Public + no-cache so converging workers always see the current build.

        When the update service runs the worker feed, this is its *released*
        build: a newer bundle reaches workers through a staged rollout first."""
        released = self._feed_manifest()
        if released is not None:
            return web.json_response(released, headers={"Cache-Control": "no-store"})
        manifest_path = self._bundle.path("band-worker.json")
        if manifest_path is None:
            return self._bundle_missing("band-worker.json")
        return web.Response(
            body=manifest_path.read_bytes(),
            content_type="application/json",
            headers={"Cache-Control": "no-store"},
        )

    def _feed_manifest(self) -> dict | None:
        """The update service's released worker manifest, or None when the
        service is off (hub plugins off, or its plugin switched off on the
        Plugins page) or not running (its summary.json has gone stale while
        this dashboard has been up long enough to see a fresh one): then the
        bundle is served and pushed as before, and a feed left behind pins
        nobody."""
        if os.environ.get("ROOK_HUB_PLUGINS", "1") == "0":
            return None
        try:
            from ..hub.plugin_profiles import hub_disabled
            if "updates" in hub_disabled(self._settings_store):
                return None
        except Exception:
            log.debug("reading the hub plugin switches failed", exc_info=True)
        from ..hub.update_feed import SERVICE_STALE_SECS, service_alive
        now = time.time()
        if now - self._started_at > SERVICE_STALE_SECS and not service_alive(self._feed.root, now):
            return None
        return self._feed.released_manifest()

    async def _feed_pyz(self, request: web.Request) -> web.Response:
        """A worker bundle kept in the update feed, by build. Public like
        /band-worker.pyz: the signed manifest that names it is the trust."""
        path = self._feed.pyz_for(request.match_info.get("build", ""))
        if path is None:
            return web.Response(status=404, text="no such worker build on this hub\n",
                                headers={"Cache-Control": "no-store"})
        return web.FileResponse(path, headers={
            "Content-Type": "application/octet-stream",
            "Content-Disposition": "attachment; filename=band-worker.pyz"})

    async def _desktop_update(self, request: web.Request) -> web.Response:
        """The desktop app's update check (docs/operations/updates.md):
        ``?version=0.1.0&platform=linux|macos|windows``. Public, and only
        what the GitHub release page shows anyway."""
        from ..hub.update_feed import summary
        from ..hub.release_meta import version_lt
        latest = (summary() or {}).get("latest") or {}
        version = request.query.get("version", "")[:40]
        plat = request.query.get("platform", "").lower()[:20]
        patterns = {"linux": (".AppImage", ".deb"), "macos": (".dmg",), "windows": ("-setup.exe", ".msi")}
        downloads = [{"name": n, "url": u} for n, u in (latest.get("assets") or {}).items()
                     if n.lower().startswith("rook-desktop") and n.endswith(patterns.get(plat, ()))]
        return web.json_response({
            "current": version, "latest": latest.get("version"),
            "update_available": bool(latest.get("version") and version
                                     and version_lt(version, latest["version"])),
            "notes_url": latest.get("html_url"), "published_at": latest.get("published_at"),
            "downloads": downloads,
        }, headers={"Cache-Control": "no-store"})

    def _apk_files(self) -> tuple[Path, Path, str]:
        """(apk, manifest, approved sha256): the APK the update service
        published from a release (approved by its verified manifest), else the
        operator's rook/remote copy with ``ROOK_PUBLIC_APK_SHA256``."""
        env_hash = os.environ.get("ROOK_PUBLIC_APK_SHA256", "")
        from ..hub.update_feed import published_apk, updates_dir
        pub = published_apk()
        if pub is not None and (not env_hash or env_hash == pub[1].get("sha256")):
            return pub[0], updates_dir() / "apk" / "rook-worker-apk.json", pub[1]["sha256"]
        here = Path(__file__).parent
        return here / "rook-worker.apk", here / "rook-worker-apk.json", env_hash

    async def _worker_apk(self, request: web.Request) -> web.Response:
        """Serve the native Android worker APK (built by android/, dropped here)."""
        # Legacy APK builds may contain a default PSK. Keep them behind login
        # until the Google enrollment work supplies a verified generic APK.
        apk_path, _, expected_hash = self._apk_files()
        if not self.web_pass and not expected_hash:
            return web.Response(status=503, text="Configure dashboard login before downloading the APK.")
        if not apk_path.exists():
            return web.Response(
                status=404,
                text="rook-worker.apk not built yet. Build android/ and drop the APK here.",
            )
        if expected_hash:
            import hashlib
            import hmac
            stat = apk_path.stat()
            fingerprint = (stat.st_ino, stat.st_size, stat.st_mtime_ns, expected_hash)
            if getattr(self, "_verified_apk", None) != fingerprint:
                def hash_apk():
                    with apk_path.open("rb") as source:
                        return hashlib.file_digest(source, "sha256").hexdigest()
                actual = await asyncio.to_thread(hash_apk)
                if not hmac.compare_digest(actual, expected_hash):
                    return web.Response(status=503, text="APK verification failed; contact the operator.")
                self._verified_apk = fingerprint
        return web.FileResponse(
            apk_path,
            headers={
                "Content-Disposition": "attachment; filename=rook-worker.apk",
                "Content-Type": "application/vnd.android.package-archive",
            },
        )

    async def _worker_apk_manifest(self, request: web.Request) -> web.Response:
        """Public update metadata bound to the same approved APK as /apk."""
        response = await self._worker_apk(request)
        if not isinstance(response, web.FileResponse):
            return response
        try:
            apk, manifest_path, expected = self._apk_files()
            manifest = json.loads(manifest_path.read_text())
            if (not expected or manifest.get("sha256") != expected
                    or manifest.get("package") != "systems.bake.rook"
                    or type(manifest.get("version_code")) is not int
                    or manifest["version_code"] <= 0
                    or not isinstance(manifest.get("version_name"), str)
                    or manifest.get("size") != apk.stat().st_size):
                raise ValueError("APK manifest does not match approved release")
            return web.json_response(manifest, headers={"Cache-Control": "no-store"})
        except (OSError, ValueError, TypeError):
            return web.Response(status=503, text="APK update metadata unavailable.")

    async def _api_generate_psk(self, request: web.Request) -> web.Response:
        """Suggest a new key behind the existing dashboard login gate.

        Generation does not create a band or rotate any existing key.
        """
        from . import setup_store
        return web.json_response({"psk": setup_store.gen_psk()},
                                 headers={"Cache-Control": "no-store"})

    async def _api_bands(self, request: web.Request) -> web.Response:
        """Known bands for the dashboard selector: ``[{id, name}]`` where ``id``
        is the band_id label. Raw PSKs are never sent to the browser."""
        await self._sync_enrollment()
        out = [{"id": lbl, "name": name, "primary": lbl == self._primary_label}
               for lbl, name in self._band_names.items()]
        out.sort(key=lambda x: x["name"])
        return web.json_response(out)

    async def _api_add_band(self, request: web.Request) -> web.Response:
        """Add a band by PSK (authenticated dashboard users only). The PSK is
        persisted server-side in enrollment.db (gitignored) and never echoed back;
        the band is joined live so its workers appear without a restart."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        name = (data.get("name") or "").strip() or "band"
        psk = (data.get("psk") or "").strip()
        if not psk:
            return web.json_response({"error": "psk required"}, status=400)
        try:
            from telesthete.protocol.crypto import derive_band_id
            label = derive_band_id(psk).hex()[:8]
        except Exception as e:
            return web.json_response({"error": f"crypto unavailable: {e}"}, status=500)

        try:
            self._enrollment.register(name, psk, self.hub_public)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        try:
            await self._sync_enrollment()
        except Exception as e:
            log.warning("live join of band %s failed: %s", label, e)
        return web.json_response({"id": label, "name": name})

    async def _api_remove_band(self, request: web.Request) -> web.Response:
        """Remove a band by band_id label. The primary (configured) band can't
        be removed — that's the dashboard's own band."""
        label = request.match_info.get("id", "")
        if not label:
            return web.json_response({"error": "id required"}, status=400)
        if label == self._primary_label:
            return web.json_response({"error": "cannot remove the primary band"}, status=400)
        band = next((b for b in self._enrollment.bands() if b["label"] == label), None)
        if not band:
            return web.json_response({"error": "band not found"}, status=404)
        self._enrollment.revoke(band["id"])
        await self._sync_enrollment()
        return web.json_response({"removed": label})

    async def _api_band_workers(self, request: web.Request) -> web.Response:
        """Live band roster from our hub-joined MultiBandClient. Each worker is
        tagged with the ``band`` (band_id label) it was seen on; an optional
        ``?band=<id>`` query filters to one band."""
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        rows = self._band_worker_rows(request.query.get("band") or "")
        hosting = await self._hosting()
        for row in rows:
            entry = hosting.get(str(row.get("name") or "").lower())
            if entry:
                row["serves"] = {k: entry[k] for k in ("sites", "services") if entry.get(k)}
        return web.json_response(rows)

    async def _hosting(self) -> dict:
        """What each worker hosts (the hub's ``serves`` plugin), by lower-cased
        worker name. Asked of the hub node over the band at most twice a minute;
        a hub without the plugin, or one that does not answer, means none."""
        cached = getattr(self, "_hosting_cache", None)
        now = time.time()
        if cached and now - cached[0] < 30:
            return cached[1]
        data: dict = cached[1] if cached else {}
        try:
            hub = next((wid for wid, w in self._band.workers.items()
                        if "is_hub" in (w.get("roles") or ())), None)
            if hub is not None:
                reply = await self._band.call("serves.list", target=hub, timeout=5.0)
                if reply.get("ok"):
                    data = (reply.get("result") or {}).get("serves") or {}
        except Exception:
            log.debug("serves.list failed", exc_info=True)
        self._hosting_cache = (now, data)
        return data

    def _band_worker_rows(self, want: str = "") -> list[dict]:
        if self._band is None:
            return []
        now = time.time()
        out = []
        from ..hub.update_feed import summary
        pending = summary().get("pending_floors")
        for w in self._band.workers.values():
            band = w.get("band")
            if want and band not in (want, "*"):
                continue
            out.append({
                "worker_id": w["worker_id"],
                "name": w.get("name"),
                # Verified roles (is_hub) and self-reported platform facts.
                "roles": list(w.get("roles") or []),
                "facts": w.get("facts") or {},
                "description": w.get("description", ""),
                "caps": w.get("caps", []),
                "plugins": w.get("plugins", []),
                "band": band,
                "version": w.get("version"),
                "build": w.get("build"),
                "app_release": w.get("app_release") or {},
                "hb": w.get("hb") or {},
                "last_seen_age_secs": round(now - w.get("last_seen", 0.0), 2),
                # deauthed nodes normally park off-band and vanish from the roster;
                # flag any still lingering (not yet parked / uncooperative).
                "banned": self._ban_match(w.get("name"), w.get("worker_id")),
            })
            out[-1]["update_note"] = _update_note(out[-1], pending)
        out.sort(key=lambda x: x["name"] or "")
        return out

    async def _overview_call(self, cap, **kwargs):
        if self._band is None:
            return {"ok": False}
        return await self._band.call(cap=cap, **kwargs)

    async def _api_band_overview(self, request: web.Request) -> web.Response:
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        return web.json_response(self._overview.snapshot(request.query.get("band") or ""),
                                 headers={"Cache-Control": "no-store"})

    async def _close_overview(self, app):
        await self._overview.close()

    # -- permissions (docs/design/permissions.md) -----------------------------

    def _authorizer(self):
        """Policy evaluation + call tickets for the dashboard's band client.
        Shares policy.json (and, when present, the journal) with the MCP
        bridge's data directory. Never raises."""
        from ..paths import data_path
        from ..hub.authz import build_authorizer
        policy_dir = os.path.dirname(data_path("policy.json", "/var/lib/rook-band-mcp/policy.json"))
        journal_path = data_path("journal.db", "/var/lib/rook-band-mcp/journal.db")
        state = {"journal": None}

        def record(**kw):
            if state["journal"] is None:
                if not os.path.isdir(os.path.dirname(journal_path)):
                    return None
                from ..band_mcp.journal import Journal
                state["journal"] = Journal(journal_path)
            return state["journal"].record(**kw)

        try:
            authz = build_authorizer(policy_dir, record=record, banned=self._ban_match)
        except Exception:
            log.exception("ROOK AUTHZ ALERT: dashboard authorizer unavailable")
            return None
        self._authz = authz
        return authz

    def _principal_for(self, request: web.Request):
        """The authenticated principal behind an admitted dashboard request:
        the signed-in account (owner if admin or a band owner), else the shared
        dashboard password (``human:dashboard``, an owner for compatibility)."""
        from ..hub.authz import principal_for_user
        if self._auth_exempt(request.path):
            return None  # public endpoints carry no principal
        user = self._accounts.current(request)
        if not user:
            if self._accounts.handles(request.path):
                return None  # account routes enforce their own sessions
            return principal_for_user(None, True)
        owner = bool(user.get("admin"))
        if not owner:
            try:
                owner = any(b.get("role") == "owner"
                            for b in self._accounts.store.bands(user["id"]))
            except Exception:
                owner = False
        return principal_for_user(user, owner)

    @web.middleware
    async def _principal_middleware(self, request: web.Request, handler):
        """Runs after the auth middleware admitted the request: put the
        caller's principal in context so every band call this request makes is
        authorized for it (the band client reads it)."""
        from ..hub.authz import current_principal
        try:
            principal = self._principal_for(request)
        except Exception:
            log.exception("ROOK AUTHZ ALERT: dashboard principal lookup failed")
            principal = None
        if principal is None:
            return await handler(request)
        tok = current_principal.set(principal)
        try:
            return await handler(request)
        finally:
            current_principal.reset(tok)

    def _dashboard_identity(self, request: web.Request) -> str:
        """Audit identity for a dashboard band call. The auth middleware has
        already admitted the request; this only names who it was — the signed-in
        account, or ``human:dashboard`` for the shared dashboard password."""
        try:
            user = self._accounts.current(request)
        except Exception:
            log.exception("ROOK AUDIT ALERT: dashboard caller lookup failed; call let through")
            return "unverified"
        if user:
            from .work_web import actor
            return actor(user)
        return "human:dashboard"

    async def _api_band_whoami(self, request: web.Request) -> web.Response:
        """Who the band calls of this login are authorized as, and whether
        this hub signs call tickets for them. Clients such as the ``rook band``
        TUI read it once to explain refusals from enforcing workers; a 404
        tells them the hub predates permissions (no tickets at all)."""
        from ..hub.authz import current_principal
        p = current_principal.get()
        authz = getattr(self._band, "authz", None) if self._band is not None else None
        if authz is None:
            authz = self._authz
        tickets, mode = False, "off"
        if authz is not None:
            try:
                tickets = bool(authz.signing_ready())
                policy = authz.store.current()
                mode = policy.mode_for(p) if p is not None else policy.mode
            except Exception:
                log.exception("ROOK AUTHZ ALERT: whoami could not read the authorizer")
        return web.json_response({
            "principal": p.id if p is not None else None,
            "role": (p.role if p is not None else "") or None,
            "groups": list(p.groups) if p is not None else [],
            "authz": authz is not None,       # hub evaluates policy on band calls
            "mode": mode,                     # hub policy mode for this principal
            "tickets": tickets,               # targeted calls carry a signed ticket
            "band": self._band is not None,
        })

    async def _api_band_call(self, request: web.Request) -> web.Response:
        """Invoke a capability on the band and return the worker's reply."""
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        cap = data.get("cap")
        if not cap:
            return web.json_response({"error": "cap required"}, status=400)
        args = data.get("args") or {}
        # Attribute chat/notify sent through the dashboard API. The band CLI sets
        # its own sender (the client hostname); the MCP sets "MCP"; a bare web
        # dashboard call falls back to "dashboard".
        if cap in ("chat.send", "msg.send") and isinstance(args, dict):
            args.setdefault("sender", "dashboard")
        target = data.get("worker_id") or data.get("target")
        # Accept a worker name as well as its id, as the MCP rook_call does.
        if target and self._band and target not in self._band.workers:
            named = [wid for wid, w in self._band.workers.items() if w.get("name") == target]
            if len(named) == 1:
                target = named[0]
        # Refuse to drive a deauthed worker (its name may persist across restarts).
        if target:
            tw = self._band.workers.get(target) if self._band else None
            if self._ban_match(tw.get("name") if tw else None, target):
                return web.json_response(
                    {"ok": False, "error": "worker is deauthed (banned)"}, status=403)
        try:
            timeout = float(data.get("timeout", 15.0))
        except (TypeError, ValueError):
            timeout = 15.0
        identity = self._dashboard_identity(request)
        # The cap buttons are a front end for the MCP side: it runs the call as
        # the signed-in person (hub caps in-process, policy, masking, journal),
        # in one process or two (upstream.request_json).
        forwarded = await self._forward_band_call(cap, args, target, timeout, identity)
        if forwarded is not None:
            return forwarded
        try:
            reply = await self._band.call(cap=cap, args=args, target=target, timeout=timeout,
                                          identity=identity)
        except asyncio.TimeoutError:
            return web.json_response({"ok": False, "error": "timeout waiting for reply"}, status=504)
        except Exception as e:
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)
        if (target and self._is_hub_target(target) and isinstance(reply, dict)
                and "not callable over the band" in str(reply.get("error") or "")):
            reply = {**reply, "error": "The MCP server isn't reachable, so the dashboard can't "
                                       "run this hub action right now (only read-only hub "
                                       "actions work without it)."}
        return web.json_response(reply)

    def _is_hub_target(self, target: str) -> bool:
        w = (self._band.workers.get(target) if self._band else None) or {}
        return target == "rook" or w.get("name") == "rook" or "is_hub" in (w.get("roles") or [])

    def _band_call_url(self) -> str:
        """The bridge's /internal/band-call, beside the /internal/mask the
        dashboard is configured for (ROOK_MASK_URL)."""
        return self._bridge.url.replace("/internal/mask", "/internal/band-call")

    async def _forward_band_call(self, cap: str, args: dict, target: str | None,
                                 timeout: float, identity: str) -> web.Response | None:
        """Run a dashboard cap call on the MCP side (dashboard_call.py) as the
        admitted person. None only when the request certainly never ran there
        (no person in context, no internal token, no connection, or an MCP
        side without the route): the caller then sends it over the band."""
        from ..hub.authz import current_principal
        from . import upstream
        p = current_principal.get()
        bridge = getattr(self, "_bridge", None)
        if p is None or p.kind != "human" or bridge is None:
            return None
        who = json.dumps({"id": p.id, "role": "owner" if p.fail_open() else "member",
                          "label": p.label or identity})
        body = {"cap": cap, "args": args, "target": target, "timeout": timeout,
                "identity": identity}
        for attempt in (0, 1):
            if bridge._token is None:
                bridge._token = await asyncio.to_thread(bridge._read_token)
            if not bridge._token:
                return None
            try:
                status, reply = await upstream.request_json(
                    "POST", self._band_call_url(), json=body, timeout=timeout + 10,
                    headers={"Authorization": f"Bearer {bridge._token}", "X-Rook-Principal": who})
            except upstream.NotConnected:
                return None  # nothing reached the MCP side
            except upstream.Unavailable:
                # Reached but no usable answer: the call may have run there, so
                # it is not sent a second time over the band.
                return web.json_response(
                    {"ok": False, "timeout": True,
                     "error": "no usable reply from the MCP server; the call may have run, "
                              "check the journal before retrying"}, status=504)
            if status == 401 and attempt == 0:
                bridge._token = None  # rotated token: read it again once
                continue
            if status in (401, 404):
                return None  # an MCP side without this route (or the token): not run
            return web.json_response(reply, status=status)
        return None

    # -- deauth / ban --------------------------------------------------------

    def _ban_match(self, name: str | None, worker_id: str | None) -> bool:
        """True if a worker with this (name, worker_id) is on the deauth list.
        Name is the durable match (survives a worker restart); worker_id is exact."""
        for b in self._bans:
            if worker_id and b.get("worker_id") and b["worker_id"] == worker_id:
                return True
            if name and b.get("name") and b["name"] == name:
                return True
        return False

    def _sign_deauth(self, worker_id: str, name: str, reason: str) -> dict | None:
        """A signed worker.deauth payload: a deauth v2 order (own signature
        domain, bound to ``worker_id`` + ``issued_at``) nested in a legacy v1
        body so workers from before v2 still park (permissions 6.3). None if
        this host holds no signing key (then it's denylist-only)."""
        from .update_keys import load_signing_key
        from ..hub.keys import deauth_payload
        sk = load_signing_key()
        if sk is None or not worker_id:
            return None
        return deauth_payload(sk, worker_id, name, reason)

    async def _api_bans(self, request: web.Request) -> web.Response:
        """The current deauth list (for the dashboard 'Banned' view)."""
        return web.json_response(self._bans)

    async def _api_ban(self, request: web.Request) -> web.Response:
        """Deauth a worker: add it to the persistent controller denylist (hidden,
        no OTA pushes, calls refused) AND send it a signed worker.deauth so a
        cooperative node parks itself off-band. Body: {worker_id?, name?, reason?}."""
        if self._band is None:
            return web.json_response({"error": "band client not connected"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        worker_id = str(data.get("worker_id") or "").strip()
        name = str(data.get("name") or "").strip()
        reason = str(data.get("reason") or "").strip()
        if not worker_id and not name:
            return web.json_response({"error": "worker_id or name required"}, status=400)
        # Fill in whichever of (name, worker_id) is missing from the live roster.
        live = self._band.workers.get(worker_id) if worker_id else None
        if live is None and name:
            for w in self._band.workers.values():
                if w.get("name") == name:
                    live = w
                    break
        if live is not None:
            name = name or (live.get("name") or "")
            worker_id = worker_id or (live.get("worker_id") or "")

        # 1) Denylist first — always works, so even if the in-band deauth misses,
        #    the worker is immediately hidden, cut off from calls, and no longer
        #    receives OTA pushes.
        if not self._ban_match(name or None, worker_id or None):
            self._bans.append({"name": name, "worker_id": worker_id,
                               "at": int(time.time()), "reason": reason})
            try:
                from . import setup_store
                setup_store.save_bans(self._bans)
            except Exception:
                log.exception("failed to persist ban list")

        # 2) Best-effort signed deauth so a cooperative worker parks off-band.
        deauth = self._sign_deauth(worker_id, name, reason)
        if deauth is None:
            sent = {"ok": False, "error": "no signing key on host — controller denylist only"}
            log.warning("ban %s: no signing key; denylist only", name or worker_id)
        elif worker_id:
            try:
                sent = await self._band.call("worker.deauth", args={"payload": deauth},
                                             target=worker_id, timeout=20)
            except Exception as e:
                sent = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        else:
            sent = {"ok": False, "error": "worker offline — denylisted, will park if it returns"}
        return web.json_response({"ok": True,
                                  "banned": {"name": name, "worker_id": worker_id},
                                  "deauth_sent": sent})

    async def _api_unban(self, request: web.Request) -> web.Response:
        """Remove a worker from the controller denylist. NOTE: a cooperative node
        that already parked itself off-band must be revived locally (clear
        ~/.rook-band-worker/banned + restart) — the controller can't reach a
        dormant node in-band. Body: {worker_id?, name?}."""
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        worker_id = str(data.get("worker_id") or "").strip()
        name = str(data.get("name") or "").strip()
        before = len(self._bans)
        self._bans = [b for b in self._bans
                      if not ((worker_id and b.get("worker_id") == worker_id)
                              or (name and b.get("name") == name))]
        try:
            from . import setup_store
            setup_store.save_bans(self._bans)
        except Exception:
            log.exception("failed to persist ban list")
        return web.json_response({"ok": True, "removed": before - len(self._bans),
                                  "note": "revive a dormant node locally to rejoin"})

    # -- chat panel (shared site chat store) ---------------------------------

    _OPERATOR = "user:operator"   # dashboard identity for chat attribution

    async def _api_chat_rooms(self, request: web.Request) -> web.Response:
        if self._chat is None:
            return web.json_response({"error": "chat unavailable"}, status=503)
        return web.json_response(await self._bridge_mask(
            self._chat.rooms_for(self._OPERATOR, limit=200, include_all=True)))

    async def _api_chat_read(self, request: web.Request) -> web.Response:
        if self._chat is None:
            return web.json_response({"error": "chat unavailable"}, status=503)
        room = request.query.get("room", "")
        try:
            since = int(request.query.get("since", "0"))
        except ValueError:
            since = 0
        return web.json_response(await self._bridge_mask(
            self._chat.read(room, self._OPERATOR, since_seq=since)))

    async def _api_chat_send(self, request: web.Request) -> web.Response:
        if self._chat is None:
            return web.json_response({"error": "chat unavailable"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        room = str(data.get("room") or "").strip()
        text = str(data.get("text") or "")
        mention = data.get("mention") or []
        if isinstance(mention, str):
            mention = [m.strip() for m in mention.split(",") if m.strip()]
        self._chat.touch(self._OPERATOR)
        text = await self._bridge_mask(text)
        return web.json_response(self._chat.send(
            room, self._OPERATOR, text, mention,
            bool(data.get("expects_reply"))))

    async def _api_chat_start(self, request: web.Request) -> web.Response:
        if self._chat is None:
            return web.json_response({"error": "chat unavailable"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        invite = data.get("invite") or []
        if isinstance(invite, str):
            invite = [s.strip() for s in invite.split(",") if s.strip()]
        self._chat.touch(self._OPERATOR)
        return web.json_response(self._chat.start(
            await self._bridge_mask(str(data.get("title") or "chat")), self._OPERATOR, invite))

    async def _api_chat_delete(self, request: web.Request) -> web.Response:
        """Delete a room (final). The dashboard operator oversees every room
        (the list includes agent-only rooms), so it deletes as a trusted
        caller."""
        if self._chat is None:
            return web.json_response({"error": "chat unavailable"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        room = str(data.get("room") or "").strip()
        if not room:
            return web.json_response({"error": "room required"}, status=400)
        return web.json_response(self._chat.delete(room, None))

    async def _api_avatars(self, request: web.Request) -> web.Response:
        """``{identity: version}`` for identities that have a picture (set on
        the /tokens page). The UI renders an <img> for those and initials for
        everyone else."""
        if self._chat is None:
            return web.json_response({})
        return web.json_response(self._chat.avatar_index())

    async def _api_avatar(self, request: web.Request) -> web.Response:
        if self._chat is None:
            raise web.HTTPNotFound()
        got = self._chat.get_avatar(request.query.get("id", ""))
        if not got:
            raise web.HTTPNotFound()
        mime, data, _ = got
        return web.Response(body=data, content_type=mime,
                            headers={"Cache-Control": "private, max-age=86400"})

    async def _api_presence(self, request: web.Request) -> web.Response:
        """Who the dashboard can talk to. Two kinds of entry, merged into one
        ``agents`` list so the UI (and @mention resolution) has a single roster:
          * identities seen over the MCP (chat store presence) — ``online`` if
            they called in within ~90s, otherwise voicemail-only;
          * agents hosted on live band workers that can be *woken* right now:
            a hermes box (``hermes.chat`` cap) shows up as ``agent:hermes_<w>``
            and a worker with ``agent.wake`` as ``agent:<w>``. These carry
            ``worker`` + ``wake`` so a mention can be turned into a wake call.
        Without the second kind, an @mention of a band agent had nothing to resolve against."""
        if self._chat is None:
            return web.json_response({"error": "chat unavailable"}, status=503)
        self._chat.touch(self._OPERATOR)
        agents = self._chat.online()
        by_id = {a["identity"]: a for a in agents}
        workers = []
        now = time.time()
        roster = self._band.workers if self._band is not None else {}
        for wid, w in roster.items():
            name = w.get("name") or wid
            caps = w.get("caps", []) or []
            wake = ("hermes.chat" if "hermes.chat" in caps
                    else "agent.wake" if "agent.wake" in caps else None)
            workers.append({"name": name, "worker_id": wid, "wake": wake,
                            "last_seen_age_secs": round(now - w.get("last_seen", 0.0), 1)})
            if not wake:
                continue
            ident = f"agent:hermes_{name}" if wake == "hermes.chat" else f"agent:{name}"
            ent = by_id.get(ident)
            if ent is None:
                ent = {"identity": ident, "online": False, "last_seen_age_secs": None}
                by_id[ident] = ent
                agents.append(ent)
            ent["worker"] = name
            ent["wake"] = wake
        home = self._home_identity()
        if home:
            # The hub's home agent (hub plugin "home") answers by itself; it is
            # online while the MCP server runs it.
            ent = by_id.get(home)
            if ent is None:
                ent = {"identity": home, "online": False, "last_seen_age_secs": None}
                agents.append(ent)
            ent["home"] = True
        return web.json_response({"agents": agents, "workers": workers})

    def _home_identity(self) -> str | None:
        """``agent:<name>`` of the enabled home agent, from the shared
        settings store (None when it is off or unreadable)."""
        try:
            vals = self._settings_store.namespace_values("home")
        except Exception:
            return None
        if not vals.get("enabled"):
            return None
        name = str(vals.get("name") or "home").strip().lower()
        return f"agent:{name}" if _re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", name) else None

    async def _api_chat_wake(self, request: web.Request) -> web.Response:
        """Wake an agent to reply in a room. For a hermes box (hermes.chat cap)
        we hand it the transcript and post its reply back; otherwise we try the
        agent.wake cap. Same bridge as band_mcp's rook_chat_wake, over the
        dashboard's own band client."""
        if self._chat is None or self._band is None:
            return web.json_response({"error": "chat/band unavailable"}, status=503)
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json body"}, status=400)
        room = str(data.get("room") or "").strip()
        worker = str(data.get("worker") or "").strip()
        if not room or not worker:
            return web.json_response({"error": "room and worker required"}, status=400)
        # Resolve the worker + its caps from the live roster (name or id).
        roster = self._band.workers
        wid = None
        for k, w in roster.items():
            if k == worker or (w.get("name") or "").lower() == worker.lower():
                wid = k
                break
        if wid is None:
            return web.json_response({"ok": False, "error": f"unknown worker {worker!r}"})
        caps = roster[wid].get("caps", [])
        wname = roster[wid].get("name") or worker
        tail = self._chat.read(room, None, since_seq=0, mark=False, limit=30)
        transcript = tail.get("messages", []) if tail.get("ok") else []
        try:
            if "hermes.chat" in caps:
                convo = "\n".join(f"[{m.get('sender')}] {m.get('text')}" for m in transcript)
                prompt = _wake_prompt(tail.get("title") or room, convo, "")
                reply = await self._band.call(cap="hermes.chat",
                                              args={"message": prompt},
                                              target=wid, timeout=120.0)
                res = reply.get("result") or {}
                ans = _clean_hermes_stdout(res.get("stdout", "")) if isinstance(res, dict) else ""
                if not ans:
                    return web.json_response({"ok": False, "error": "hermes gave no parseable reply"})
                self._chat.send(room, f"agent:hermes_{wname}", await self._bridge_mask(ans), [], False)
                return web.json_response({"ok": True, "via": "hermes.chat", "worker": wname})
            if "agent.wake" in caps:
                wake_args = {"room": room, "thread_id": room,
                             "title": tail.get("title"), "transcript": transcript,
                             "woken_by": self._OPERATOR, "note": ""}
                reply = await self._band.call(cap="agent.wake", args=wake_args,
                                              target=wid, timeout=20.0)
                return web.json_response({"ok": True, "via": "agent.wake", "reply": reply})
        except Exception as e:
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return web.json_response({"ok": False,
                                  "error": f"{wname} exposes neither hermes.chat nor agent.wake"})

    async def _band_cli(self, request: web.Request) -> web.Response:
        """Serve the `rook band` terminal control panel as a standalone script
        (the payload the /install installer downloads)."""
        p = Path(__file__).resolve().parents[1] / "cli" / "band_tui.py"
        try:
            text = p.read_text()
        except Exception:
            return web.Response(status=404, text="# band cli unavailable\n")
        if not text.startswith("#!"):
            text = "#!/usr/bin/env python3\n" + text
        return web.Response(text=text, content_type="text/x-python",
                            headers={"Content-Disposition": 'inline; filename="rook"',
                                     "Cache-Control": "no-store"})

    def _installer_web_user(self) -> str:
        """Default dashboard user written into installed CLI configs: this hub's
        own --web-user when it is shell-safe, else "admin" (the documented default)."""
        user = self.web_user or ""
        return user if _re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", user) else "admin"

    async def _installer(self, request: web.Request) -> web.Response:
        """Serve the CLI installer. POSIX: `curl .../install | bash` — the
        unified bash installer (worker | cli | both). Windows: `iex (irm
        .../install?os=windows)` — a PowerShell installer for the `rook` CLI
        (the worker has its own `/worker?os=windows`). OS is chosen by the
        `?os=` query, else sniffed from the User-Agent (same rule as /worker)."""
        ua = request.headers.get("User-Agent", "").lower()
        os_q = request.query.get("os", "").lower()
        if os_q in ("windows", "win"):
            want_ps = True
        elif os_q in ("unix", "linux", "mac", "macos", "darwin", "posix"):
            want_ps = False
        else:
            is_ps = ("powershell" in ua) or ("pwsh" in ua)
            on_windows = ("windows" in ua) or ("win32" in ua) or ("win64" in ua)
            want_ps = is_ps and on_windows
        if want_ps:
            script = (PS_CLI_BOOTSTRAP.replace("__BASE__", self.public_base)
                      .replace("__WEB_USER__", self._installer_web_user()))
            return web.Response(text=script, content_type="text/plain",
                                headers={"Content-Disposition": 'inline; filename="rook-cli-install.ps1"',
                                         "Cache-Control": "no-store"})
        base = self.public_base
        code = request.query.get("band", "")
        if code and not _re.fullmatch(r"[a-z0-9]{6}", code):
            return web.Response(text="invalid pairing code", status=400)
        script = (_INSTALL_SCRIPT.replace("__BASE__", base).replace("__JOIN_CODE__", code)
                  .replace("__WEB_USER__", self._installer_web_user()))
        return web.Response(text=script, content_type="text/x-shellscript",
                            headers={"Content-Disposition": 'inline; filename="rook-install.sh"',
                                     "Cache-Control": "no-store"})

    async def _hub_installer(self, request: web.Request) -> web.Response:
        """`curl -fsSL https://<host>/hub | bash` — stand up a telesthete-hub
        (the band relay) with a short wizard, hardened systemd unit, and auto
        dependency install. `bash -s -- --yes` (or env vars) for unattended."""
        base = self.public_base
        script = _HUB_INSTALL_SCRIPT.replace("__BASE__", base)
        return web.Response(text=script, content_type="text/x-shellscript",
                            headers={"Content-Disposition": 'inline; filename="hub-install.sh"',
                                     "Cache-Control": "no-store"})

    async def _hub_binary(self, request: web.Request) -> web.Response:
        """Serve a prebuilt ``telesthete-hub`` for the caller's arch, if one was
        dropped next to this module. Files are named ``telesthete-hub-<arch>``
        (e.g. ``telesthete-hub-x86_64``); a bare ``telesthete-hub`` is a generic
        fallback. 404 → the /hub installer builds from source via cargo instead."""
        d = Path(__file__).parent
        arch = (request.query.get("arch") or "").strip()
        # Only allow simple arch tokens in the filename (no path traversal).
        candidates = []
        if arch and all(c.isalnum() or c in ("_", "-") for c in arch):
            candidates.append(d / f"telesthete-hub-{arch}")
        candidates.append(d / "telesthete-hub")
        for p in candidates:
            if p.exists() and p.is_file():
                return web.FileResponse(p, headers={
                    "Content-Disposition": "attachment; filename=telesthete-hub",
                    "Content-Type": "application/octet-stream",
                })
        return web.Response(status=404,
                            text="no prebuilt telesthete-hub for this arch — /hub will build from source")

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text=json.dumps({
            "status": "ok",
            "band_workers": len(self._band.workers) if self._band else 0,
        }), content_type="application/json")


def add_arguments(ap) -> None:
    """The dashboard's options (``rook-dashboard``; ``rook hub serve`` adds
    the MCP server's to the same parser)."""
    import argparse

    env = os.environ.get
    ap.add_argument("--bind", default=env("ROOK_BIND", "0.0.0.0"),
                    help="HTTP listen address (ROOK_BIND, default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=int(env("ROOK_PORT", "7005")),
                    help="HTTP listen port (ROOK_PORT, default 7005)")
    ap.add_argument("--domain", default=env("ROOK_DOMAIN", "hub.example.com"),
                    help="public host[:port] of this dashboard, or a full http(s):// URL, used "
                         "in installer URLs (ROOK_DOMAIN). Without a scheme it is https, except "
                         "a LAN/local address on --port (this plain-HTTP dashboard) or :80")
    ap.add_argument("--psk", default=env("ROOK_BAND_PSK", ""),
                    dest="band_psk", help="band pre-shared key (ROOK_BAND_PSK; "
                                          "blank = configure via the /setup wizard)")
    ap.add_argument("--hub-public", default=env("ROOK_HUB_PUBLIC", "hub.example.com:443"),
                    help="host:port workers call back to, templated into installers (ROOK_HUB_PUBLIC)")
    ap.add_argument("--band-name", default=env("ROOK_BAND_NAME", "rook-band"),
                    help="cosmetic band label (ROOK_BAND_NAME)")
    # The legacy exec worker and its /ws endpoint are gone; the flag is still
    # accepted so an old service unit that passes it keeps starting.
    ap.add_argument("--token", default="", help=argparse.SUPPRESS)
    ap.add_argument("--web-user", default=env("ROOK_WEB_USER", ""),
                    help="dashboard username (ROOK_WEB_USER; blank = password only)")
    ap.add_argument("--web-pass", default=env("ROOK_WEB_PASS", ""),
                    help="dashboard admin password (ROOK_WEB_PASS). Prefer the "
                         "variable: command-line arguments are visible in ps.")
    ap.add_argument("--insecure-no-auth", action="store_true",
                    help="allow serving without a dashboard password on a "
                         "non-loopback address (anyone who can reach it can run "
                         "commands on every worker)")
    ap.add_argument("--hub-host", default=env("ROOK_HUB_HOST", "127.0.0.1"),
                    help="telesthete hub host the dashboard joins (ROOK_HUB_HOST)")
    ap.add_argument("--hub-port", type=int, default=int(env("ROOK_HUB_PORT", "7474")),
                    help="telesthete hub UDP port (ROOK_HUB_PORT)")


def prepare(ap, args) -> None:
    """Checks and one-time setup before the dashboard starts: the password
    rule, recording a flag/env configuration as setup, the signing key."""
    loopback = args.bind in ("127.0.0.1", "::1", "localhost")
    if not args.web_pass and not loopback and not args.insecure_no_auth:
        ap.error("refusing to serve the dashboard on %s without a password: set "
                 "ROOK_WEB_PASS (or --web-pass), bind to 127.0.0.1, or pass "
                 "--insecure-no-auth" % args.bind)

    # Configured through flags/env instead of the /setup wizard: record it the
    # way the wizard would, so the hub starts configured rather than serving an
    # open setup page. An existing configuration is never overwritten.
    from . import setup_store
    if args.band_psk and not setup_store.is_configured():
        setup_store.save({"band_name": args.band_name, "band_psk": args.band_psk,
                          "hub_public": args.hub_public, "pyz_domain": args.domain})

    # This hub's own key for signing worker updates and deauth orders; the
    # public half is stamped into the worker bundles it builds.
    from .update_keys import ensure_key, key_path, public_key_b64
    signing_key = ensure_key()
    if signing_key:
        log.info("update signing key %s (public %s)", key_path(), public_key_b64(signing_key))


def server_from_args(args, argv, **shared) -> "CombinedServer":
    from .dashboard_settings import explicit_sources
    return CombinedServer(
        explicit=explicit_sources(argv),
        argv=argv,
        port=args.port,
        domain=args.domain,
        band_psk=args.band_psk,
        web_user=args.web_user,
        web_pass=args.web_pass,
        hub_host=args.hub_host,
        hub_port=args.hub_port,
        hub_public=args.hub_public,
        band_name=args.band_name,
        bind=args.bind,
        **shared,
    )


def _cli_main() -> None:
    """Argparse entry point: python -m rook.remote.bootstrap [options]."""
    import argparse

    ap = argparse.ArgumentParser(
        description="Rook hub dashboard: web UI, worker installer and band controller",
        epilog="Every option can also be set with the ROOK_* variable named in its help. "
               "`rook hub serve` runs this dashboard and the MCP server in one process.")
    add_arguments(ap)
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    prepare(ap, args)
    server = server_from_args(args, sys.argv[1:])

    async def _run() -> None:
        await server.start()
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await server.stop()

    asyncio.run(_run())


if __name__ == "__main__":
    _cli_main()
