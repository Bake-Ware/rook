"""Minimal async client for the rook band MCP endpoint (streamable-HTTP transport).

The endpoint needs the MCP handshake before any tools/call:
    initialize -> grab the `mcp-session-id` response header -> notifications/initialized
Responses come back as SSE frames (`data: {...}`), not plain JSON.
"""
import asyncio
import json
import os
import httpx
ROOK_MCP_URL = os.environ.get('ROOK_MCP_URL', 'http://127.0.0.1:8765/mcp')
ROOK_MCP_TOKEN = os.environ.get('ROOK_MCP_TOKEN', '')
_HDR = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}

def _parse(resp):
    """Accept either a plain JSON body or an SSE stream carrying one JSON frame."""
    txt = resp.text
    if txt.lstrip().startswith('{'):
        return resp.json()
    for line in txt.splitlines():
        if line.startswith('data:'):
            return json.loads(line[5:].strip())
    raise ValueError(f'unparseable MCP response: {txt[:200]}')

class RookMCP:
    """One MCP session per process, shared by every RookMCP() instance.

    The hub caps concurrent sessions; opening a fresh one per call and never
    closing it (the old behaviour) filled that table and locked other clients
    out. The session is created on first use and reused; if the hub has dropped
    it (restart, idle expiry or eviction: 404) it is re-established once and the
    call retried.
    """

    _sid = None
    _lock = None
    _lock_loop = None
    _n = 0

    def __init__(self, url=ROOK_MCP_URL, token=ROOK_MCP_TOKEN, timeout=30.0):
        self.url, self.token, self.timeout = (url, token, timeout)

    def _headers(self, sid=None):
        h = dict(_HDR)
        if self.token:
            h['Authorization'] = f'Bearer {self.token}'
        if sid:
            h['mcp-session-id'] = sid
        return h

    @classmethod
    def _next_id(cls):
        cls._n += 1
        return cls._n

    async def _post(self, client, method, params=None, notify=False, sid=None):
        body = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            body['params'] = params
        if not notify:
            body['id'] = self._next_id()
        r = await client.post(self.url, headers=self._headers(sid), json=body)
        r.raise_for_status()
        return r, (None if notify else _parse(r))

    async def _session(self, client):
        cls = type(self)
        loop = asyncio.get_running_loop()
        if cls._lock is None or cls._lock_loop is not loop:
            cls._lock, cls._lock_loop = asyncio.Lock(), loop
        async with cls._lock:
            if cls._sid is None:
                r, _ = await self._post(client, 'initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'rook-voice-agent', 'version': '1'}})
                sid = r.headers.get('mcp-session-id')
                if not sid:
                    raise RuntimeError('MCP initialize returned no session id')
                await self._post(client, 'notifications/initialized', {}, notify=True, sid=sid)
                cls._sid = sid
            return cls._sid

    async def call(self, tool, args):
        """tools/call on the shared session. Returns the tool's text payload."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in (1, 2):
                sid = await self._session(client)
                try:
                    _, out = await self._post(client, 'tools/call', {'name': tool, 'arguments': args}, sid=sid)
                    break
                except httpx.HTTPStatusError as e:
                    # 404: the hub no longer knows this session. Start a new one, once.
                    if e.response.status_code in (400, 404) and attempt == 1:
                        if type(self)._sid == sid:
                            type(self)._sid = None
                        continue
                    raise
        if 'error' in out:
            raise RuntimeError(out['error'].get('message', 'mcp error'))
        content = out.get('result', {}).get('content', [])
        return ''.join((c.get('text', '') for c in content if c.get('type') == 'text'))
