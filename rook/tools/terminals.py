"""Persistent terminal sessions — named shells the model can manage over time."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Any

from .base import Tool, ToolDef, ToolResult

log = logging.getLogger(__name__)

MAX_BUFFER = 8000  # chars to keep in output buffer per terminal


class Terminal:
    """A persistent shell session with output capture."""

    def __init__(self, name: str, shell: str = "bash"):
        self.name = name
        self.shell = shell
        self.process: asyncio.subprocess.Process | None = None
        self.output: deque[str] = deque()
        self.output_size: int = 0
        self.created_at: float = time.time()
        self.last_command: str = ""
        self.last_activity: float = time.time()
        self._reader_task: asyncio.Task | None = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_shell(
            self.shell,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 64,
        )
        self._reader_task = asyncio.create_task(self._read_output())
        log.info("Terminal '%s' started (pid=%s)", self.name, self.process.pid)

    async def _read_output(self) -> None:
        """Continuously read output from the process."""
        if not self.process or not self.process.stdout:
            return
        try:
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                self.output.append(text)
                self.output_size += len(text)
                # Trim buffer if too large
                while self.output_size > MAX_BUFFER and self.output:
                    removed = self.output.popleft()
                    self.output_size -= len(removed)
        except Exception as e:
            log.debug("Terminal '%s' reader stopped: %s", self.name, e)

    async def send(self, command: str) -> None:
        """Send a command to the terminal."""
        if not self.process or not self.process.stdin:
            raise RuntimeError(f"Terminal '{self.name}' is not running")
        self.last_command = command
        self.last_activity = time.time()
        self.process.stdin.write((command + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    def read(self, last_n: int | None = None) -> str:
        """Read output buffer. Optionally only last N chars."""
        text = "".join(self.output)
        if last_n and len(text) > last_n:
            text = "..." + text[-last_n:]
        return text

    def clear(self) -> None:
        """Clear the output buffer."""
        self.output.clear()
        self.output_size = 0

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def kill(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self.process:
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
        log.info("Terminal '%s' killed", self.name)


class TerminalPool:
    """Manages named persistent terminal sessions."""

    def __init__(self):
        self._terminals: dict[str, Terminal] = {}

    async def create(self, name: str, shell: str = "bash") -> Terminal:
        if name in self._terminals and self._terminals[name].alive:
            raise ValueError(f"Terminal '{name}' already exists")
        t = Terminal(name, shell)
        await t.start()
        self._terminals[name] = t
        return t

    def get(self, name: str) -> Terminal | None:
        return self._terminals.get(name)

    async def kill(self, name: str) -> bool:
        t = self._terminals.get(name)
        if t:
            await t.kill()
            del self._terminals[name]
            return True
        return False

    def list_terminals(self) -> list[dict[str, Any]]:
        results = []
        for name, t in self._terminals.items():
            results.append({
                "name": name,
                "alive": t.alive,
                "pid": t.process.pid if t.process else None,
                "last_command": t.last_command[:80],
                "buffer_size": t.output_size,
                "age": f"{time.time() - t.created_at:.0f}s",
            })
        return results

    async def cleanup_dead(self) -> int:
        dead = [n for n, t in self._terminals.items() if not t.alive]
        for n in dead:
            del self._terminals[n]
        return len(dead)


# -- Tools --

class TerminalCreateTool(Tool):
    def __init__(self, pool: TerminalPool):
        self.pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="terminal_create",
            description=(
                "Create a named persistent terminal session. "
                "The terminal stays open across conversation turns — use it for long-running processes, "
                "interactive workflows, or anything that needs state between commands."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for this terminal (e.g., 'build', 'server', 'monitor')."},
                    "shell": {"type": "string", "description": "Shell to use. Default: bash.", "default": "bash"},
                },
                "required": ["name"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        shell = kwargs.get("shell", "bash")
        if not name:
            return ToolResult(success=False, output="", error="name required")
        try:
            t = await self.pool.create(name, shell)
            return ToolResult(success=True, output=f"Terminal '{name}' created (pid={t.process.pid})")
        except ValueError as e:
            return ToolResult(success=False, output="", error=str(e))


class TerminalSendTool(Tool):
    def __init__(self, pool: TerminalPool):
        self.pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="terminal_send",
            description=(
                "Send a command to a named terminal. The command runs in the persistent shell session. "
                "Output is captured in the terminal's buffer — use terminal_read to see it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Terminal name."},
                    "command": {"type": "string", "description": "Command to execute."},
                    "wait": {"type": "number", "description": "Seconds to wait for output before returning (default 2).", "default": 2},
                },
                "required": ["name", "command"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        command = kwargs.get("command", "")
        wait = kwargs.get("wait", 2)

        t = self.pool.get(name)
        if not t:
            return ToolResult(success=False, output="", error=f"Terminal '{name}' not found")
        if not t.alive:
            return ToolResult(success=False, output="", error=f"Terminal '{name}' is dead")

        t.clear()  # clear buffer before sending so we capture just this command's output
        try:
            await t.send(command)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e))

        # Wait a bit for output
        await asyncio.sleep(min(float(wait), 30))

        output = t.read(last_n=4000)
        return ToolResult(success=True, output=output if output else "(no output yet)")


class TerminalReadTool(Tool):
    def __init__(self, pool: TerminalPool):
        self.pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="terminal_read",
            description="Read the output buffer of a named terminal. Shows recent output.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Terminal name."},
                    "last_n": {"type": "integer", "description": "Only return last N chars (default: all buffer).", "default": 4000},
                },
                "required": ["name"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        last_n = kwargs.get("last_n", 4000)

        t = self.pool.get(name)
        if not t:
            return ToolResult(success=False, output="", error=f"Terminal '{name}' not found")

        output = t.read(last_n=int(last_n))
        alive = "running" if t.alive else "dead"
        return ToolResult(success=True, output=f"[{alive}] {output}" if output else f"[{alive}] (empty buffer)")


class TerminalListTool(Tool):
    def __init__(self, pool: TerminalPool):
        self.pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="terminal_list",
            description="List all active terminal sessions.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        terminals = self.pool.list_terminals()
        if not terminals:
            return ToolResult(success=True, output="No active terminals.")
        return ToolResult(success=True, output=json.dumps(terminals, indent=2))


class TerminalKillTool(Tool):
    def __init__(self, pool: TerminalPool):
        self.pool = pool

    def definition(self) -> ToolDef:
        return ToolDef(
            name="terminal_kill",
            description="Kill a named terminal session.",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Terminal name to kill."},
                },
                "required": ["name"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        name = kwargs.get("name", "")
        if await self.pool.kill(name):
            return ToolResult(success=True, output=f"Terminal '{name}' killed.")
        return ToolResult(success=False, output="", error=f"Terminal '{name}' not found.")
