"""hermes.* — drive a locally-installed Hermes Agent (NousResearch/hermes-agent).

The plugin only activates on machines where Hermes is installed, detected by the
presence of ``~/.hermes/config.yaml``. On every other machine the module exports
``PLUGIN = None`` and is skipped by the loader, so ``hermes.*`` capabilities never
appear on the band there.

Everything is driven through the ``hermes`` CLI (no Python/YAML deps pulled into
the worker venv):
  - ``hermes -z "<prompt>"``  one-shot, final answer only (headless/pipe-friendly)
  - ``hermes version``        version string
  - ``hermes mcp list``       configured MCP servers
  - ``hermes memory status``  persistent-memory provider status
  - ``hermes sessions list``  recent conversation sessions
"""

from __future__ import annotations

import asyncio
import os
import shutil

from ..plugin import Plugin, capability


def _config_path() -> str | None:
    """Return the Hermes config path if Hermes is installed here, else None."""
    p = os.path.expanduser("~/.hermes/config.yaml")
    return p if os.path.isfile(p) else None


class HermesPlugin(Plugin):
    NAMESPACE = "hermes"

    async def _hermes(self, args: list[str], stdin: str | None = None,
                      timeout: float = 120.0) -> dict:
        """Invoke the hermes CLI with argv `args`. Returns code/stdout/stderr."""
        exe = shutil.which("hermes")
        if not exe:
            return {"ok": False,
                    "error": "hermes config present but `hermes` binary not on PATH"}
        proc = await asyncio.create_subprocess_exec(
            exe, *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode() if stdin is not None else None),
                timeout)
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

    # ---- run a prompt --------------------------------------------------

    @capability("run")
    async def _run(self, prompt: str, model: str | None = None,
                   provider: str | None = None, stdin: str | None = None,
                   timeout: float = 120.0) -> dict:
        """Run a one-shot prompt through Hermes and return its final answer.

        Uses ``hermes -z`` (clean, final-answer-only output). `stdin` is piped
        to Hermes (e.g. a file's contents to summarize). `model`/`provider`
        override the configured defaults for this single call. Note: pass a
        `timeout` on the band call larger than the LLM round-trip.
        """
        args = ["-z", prompt]
        if model:
            args += ["--model", model]
        if provider:
            args += ["--provider", provider]
        res = await self._hermes(args, stdin=stdin, timeout=timeout)
        if res.get("ok"):
            res["answer"] = res.get("stdout", "").strip()
        return res

    # ---- introspection -------------------------------------------------

    @capability("status")
    async def _status(self) -> dict:
        """Report Hermes install status: config path, binary, version."""
        ver = await self._hermes(["version"], timeout=20.0)
        return {
            "installed": True,
            "config": _config_path(),
            "binary": shutil.which("hermes"),
            "version": ver.get("stdout", "").strip() if ver.get("ok") else None,
            "error": None if ver.get("ok") else (ver.get("stderr") or ver.get("error")),
        }

    @capability("mcp")
    async def _mcp(self) -> dict:
        """List the MCP servers Hermes is configured with (`hermes mcp list`)."""
        return await self._hermes(["mcp", "list"], timeout=30.0)

    @capability("memory")
    async def _memory(self) -> dict:
        """Report Hermes persistent-memory provider status (`hermes memory status`)."""
        return await self._hermes(["memory", "status"], timeout=30.0)

    @capability("sessions")
    async def _sessions(self) -> dict:
        """List recent Hermes conversation sessions (`hermes sessions list`)."""
        return await self._hermes(["sessions", "list"], timeout=30.0)


# Activate only where Hermes is installed; otherwise opt out of loading entirely.
PLUGIN = HermesPlugin() if _config_path() else None
