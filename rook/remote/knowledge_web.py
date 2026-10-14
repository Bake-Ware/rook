"""Same-origin human displays and proxy to the authoritative knowledge service.

:class:`KnowledgeWeb` is the shared session/CSRF proxy every ``/account/<x>/api``
uses to reach the MCP side's ``/<x>/account-api``. It goes through
rook/remote/upstream.py: HTTP when the MCP server is its own process, an
in-memory call when ``rook hub serve`` runs both in one.
"""
import os
from pathlib import Path
from aiohttp import web
from . import upstream
from .account_web import COOKIE, NO_STORE


class KnowledgeWeb:
    PATH = '/account/knowledge'
    UPSTREAM = ('ROOK_KNOWLEDGE_ADMIN_URL', 'http://127.0.0.1:8765/knowledge/account-api')
    ASSETS = ('knowledge.js', 'knowledge.css')
    UNAVAILABLE = 'Knowledge service is unavailable.'
    ADMIN_ONLY = True
    FORBIDDEN = 'This requires the operator account.'
    TIMEOUT = 20

    def __init__(self, account):
        self.account = account
        self.url = os.environ.get(*self.UPSTREAM)

    def install(self, app):
        app.router.add_route('*', self.PATH + '/api', self.api)
        app.router.add_get(self.PATH + '/assets/{name}', self.asset)

    async def asset(self, request):
        name = request.match_info['name']
        if name not in self.ASSETS:
            raise web.HTTPNotFound()
        return web.FileResponse(Path(__file__).parent.parent / 'web' / name, headers={'Cache-Control': 'no-cache'})

    async def api(self, request):
        user = self.account.require(request)
        if self.ADMIN_ONLY and not user['admin']:
            # JSON, like every other answer here: the pages parse the reply.
            return web.json_response({'error': self.FORBIDDEN}, status=403, headers=NO_STORE)
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
            status, body = await upstream.request_json(request.method, self.url, params=request.query,
                                                       json=data, cookies={COOKIE: token},
                                                       timeout=self.TIMEOUT)
        except upstream.Unavailable:
            return web.json_response({'error': self.UNAVAILABLE}, status=503, headers=NO_STORE)
        return web.json_response(body, status=status, headers=NO_STORE)


class GuidanceWeb(KnowledgeWeb):
    """Agent instructions editor: same session/CSRF proxy, different upstream."""
    PATH = '/account/guidance'
    UPSTREAM = ('ROOK_GUIDANCE_ADMIN_URL', 'http://127.0.0.1:8765/guidance/account-api')
    ASSETS = ('guidance.js',)
    UNAVAILABLE = 'Agent instructions service is unavailable.'


class VaultWeb(KnowledgeWeb):
    """Secrets page: same session/CSRF proxy; values are write-only."""
    PATH = '/account/vault'
    UPSTREAM = ('ROOK_VAULT_ADMIN_URL', 'http://127.0.0.1:8765/vault/account-api')
    ASSETS = ('vault.js',)
    UNAVAILABLE = 'The vault service is unavailable.'


class SettingsWeb(KnowledgeWeb):
    """The unified Settings area. Any signed-in account may call it: the
    upstream limits non-operators to their own preferences (user scope)."""
    PATH = '/account/settings'
    UPSTREAM = ('ROOK_SETTINGS_ADMIN_URL', 'http://127.0.0.1:8765/settings/account-api')
    ASSETS = ('settings-area.js', 'home.js')
    UNAVAILABLE = 'The settings service is unavailable (is the MCP server running?).'
    ADMIN_ONLY = False
    TIMEOUT = 30   # a worker page asks the worker for its plugins and config


class PluginsWeb(KnowledgeWeb):
    """Manage > Plugins: the band-wide plugin grid, presets and the
    marketplace preview. Operator account only, like the upstream."""
    PATH = '/account/plugins'
    UPSTREAM = ('ROOK_PLUGINS_ADMIN_URL', 'http://127.0.0.1:8765/plugins/account-api')
    ASSETS = ('plugins.js',)
    UNAVAILABLE = 'The plugins service is unavailable (is the MCP server running?).'
    TIMEOUT = 60   # a band-wide switch waits on every worker it touches


class UpdatesWeb(KnowledgeWeb):
    """Manage > Updates: versions across the band, release notes, staged
    rollouts and history. Operator account only, like the upstream."""
    PATH = '/account/updates'
    UPSTREAM = ('ROOK_UPDATES_ADMIN_URL', 'http://127.0.0.1:8765/updates/account-api')
    ASSETS = ('updates.js', 'updates.css')
    UNAVAILABLE = 'The update service is unavailable (is the MCP server running?).'
    TIMEOUT = 300   # applying downloads and verifies the release first
