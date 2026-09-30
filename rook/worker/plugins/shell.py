"""shell.* — run shell commands and inspect the environment."""

from __future__ import annotations

import asyncio
import os

from ..plugin import Plugin, capability


class ShellPlugin(Plugin):
    NAMESPACE = "shell"

    @capability("exec")
    async def _exec(self, cmd: str | None = None, argv: list | None = None,
                    stdin: str | None = None, timeout: float = 30.0,
                    cwd: str | None = None, env: dict | None = None) -> dict:
        """Run a command. Returns code/stdout/stderr or an error dict.

        Pass EITHER ``argv`` (a list — exec'd directly, no shell) or ``cmd``
        (a string handed to ``/bin/sh -c``). Prefer ``argv``: it needs no shell
        quoting at all, so arguments containing spaces, quotes, ``$``, or
        newlines just work. Use ``cmd`` only when you actually want shell
        features (pipes, redirection, globbing).

        ``stdin`` is fed to the process on its standard input — the way to pass
        a file body or a script without embedding it in a command string
        (e.g. ``argv=["python3","-"], stdin="print(1)"``). ``env`` overlays the
        worker's environment (it does not replace it).
        """
        if argv and cmd:
            return {"ok": False, "error": "pass argv or cmd, not both"}
        if not argv and not cmd:
            return {"ok": False, "error": "argv or cmd required"}

        child_env = None
        if env:
            child_env = dict(os.environ)
            child_env.update({str(k): str(v) for k, v in env.items()})

        kwargs = dict(stdout=asyncio.subprocess.PIPE,
                      stderr=asyncio.subprocess.PIPE,
                      stdin=asyncio.subprocess.PIPE if stdin is not None else None,
                      cwd=cwd, env=child_env)
        if argv:
            proc = await asyncio.create_subprocess_exec(
                *[str(a) for a in argv], **kwargs)
        else:
            proc = await asyncio.create_subprocess_shell(cmd, **kwargs)

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
        return {
            "ok": proc.returncode == 0,
            "code": proc.returncode,
            "stdout": out.decode(errors="replace"),
            "stderr": err.decode(errors="replace"),
        }

    @capability("which")
    def _which(self, name: str) -> str | None:
        import shutil
        return shutil.which(name)

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

    @capability("env.get")
    def _env_get(self, name: str, default: str | None = None) -> str | None:
        """One environment variable (values pushed as secrets read ``***``)."""
        if name in os.environ and name in self._masked():
            return "***"
        return os.environ.get(name, default)

    @capability("env.list")
    def _env_list(self, prefix: str = "") -> dict[str, str]:
        """The environment (values pushed as secrets or credentials read ``***``)."""
        masked = self._masked()
        return {k: ("***" if k in masked else v) for k, v in os.environ.items()
                if k.startswith(prefix)}


PLUGIN = ShellPlugin
