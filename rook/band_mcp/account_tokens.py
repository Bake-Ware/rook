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
from ..remote.account_web import COOKIE, NO_STORE

#: The Tokens page's "May relay my approvals" (rook/hub/relay.py): who may
#: approve or deny waiting items on the person's behalf. Only this
#: operator-account route writes it, and never for a sign-in that used a
#: password agents could also read (a vault value).
WEAK_RELAY = ('This sign-in uses a password that is stored as a band secret, so agents could sign '
              'in as you with it. It can\'t change who may relay your approvals. Change the '
              'password (or remove that secret) and sign in again.')
HOUSE_AGENT = 'house'


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


def build_account_token_routes(provider, chat=None, accounts=None, approvals=None, house=None,
                                devices=None):
    """``approvals()``: the hub's approval queue holder (or None);
    ``house()``: ``(identity, label)`` of the house agent (or None);
    ``devices()``: ``[(device:<worker_id>, label)]`` of the phones the
    attention plugin puts decision cards on (their taps relay)."""
    accounts = accounts or AccountStore()

    response = json_reply

    def queue():
        ap = approvals() if approvals is not None else None
        return getattr(ap, 'queue', None)

    def house_agent():
        try:
            return house() if house is not None else None
        except Exception:
            return None

    def phones():
        try:
            return list(devices() or []) if devices is not None else []
        except Exception:
            return []

    def relay_view(tokens):
        """Each token's relay switch, the house agent's and each phone's."""
        q = queue()
        on = {r['principal']: r for r in (q.relayers() if q is not None else [])}
        for t in tokens:
            r = on.get(f"token:{t.get('agent_id')}")
            t['relay'] = bool(r)
            if r:
                t['relay_by'] = r.get('enabled_by')
        out = {'available': q is not None}
        h = house_agent()
        if h:
            r = on.get(h[0])
            out['house'] = {'identity': h[0], 'label': h[1], 'relay': bool(r),
                            **({'relay_by': r.get('enabled_by')} if r else {})}
        rows = []
        for ident, label in phones():
            r = on.get(ident)
            rows.append({'identity': ident, 'label': label, 'relay': bool(r),
                         **({'relay_by': r.get('enabled_by')} if r else {})})
        if rows:
            out['devices'] = rows
        return out

    # The dashboard proxy also verifies Origin. CSRF remains mandatory here
    # (account_api), including when this endpoint is reached directly on MCP.
    @account_api(accounts, sign_in='Sign in to manage API tokens.',
                 operator_only='API tokens require an operator account.')
    async def api(request, user, data):
        if data is None:
            tokens = provider.list_api_tokens()
            relay = relay_view(tokens)
            return response({'tokens': tokens, 'relay': relay,
                             'avatars': chat.avatar_index() if chat and chat.enabled else {}})
        try:
            op = data.get('op')
            if op == 'relay':
                q = queue()
                if q is None:
                    return response({'error': 'Approvals are unavailable on this hub.'}, 503)
                weak = True
                try:
                    weak = bool(accounts.session_weak(request.cookies.get(COOKIE, '')))
                except Exception:
                    pass  # unknown: fail closed
                if weak:
                    return response({'error': WEAK_RELAY, 'weak_credential': True}, 403)
                enabled = data.get('enabled')
                if not isinstance(enabled, bool):
                    raise ValueError('enabled must be true or false.')
                ident = str(data.get('id', ''))
                if ident == HOUSE_AGENT:
                    h = house_agent()
                    if not h:
                        raise ValueError('There is no house agent on this hub.')
                    principal, label = h
                elif ident.startswith(('device:', 'voice:')):
                    hit = next((d for d in phones() if d[0] == ident), None)
                    if hit is None:
                        return response({'error': 'That phone is not in attention.devices.'
                                         if ident.startswith('device:') else
                                         'The house voice is not set up on this hub.'}, 404)
                    principal, label = hit
                else:
                    t = next((t for t in provider.list_api_tokens() if t.get('id') == ident), None)
                    if t is None or not t.get('agent_id'):
                        return response({'error': 'Token no longer exists.'}, 404)
                    principal, label = f"token:{t['agent_id']}", t.get('name') or ident
                changed = q.set_relayer(principal, enabled,
                                        'human:' + str(user.get('username') or user.get('id')),
                                        label)
                return response({'ok': True, 'relay': enabled, 'changed': changed})
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
                gone = next((t for t in provider.list_api_tokens()
                             if t.get('id') == str(data.get('id', ''))), None)
                if not provider.revoke_api_token(str(data.get('id', ''))):
                    return response({'error': 'Token no longer exists.'}, 404)
                q = queue()
                if q is not None and gone and gone.get('agent_id'):
                    still = any(t.get('agent_id') == gone['agent_id']
                                for t in provider.list_api_tokens())
                    if not still:   # its agent has no key left: it relays nothing
                        q.set_relayer(f"token:{gone['agent_id']}", False,
                                      'human:' + str(user.get('username') or user.get('id')),
                                      gone.get('name') or '')
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
