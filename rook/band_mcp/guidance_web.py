"""Operator-only API for viewing and editing agent guidance (see guidance.py).
Same session + CSRF rules as the knowledge API; the site proxies to it."""
import hmac

from starlette.responses import JSONResponse
from starlette.routing import Route

from ..remote.account_web import NO_STORE


def routes(guidance, reapply, tool_names, accounts):
    async def api(request):
        user = accounts.session(request.cookies.get('rook_account', ''))
        if not user:
            return JSONResponse({'error': 'Sign in to edit agent instructions.'}, 401, headers=NO_STORE)
        if not user['admin']:
            return JSONResponse({'error': 'Agent instructions require the operator account.'}, 403, headers=NO_STORE)
        if request.method == 'GET':
            key = request.query_params.get('history')
            if key:
                return JSONResponse({'history': guidance.history(key)}, headers=NO_STORE)
            return JSONResponse({'csrf': user['csrf'], 'editable': guidance.editable,
                                 'slots': guidance.slots(), 'tools': sorted(tool_names())},
                                headers=NO_STORE)
        try:
            data = await request.json()
            if not isinstance(data, dict) or not hmac.compare_digest(str(data.get('csrf', '')), user['csrf']):
                return JSONResponse({'error': 'Form expired; reload the page.'}, 403, headers=NO_STORE)
            key = data.get('key')
            if isinstance(key, str) and key.startswith('tool:') and key[5:] not in tool_names():
                raise ValueError('No such tool')
            actor = 'human:' + str(user.get('username') or user['id'])
            if data.get('action') == 'reset':
                guidance.reset(key, actor)
            elif data.get('action') == 'set':
                guidance.set(key, data.get('text', ''), actor)
            else:
                raise ValueError('Use set or reset')
            reapply()
            return JSONResponse({'ok': True, 'slots': guidance.slots()}, headers=NO_STORE)
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': str(error)}, 400, headers=NO_STORE)
    return [Route('/guidance/account-api', api, methods=['GET', 'POST'])]
