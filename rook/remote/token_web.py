"""Same-origin, operator-only bridge to the live MCP token store."""
import os

import aiohttp
from aiohttp import web

from .account_web import COOKIE, NO_STORE


class TokenWeb:
    def __init__(self, account):
        self.account = account
        # Operator-controlled service address, never a request-supplied URL.
        self.url = os.environ.get('ROOK_TOKEN_ADMIN_URL', 'http://127.0.0.1:8765/tokens/account-api')

    def install(self, app):
        app.router.add_route('*', '/account/tokens/api', self.api)
        app.router.add_get('/account/tokens', self.page)
        app.router.add_get('/tokens', self.page)

    async def page(self, request):
        raise web.HTTPFound('/#tokens', headers=NO_STORE)

    async def api(self, request):
        user = self.account.require(request)
        if not user['admin']:
            return web.json_response({'error': 'API tokens require an operator account.'}, status=403, headers=NO_STORE)
        if request.method not in ('GET', 'POST'):
            raise web.HTTPMethodNotAllowed(request.method, ['GET', 'POST'])
        data = None
        if request.method == 'POST':
            try:
                data = await request.json()
                if not isinstance(data, dict):
                    raise ValueError('Expected an object.')
                self.account.csrf(request, data, user)
            except PermissionError as error:
                return web.json_response({'error': str(error)}, status=403, headers=NO_STORE)
            except (ValueError, TypeError) as error:
                return web.json_response({'error': str(error)}, status=400, headers=NO_STORE)
        token = request.cookies.get(COOKIE, '')
        if request.headers.get('Authorization', '').startswith('Bearer '):
            token = request.headers['Authorization'][7:]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.request(request.method, self.url, json=data,
                                           cookies={COOKIE: token}, allow_redirects=False) as upstream:
                    result = await upstream.json()
                    return web.json_response(result, status=upstream.status, headers=NO_STORE)
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return web.json_response({'error': 'Token service is unavailable. Try again shortly.'}, status=503, headers=NO_STORE)
