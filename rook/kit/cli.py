"""A program signed in on one worker, run from a hub script: what llm-cli and
the agent engines (agents-claude-code, agents-codex, agents-opencode) share,
and no more. The plugin names the worker in its ``worker`` setting and lists
the caps it reaches (``shell.exec``, or ``shell.open`` with ``stream.read``,
``stream.write`` and ``stream.close``; ``shell.env``; ``file.read``,
``file.write``) in its ``uses``.

    ctx = itself(ctx)                                # its calls to the worker as the plugin itself
    w = worker(ctx)                                  # the setting, or a plain error
    home = await Place.of(ctx, w)                    # its separator and home folder
    work = await home.folder("work")                 # ~/.rook-agents/<plugin>/work
    code, tail = await stream(ctx, w, argv, stdin=prompt, cwd=work, timeout="20m",
                              name="Claude Code", setting="claude", line=seen)
"""
from __future__ import annotations

import time
from typing import Callable

from ._types import parse_duration
from .ctx import Caller, KitError
from .net import Stream

TAIL = 2000               # output kept for an error message


class itself:
    """``ctx`` whose ``call_on`` runs as the plugin itself (``system:plugin.<name>``,
    its ``uses`` its grants), not as the caller: running the program is the
    plugin's work, which an agent's own grants needn't name. What the
    program does in Rook it does with its own token (an agent turn's door)."""

    def __init__(self, ctx):
        self._ctx = ctx
        self._who = Caller(plugin=ctx.plugin, band=ctx.caller.band, why=ctx.caller.why)

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def call_on(self, target: str, cap: str, /, **args):
        return self._ctx.call_as(self._who, cap, args, target=target)


def worker(ctx) -> str:
    """The worker the program runs on: the plugin's ``worker`` setting."""
    if not ctx.settings["worker"]:
        raise KitError(f"set {ctx.plugin}.worker to the worker where the program is installed and signed in")
    return ctx.settings["worker"]


class Place:
    """A worker's path conventions and home folder."""

    def __init__(self, ctx, wid: str, windows: bool, home: str):
        self.ctx, self.wid, self.windows, self.home = ctx, wid, windows, home
        self.sep = "\\" if windows else "/"

    @classmethod
    async def of(cls, ctx, wid: str) -> "Place":
        windows = await ctx.call_on(wid, "shell.env", name="OS") == "Windows_NT"
        home = await ctx.call_on(wid, "shell.env", name="USERPROFILE" if windows else "HOME", default="")
        return cls(ctx, wid, windows, str(home or ""))

    def join(self, *parts: str) -> str:
        return self.sep.join(p.rstrip("/\\") for p in parts)

    def expand(self, path: str) -> str:
        """A leading ``~`` as the home folder (a program or folder setting)."""
        if path == "~" or path.startswith(("~/", "~\\")):
            return self.home + path[1:]
        return path

    async def env(self, name: str, default: str = "") -> str:
        return str(await self.ctx.call_on(self.wid, "shell.env", name=name, default=default) or default)

    async def folder(self, *parts: str) -> str:
        """The plugin's own folder on the worker, ``~/.rook-agents/<plugin>``
        (or a folder in it), made if missing: what it keeps there from one
        call to the next."""
        if not self.home:
            raise KitError(f"{self.wid} has no home folder (HOME or USERPROFILE) to keep files in")
        return await self.mkdir(self.join(self.home, ".rook-agents", self.ctx.plugin, *parts))

    async def mkdir(self, path: str) -> str:
        """``path`` exists afterwards (a ``.rook`` marker file in it)."""
        await self.ctx.call_on(self.wid, "file.write", path=self.join(path, ".rook"), content="",
                               create_parents=True)
        return path


def plain(e: KitError, program: str, setting: str, took: str, name: str | None = None) -> KitError:
    """The worker's "not found" and "timed out" as what to do about them."""
    if e.message == f"not found: {program}":
        return KitError(f"{program} not found: install {name or 'it'} or set {setting}")
    if e.message.startswith("timed out after"):
        return KitError(f"{name or program} took longer than {took}", retry=True)
    return e


async def stream(ctx, wid: str, argv: list, *, stdin: str, timeout: str, name: str, setting: str,
                 cwd: str | None = None, env: dict | None = None,
                 line: Callable[[str], bool | None] | None = None) -> tuple[int | None, str]:
    """``argv`` on ``wid`` as a process (shell.open on pipes, stdout and
    stderr as one), ``stdin`` its whole input. Each output line goes to
    ``line`` as it arrives; ``line`` returning True ends the process. The
    process ends too after ``timeout`` (a duration) or when this call is
    cancelled. Returns (its exit code, None when ended here; the output's
    last part)."""
    args = {"argv": argv, "stdin": stdin, "pty": False, **({"cwd": cwd} if cwd else {}),
            **({"env": env} if env else {})}
    try:
        s = await Stream.open(ctx, wid, "shell.open", **args)
    except KitError as e:
        raise plain(e, argv[0], setting, "", name) from None
    deadline, tail, rest = time.monotonic() + parse_duration(timeout), "", b""
    try:
        while not s.closed:
            left = deadline - time.monotonic()
            if left <= 0:
                raise KitError(f"{name} took longer than {timeout}", retry=True)
            await s._more(left)
            data, s.buf = rest + bytes(s.buf), bytearray()
            s.datagrams.clear()           # kept for UDP; a process's output is read here once
            *done, rest = data.split(b"\n")
            if s.closed and rest:
                done, rest = [*done, rest], b""
            for raw in done:
                text = raw.decode("utf-8", "replace").rstrip("\r")
                tail = (tail + "\n" + text)[-TAIL:]
                if line is not None and line(text):
                    return None, tail
        return s.exit, tail
    finally:
        if not s.closed:          # ended here: timed out, stopped by ``line`` or cancelled
            try:
                await ctx.call_on(wid, "stream.write", stream=s.id, body={"signal": "KILL"})
            except KitError:
                pass
            await s.close()
