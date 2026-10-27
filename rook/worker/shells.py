"""How a ``cmd`` string runs: the same shell model on every worker.

``shell.exec``, ``proc.start`` and ``work.stream.open`` take ``cmd`` as a
string for a shell. On Linux and macOS that is ``/bin/sh -c``. On Windows it
is PowerShell (``pwsh.exe`` 7+ when installed, which also takes ``&&`` and
``||``; else Windows PowerShell 5.1), so the commands agents reach for on
Linux (``ls``, ``cat``, ``pwd``, ``rm``, ``cp``, ``mv``, ``echo``, ``sleep``,
pipes, ``$env:X``) mean something there. cmd.exe has none of them. A caller
that wants a particular shell names it: ``shell="sh"|"bash"|"powershell"|
"cmd"`` (on Windows ``sh``/``bash`` are Git for Windows' own, never WSL).

The PowerShell wrapper makes it behave like ``sh -c`` where it matters to a
caller reading the reply: output is UTF-8 (5.1 writes the OEM code page into
a pipe), no progress records, and the exit code is the last command's
(``-Command`` alone turns a native command's failure into 1, and its success
after an earlier failure into 1 as well).

On Windows ``argv[0]`` is also resolved the way a shell would: PATHEXT
(``npm`` is ``npm.cmd``), an npm shim runs its real target, and ``python3``
or ``python`` that only find the Microsoft Store alias stub run a real
interpreter instead.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

_IS_WIN = sys.platform == "win32"

SHELLS = ("sh", "bash", "powershell", "cmd")
_ALIASES = {"pwsh": "powershell", "powershell.exe": "powershell", "pwsh.exe": "powershell",
            "cmd.exe": "cmd", "/bin/sh": "sh", "sh.exe": "sh", "bash.exe": "bash"}

#: CreateProcess flag for children on pipes: the worker runs under pythonw.exe
#: (no console), so every console program would otherwise pop its own window.
CREATE_NO_WINDOW = 0x08000000

#: Longest Windows command line (CreateProcess counts the NUL).
MAX_CMDLINE = 32767

_PYTHONS = ("python3", "python")
_STORE_STUB = re.compile(r"[\\/]Microsoft[\\/]WindowsApps[\\/]", re.I)
# The target of an npm cmd-shim: "%dp0%\node_modules\pkg\bin\cli.js" %*
_NPM_SHIM = re.compile(r'"%~?dp0%?\\([^"%*\r\n]+?\.(?:js|cjs|mjs|exe))"', re.I)

_PS_PRELUDE = (
    "$ProgressPreference='SilentlyContinue'\n"
    "try{$__u=New-Object Text.UTF8Encoding $false;[Console]::OutputEncoding=$__u;"
    "[Console]::InputEncoding=$__u;$OutputEncoding=$__u}catch{}\n")
# $? first: it is the status of the caller's last statement, and nothing else
# may run before it is read.
_PS_EPILOGUE = "\nif($?){exit 0}elseif($LASTEXITCODE){exit $LASTEXITCODE}else{exit 1}"


def default_shell() -> str:
    return "powershell" if _IS_WIN else "sh"


def shell_name(shell: str | None) -> str:
    """``shell`` normalised to one of :data:`SHELLS` ('' = this OS's default)."""
    name = str(shell or "").strip().lower()
    if not name:
        return default_shell()
    name = _ALIASES.get(name, name)
    if name not in SHELLS:
        raise ValueError(f"shell must be one of {', '.join(SHELLS)}")
    return name


def powershell() -> str | None:
    """PowerShell 7 (``pwsh``) when installed, else Windows PowerShell 5.1."""
    names = ("pwsh", "powershell") if _IS_WIN else ("pwsh",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if _IS_WIN:
        root = os.environ.get("SystemRoot") or r"C:\Windows"
        path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.isfile(path):
            return path
    return None


def _git_sh(name: str) -> str | None:
    """Git for Windows' ``sh.exe``/``bash.exe``. A bare ``bash`` on PATH is
    usually WSL's launcher (System32 or WindowsApps), a different machine
    with a different filesystem, so it never counts."""
    roots = []
    git = shutil.which("git")
    if git:
        # <Git>\cmd\git.exe or <Git>\bin\git.exe or <Git>\mingw64\bin\git.exe
        roots.append(os.path.dirname(os.path.dirname(git)))
        roots.append(os.path.dirname(os.path.dirname(os.path.dirname(git))))
    for var in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if base:
            roots.append(os.path.join(base, "Git"))
            roots.append(os.path.join(base, "Programs", "Git"))
    for root in roots:
        for sub in ("bin", os.path.join("usr", "bin")):
            path = os.path.join(root, sub, name + ".exe")
            if os.path.isfile(path):
                return path
    found = shutil.which(name)
    if found and not re.search(r"[\\/](System32|WindowsApps)[\\/]", found, re.I):
        return found
    return None


def _quote_ps(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def powershell_script(cmd: str) -> str:
    """``cmd`` wrapped so PowerShell answers like ``sh -c`` (module doc)."""
    aliases = "".join(f"Set-Alias {name} {_quote_ps(path)}\n"
                      for name, path in _python_aliases().items())
    return _PS_PRELUDE + aliases + cmd + _PS_EPILOGUE


def shell_argv(cmd: str, shell: str | None = None) -> tuple[list[str], str]:
    """``(argv, shell name)`` that runs the string ``cmd`` in ``shell``
    ('' = this OS's default). ValueError when that shell isn't here."""
    if not isinstance(cmd, str):
        raise ValueError("cmd must be a string")
    name = shell_name(shell)
    if name == "powershell":
        binary = powershell()
        if not binary:
            raise ValueError("PowerShell is not installed on this worker")
        argv = [binary, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                powershell_script(cmd)]
        if _IS_WIN and len(subprocess.list2cmdline(argv)) >= MAX_CMDLINE:
            raise ValueError("cmd is too long for a Windows command line; "
                             "write it to a .ps1 file and run that")
        return argv, name
    if name == "cmd":
        if not _IS_WIN:
            raise ValueError("cmd.exe only exists on Windows workers")
        return [os.environ.get("COMSPEC") or "cmd.exe", "/d", "/s", "/c", cmd], name
    if _IS_WIN:
        binary = _git_sh(name)
        if not binary:
            raise ValueError(f"no {name} on this worker (install Git for Windows; "
                             "WSL's bash is not used)")
        return [binary, "-c", cmd], name
    if name == "bash":
        binary = shutil.which("bash")
        if not binary:
            raise ValueError("bash is not installed on this worker")
        return [binary, "-c", cmd], name
    return ["/bin/sh", "-c", cmd], name


def is_cmd_argv(argv: list[str]) -> bool:
    """The ``cmd`` form :func:`shell_argv` builds for cmd.exe."""
    return len(argv) == 5 and [a.lower() for a in argv[1:4]] == ["/d", "/s", "/c"]


def command_line(argv: list[str]) -> str:
    """The CreateProcess command line for ``argv``. cmd.exe doesn't take
    MSVCRT quoting (``\\"``), so its ``/s /c`` command goes in verbatim, in
    the one pair of quotes ``/s`` strips."""
    if is_cmd_argv(argv):
        line = f'"{argv[0]}" /d /s /c "{argv[4]}"'
    else:
        line = subprocess.list2cmdline([str(a) for a in argv])
    if len(line) >= MAX_CMDLINE:
        raise ValueError("command line is too long for Windows")
    return line


# -- argv[0] on Windows ----------------------------------------------------------

def _real_python(name: str) -> str | None:
    """A Python on PATH that isn't the Store alias stub, for ``python3`` or
    ``python``; else the interpreter the worker itself was made from."""
    for candidate in (name, *(p for p in _PYTHONS if p != name)):
        path = os.environ.get("PATH", "")
        for d in path.split(os.pathsep):
            if not d or _STORE_STUB.search(d + os.sep):
                continue
            exe = os.path.join(d, candidate + ".exe")
            if os.path.isfile(exe):
                return exe
    base = getattr(sys, "_base_executable", "") or sys.executable
    if base:
        exe = os.path.join(os.path.dirname(base), "python.exe")
        if os.path.isfile(exe):
            return exe
    return None


def _python_aliases() -> dict[str, str]:
    """``python3``/``python`` -> a real interpreter, where PATH finds only the
    Store stub (or nothing) for that name. Windows only."""
    if not _IS_WIN:
        return {}
    out = {}
    for name in _PYTHONS:
        found = shutil.which(name)
        if found and not _STORE_STUB.search(found):
            continue
        real = _real_python(name)
        if real:
            out[name] = real
    return out


def npm_shim_target(path: str) -> list[str] | None:
    """[node, script] (or [exe]) behind an npm cmd-shim, so it runs without
    cmd.exe re-parsing its arguments. None if it isn't one."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            m = _NPM_SHIM.search(f.read(16384))
    except OSError:
        return None
    if not m:
        return None
    base = os.path.dirname(path)
    target = os.path.normpath(os.path.join(base, *re.split(r"[\\/]", m.group(1))))
    if not os.path.isfile(target):
        return None
    if target.lower().endswith(".exe"):
        return [target]
    node = os.path.join(base, "node.exe")
    if not os.path.isfile(node):
        node = shutil.which("node")
    return [node, target] if node else None


def resolve_argv(argv: list[str]) -> list[str]:
    """``argv`` with ``argv[0]`` found the way a shell would find it (module
    doc). Unchanged off Windows, and for anything it can't place (the spawn
    then reports it missing)."""
    argv = [str(a) for a in argv]
    if not _IS_WIN or not argv:
        return argv
    head = argv[0]
    if os.path.basename(head).lower().removesuffix(".exe") in _PYTHONS and not os.path.dirname(head):
        real = _python_aliases().get(os.path.basename(head).lower().removesuffix(".exe"))
        if real:
            return [real] + argv[1:]
    found = head if os.path.dirname(head) else shutil.which(head)
    if not found:
        return argv
    if found.lower().endswith((".cmd", ".bat")):
        target = npm_shim_target(found)
        if target:
            return target + argv[1:]
    return [found] + argv[1:]


def decode(data: bytes) -> str:
    """Process output as text: UTF-8, else (Windows programs writing the OEM
    code page into a pipe, such as ipconfig) the console's code page."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if _IS_WIN:
        try:
            return data.decode("oem")
        except (LookupError, UnicodeDecodeError):
            pass
    return data.decode("utf-8", errors="replace")


def popen_flags() -> dict:
    """Extra ``create_subprocess_*`` kwargs for a child on pipes."""
    return {"creationflags": CREATE_NO_WINDOW} if _IS_WIN else {}
