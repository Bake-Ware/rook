"""Same-origin human displays and proxy to the authoritative knowledge service.

:class:`KnowledgeWeb` is the shared session/CSRF proxy every ``/account/<x>/api``
uses to reach the MCP side's ``/<x>/account-api``. It goes through
rook/remote/upstream.py: HTTP when the MCP server is its own process, an
in-memory call when ``rook hub serve`` runs both in one.
"""
import os
import re
from pathlib import Path
from aiohttp import web
from . import upstream
from ..core import restart
from .account_web import COOKIE, NO_STORE


class KnowledgeWeb:
    PATH = '/account/knowledge'
    UPSTREAM = ('ROOK_KNOWLEDGE_ADMIN_URL', 'http://127.0.0.1:8765/knowledge/account-api')
    ASSETS = ('knowledge.js', 'knowledge.css', 'workviews.js', 'workviews.css')
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
    ASSETS = ('settings-area.js', 'home.js', 'home-dock.js', 'memory.js', 'voice.js', 'llm.js')
    UNAVAILABLE = 'The settings service is unavailable (is the MCP server running?).'
    ADMIN_ONLY = False
    TIMEOUT = 30   # a worker page asks the worker for its plugins and config

    async def api(self, request):
        # Settings > Restart. When the dashboard is its own process (not `rook
        # hub serve`), it restarts along with the MCP server: it must be able to
        # come back first, and goes only once the MCP side has agreed.
        if request.method != 'POST' or upstream.local_app(self.url) is not None:
            return await super().api(request)
        try:
            data = await request.json()   # aiohttp keeps the body for super().api
        except ValueError:
            data = None
        if not (isinstance(data, dict) and data.get('action') == 'restart'):
            return await super().api(request)
        user = self.account.require(request)
        if user['admin']:
            why = restart.supervised() if restart.available() else "it can't restart itself"
            if why:
                return web.json_response({'error': f'Not restarted: the dashboard: {why}.'},
                                         status=400, headers=NO_STORE)
        reply = await super().api(request)
        if user['admin'] and reply.status == 200:
            restart.request('human:' + str(user.get('username') or user.get('id') or ''))
        return reply


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
    ASSETS = ('updates.js',)
    UNAVAILABLE = 'The update service is unavailable (is the MCP server running?).'
    TIMEOUT = 300   # applying downloads and verifies the release first


class ManualWeb(KnowledgeWeb):
    """Help (the user manual) and the manual update prompt. Any signed-in
    account may read; the upstream keeps updating to the operator."""
    PATH = '/account/manual'
    UPSTREAM = ('ROOK_MANUAL_ADMIN_URL', 'http://127.0.0.1:8765/manual/account-api')
    ASSETS = ('manual.js', 'manual.css')
    UNAVAILABLE = 'The manual service is unavailable (is the MCP server running?).'
    ADMIN_ONLY = False


class HuddleWeb(KnowledgeWeb):
    """Huddles (chaired huddles, old chat rooms read-only), Work > Huddle
    (agents' status strip, pinned questions and answers) and Manage > Roles.
    Operator account only, like the upstream (rook/hub/plugins/huddle/web.py)."""
    PATH = '/account/huddle'
    UPSTREAM = ('ROOK_HUDDLE_ADMIN_URL', 'http://127.0.0.1:8765/huddle/account-api')
    ASSETS = ('huddle.js', 'huddle.css', 'huddles.js', 'roles.js', 'front.js')
    UNAVAILABLE = 'Huddles are unavailable (is the MCP server running?).'
    TIMEOUT = 150   # moving a role packs its home on one host and unpacks it on another


class UiWeb(KnowledgeWeb):
    """Overview, start page and themes (the ``ui`` hub plugin). Any signed-in
    account: each has its own theme and start page; the upstream keeps the
    knowledge half of the Overview to the operator account."""
    PATH = '/account/ui'
    UPSTREAM = ('ROOK_UI_ADMIN_URL', 'http://127.0.0.1:8765/ui/account-api')
    ASSETS = ('overview.js', 'startpage.js', 'widgets.js', 'themes.js', 'theme-boot.js', 'ui.css', 'mobile.css')
    UNAVAILABLE = 'The dashboard service is unavailable (is the MCP server running?).'
    ADMIN_ONLY = False
    TIMEOUT = 40   # a start page feed may take the hub's feed time limit, and redirects
    THEME_NAME = re.compile(r'^[a-z0-9][a-z0-9-]{0,39}\.json$')

    def install(self, app):
        super().install(app)
        # The theme files themselves (rook-theme/1 JSON), for other clients
        # such as the desktop app; public like the other static assets.
        app.router.add_get(self.PATH + '/themes/{name}', self.theme_file)

    async def asset(self, request):
        name = request.match_info['name']
        if name not in self.ASSETS:
            raise web.HTTPNotFound()
        types = {'.js': 'application/javascript', '.css': 'text/css'}
        path = Path(__file__).parent.parent / 'web' / name
        return web.FileResponse(path, headers={'Cache-Control': 'no-cache',
                                               'Content-Type': types[path.suffix],
                                               'X-Content-Type-Options': 'nosniff'})

    async def theme_file(self, request):
        name = request.match_info['name']
        path = Path(__file__).parent.parent / 'web' / 'themes' / name
        if not self.THEME_NAME.match(name) or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={'Cache-Control': 'no-cache',
                                               'Content-Type': 'application/json',
                                               'X-Content-Type-Options': 'nosniff'})


class CcmodWeb(KnowledgeWeb):
    """Manage > Claude Code: rules for Claude Code on the workers, their
    pushes and previews. Operator account only, like the upstream."""
    PATH = '/account/ccmod'
    UPSTREAM = ('ROOK_CCMOD_ADMIN_URL', 'http://127.0.0.1:8765/ccmod/account-api')
    ASSETS = ('ccmod.js',)
    UNAVAILABLE = 'The Claude Code rules service is unavailable (is the MCP server running?).'
    TIMEOUT = 90   # a push waits on every worker it writes to
