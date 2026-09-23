"""Operator-only API for the secret vault (see vault.py). Same session + CSRF
rules as the other account APIs; the site proxies to it. Values are
write-only here: this API never returns one."""
import hmac

from starlette.responses import JSONResponse
from starlette.routing import Route

from ..remote.account_web import NO_STORE


def routes(vault, on_set, accounts):
    async def api(request):
        user = accounts.session(request.cookies.get('rook_account', ''))
        if not user:
            return JSONResponse({'error': 'Sign in to manage secrets.'}, 401, headers=NO_STORE)
        if not user['admin']:
            return JSONResponse({'error': 'Secrets require the operator account.'}, 403, headers=NO_STORE)
        if vault is None:
            return JSONResponse({'error': 'The vault is unavailable on this hub.'}, 503, headers=NO_STORE)
        if request.method == 'GET':
            return JSONResponse({'csrf': user['csrf'], 'secrets': vault.list(),
                                 'access': vault.access_log(request.query_params.get('name'), 200)},
                                headers=NO_STORE)
        try:
            data = await request.json()
            if not isinstance(data, dict) or not hmac.compare_digest(str(data.get('csrf', '')), user['csrf']):
                return JSONResponse({'error': 'Form expired; reload the page.'}, 403, headers=NO_STORE)
            actor = 'human:' + str(user.get('username') or user['id'])
            if data.get('action') == 'set':
                res = vault.set(data.get('name'), data.get('value'), data.get('description', ''), actor)
                res['journal_rows_masked'] = on_set(data.get('value'))
            elif data.get('action') == 'describe':
                # Update only the description: re-set with the existing value.
                name = data.get('name')
                res = vault.set(name, vault.get(name, actor, via='describe'), data.get('description', ''), actor)
            elif data.get('action') == 'delete':
                if not vault.delete(data.get('name'), actor):
                    raise ValueError('No such secret')
                res = {'deleted': data.get('name')}
            else:
                raise ValueError('Use set, describe or delete')
            return JSONResponse({'ok': True, **res, 'secrets': vault.list()}, headers=NO_STORE)
        except KeyError:
            return JSONResponse({'error': 'No such secret'}, 404, headers=NO_STORE)
        except (ValueError, TypeError) as error:
            return JSONResponse({'error': str(error)}, 400, headers=NO_STORE)
    return [Route('/vault/account-api', api, methods=['GET', 'POST'])]
