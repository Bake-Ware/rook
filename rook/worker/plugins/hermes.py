"""hermes.* — drive a locally-installed Hermes Agent (NousResearch/hermes-agent).

Exposes Hermes capabilities as Rook worker capabilities so other agents on
the Rook network can call on this agent's brain, memory, skills, and sessions.

Only activates on machines where Hermes is installed (detected by the
presence of ``~/.hermes/config.yaml`` and the ``hermes`` binary on PATH).
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

    async def _cli(self, args: list[str], stdin: str | None = None,
                   timeout: float = 120.0) -> dict:
        """Invoke the hermes CLI with argv `args`. Returns code/stdout/stderr."""
        exe = shutil.which("hermes")
        if not exe:
            return {"ok": False,
                    "error": "hermes binary not on PATH"}
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

    # ---- interaction -----------------------------------------------------

    @capability("chat")
    async def _chat(self, message: str, session_id: str | None = None,
                    timeout: float = 120.0) -> dict:
        """Send a conversational message to Hermes and return its response.

        Uses ``hermes chat -q`` for one-shot interaction. Optionally resume
        an existing session via `session_id`. `model`/`provider` override
        configured defaults. Pass a larger `timeout` on the band call if
        the LLM round-trip is expected to be slow.

        Returns {ok, answer, code, stdout, stderr}.
        """
        args = ["chat", "-q"] + [message]
        if session_id:
            args += ["-r", session_id]
        res = await self._cli(args, timeout=timeout)
        if res.get("ok"):
            res["answer"] = res.get("stdout", "").strip()
        return res

    @capability("run")
    async def _run(self, prompt: str, model: str | None = None,
                   provider: str | None = None, timeout: float = 120.0) -> dict:
        """Run a one-shot prompt through Hermes (clean, final-answer-only).

        Uses ``hermes -z``. `model`/`provider` override defaults for this
        call. Pass a larger `timeout` if the round-trip is expected to be
        slow.

        Returns {ok, answer, code, stdout, stderr}.
        """
        args = ["-z", prompt]
        if model:
            args += ["--model", model]
        if provider:
            args += ["--provider", provider]
        res = await self._cli(args, timeout=timeout)
        if res.get("ok"):
            res["answer"] = res.get("stdout", "").strip()
        return res

    # ---- memory ----------------------------------------------------------

    @capability("memory.read")
    async def _memory_read(self, entry: str = "MEMORY.md") -> dict:
        """Read a built-in memory file (MEMORY.md or USER.md).

        Returns {ok, content, path} or {ok: False, error}.
        """
        path_map = {
            "MEMORY.md": os.path.expanduser("~/.hermes/MEMORY.md"),
            "USER.md": os.path.expanduser("~/.hermes/USER.md"),
        }
        path = path_map.get(entry)
        if not path:
            return {"ok": False,
                    "error": f"Unknown entry: {entry}. Use MEMORY.md or USER.md"}
        if not os.path.isfile(path):
            return {"ok": False, "error": f"{entry} not found on this machine"}
        try:
            content = open(path).read()
            return {"ok": True, "content": content, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @capability("memory.status")
    async def _memory_status(self) -> dict:
        """Report persistent-memory provider status."""
        return await self._cli(["memory", "status"], timeout=30.0)

    # ---- skills ----------------------------------------------------------

    @capability("skills.list")
    async def _skills_list(self) -> dict:
        """List installed skills. Returns {ok, skills} with structured entries.

        ``hermes skills list`` renders a Rich box-drawing table; parse its
        data rows (cells split on the ``│`` column separator) into
        {name, category, source, trust, status} dicts.
        """
        res = await self._cli(["skills", "list"], timeout=30.0)
        if res.get("ok"):
            skills = []
            for line in res["stdout"].split("\n"):
                if "│" not in line:  # not a table data row (header uses ┃)
                    continue
                cells = [c.strip() for c in line.split("│")]
                # drop only the outer border cells, keep interior blanks
                # (some skills have an empty category column)
                if cells and cells[0] == "":
                    cells = cells[1:]
                if cells and cells[-1] == "":
                    cells = cells[:-1]
                if len(cells) < 5 or cells[0] in ("", "Name"):  # skip header
                    continue
                skills.append({
                    "name": cells[0],
                    "category": cells[1],
                    "source": cells[2],
                    "trust": cells[3],
                    "status": cells[4],
                })
            res["skills"] = skills
        return res

    @capability("skills.search")
    async def _skills_search(self, query: str) -> dict:
        """Search skill registries."""
        return await self._cli(["skills", "search", query], timeout=60.0)

    # ---- sessions --------------------------------------------------------

    @capability("sessions.list")
    async def _sessions_list(self) -> dict:
        """List recent conversation sessions."""
        return await self._cli(["sessions", "list"], timeout=30.0)

    @capability("sessions.read")
    async def _sessions_read(self, session_id: str = "",
                             limit: int = 50) -> dict:
        """Read a session transcript.

        If `session_id` is empty, reads the most recent session. Returns
        {ok, content, total_lines}.
        """
        args = ["sessions", "export"]
        if session_id:
            args += [f"-r={session_id}"]
        res = await self._cli(args, timeout=30.0)
        if res.get("ok"):
            lines = [l for l in res["stdout"].strip().split("\n") if l.strip()]
            res["content"] = "\n".join(lines[-limit:])
            res["total_lines"] = len(lines)
        return res

    # ---- system ----------------------------------------------------------

    @capability("status")
    async def _status(self) -> dict:
        """Report Hermes install status: config, binary, version."""
        ver = await self._cli(["version"], timeout=20.0)
        return {
            "installed": True,
            "config": _config_path(),
            "binary": shutil.which("hermes"),
            "version": ver.get("stdout", "").strip() if ver.get("ok") else None,
            "error": None if ver.get("ok") else (ver.get("stderr") or ver.get("error")),
        }

    @capability("mcp.list")
    async def _mcp_list(self) -> dict:
        """List configured MCP servers."""
        return await self._cli(["mcp", "list"], timeout=30.0)

    # ---- registration ----------------------------------------------------

    async def start(self) -> None:
        """Log that this agent is ready on the Rook network."""
        if _config_path() and shutil.which("hermes"):
            print(f"[hermes] plugin loaded — config={_config_path()} "
                  f"binary={shutil.which('hermes')}")
        else:
            print("[hermes] plugin skipped — Hermes not installed on this machine")


# Activate only where Hermes is installed; otherwise opt out entirely.
PLUGIN = HermesPlugin() if _config_path() and shutil.which("hermes") else None
