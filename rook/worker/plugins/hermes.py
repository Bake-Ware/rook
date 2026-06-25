"""hermes.* — drive a locally-installed Hermes Agent (NousResearch/hermes-agent).

The plugin only activates on machines where Hermes is installed, detected by the
presence of ``~/.hermes/config.yaml``. On every other machine the module exports
``PLUGIN = None`` and is skipped by the loader, so ``hermes.*`` capabilities never
appear on the band there.

Everything is driven through the ``hermes`` CLI (no Python/YAML deps pulled into
the worker venv):
  - ``hermes -z "<prompt>"``        one-shot, final answer only
  - ``hermes chat -q "<prompt>"``   conversational one-shot
  - ``hermes sessions list/export`` session management
  - ``hermes skills list/search``   skill discovery
  - ``hermes memory status``        persistent-memory provider status
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

    # ---- conversational chat -------------------------------------------

    @capability("chat")
    async def _chat(self, message: str, model: str | None = None,
                    provider: str | None = None, session_id: str | None = None,
                    timeout: float = 120.0) -> dict:
        """Send a conversational message to Hermes Agent and return its response.

        Uses ``hermes chat -q`` for one-shot interaction. Optionally resume an
        existing session via `session_id`. `model`/`provider` override the configured
        defaults for this single call. Note: pass a `timeout` on the band call larger
        than the LLM round-trip.

        Returns {ok, answer, code, stdout, stderr}.
        """
        args = ["chat", "-q"] + [message]
        if model:
            args += ["--model", model]
        if provider:
            args += ["--provider", provider]
        if session_id:
            args += ["-r", session_id]
        res = await self._hermes(args, timeout=timeout)
        if res.get("ok"):
            res["answer"] = res.get("stdout", "").strip()
        return res

    # ---- one-shot run (legacy alias) -----------------------------------

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

    # ---- memory read (built-in entries) --------------------------------

    @capability("memory.read")
    async def _memory_read(self, entry: str = "MEMORY.md") -> dict:
        """Read a built-in Hermes memory file (MEMORY.md or USER.md).

        Returns {ok, content} with the file contents.
        """
        path_map = {
            "MEMORY.md": os.path.expanduser("~/.hermes/MEMORY.md"),
            "USER.md": os.path.expanduser("~/.hermes/USER.md"),
        }
        path = path_map.get(entry)
        if not path:
            return {"ok": False, "error": f"Unknown memory entry: {entry}. Use MEMORY.md or USER.md"}
        if not os.path.isfile(path):
            return {"ok": False, "error": f"{entry} does not exist on this machine"}
        try:
            content = open(path).read()
            return {"ok": True, "content": content, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- skills --------------------------------------------------------

    @capability("skills.list")
    async def _skills_list(self) -> dict:
        """List installed Hermes skills (`hermes skills list`)."""
        res = await self._hermes(["skills", "list"], timeout=30.0)
        if res.get("ok"):
            # Parse the output into a structured format
            lines = [l for l in res["stdout"].strip().split("\n") if l.strip()]
            skills = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 2 and not line.startswith("==="):
                    name = parts[0]
                    desc = " ".join(parts[1:])
                    skills.append({"name": name, "description": desc})
            res["skills"] = skills
        return res

    @capability("skills.search")
    async def _skills_search(self, query: str) -> dict:
        """Search Hermes skill registries (`hermes skills search <query>`)."""
        return await self._hermes(["skills", "search", query], timeout=60.0)

    @capability("skills.load")
    async def _skills_load(self, name: str) -> dict:
        """Load a skill into the current Hermes session (`hermes -s <name>`).

        This is a fire-and-forget operation — it loads the skill for future
        one-shot calls. Returns {ok, loaded_skill}.
        """
        res = await self._hermes(["-z", f"Load skill: {name}", "--skills", name], timeout=30.0)
        if res.get("ok"):
            res["loaded_skill"] = name
        return res

    # ---- sessions ------------------------------------------------------

    @capability("sessions")
    async def _sessions(self) -> dict:
        """List recent Hermes conversation sessions (`hermes sessions list`)."""
        return await self._hermes(["sessions", "list"], timeout=30.0)

    @capability("sessions.read")
    async def _sessions_read(self, session_id: str = "", limit: int = 50) -> dict:
        """Read a specific Hermes conversation session's transcript.

        If `session_id` is empty, reads the most recent session. Returns {ok, content}.
        """
        args = ["sessions", "export"]
        if session_id:
            args += [f"-r={session_id}"]
        res = await self._hermes(args, timeout=30.0)
        if res.get("ok"):
            # The export is JSONL — take the last N lines as context
            lines = [l for l in res["stdout"].strip().split("\n") if l.strip()]
            content = "\n".join(lines[-limit:])
            return {"ok": True, "content": content, "total_lines": len(lines)}
        return res

    @capability("sessions.export")
    async def _sessions_export(self, session_id: str | None = None) -> dict:
        """Export a session to JSONL format.

        If `session_id` is provided, exports that specific session. Otherwise exports all recent sessions.
        Returns {ok, content} with the JSONL transcript.
        """
        args = ["sessions", "export"]
        if session_id:
            args += [f"-r={session_id}"]
        return await self._hermes(args, timeout=30.0)

    @capability("sessions.list")
    async def _sessions_list(self) -> dict:
        """List recent Hermes conversation sessions (alias for `sessions`)."""
        return await self._hermes(["sessions", "list"], timeout=30.0)


# Activate only where Hermes is installed; otherwise opt out of loading entirely.
PLUGIN = HermesPlugin() if _config_path() else None
