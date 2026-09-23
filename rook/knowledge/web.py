"""Operator-only knowledge API using existing account sessions and CSRF."""
import hmac
from starlette.responses import JSONResponse
from starlette.routing import Route
from ..remote.account_web import NO_STORE


def routes(service, accounts):
    async def api(request):
        user = accounts.session(request.cookies.get('rook_account', ''))
        if not user:
            return JSONResponse({'error': 'Sign in to use Knowledge.'}, 401, headers=NO_STORE)
        if not user['admin']:
            return JSONResponse({'error': 'Knowledge administration requires the operator account.'}, 403, headers=NO_STORE)
        actor = {'id': 'human:' + user['id'], 'kind': 'human', 'label': user['name']}
        if request.method == 'GET':
            bands = service.bands()
            return JSONResponse({'csrf': user['csrf'], 'bands': bands, 'actor': actor}, headers=NO_STORE)
        try:
            data = await request.json()
            if not isinstance(data, dict) or not hmac.compare_digest(str(data.get('csrf', '')), user['csrf']):
                return JSONResponse({'error': 'Form expired; reload the page.'}, 403, headers=NO_STORE)
            result = await service.dispatch(data.get('action', 'list'), data.get('band'), data.get('kind'),
                                            data.get('id'), data.get('query', ''), data.get('data'),
                                            data.get('request_id'), actor=actor)
            return JSONResponse({'ok': True, 'result': result}, headers=NO_STORE)
        except PermissionError as error:
            return JSONResponse({'error': str(error)}, 403, headers=NO_STORE)
        except (ValueError, KeyError, TypeError) as error:
            return JSONResponse({'error': str(error)}, 409 if type(error).__name__ == 'Conflict' else 400, headers=NO_STORE)
    return [Route('/knowledge/account-api', api, methods=['GET', 'POST'])]
