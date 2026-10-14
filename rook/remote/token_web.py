"""Same-origin, operator-only bridge to the live MCP token store."""
from aiohttp import web

from .account_web import NO_STORE
from .knowledge_web import KnowledgeWeb


class TokenWeb(KnowledgeWeb):
    """The Tokens view's API: the shared session/CSRF proxy, to the MCP
    process's ``/tokens/account-api``. Its script (tokens.js) is served with
    the other account assets, so this class adds only the page redirects."""
    PATH = '/account/tokens'
    # Operator-controlled service address, never a request-supplied URL.
    UPSTREAM = ('ROOK_TOKEN_ADMIN_URL', 'http://127.0.0.1:8765/tokens/account-api')
    ASSETS = ()
    UNAVAILABLE = 'Token service is unavailable. Try again shortly.'
    FORBIDDEN = 'API tokens require an operator account.'
    TIMEOUT = 15

    def install(self, app):
        super().install(app)
        app.router.add_get('/account/tokens', self.page)
        app.router.add_get('/tokens', self.page)

    async def page(self, request):
        raise web.HTTPFound('/#tokens', headers=NO_STORE)
