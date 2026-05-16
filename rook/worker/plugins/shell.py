"""shell.* — run shell commands and inspect the environment."""

from __future__ import annotations

import asyncio
import os
import shlex

from ..plugin import Plugin, capability


class ShellPlugin(Plugin):
    NAMESPACE = "shell"

    @capability("exec")
    async def _exec(self, cmd: str, timeout: float = 30.0,
                    cwd: str | None = None) -> dict:
        """Run a shell command. Returns code/stdout/stderr or an error dict.

        `cmd` is passed to ``/bin/sh -c`` so shell features work; quote
        appropriately on the caller's side.
        """
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
            return {"ok": False, "error": "timeout", "timeout": timeout}
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

    @capability("env.get")
    def _env_get(self, name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, default)

    @capability("env.list")
    def _env_list(self, prefix: str = "") -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k.startswith(prefix)}


PLUGIN = ShellPlugin
