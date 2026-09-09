"""Dashboard token administration against the MCP process's live token store."""
import hmac

from starlette.responses import JSONResponse
from starlette.routing import Route

from ..remote.accounts import AccountStore
from ..remote.account_web import NO_STORE


def build_account_token_routes(provider, chat=None, accounts=None):
    accounts = accounts or AccountStore()

    def response(data, status=200):
        return JSONResponse(data, status_code=status, headers=NO_STORE)

    async def api(request):
        user = accounts.session(request.cookies.get('rook_account', ''))
        if not user:
            return response({'error': 'Sign in to manage API tokens.'}, 401)
        if not user['admin']:
            return response({'error': 'API tokens require an operator account.'}, 403)
        if request.method == 'GET':
            return response({'tokens': provider.list_api_tokens(),
                             'avatars': chat.avatar_index() if chat and chat.enabled else {}})
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError('Expected an object.')
            # The dashboard proxy also verifies Origin. CSRF remains mandatory
            # here, including when this endpoint is reached directly on MCP.
            if not hmac.compare_digest(str(data.get('csrf', '')), user['csrf']):
                return response({'error': 'Form expired; reload the page.'}, 403)
            op = data.get('op')
            if op == 'create':
                name = data.get('name')
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 64:
                    raise ValueError('Label must be 1–64 characters.')
                ttl = data.get('ttl')
                if ttl not in (None, 86400, 604800, 2592000, 7776000, 31536000):
                    raise ValueError('Choose a supported expiry.')
                entry = provider.mint_api_token(name.strip(), ttl_seconds=ttl)
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
                    from .api_tokens_ui import _parse_data_url
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

    return [Route('/tokens/account-api', api, methods=['GET', 'POST'])]
