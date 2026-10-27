"""shell.* — run shell commands and inspect the environment."""

from __future__ import annotations

import asyncio
import os

from ...core.pluginkit import ArgError, require_str, which
from .. import shells
from ..plugin import Plugin, capability


class ShellPlugin(Plugin):
    NAMESPACE = "shell"

    @capability("exec", risk="exec", scope="local")
    async def _exec(self, cmd: str | None = None, argv: list | None = None,
                    stdin: str | None = None, timeout: float = 30.0,
                    cwd: str | None = None, env: dict | None = None,
                    shell: str | None = None) -> dict:
        """Run a command. Returns code/stdout/stderr or an error dict.

        Pass EITHER ``argv`` (a list — exec'd directly, no shell) or ``cmd``
        (a string for a shell: ``/bin/sh -c``, PowerShell on Windows). Prefer
        ``argv``: it needs no shell quoting at all, so arguments containing
        spaces, quotes, ``$``, or newlines just work. Use ``cmd`` only when
        you actually want shell features (pipes, redirection, globbing).
        ``shell`` picks another one for ``cmd``: sh, bash, powershell or cmd
        (cmd.exe); a reply from any shell but sh names it.

        On Windows the output comes back as UTF-8 and PowerShell's exit code
        is the last command's, as with sh; in ``argv``, ``python3``, ``npm``
        and other PATHEXT names resolve as a shell would resolve them.

        ``stdin`` is fed to the process on its standard input — the way to pass
        a file body or a script without embedding it in a command string
        (e.g. ``argv=["python3","-"], stdin="print(1)"``). ``env`` overlays the
        worker's environment (it does not replace it).
        """
        if argv and cmd:
            raise ArgError("pass argv or cmd, not both")
        if not argv and not cmd:
            raise ArgError("argv or cmd required")

        child_env = None
        if env:
            child_env = dict(os.environ)
            child_env.update({str(k): str(v) for k, v in env.items()})

        ran = None
        if cmd:
            try:
                shell_argv, ran = shells.shell_argv(cmd, shell)
            except ValueError as e:
                raise ArgError(str(e)) from None
        elif shell:
            raise ArgError("shell applies to cmd; argv runs with no shell")

        kwargs = dict(stdout=asyncio.subprocess.PIPE,
                      stderr=asyncio.subprocess.PIPE,
                      stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                      cwd=cwd, env=child_env, **shells.popen_flags())
        if argv:
            proc = await asyncio.create_subprocess_exec(
                *shells.resolve_argv(argv), **kwargs)
        elif ran == "cmd":
            # cmd.exe doesn't take MSVCRT quoting: hand it the line as is.
            proc = await asyncio.create_subprocess_shell(cmd, **kwargs)
        else:
            proc = await asyncio.create_subprocess_exec(*shell_argv, **kwargs)

        payload = stdin.encode() if stdin is not None else None
        try:
            out, err = await asyncio.wait_for(proc.communicate(payload), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            # The command is dead, but anything it already did still happened —
            # say so rather than implying nothing ran.
            return {"ok": False, "error": "timeout", "timeout": timeout,
                    "note": "killed after timeout; partial side effects may "
                            "have landed. For long jobs use proc.start."}
        reply = {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": shells.decode(out),
            "stderr": shells.decode(err),
        }
        if ran and ran != "sh":     # say so whenever it isn't the classic sh -c
            reply["shell"] = ran
        return reply

    @capability("which", risk="read", scope="local")
    def _which(self, name: str) -> str | None:
        """Full path of an executable on this worker's PATH, or null if it is not found."""
        return which(require_str(name, "name"))

    def _masked(self) -> set:
        """Env keys set by a pushed config (other than declared non-secret
        settings) or fetched from the hub vault: shown as ``***``."""
        from .. import wconfig
        public = set()
        for p in getattr(getattr(self, "_worker", None), "plugins", None) or []:
            for s in getattr(p, "SETTINGS", ()):
                if not s.secret:
                    public.update(s.env_names())
        return wconfig.masked_env_keys(public)

    def bind_worker(self, worker) -> None:
        self._worker = worker

    @capability("env.get", risk="read", tags=("sensitive",), scope="local")
    def _env_get(self, name: str, default: str | None = None) -> str | None:
        """One environment variable (values pushed as secrets read ``***``)."""
        if name in os.environ and name in self._masked():
            return "***"
        return os.environ.get(name, default)

    @capability("env.list", risk="read", tags=("sensitive",), scope="local")
    def _env_list(self, prefix: str = "") -> dict[str, str]:
        """The environment (values pushed as secrets or credentials read ``***``)."""
        masked = self._masked()
        return {k: ("***" if k in masked else v) for k, v in os.environ.items()
                if k.startswith(prefix)}


PLUGIN = ShellPlugin
