"""Operator-only knowledge API using existing account sessions and CSRF."""
from starlette.routing import Route

from ....remote.account_api import account_api, json_error, json_reply


def routes(service, accounts):
    """``service`` is the KnowledgeService or a callable returning the current
    one (the bridge passes a getter so a plugin reload is followed)."""
    current = service if callable(service) and not hasattr(service, 'dispatch') else (lambda: service)

    @account_api(accounts, sign_in='Sign in to use Knowledge.',
                 operator_only='Knowledge administration requires an owner account.')
    async def api(request, user, data):
        actor = {'id': 'human:' + user['id'], 'kind': 'human', 'label': user['name']}
        service = current()
        if service is None:
            return json_error('The knowledge plugin is not loaded', 503)
        if data is None:
            return json_reply({'csrf': user['csrf'], 'bands': service.bands(), 'actor': actor})
        try:
            result = await service.dispatch(data.get('action', 'list'), data.get('band'), data.get('kind'),
                                            data.get('id'), data.get('query', ''), data.get('data'),
                                            data.get('request_id'), actor=actor)
            return json_reply({'ok': True, 'result': result})
        except PermissionError as error:
            return json_error(str(error), 403)
        except (ValueError, KeyError, TypeError) as error:
            if getattr(error, 'candidates', None):   # a name that was not exactly one record's
                return json_reply(error.payload(), 400)
            return json_error(str(error), 409 if type(error).__name__ == 'Conflict' else 400)
    return [Route('/knowledge/account-api', api, methods=['GET', 'POST'])]
