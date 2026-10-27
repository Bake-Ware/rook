"""Operator-only API for the secret vault (see vault.py). Same session + CSRF
rules as the other account APIs; the site proxies to it. Values are
write-only here: this API never returns one."""
from starlette.routing import Route

from ..remote.account_api import account_api, json_error, json_reply


def routes(vault, on_set, accounts):
    @account_api(accounts, sign_in='Sign in to manage secrets.',
                 operator_only='Secrets require an owner account.')
    async def api(request, user, data):
        if vault is None:
            return json_error('The vault is unavailable on this hub.', 503)
        if data is None:
            return json_reply({'csrf': user['csrf'], 'secrets': vault.list(),
                               'access': vault.access_log(request.query_params.get('name'), 200)})
        try:
            actor = 'human:' + str(user.get('username') or user['id'])
            if data.get('action') == 'set':
                res = vault.set(data.get('name'), data.get('value'), data.get('description', ''), actor)
                res['journal_rows_masked'] = on_set(data.get('name'), data.get('value'))
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
            return json_reply({'ok': True, **res, 'secrets': vault.list()})
        except KeyError:
            return json_error('No such secret', 404)
        except (ValueError, TypeError) as error:
            return json_error(str(error), 400)
    return [Route('/vault/account-api', api, methods=['GET', 'POST'])]
