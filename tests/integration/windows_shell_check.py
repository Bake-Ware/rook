"""Manual check of shell parity on Windows: shell.exec, proc.start and
terminal ``cmd`` strings through PowerShell, ``argv`` resolution, and
``proc.start(pty=True)`` on a ConPTY.

Run on a Windows 10 1809+ / 11 machine from a checkout (or an unpacked
archive) of this repository, with Python 3.10+::

    py tests\\integration\\windows_shell_check.py

It is not collected by pytest (the file name does not start with test_).
Every step prints PASS/FAIL (SKIP when the tool it needs isn't installed);
the exit code is the number of failures. It starts only short-lived
PowerShell/cmd/python children in a temporary folder and leaves nothing
behind.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from rook.worker import shells, termwire  # noqa: E402
from rook.worker.plugins.proc import ProcPlugin  # noqa: E402
from rook.worker.plugins.shell import ShellPlugin  # noqa: E402
from rook.worker.plugins.terminals import TerminalsPlugin  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def skip(name: str, why: str) -> None:
    print(f"SKIP  {name}  ({why})")


async def shell_exec_checks(tmp: str) -> None:
    p = ShellPlugin()
    run = p._exec

    r = await run(cmd="Set-Content -Path a.txt -Value hello; ls; cat a.txt; pwd", cwd=tmp)
    check("cmd defaults to PowerShell", r.get("shell") == "powershell", str(r.get("shell")))
    check("ls, cat, pwd work", r["ok"] and "a.txt" in r["stdout"] and "hello" in r["stdout"]
          and tmp.lower() in r["stdout"].lower(), repr(r)[:300])

    r = await run(cmd="Write-Output 'caf\u00e9 \u2713 \u65e5\u672c'")
    check("UTF-8 output from PowerShell", r["stdout"].strip() == "caf\u00e9 \u2713 \u65e5\u672c", repr(r["stdout"]))

    r = await run(cmd="cmd /c exit 3")
    check("exit code is the last native command's", r["code"] == 3, str(r["code"]))
    r = await run(cmd="cmd /c exit 3; Write-Output ok")
    check("success after a failure exits 0, as sh does", r["code"] == 0 and r["stdout"].strip() == "ok",
          str(r["code"]))
    r = await run(cmd="exit 5")
    check("exit N", r["code"] == 5, str(r["code"]))
    r = await run(cmd="Write-Progress -Activity a -Status b; Get-Item C:\\rook-no-such-path-xyz")
    check("a failing last cmdlet exits non-zero", r["code"] == 1, str(r["code"]))
    check("errors are plain text, never CLIXML", "CLIXML" not in r["stderr"] and "rook-no-such-path-xyz" in r["stderr"],
          repr(r["stderr"])[:200])
    r = await run(cmd="Write-Output 'oops")
    check("a parse error exits non-zero with the message", r["code"] != 0 and r["stderr"].strip() != "",
          repr(r)[:200])

    tricky = 'a "b" \'c\' $d %PATH% & | < > ^ \\" \\\\'
    r = await run(cmd="Write-Output '" + tricky.replace("'", "''") + "'")
    check("quotes and metacharacters survive", r["stdout"].rstrip("\r\n") == tricky, repr(r["stdout"]))

    r = await run(cmd="$input | ForEach-Object { $_.ToUpper() }", stdin="abc\ndef\n")
    check("stdin reaches the command", r["stdout"].split() == ["ABC", "DEF"], repr(r["stdout"]))

    r = await run(cmd="python3 -c \"import sys; print(sys.version_info[0])\"")
    check("python3 runs a real Python in cmd", r["code"] == 0 and r["stdout"].strip() == "3",
          repr(r)[:300])
    r = await run(argv=["python3", "-c", "import sys; print(sys.stdin.read().upper())"], stdin="piped")
    check("python3 runs a real Python in argv, stdin piped", r["code"] == 0 and r["stdout"].strip() == "PIPED",
          repr(r)[:300])

    r = await run(cmd="[int](Get-Process -Id $PID).MainWindowHandle")
    check("no visible console window for the child", r["stdout"].strip() == "0", repr(r["stdout"]))

    r = await run(cmd='echo "%OS%" & ver', shell="cmd")
    check("shell=cmd runs cmd.exe with its own quoting", r.get("shell") == "cmd"
          and '"Windows_NT"' in r["stdout"], repr(r)[:200])

    if shells._git_sh("sh"):
        r = await run(cmd="uname -s; echo $((6*7))", shell="sh")
        check("shell=sh runs Git for Windows' sh", r["code"] == 0 and "42" in r["stdout"]
              and r.get("shell") == "sh", repr(r)[:200])
    else:
        skip("shell=sh", "Git for Windows not installed")

    for tool, args, needle in (("git", ["--version"], "git version"), ("npm", ["--version"], ".")):
        if shutil.which(tool):
            r = await run(argv=[tool, *args])
            check(f"argv {tool} resolves like a shell (PATHEXT, npm shim)",
                  r["code"] == 0 and needle in r["stdout"], repr(r)[:200])
        else:
            skip(f"argv {tool}", "not installed")


async def proc_checks(tmp: str) -> None:
    p = ProcPlugin()
    try:
        r = await p._start(cmd="Write-Output proc-$((6*7)); exit 4", cwd=tmp)
        check("proc.start cmd runs in PowerShell", r["ok"] and r.get("shell") == "powershell", str(r))
        out = ""
        for _ in range(100):
            got = await p._read(r["handle"], 0, 8192)
            out = got["chunk"]
            if got["eof"]:
                break
            await asyncio.sleep(0.1)
        check("proc.start cmd output and exit code", "proc-42" in out and got["exit_code"] == 4,
              repr(got)[:200])

        if shells._git_sh("sh"):
            r = await p._start(cmd="echo sh-$((6*7))", shell="sh", cwd=tmp)
            for _ in range(100):
                got = await p._read(r["handle"], 0, 8192)
                if got["eof"]:
                    break
                await asyncio.sleep(0.1)
            check("proc.start shell=sh runs Git's sh, named in the reply",
                  r.get("shell") == "sh" and "sh-42" in got["chunk"], repr(got)[:200])
        else:
            skip("proc.start shell=sh", "Git for Windows not installed")

        ps = shells.powershell()
        r = await p._start(argv=[ps, "-NoLogo", "-NoProfile"], cwd=tmp, pty=True, label="repl")
        check("proc.start(pty=True) starts on a ConPTY", r["ok"] and r["pty"], str(r))
        if r["ok"]:
            h = r["handle"]
            await asyncio.sleep(2)
            await p._write(h, "6*7")
            text, cursor = "", 0
            for _ in range(100):
                got = await p._read(h, cursor, 32768)
                text += got["chunk"]
                cursor = got["next_cursor"]
                if "42" in text:
                    break
                await asyncio.sleep(0.1)
            check("an interactive REPL answers (Enter is CR)", "42" in text, repr(text[-200:]))
            await p._write(h, "exit 6")
            for _ in range(100):
                info = next(s for s in (await p._list())["sessions"] if s["handle"] == h)
                if not info["running"]:
                    break
                await asyncio.sleep(0.1)
            check("REPL exit code", info["exit_code"] == 6, str(info))

            r = await p._start(argv=[ps, "-NoProfile", "-Command", "Start-Sleep 60"], pty=True)
            await asyncio.sleep(1)
            closed = await p._close(r["handle"])
            check("proc.close stops a ConPTY session", closed["ok"], str(closed))
    finally:
        await p.stop()


async def terminal_checks(tmp: str) -> None:
    os.environ.setdefault("ROOK_WORK_TERM_DIR", os.path.join(tmp, "terms"))
    p = TerminalsPlugin()
    try:
        r = await p.open(harness="shell", cwd=tmp, cmd="Write-Output term-$((6*7)); cmd /c exit 9")
        tid = r["id"]
        out, cursor = b"", 0
        for _ in range(60):
            got = await p.read(tid, cursor, wait=1)
            out += termwire.decode(got["enc"], got["data"])
            cursor = got["next"]
            if got["eof"]:
                break
        t = p.terms[tid]
        check("console cmd runs in PowerShell on a ConPTY", b"term-42" in out, repr(out[-200:]))
        check("console cmd exit code", t.exit_code == 9, str(t.exit_code))
    finally:
        await p.stop()


def main() -> int:
    if sys.platform != "win32":
        print("run this on Windows")
        return 1
    print(f"PowerShell: {shells.powershell()}")
    with tempfile.TemporaryDirectory(prefix="rook-shell-check-") as tmp:
        asyncio.run(shell_exec_checks(tmp))
        asyncio.run(proc_checks(tmp))
        asyncio.run(terminal_checks(tmp))
    print(f"\n{failures} failure(s)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
