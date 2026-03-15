"""Remote execution tools — run commands on connected workers."""

from __future__ import annotations

import json
import logging
from typing import Any

from .base import Tool, ToolDef, ToolResult
from ..remote.bootstrap import CombinedServer as WorkerServer

log = logging.getLogger(__name__)


class RemoteExecTool(Tool):
    def __init__(self, worker_server: WorkerServer):
        self.server = worker_server

    def definition(self) -> ToolDef:
        return ToolDef(
            name="remote_exec",
            description=(
                "Execute a command on a remote machine connected via the worker system. "
                "Specify the worker by name or ID."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "worker": {"type": "string", "description": "Worker name or ID."},
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "timeout": {"type": "number", "description": "Timeout in seconds (default 60).", "default": 60},
                },
                "required": ["worker", "command"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        worker_name = kwargs.get("worker", "")
        command = kwargs.get("command", "")
        timeout = kwargs.get("timeout", 60)

        if not worker_name or not command:
            return ToolResult(success=False, output="", error="worker and command required")

        worker = self.server.get_worker(worker_name)
        if not worker:
            available = ", ".join(w["name"] for w in self.server.list_workers())
            return ToolResult(
                success=False, output="",
                error=f"Worker '{worker_name}' not found. Connected: {available or 'none'}",
            )

        if hasattr(worker, 'alive') and not worker.alive:
            return ToolResult(success=False, output="", error=f"Worker '{worker_name}' is disconnected")
        if hasattr(worker, 'ws') and worker.ws.closed:
            return ToolResult(success=False, output="", error=f"Worker '{worker_name}' is disconnected")

        log.info("Remote exec on '%s': %s", worker_name, command[:100])
        result = await worker.execute(command, timeout=float(timeout))

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        rc = result.get("returncode", -1)

        output = stdout
        if stderr:
            output += f"\nSTDERR: {stderr}"
        output += f"\n(exit code: {rc})"

        return ToolResult(success=(rc == 0), output=output, error=stderr if rc != 0 else None)


class RemoteListTool(Tool):
    def __init__(self, worker_server: WorkerServer):
        self.server = worker_server

    def definition(self) -> ToolDef:
        return ToolDef(
            name="remote_list",
            description="List all connected remote workers.",
            parameters={"type": "object", "properties": {}},
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        workers = self.server.list_workers()
        if not workers:
            return ToolResult(success=True, output="No remote workers connected.")
        return ToolResult(success=True, output=json.dumps(workers, indent=2))


class RemoteUpdateTool(Tool):
    def __init__(self, worker_server: WorkerServer):
        self.server = worker_server

    def definition(self) -> ToolDef:
        return ToolDef(
            name="remote_update",
            description=(
                "Push the latest worker script to a remote worker (or all workers). "
                "The worker overwrites its own script and restarts. "
                "Use worker='all' to update every connected worker."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "worker": {"type": "string", "description": "Worker name, ID, or 'all'."},
                },
                "required": ["worker"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        from pathlib import Path
        target = kwargs.get("worker", "")
        if not target:
            return ToolResult(success=False, output="", error="worker name required")

        # Read the current worker script from disk
        script_path = Path(__file__).resolve().parents[1] / "remote" / "worker.py"
        try:
            new_script = script_path.read_text(encoding="utf-8")
        except Exception as e:
            return ToolResult(success=False, output="", error=f"Failed to read worker.py: {e}")

        if target.lower() == "all":
            # Clean dead connections first
            dead = [wid for wid, w in self.server._workers.items() if w.ws.closed]
            for wid in dead:
                del self.server._workers[wid]
            workers = list(self.server._workers.values())
            if not workers:
                return ToolResult(success=False, output="", error="No workers connected")
            results = []
            for w in workers:
                r = await w.update(new_script)
                results.append(f"{w.name}: {r.get('stdout', r.get('stderr', '?'))}")
            return ToolResult(success=True, output="\n".join(results))

        worker = self.server.get_worker(target)
        if not worker:
            return ToolResult(success=False, output="", error=f"Worker '{target}' not found")

        log.info("Sending update to worker '%s' (%d bytes)", target, len(new_script))
        result = await worker.update(new_script, timeout=15)
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        rc = result.get("returncode", -1)
        log.info("Update result for '%s': rc=%d stdout=%s stderr=%s", target, rc, stdout[:100], stderr[:100])

        # If WS update timed out, fall back to remote_exec with curl
        if rc != 0 and "timed out" in stderr.lower():
            log.info("WS update timed out for '%s', falling back to remote_exec curl", target)
            domain = self.server.domain
            fallback_cmd = (
                f"curl -sL https://{domain}/worker.py -o /tmp/rook_worker_new.py && "
                f"cp /tmp/rook_worker_new.py $(which rook 2>/dev/null || echo ~/.local/share/rook/worker.py) 2>/dev/null; "
                f"cp /tmp/rook_worker_new.py /tmp/rook_worker.py 2>/dev/null; "
                f"echo 'Script updated'"
            )
            result = await worker.execute(fallback_cmd, timeout=30)
            rc = result.get("returncode", -1)
            if rc == 0:
                # Don't restart via remote_exec — it kills the connection mid-call.
                # Instead tell the model to let the user know.
                return ToolResult(
                    success=True,
                    output=f"Worker '{target}' script updated. The worker will pick up changes on next reconnect, or the user can run: systemctl --user restart rook-worker",
                )

        if rc == 0:
            return ToolResult(success=True, output=f"Worker '{target}' updated: {stdout}")
        return ToolResult(success=False, output=stdout, error=stderr)


class RemoteUninstallTool(Tool):
    def __init__(self, worker_server: WorkerServer):
        self.server = worker_server

    def definition(self) -> ToolDef:
        return ToolDef(
            name="remote_uninstall",
            description=(
                "Uninstall rook worker from a remote machine. "
                "Removes the service, CLI command, venv, and all rook files. "
                "The worker will disconnect after uninstall."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "worker": {"type": "string", "description": "Worker name or ID to uninstall."},
                },
                "required": ["worker"],
            },
        )

    async def execute(self, **kwargs: Any) -> ToolResult:
        worker_name = kwargs.get("worker", "")
        if not worker_name:
            return ToolResult(success=False, output="", error="worker name required")

        worker = self.server.get_worker(worker_name)
        if not worker:
            return ToolResult(success=False, output="", error=f"Worker '{worker_name}' not found")

        log.info("Remote uninstall on '%s'", worker_name)
        result = await worker.uninstall()

        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        rc = result.get("returncode", -1)

        if rc == 0:
            return ToolResult(success=True, output=f"Worker '{worker_name}' uninstalled: {stdout}")
        return ToolResult(success=False, output=stdout, error=stderr)
