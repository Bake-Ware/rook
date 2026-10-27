"""Operator-only API for viewing and editing agent guidance (see guidance.py).
Same session + CSRF rules as the knowledge API; the site proxies to it."""
from starlette.routing import Route

from ..remote.account_api import account_api, json_error, json_reply


def routes(guidance, reapply, tool_names, accounts):
    @account_api(accounts, sign_in='Sign in to edit agent instructions.',
                 operator_only='Agent instructions require an owner account.')
    async def api(request, user, data):
        if data is None:
            key = request.query_params.get('history')
            if key:
                return json_reply({'history': guidance.history(key)})
            return json_reply({'csrf': user['csrf'], 'editable': guidance.editable,
                               'slots': guidance.slots(), 'tools': sorted(tool_names())})
        try:
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
            return json_reply({'ok': True, 'slots': guidance.slots()})
        except (ValueError, TypeError) as error:
            return json_error(str(error), 400)
    return [Route('/guidance/account-api', api, methods=['GET', 'POST'])]
