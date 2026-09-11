"""Minimal async client for the rook band MCP endpoint (streamable-HTTP transport).

The endpoint needs the MCP handshake before any tools/call:
    initialize -> grab the `mcp-session-id` response header -> notifications/initialized
Responses come back as SSE frames (`data: {...}`), not plain JSON.
"""
import json
import os
import httpx
ROOK_MCP_URL = os.environ.get('ROOK_MCP_URL', 'https://mcp.bakeforge.com/mcp')
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

    def __init__(self, url=ROOK_MCP_URL, token=ROOK_MCP_TOKEN, timeout=30.0):
        self.url, self.token, self.timeout = (url, token, timeout)
        self._sid = None
        self._n = 0

    def _headers(self):
        h = dict(_HDR)
        if self.token:
            h['Authorization'] = f'Bearer {self.token}'
        if self._sid:
            h['mcp-session-id'] = self._sid
        return h

    async def _post(self, client, method, params=None, notify=False):
        self._n += 1
        body = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            body['params'] = params
        if not notify:
            body['id'] = self._n
        r = await client.post(self.url, headers=self._headers(), json=body)
        r.raise_for_status()
        if not self._sid and r.headers.get('mcp-session-id'):
            self._sid = r.headers['mcp-session-id']
        return None if notify else _parse(r)

    async def call(self, tool, args):
        """One-shot: handshake + tools/call. Returns the tool's text payload."""
        self._sid = None
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            await self._post(client, 'initialize', {'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'rook-voice-agent', 'version': '1'}})
            await self._post(client, 'notifications/initialized', {}, notify=True)
            out = await self._post(client, 'tools/call', {'name': tool, 'arguments': args})
        if 'error' in out:
            raise RuntimeError(out['error'].get('message', 'mcp error'))
        content = out.get('result', {}).get('content', [])
        return ''.join((c.get('text', '') for c in content if c.get('type') == 'text'))
