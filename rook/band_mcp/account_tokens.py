"""Dashboard token administration against the MCP process's live token store.

The MCP port used to serve its own password-gated ``/tokens`` page. The
dashboard's Tokens view (``rook/web/tokens.js`` over ``/account/tokens/api``)
replaced it; ``/tokens`` here now only sends a browser to that view."""
import base64
import re

from starlette.responses import RedirectResponse
from starlette.routing import Route

from ..remote.account_api import account_api, json_reply
from ..remote.accounts import AccountStore
from ..remote.account_web import NO_STORE


def _parse_data_url(u: str) -> tuple[str, bytes | None]:
    """``data:image/png;base64,…`` → (mime, bytes); (``""``, None) if malformed
    or over 256 KB decoded."""
    if not u.startswith("data:image/") or ";base64," not in u:
        return "", None
    head, b64 = u.split(";base64,", 1)
    mime = head[5:]
    if len(b64) > 360_000:
        return mime, None
    try:
        return mime, base64.b64decode(b64, validate=True)
    except Exception:
        return mime, None


def dashboard_tokens_url() -> str:
    """Where the Tokens view lives. The installer domain is the dashboard's
    public host (operator configuration, never a request's Host header);
    without one, a same-origin path suits the documented layout where one
    domain fronts both services."""
    from ..remote import setup_store
    domain = setup_store.load().get("pyz_domain", "")
    if re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", domain or ""):
        return f"https://{domain}/#tokens"
    return "/#tokens"


def build_account_token_routes(provider, chat=None, accounts=None):
    accounts = accounts or AccountStore()

    response = json_reply

    # The dashboard proxy also verifies Origin. CSRF remains mandatory here
    # (account_api), including when this endpoint is reached directly on MCP.
    @account_api(accounts, sign_in='Sign in to manage API tokens.',
                 operator_only='API tokens require an operator account.')
    async def api(request, user, data):
        if data is None:
            return response({'tokens': provider.list_api_tokens(),
                             'avatars': chat.avatar_index() if chat and chat.enabled else {}})
        try:
            op = data.get('op')
            if op == 'create':
                name = data.get('name')
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
                    raise ValueError('Label must be 1–64 characters.')
                ttl = data.get('ttl')
                if ttl not in (None, 86400, 604800, 2592000, 7776000, 31536000):
                    raise ValueError('Choose a supported expiry.')
                # Optional scope tags. Only the Work session tag is accepted;
                # scopes are recorded for attribution.
                scopes = data.get('scopes')
                if scopes is not None:
                    if (not isinstance(scopes, list) or len(scopes) > 4 or scopes[:1] != ['rook']
                            or not all(isinstance(x, str) and re.fullmatch(r'work-session:[a-f0-9]{32}', x)
                                       for x in scopes[1:])):
                        raise ValueError('Unsupported token scope.')
                # Token role (permissions.md 1), chosen at mint; work-session
                # tokens and the default are "agent".
                role = data.get('role') or 'agent'
                from .tokens import TOKEN_ROLES
                if role not in TOKEN_ROLES:
                    raise ValueError('Choose a supported role.')
                if role == 'operator' and not user.get('admin'):
                    raise ValueError('Only an operator account can mint operator tokens.')
                if role != 'agent' and scopes is not None:
                    raise ValueError('Work session tokens are agent tokens.')
                entry = (provider.mint_api_token(name.strip(), ttl_seconds=ttl, scopes=scopes, role=role)
                         if role != 'agent' else
                         provider.mint_api_token(name.strip(), ttl_seconds=ttl, scopes=scopes))
                return response({'id': entry['id'], 'name': entry['name'], 'token': entry['token']})
            if op == 'revoke':
                if data.get('confirm') is not True:
                    raise ValueError('Confirm revoking this token.')
                if not provider.revoke_api_token(str(data.get('id', ''))):
                    return response({'error': 'Token no longer exists.'}, 404)
                return response({'ok': True})
            if op in ('avatar', 'avatar_clear'):
                if not chat or not chat.enabled:
                    return response({'error': 'Chat avatars are unavailable.'}, 503)
                ident = data.get('identity')
                if not isinstance(ident, str) or not 1 <= len(ident) <= 200:
                    raise ValueError('Enter a valid identity.')
                if op == 'avatar_clear':
                    chat.clear_avatar(ident)
                else:
                    mime, raw = _parse_data_url(str(data.get('data', '')))
                    if raw is None:
                        raise ValueError('Choose a valid image.')
                    result = chat.set_avatar(ident, mime, raw)
                    if not result.get('ok'):
                        raise ValueError(result.get('error', 'Unable to save image.'))
                return response({'ok': True})
            raise ValueError('Unknown action.')
        except (ValueError, TypeError) as error:
            return response({'error': str(error)}, 400)

    async def legacy_page(request):
        return RedirectResponse(dashboard_tokens_url(), status_code=303, headers=NO_STORE)

    return [Route('/tokens/account-api', api, methods=['GET', 'POST']),
            Route('/tokens', legacy_page, methods=['GET'])]
