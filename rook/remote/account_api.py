"""One gate for the account JSON APIs the MCP process serves to the dashboard
(``/guidance``, ``/vault``, ``/tokens``, ``/knowledge``, ``/settings``,
``/jobs`` ``/account-api``; the dashboard proxies ``/account/*/api`` to them).

Each used to repeat the same steps: look up the ``rook_account`` session
(401), require the operator account (403), parse the JSON object (400) and
compare the session CSRF token in constant time (403). ``account_api`` does
those steps once, in that order, and hands the handler ``(request, user,
data)``; ``data`` is ``None`` on GET, which never needs CSRF.
"""
from functools import wraps

from starlette.responses import JSONResponse

from .accounts import csrf_ok
from .account_web import COOKIE, NO_STORE

FORM_EXPIRED = 'Form expired; reload the page.'
NOT_JSON = 'Expected a JSON object.'


def json_reply(data, status=200):
    return JSONResponse(data, status, headers=NO_STORE)


def json_error(message, status):
    return json_reply({'error': message}, status)


async def read_object(request):
    """The request's JSON object, or ``None`` when the body is not one."""
    try:
        data = await request.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def account_api(accounts, *, admin=True, csrf=True,
                sign_in='Sign in with your operator account.',
                operator_only='This requires the operator account.'):
    """Decorate ``async handler(request, user, data)``.

    ``admin``: refuse non-operator accounts (403). ``csrf``: a non-GET body
    must be a JSON object whose ``csrf`` matches the session (400 / 403).
    """
    def wrap(handler):
        @wraps(handler)
        async def api(request):
            user = accounts.session(request.cookies.get(COOKIE, ''))
            if not user:
                return json_error(sign_in, 401)
            if admin and not user.get('admin'):
                return json_error(operator_only, 403)
            data = None
            if request.method not in ('GET', 'HEAD'):
                data = await read_object(request)
                if data is None:
                    return json_error(NOT_JSON, 400)
                if csrf and not csrf_ok(data, user.get('csrf')):
                    return json_error(FORM_EXPIRED, 403)
            return await handler(request, user, data)
        return api
    return wrap
