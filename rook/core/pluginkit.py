"""Small helpers every plugin body ends up needing.

One copy of what worker plugins used to paste into each module: running a
command with a timeout, platform checks, the worker state directory, raising
a failure the host reports, and checking cap arguments.

Errors: a cap reports failure by raising, not by returning
``{"ok": False, "error": ...}``. The host (``rook.core.host`` and the worker
dispatcher) turns a raise into the band reply ``{"ok": false, "error":
"<Type>: <message>"}``, so every caller sees one shape. :func:`fail` raises
:class:`CapError`; the argument checks raise :class:`ArgError`, a
``TypeError``, which the dispatchers report as ``bad args: <message>``. Caps
whose *result* is a verdict (``shell.exec``'s exit code, an HTTP status)
still return it.

Stdlib only: it ships inside the worker bundle and the Android app as part of
``rook.core``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, NoReturn, Sequence

from .paths import worker_state_dir, worker_state_path

__all__ = [
    "ArgError", "CapError", "Completed", "RunTimeout", "fail", "has_display",
    "is_android", "is_termux", "is_windows", "opt_int", "require_choice",
    "require_int", "require_str", "run", "state_dir", "state_path", "which",
]


# -- errors -------------------------------------------------------------------

class CapError(Exception):
    """A cap could not do what was asked. The message is what the caller sees."""


class ArgError(CapError, TypeError):
    """A cap argument is missing or malformed (reported as ``bad args: ...``)."""


class RunTimeout(CapError, TimeoutError):
    """A command run by :func:`run` outlived its timeout and was killed. Still
    a ``TimeoutError``, so code that caught ``asyncio.TimeoutError`` keeps
    working."""


def fail(message: str) -> NoReturn:
    """Stop the cap and report ``message`` as its error."""
    raise CapError(message)


# -- running commands ---------------------------------------------------------

class Completed(NamedTuple):
    """A finished command. Unpacks as ``code, stdout, stderr``."""

    code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.code == 0

    def error(self, limit: int = 300) -> str:
        """The command's own complaint: stderr, else stdout, else the exit code."""
        text = (self.stderr or self.stdout).strip()
        return text[:limit] if text else f"exit {self.code}"


async def run(cmd: Sequence[str], timeout: float = 30.0, *,
              stdin: "bytes | str | None" = None,
              env: Mapping[str, str] | None = None,
              cwd: "str | os.PathLike | None" = None) -> Completed:
    """Run ``cmd`` (an argv, no shell) and wait at most ``timeout`` seconds.

    Output is decoded as UTF-8 with ``errors="replace"``, so a stray byte never
    fails a cap. On timeout the process is killed and reaped (no zombie, no
    orphan holding a device) and :class:`RunTimeout` is raised. A missing
    binary raises ``FileNotFoundError`` as ``subprocess`` does. ``env``
    overlays the worker's environment rather than replacing it."""
    argv = [str(a) for a in cmd]
    child_env = None
    if env:
        child_env = dict(os.environ)
        child_env.update({str(k): str(v) for k, v in env.items()})
    payload = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if payload is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=child_env, cwd=cwd, **no_window())
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        raise RunTimeout(f"{os.path.basename(argv[0])} timed out after {timeout:g}s") from None
    except asyncio.CancelledError:
        # The cap was cancelled (caller went away): don't leave the child running.
        if proc.returncode is None:
            proc.kill()
        raise
    return Completed(proc.returncode or 0, out.decode("utf-8", errors="replace"),
                     err.decode("utf-8", errors="replace"))


def which(name: str, *alternatives: str) -> str | None:
    """The first of ``name``/``alternatives`` found on PATH. On Windows a bare
    name also tries ``.exe`` and ``.cmd`` explicitly: npm installs ``.cmd``
    shims that ``shutil.which`` misses when PATHEXT has been trimmed."""
    for candidate in (name, *alternatives):
        found = shutil.which(candidate)
        if found:
            return found
        if is_windows() and not os.path.splitext(candidate)[1]:
            for ext in (".exe", ".cmd"):
                found = shutil.which(candidate + ext)
                if found:
                    return found
    return None


# -- platform -----------------------------------------------------------------

def is_windows() -> bool:
    return sys.platform == "win32"


#: CreateProcess flag: the Windows worker runs under pythonw.exe (no console),
#: so every console program it starts would otherwise flash its own window.
CREATE_NO_WINDOW = 0x08000000


def no_window() -> dict:
    """Extra ``subprocess``/``create_subprocess_*`` kwargs that keep a console
    child's window hidden on Windows (and nothing elsewhere)."""
    return {"creationflags": CREATE_NO_WINDOW} if is_windows() else {}


def is_termux() -> bool:
    """Running under Termux on Android. The app sets ``TERMUX_VERSION`` in its
    sessions; a service started by termux-services or termux-boot may only
    have ``PREFIX`` (``/data/data/com.termux/files/usr``), so either counts."""
    return bool(os.environ.get("TERMUX_VERSION")) or "com.termux" in os.environ.get("PREFIX", "")


def is_android() -> bool:
    """Any Android userland: Termux, or the native app (Chaquopy)."""
    return ("ANDROID_ROOT" in os.environ or sys.platform == "android"
            or hasattr(sys, "getandroidapilevel") or is_termux())


def has_display() -> bool:
    """A graphical session to draw on: always on Windows/macOS, X11 or
    Wayland elsewhere. Termux has none (its X11 needs ``DISPLAY`` too)."""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# -- state --------------------------------------------------------------------

def state_dir() -> Path:
    """The worker state directory (``ROOK_WORKER_HOME`` or ``~/.rook-band-worker``)."""
    return worker_state_dir()


def state_path(*parts: str) -> Path:
    """A path inside the worker state directory (parents not created)."""
    return worker_state_path(*parts)


# -- argument checks ----------------------------------------------------------

def require_str(value: Any, name: str, *, max_len: int | None = None,
                strip: bool = True, empty: bool = False) -> str:
    """``value`` as a string: must be a ``str``, non-blank unless ``empty``,
    and at most ``max_len`` characters. Returned stripped when ``strip``."""
    if not isinstance(value, str):
        raise ArgError(f"{name} must be a string")
    out = value.strip() if strip else value
    if not empty and not out.strip():
        raise ArgError(f"{name} is required")
    if max_len is not None and len(out) > max_len:
        raise ArgError(f"{name} must be at most {max_len} characters")
    return out


def require_int(value: Any, name: str, *, min: int | None = None,
                max: int | None = None) -> int:
    """``value`` as an int. Accepts an int or a decimal string (forms and
    query strings send text); refuses bools and floats with a fraction."""
    if isinstance(value, bool):
        raise ArgError(f"{name} must be an integer")
    if isinstance(value, int):
        out = value
    elif isinstance(value, float) and value.is_integer():
        out = int(value)
    elif isinstance(value, str) and value.strip().lstrip("+-").isdigit():
        out = int(value.strip())
    else:
        raise ArgError(f"{name} must be an integer")
    if min is not None and out < min:
        raise ArgError(f"{name} must be at least {min}")
    if max is not None and out > max:
        raise ArgError(f"{name} must be at most {max}")
    return out


def opt_int(value: Any, name: str, default: int, *, min: int | None = None,
            max: int | None = None) -> int:
    """:func:`require_int`, with ``None`` meaning ``default``."""
    if value is None:
        return default
    return require_int(value, name, min=min, max=max)


def require_choice(value: Any, name: str, choices: Iterable[str]) -> str:
    """``value`` if it is one of ``choices``."""
    allowed = list(choices)
    if value not in allowed:
        raise ArgError(f"{name} must be one of {', '.join(map(str, allowed))}")
    return value
