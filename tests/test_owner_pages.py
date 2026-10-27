"""Every owner sees and does what the operator does (Bake, 2026-10-27:
"anybody that's an owner should be able to see all of the same shit an owner
should see"). A second owner (access.db role ``owner``, not the operator
account) opens every operator page on both hub processes: the MCP side's
account APIs (rook/remote/account_api.py) and the dashboard's proxies and
admission. Members are still refused. Hard stops stay: an agent's secret
reveal is still held, and any owner can answer it."""
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.testclient import TestClient as ASGIClient

from test_band_management import portal  # noqa: F401 - fixture
from rook.hub.access import AccessStore

PASSWORD = 'a long enough test password'


@pytest.fixture
def people(portal, tmp_path):  # noqa: F811 - the fixture above
    """The operator, a second owner and a member, with sessions."""
    p = portal
    access = AccessStore(str(tmp_path / 'access.db'))
    partner = p.store.create_local('partner', PASSWORD)
    kid = p.store.create_local('kid', PASSWORD)
    access.set_role(p.uid, 'owner', actor='test', words='the operator')
    access.set_role(partner, 'owner', actor='human:operator', words="Bake: she's an owner")
    access.set_role(kid, 'member', actor='human:operator', words='Bake: a member')
    p.store.owner_check = access.is_owner
    tokens = {who: p.store.new_session(uid)
              for who, uid in (('operator', p.uid), ('partner', partner), ('kid', kid))}
    return SimpleNamespace(p=p, access=access, uid={'operator': p.uid, 'partner': partner, 'kid': kid},
                           tokens=tokens, cookie=lambda who: {'Cookie': 'rook_account=' + tokens[who]})


def _same(reply):
    """A reply with what is per-session (the CSRF token) left out."""
    try:
        body = reply.json()
    except ValueError:
        return reply.status_code, reply.text
    if isinstance(body, dict):
        body = {k: v for k, v in body.items() if k not in ('csrf', 'user', 'me', 'actor')}
    return reply.status_code, body


# -- the session ----------------------------------------------------------------

def test_a_second_owner_session_counts_as_an_owner(people):
    s = people.p.store
    op = s.session(people.tokens['operator'])
    partner = s.session(people.tokens['partner'])
    kid = s.session(people.tokens['kid'])
    assert op['admin'] and op['operator']
    assert partner['admin'] and not partner['operator']
    assert not kid['admin'] and not kid['operator']
    assert not s.user(people.uid['partner'])['admin']       # the account flag is untouched
    # Demoted: the next request is a member's.
    people.access.set_role(people.uid['partner'], 'member', actor='human:operator',
                           words='Bake: member again')
    assert not s.session(people.tokens['partner'])['admin']


def test_owner_check_failing_leaves_only_the_operator(people):
    s = people.p.store

    def broken(uid):
        raise OSError('access.db is gone')
    s.owner_check = broken
    assert s.session(people.tokens['operator'])['admin']    # never locked out
    assert not s.session(people.tokens['partner'])['admin']
    s.owner_check = None
    assert s.session(people.tokens['operator'])['admin']
    assert not s.session(people.tokens['partner'])['admin']


def test_a_second_owner_owns_every_band_as_the_operator_does(people):
    s, band = people.p.store, people.p.source['id']
    partner, kid = people.uid['partner'], people.uid['kid']
    assert [b['id'] for b in s.bands(people.uid['operator'])] == [band]
    assert [(b['id'], b['role']) for b in s.bands(partner)] == [(band, 'owner')]
    assert s.require_band(partner, band, True) == 'owner'
    assert [m['id'] for m in s.members(partner, band)] == [people.uid['operator']]
    assert s.bands(kid) == []
    with pytest.raises(PermissionError):
        s.require_band(kid, band)
    with pytest.raises(PermissionError):
        s.require_band(partner, 'no-such-band')


@pytest.mark.asyncio
async def test_a_second_owner_sees_the_bands_page_as_the_operator_does(people):
    async with TestClient(TestServer(people.p.app)) as http:
        op = await (await http.get('/account/bands/api', headers=people.cookie('operator'))).json()
        partner = await (await http.get('/account/bands/api', headers=people.cookie('partner'))).json()
        kid = await (await http.get('/account/bands/api', headers=people.cookie('kid'))).json()
    assert partner == op and partner['bands'][0]['members']
    assert kid['bands'] == []


# -- the MCP side: every account API -----------------------------------------------

def _mcp_pages(accounts, tmp_path):
    """(name, path, routes) for each operator page's API, with small stand-ins
    for the services behind them."""
    from rook.band_mcp.account_tokens import build_account_token_routes
    from rook.band_mcp.guidance_web import routes as guidance
    from rook.band_mcp.tokens import TokenStore
    from rook.band_mcp.vault_web import routes as vault
    from rook.hub.plugins.ccmod.web import routes as ccmod
    from rook.hub.plugins.huddle.web import routes as huddle
    from rook.hub.plugins.knowledge.web import routes as knowledge
    from rook.hub.plugins.updates.web import routes as updates
    from rook.hub.plugins_web import routes as plugins
    from rook.hub.settings_web import routes as settings
    secrets = SimpleNamespace(list=lambda: [{'name': 'nas-admin'}], access_log=lambda name, n: [])
    notes = SimpleNamespace(editable=True, slots=lambda: {'server': 'be kind'},
                            history=lambda key: [])
    know = SimpleNamespace(bands=lambda: [{'id': 'b1', 'name': 'Home'}])
    hub_settings = SimpleNamespace(overview=lambda: {'groups': ['hub']})
    return [
        ('Secrets', '/vault/account-api', vault(secrets, lambda n, v: 0, accounts)),
        ('Agent instructions', '/guidance/account-api',
         guidance(notes, lambda: None, lambda: ['rook_call'], accounts)),
        ('Tokens', '/tokens/account-api',
         build_account_token_routes(TokenStore(persist_path=str(tmp_path / 'tokens.json')),
                                    accounts=accounts)),
        ('Knowledge', '/knowledge/account-api', knowledge(know, accounts)),
        ('Settings (hub)', '/settings/account-api?view=overview',
         settings(lambda: hub_settings, accounts)),
        # Pages whose service is a live hub node: no node here, so both owners
        # get the same "unavailable" past the gate, and members the refusal.
        ('Plugins', '/plugins/account-api', plugins(lambda: None, accounts)),
        ('Claude Code rules', '/ccmod/account-api', ccmod(lambda: None, accounts)),
        ('Updates', '/updates/account-api', updates(lambda: None, accounts)),
        ('Huddles', '/huddle/account-api', huddle(lambda: None, accounts)),
    ]


def test_a_second_owner_opens_every_operator_page_api(people, tmp_path):
    pages = _mcp_pages(people.p.store, tmp_path)
    app = Starlette(routes=[r for _, _, routes in pages for r in routes])
    with ASGIClient(app) as client:
        for name, path, _ in pages:
            op = client.get(path, headers=people.cookie('operator'))
            partner = client.get(path, headers=people.cookie('partner'))
            kid = client.get(path, headers=people.cookie('kid'))
            assert op.status_code not in (401, 403), (name, op.text)
            assert _same(partner) == _same(op), name           # what the operator sees
            assert kid.status_code == 403, (name, kid.text)
            assert 'owner' in kid.json()['error'], name
        # And uses them: a write as the second owner (CSRF from their session).
        csrf = people.p.store.session(people.tokens['partner'])['csrf']
        r = client.post('/tokens/account-api', headers=people.cookie('partner'),
                        json={'op': 'create', 'name': 'her laptop', 'ttl': 86400, 'csrf': csrf})
        assert r.status_code == 200 and r.json()['token']


def test_a_second_owner_gets_the_operator_views_of_shared_pages(people):
    """Pages any account may open (manual, settings) give owners the full view."""
    from rook.hub.plugins.manual.web import routes as manual
    from rook.hub.settings_web import routes as settings
    status = {'version': 1, 'pages': 3, 'knowledge': True, 'imported': 1, 'pending': ['p1']}
    plugin = SimpleNamespace(status_view=lambda: dict(status))
    hub_settings = SimpleNamespace(overview=lambda: {'groups': ['hub']})
    app = Starlette(routes=manual(lambda: plugin, people.p.store)
                    + settings(lambda: hub_settings, people.p.store))
    with ASGIClient(app) as client:
        for who in ('operator', 'partner'):
            r = client.get('/manual/account-api', headers=people.cookie(who))
            assert r.json()['pending'] == ['p1'] and r.json()['admin'] is True, who
            r = client.get('/settings/account-api', headers=people.cookie(who))
            assert r.status_code == 200 and r.json()['admin'] is True, who
        r = client.get('/manual/account-api', headers=people.cookie('kid'))
        assert 'pending' not in r.json()
        r = client.get('/settings/account-api?view=overview', headers=people.cookie('kid'))
        assert r.status_code == 403


def test_the_mcp_service_wires_owners_into_its_account_store(tmp_path):
    from rook.band_mcp.server import McpService
    from rook.remote.accounts import AccountStore
    from rook.remote.enrollment import EnrollmentStore
    access = AccessStore(str(tmp_path / 'access.db'))
    access.set_role('u_partner', 'owner', actor='t', words='an owner')
    client = SimpleNamespace(authz=SimpleNamespace(access=access))
    enrollment = EnrollmentStore(tmp_path / 'enrollment.db')
    svc = McpService(SimpleNamespace(), client, enrollment)
    assert svc.accounts.is_owner('u_partner') and not svc.accounts.is_owner('u_kid')
    # A store the dashboard already wired (rook hub serve shares it) is kept.
    shared = AccountStore(enrollment)
    shared.owner_check = mark = lambda uid: True
    assert McpService(SimpleNamespace(), client, enrollment, accounts=shared).accounts.owner_check is mark


# -- the dashboard: admission, proxies, holds ------------------------------------

@pytest_asyncio.fixture
async def dashboard(tmp_path, monkeypatch):
    """A CombinedServer whose page proxies reach a stand-in MCP upstream, with
    an access store, the operator, a second owner and a member."""
    from rook.remote import setup_store
    from rook.remote.bootstrap import CombinedServer
    from rook.remote.enrollment import EnrollmentStore
    from rook.remote.jobs_web import JobsWeb
    from rook.remote.knowledge_web import KnowledgeWeb
    from rook.remote.token_web import TokenWeb
    monkeypatch.setenv('ROOK_SETUP_PATH', str(tmp_path / 'setup.json'))
    monkeypatch.setenv('ROOK_ENROLLMENT_DB', str(tmp_path / 'enrollment.db'))
    monkeypatch.setenv('ROOK_CHAT_DB', str(tmp_path / 'chat.db'))
    monkeypatch.delenv('ROOK_GOOGLE_CLIENT_FILE', raising=False)
    setup_store.save({'band_name': 'home', 'band_psk': 'old-key', 'hub_public': 'hub.example.com:443',
                      'pyz_domain': 'rook.example.com'})
    EnrollmentStore().import_config()

    async def upstream(request):
        return web.json_response({'ok': True, 'page': request.path})
    up = web.Application()
    up.router.add_route('*', '/{tail:.*}', upstream)
    async with TestServer(up) as upstream_server:
        proxies = [c for c in _subclasses(KnowledgeWeb)] + [TokenWeb, JobsWeb]
        paths = {}
        for cls in proxies:
            env, default = cls.UPSTREAM
            monkeypatch.setenv(env, str(upstream_server.make_url(default.split('8765', 1)[1])))
            paths[cls.PATH] = cls.ADMIN_ONLY if hasattr(cls, 'ADMIN_ONLY') else True
        server = CombinedServer(band_psk='old-key', web_user='operator', web_pass='operator-password',
                                domain='rook.example.com')
        access = AccessStore(str(tmp_path / 'access.db'))
        server._authz = SimpleNamespace(access=access)
        store = server._accounts.store
        op = store.bootstrap('operator', 'operator-password')
        partner = store.create_local('partner', PASSWORD)
        kid = store.create_local('kid', PASSWORD)
        access.set_role(op, 'owner', actor='t', words='the operator')
        access.set_role(partner, 'owner', actor='human:operator', words="Bake: she's an owner")
        access.set_role(kid, 'member', actor='human:operator', words='Bake: a member')
        tokens = {'operator': store.new_session(op), 'partner': store.new_session(partner),
                  'kid': store.new_session(kid)}
        async with TestClient(TestServer(server._app)) as http:
            yield SimpleNamespace(http=http, server=server, access=access, paths=paths,
                                  uid={'operator': op, 'partner': partner, 'kid': kid},
                                  cookie=lambda who: {'Cookie': 'rook_account=' + tokens[who]})


def _subclasses(cls):
    out = [cls]
    for sub in cls.__subclasses__():
        out += _subclasses(sub)
    return out


@pytest.mark.asyncio
async def test_a_second_owner_gets_the_whole_dashboard(dashboard):
    d = dashboard
    for who in ('operator', 'partner'):
        r = await d.http.get('/', headers=d.cookie(who), allow_redirects=False)
        assert r.status == 200, who                       # the full dashboard
        r = await d.http.get('/account', headers=d.cookie(who), allow_redirects=False)
        assert r.status == 302 and r.headers['Location'] == '/#account', who
        r = await d.http.get('/account/session', headers=d.cookie(who))
        assert (await r.json())['user']['admin'] is True, who
    r = await d.http.get('/', headers=d.cookie('kid'), allow_redirects=False)
    assert r.status == 302 and r.headers['Location'] == '/account'


@pytest.mark.asyncio
async def test_a_second_owner_opens_every_dashboard_page(dashboard):
    d = dashboard
    assert len(d.paths) >= 12
    for path, admin_only in d.paths.items():
        api = path + '/api'
        op = await d.http.get(api, headers=d.cookie('operator'))
        partner = await d.http.get(api, headers=d.cookie('partner'))
        kid = await d.http.get(api, headers=d.cookie('kid'))
        assert op.status == 200, (path, await op.text())
        assert partner.status == 200 and await partner.json() == await op.json(), path
        if admin_only:
            assert kid.status == 403, path
    # Sessions (Work) and the bands page treat her as the operator too.
    r = await d.http.get('/account/work/sessions', headers=d.cookie('partner'))
    assert r.status not in (401, 403)
    assert (await d.http.get('/account/work/sessions', headers=d.cookie('kid'))).status == 403
    r = await d.http.get('/account/bands', headers=d.cookie('partner'), allow_redirects=False)
    assert r.status == 302 and r.headers['Location'] == '/#bands'


@pytest.mark.asyncio
async def test_a_second_owner_is_an_owner_principal_and_answers_a_secret_reveal(dashboard, tmp_path):
    """The hub authorizer: her dashboard principal is an owner's, which passes
    require_hub_admin; an agent's secret reveal is still held (the hard stop
    stays), and she can answer the card."""
    from rook.hub.authz import current_principal, require_hub_admin
    from rook.remote.bootstrap import CombinedServer
    from test_approvals import AGENT, cfg, check, dashboard as approvals_dashboard
    d = dashboard
    req = SimpleNamespace(path='/api/call', cookies={'rook_account': ''}, headers={})

    def principal(who):
        d.server._accounts.current = lambda r: d.server._accounts.store.session(
            d.cookie(who)['Cookie'].split('=', 1)[1])
        return CombinedServer._principal_for(d.server, req)
    her = principal('partner')
    assert her.role == 'owner' and principal('kid').role == 'member'
    tok = current_principal.set(her)
    try:
        assert require_hub_admin('policy.set') is None
    finally:
        current_principal.reset(tok)
    holder = [her]
    app, az = approvals_dashboard(tmp_path / 'holds', holder)
    az.approvals.config_source.fixed = cfg(wait_secs=0)
    reveal = check(az, 'secret.get', principal=AGENT, local=True, args={'name': 'nas-admin'})
    assert reveal.held                                    # still needs its card
    async with TestClient(TestServer(app)) as http:
        held = await az.resolve(reveal, {'name': 'nas-admin'})
        r = await http.post(f"/api/approvals/{held['hold']}", json={'decision': 'approve'})
        assert r.status == 200 and (await r.json())['hold']['decided_by'] == her.id
