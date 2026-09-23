"""Same-origin human displays and proxy to the authoritative knowledge service."""
import os
from pathlib import Path
import aiohttp
from aiohttp import web
from .account_web import COOKIE, NO_STORE


class KnowledgeWeb:
    def __init__(self, account):
        self.account = account
        self.url = os.environ.get('ROOK_KNOWLEDGE_ADMIN_URL', 'http://127.0.0.1:8765/knowledge/account-api')

    def install(self, app):
        app.router.add_route('*', '/account/knowledge/api', self.api)
        app.router.add_get('/account/knowledge/assets/{name}', self.asset)

    async def asset(self, request):
        name = request.match_info['name']
        if name not in ('knowledge.js', 'knowledge.css'):
            raise web.HTTPNotFound()
        return web.FileResponse(Path(__file__).parent.parent / 'web' / name, headers={'Cache-Control': 'no-cache'})

    async def api(self, request):
        user = self.account.require(request)
        if not user['admin']:
            raise web.HTTPForbidden()
        if request.method not in ('GET', 'POST'):
            raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST'])
        data = None
        if request.method == 'POST':
            try:
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError('Expected an object')
                self.account.csrf(request, data, user)
            except PermissionError as error:
                return web.json_response({'error': str(error)}, status=403, headers=NO_STORE)
            except (ValueError, TypeError) as error:
                return web.json_response({'error': str(error)}, status=400, headers=NO_STORE)
        token = request.cookies.get(COOKIE, '')
        if request.headers.get('Authorization', '').startswith('Bearer '):
            token = request.headers['Authorization'][7:]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
                async with http.request(request.method, self.url, json=data, cookies={COOKIE: token}, allow_redirects=False) as response:
                    return web.json_response(await response.json(), status=response.status, headers=NO_STORE)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return web.json_response({'error': 'Knowledge service is unavailable.'}, status=503, headers=NO_STORE)
