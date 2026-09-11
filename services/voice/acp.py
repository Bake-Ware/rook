"""Bounded ACP transport. One client per background job; never retries a prompt."""
import asyncio
import contextlib
import json
import os


class ACPClient:
    def __init__(self, host, port, on_event, timeout=600):
        self.host, self.port, self.on_event = host, port, on_event
        self.timeout = timeout
        self.writer = None
        self.reader_task = None
        self.pending = {}
        self.serial = 0
        self.session_id = None

    async def connect(self):
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port, limit=4 * 1024 * 1024), 10)
        self.reader_task = asyncio.create_task(self._read())
        await self.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, 10)
        result = await self.request("session/new", {"cwd": "/root", "mcpServers": []}, 20)
        self.session_id = result["sessionId"]

    async def send(self, obj):
        if self.writer is None or self.writer.is_closing():
            raise ConnectionError("Agent connection closed")
        self.writer.write((json.dumps(obj) + "\n").encode())
        await asyncio.wait_for(self.writer.drain(), 5)

    async def request(self, method, params, timeout=None):
        self.serial += 1
        rid = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, timeout or self.timeout)
        finally:
            self.pending.pop(rid, None)
            if not future.done():
                future.cancel()

    async def _read(self):
        try:
            while line := await self.reader.readline():
                message = json.loads(line)
                if message.get("method") == "session/update":
                    update = message.get("params", {}).get("update", {})
                    self.on_event(update)
                elif "method" in message and "id" in message:
                    # Preserve the deployment's existing unattended-tool policy;
                    # operators can disable it. Never select an arbitrary option.
                    options = message.get("params", {}).get("options", [])
                    allow = next((o.get("optionId") for o in options if o.get("kind") in ("allow_once", "allow_always")), None)
                    outcome = {"outcome": "selected", "optionId": allow} if allow and os.environ.get("ACP_AUTO_APPROVE", "1") == "1" else {"outcome": "cancelled"}
                    await self.send({"jsonrpc": "2.0", "id": message["id"], "result":
                                     {"outcome": outcome}})
                elif message.get("id") in self.pending:
                    future = self.pending[message["id"]]
                    if not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError("Agent returned an RPC error"))
                        else:
                            future.set_result(message.get("result", {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError("Agent disconnected; outcome may be unknown"))

    async def run(self, text):
        await self.connect()
        return await self.request("session/prompt", {"sessionId": self.session_id,
                                  "prompt": [{"type": "text", "text": text}]})

    async def cancel(self):
        if self.session_id:
            with contextlib.suppress(Exception):
                await self.send({"jsonrpc": "2.0", "method": "session/cancel",
                                 "params": {"sessionId": self.session_id}})

    async def close(self):
        if self.writer:
            self.writer.close()
        if self.reader_task:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
