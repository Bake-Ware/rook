"""The Jobs page (``/account/jobs``) and the ``rook band`` jobs panel's API.

``/account/jobs/api`` is the same session/CSRF proxy as Knowledge and Vault,
to the MCP bridge's ``/jobs/account-api`` (rook/hub/plugins/jobs/web.py). Any
signed-in account may open it; the jobs caps decide what each may change.

``POST /api/band/jobs`` serves the terminal panel. It sits behind the
dashboard's own login (Basic auth or the dashboard session, like
``/api/band/call``), because job writes are not callable over the band. The
dashboard forwards the admitted caller's principal to the bridge with the
bridge's internal token (``mask.token``, the one the dashboard already uses
for secret masking).
"""
import json
import os

import aiohttp
from aiohttp import web

from .account_web import NO_STORE
from .knowledge_web import KnowledgeWeb


class JobsWeb(KnowledgeWeb):
    PATH = '/account/jobs'
    UPSTREAM = ('ROOK_JOBS_ADMIN_URL', 'http://127.0.0.1:8765/jobs/account-api')
    ASSETS = ('jobs.js', 'jobs.css')
    UNAVAILABLE = 'The jobs service is unavailable (is the MCP server running?).'
    ADMIN_ONLY = False
    TIMEOUT = 30

    def install(self, app):
        super().install(app)
        app.router.add_post('/api/band/jobs', self.band_api)

    def _token_paths(self):
        from ..paths import data_path
        chat_db = getattr(getattr(self.account, 'server', None), '_chat_db', None)
        return [p for p in (os.environ.get('ROOK_MASK_TOKEN_FILE'),
                            os.path.join(os.path.dirname(chat_db), 'mask.token') if chat_db else None,
                            data_path('mask.token', '/var/lib/rook-band-mcp/mask.token')) if p]

    def _token(self):
        for path in self._token_paths():
            try:
                with open(path, encoding='ascii') as f:
                    tok = f.read().strip()
                if tok:
                    return tok
            except OSError:
                continue
        return None

    async def band_api(self, request):
        from ..hub.authz import current_principal
        p = current_principal.get()
        if p is None or p.kind != 'human':
            return web.json_response({'error': 'Sign in to use Jobs.'}, status=401, headers=NO_STORE)
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            return web.json_response({'error': 'Expected a JSON object.'}, status=400, headers=NO_STORE)
        token = self._token()
        if not token:
            return web.json_response({'error': self.UNAVAILABLE}, status=503, headers=NO_STORE)
        who = json.dumps({'id': p.id, 'role': 'owner' if p.fail_open() else 'member', 'label': p.label})
        body = {k: data.get(k) for k in ('action', 'id', 'query', 'data')}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.TIMEOUT)) as http:
                async with http.post(self.url, json=body, allow_redirects=False,
                                     headers={'Authorization': 'Bearer ' + token,
                                              'X-Rook-Principal': who}) as response:
                    return web.json_response(await response.json(), status=response.status, headers=NO_STORE)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return web.json_response({'error': self.UNAVAILABLE}, status=503, headers=NO_STORE)
