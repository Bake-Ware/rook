"""Manual check of Windows terminals and the Windows Claude inbox.

Run on a Windows 10 1809+ / 11 machine from a checkout of this repository
(PowerShell or cmd, any folder; Python 3.10+ with the repo's requirements)::

    py tests\\integration\\windows_conpty_check.py
    py tests\\integration\\windows_conpty_check.py --claude      # also start claude --version
    py tests\\integration\\windows_conpty_check.py --inbox        # list Claude inboxes (read-only)
    py tests\\integration\\windows_conpty_check.py --send <session-id>   # ONE test message

It is not collected by pytest (the file name does not start with test_).
Every step prints PASS/FAIL; the exit code is the number of failures. The
default run starts only short-lived PowerShell/cmd children in a temporary
folder and leaves nothing behind. ``--inbox`` only reads
``%USERPROFILE%\\.claude\\sessions``; only ``--send`` writes anything, and only
into the one session you name.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rook.core.facts import detect_facts  # noqa: E402
from rook.worker import conpty, session_messages, termwire, winsec  # noqa: E402
from rook.worker.plugins.terminals import TerminalsPlugin  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


async def follow(p: TerminalsPlugin, tid: str, needle: str, cursor: int = 0, timeout: float = 20):
    out = b""
    try:
        async with asyncio.timeout(timeout):
            while needle.encode() not in out:
                r = await p.read(tid, cursor, wait=2)
                out += termwire.decode(r["enc"], r["data"])
                cursor = r["next"]
                if r["eof"]:
                    break
    except TimeoutError:
        pass
    return out, cursor


def raw_conpty(tmp: str) -> None:
    """conpty.py alone: output, input, resize, exit code, job teardown."""
    out = bytearray()
    pty = conpty.ConPty.spawn('cmd.exe /d /c "echo conpty-ok & set /p x= & echo got-%x% & exit /b 7"',
                              tmp, dict(os.environ), cols=100, rows=30)
    pty.start_reader(out.extend)
    time.sleep(1.0)
    pty.resize(120, 40)
    pty.write(b"abc\r")
    code = pty.wait(15)
    pty.close()
    text = out.decode("utf-8", "replace")
    check("ConPTY: child output arrives as VT text", "conpty-ok" in text, repr(text[-200:]))
    check("ConPTY: input reaches the child", "got-abc" in text)
    check("ConPTY: exit code", code == 7, f"got {code}")

    # A grandchild must die with the job (KILL_ON_JOB_CLOSE / TerminateJobObject).
    marker = Path(tmp) / "grandchild.txt"
    pty = conpty.ConPty.spawn(
        'powershell.exe -NoLogo -NoProfile -Command "$p = Start-Process -PassThru -WindowStyle Hidden '
        'powershell.exe -ArgumentList \'-NoProfile\',\'-Command\',\'Start-Sleep 300\'; '
        f'Set-Content -Path \'{marker}\' -Value $p.Id; Start-Sleep 300"', tmp, dict(os.environ))
    pty.start_reader(lambda _b: None)
    for _ in range(100):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.2)
    grandchild = int(marker.read_text().strip()) if marker.exists() else 0
    pty.kill()
    pty.wait(10)
    pty.close()
    time.sleep(0.5)
    alive = bool(grandchild) and (winsec.process_info(grandchild) or {}).get("alive", False)
    check("ConPTY: kill takes the whole job (grandchild gone)", bool(grandchild) and not alive,
          f"grandchild pid {grandchild}")


async def plugin_checks(tmp: str, with_claude: bool) -> None:
    os.environ["ROOK_WORK_TERM_DIR"] = str(Path(tmp) / "terms")
    p = TerminalsPlugin()
    try:
        check("plugin available() (ConPTY present)", p.available())
        r = await p.open(harness="shell", cwd=tmp, cols=100, rows=30)
        tid = r["id"]
        await p.write(tid, "Write-Output ('ready-' + (6*7)); $Host.UI.RawUI.WindowSize.Width\r")
        out, cur = await follow(p, tid, "ready-42")
        check("work.stream: PowerShell runs and echoes", b"ready-42" in out, repr(out[-200:]))
        p.resize(tid, 132, 40)
        await asyncio.sleep(0.5)
        await p.write(tid, "Write-Output ('width-' + $Host.UI.RawUI.WindowSize.Width)\r")
        out, cur = await follow(p, tid, "width-132", cur)
        check("work.stream: resize reaches the console", b"width-132" in out)
        await p.write(tid, "ping -t 127.0.0.1\r")
        await asyncio.sleep(2)
        p.signal(tid, "INT")
        await asyncio.sleep(1)
        await p.write(tid, "Write-Output after-ctrl-c\r")
        out, cur = await follow(p, tid, "after-ctrl-c", cur)
        check("work.stream: Ctrl-C (INT) stops the foreground program", b"after-ctrl-c" in out)
        closed = await p.close(tid)
        check("work.stream: close ends the terminal", closed["ok"] and tid not in p.terms)

        r = await p.open(harness="shell", cwd=tmp, mcp_url="https://hub.example.com/mcp", mcp_token="not-a-real-token")
        t = p.terms[r["id"]]
        owner, aces = winsec.file_security(str(Path(tmp) / "terms"))
        check("terminal dir: owner-only ACL", aces is not None and {sid for _k, sid in aces} == {winsec.current_user_sid()},
              f"{aces}")
        await p.write(r["id"], "Write-Output ('env-' + $env:TERM + '-' + [bool]$env:ROOK_MCP_TOKEN)\r")
        out, _ = await follow(p, r["id"], "env-xterm-256color-True")
        check("work.stream: TERM and ROOK_MCP_* injected", b"env-xterm-256color-True" in out)
        await p.close(r["id"])
        check("work.stream: per-session files removed", not t.files)

        if with_claude:
            r = await p.open(harness="claude", cwd=tmp, mcp_url="https://hub.example.com/mcp", mcp_token="not-a-real-token")
            cfg = p.terms[r["id"]].files[0] if p.terms[r["id"]].files else ""
            if cfg:
                _o, aces = winsec.file_security(cfg)
                check("claude MCP config: owner-only ACL", aces is not None
                      and {sid for _k, sid in aces} == {winsec.current_user_sid()}, f"{aces}")
            out, _ = await follow(p, r["id"], "Claude Code", timeout=40)
            check("work.stream: Claude Code starts and draws", len(out) > 200, f"{len(out)} bytes")
            await p.close(r["id"])
    finally:
        await p.stop()


def inbox_listing() -> None:
    home = Path.home() / ".claude" / "sessions"
    import json
    for marker in sorted(home.glob("*.json")):
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        sid = data.get("sessionId", "")
        ep = session_messages.claude_endpoint(sid)
        print(f"  {sid}  pid {data.get('pid')}  {data.get('status', '?'):<8} inbox: "
              f"{'reachable' if ep else 'not reachable'}  private: {winsec.is_private(marker)}")


async def send_one(session_id: str) -> None:
    import uuid
    res = await session_messages.deliver("claude", session_id, "manual-" + uuid.uuid4().hex[:12],
                                         "Rook Windows inbox check: please reply with just OK.")
    check("Claude inbox: message forwarded over the named pipe", bool(res.get("ok")), str(res))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true", help="also start Claude Code in a terminal")
    ap.add_argument("--inbox", action="store_true", help="list Claude inboxes (read-only)")
    ap.add_argument("--send", metavar="SESSION_ID", help="send ONE test message to this session")
    args = ap.parse_args()
    if sys.platform != "win32":
        print("Run this on Windows.")
        return 1
    check("Windows has ConPTY (CreatePseudoConsole)", conpty.available())
    check("facts: pty advertised", bool(detect_facts().get("pty")))
    print(f"      user SID {winsec.current_user_sid()}, powershell at {shutil.which('powershell.exe')}")
    with tempfile.TemporaryDirectory() as tmp:
        raw_conpty(tmp)
        asyncio.run(plugin_checks(tmp, args.claude))
    if args.inbox or args.send:
        print("Claude sessions on this host:")
        inbox_listing()
    if args.send:
        asyncio.run(send_one(args.send))
    print(f"{failures} failure(s)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
