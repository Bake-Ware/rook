"""Token management shares account auth without duplicating the live token store."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.testclient import TestClient as ASGIClient

from test_band_management import portal
from rook.band_mcp.account_tokens import build_account_token_routes
from rook.band_mcp.tokens import TokenStore
from rook.remote.token_web import TokenWeb


def test_token_account_auth_and_live_revocation(portal, tmp_path):
    p = portal
    provider = TokenStore(persist_path=str(tmp_path/'tokens.json'))
    app = Starlette(routes=build_account_token_routes(provider, accounts=p.store))
    with ASGIClient(app) as client:
        endpoint = '/tokens/account-api'
        assert client.get(endpoint).status_code == 401
        member = p.store.create_local('member', 'a long enough test password')
        assert client.get(endpoint, headers={'Cookie':'rook_account='+p.store.new_session(member)}).status_code == 403
        headers = p.headers
        assert client.post(endpoint, headers=headers, json={'op':'create','name':'test'}).status_code == 403
        response = client.post(endpoint, headers=headers, json={'op':'create','name':'test integration','ttl':86400,'csrf':p.csrf})
        assert response.status_code == 200, response.text
        created = response.json()
        secret = created['token']
        assert provider.verify_bearer(secret) is not None
        assert 'Location' not in response.headers and response.headers['Cache-Control'] == 'no-store'
        response = client.get(endpoint, headers=headers)
        assert secret not in response.text
        assert response.json()['tokens'][0]['id'] == created['id']
        assert client.post(endpoint, headers=headers, json={'op':'revoke','id':created['id'],'csrf':p.csrf}).status_code == 400
        assert client.post(endpoint, headers=headers, json={'op':'revoke','id':created['id'],'confirm':True,'csrf':p.csrf}).status_code == 200
        assert provider.verify_bearer(secret) is None
        assert TokenStore(persist_path=str(tmp_path/'tokens.json')).verify_bearer(secret) is None
        assert client.post(endpoint, headers=headers, json={'op':'create','name':'test','ttl':-1,'csrf':p.csrf}).status_code == 400


@pytest.mark.asyncio
async def test_proxy_checks_origin_and_preserves_account_boundary(portal):
    p = portal
    calls = []
    async def upstream(request):
        calls.append((request.cookies.get('rook_account'), await request.json()))
        return web.json_response({'ok':True})
    upstream_app = web.Application(); upstream_app.router.add_post('/tokens/account-api', upstream)
    async with TestServer(upstream_app) as server:
        # The installed route has the same class; use a separate app to direct
        # this test at the disposable upstream.
        proxy = TokenWeb(p.account); proxy.url = str(server.make_url('/tokens/account-api'))
        app = web.Application(); proxy.install(app)
        async with TestClient(TestServer(app)) as client:
            data = {'op':'create','name':'test','csrf':p.csrf}
            assert (await client.post('/account/tokens/api', headers={**p.headers,'Origin':'https://evil.example'},json=data)).status == 403
            assert not calls
            member = p.store.create_local('member', 'a long enough test password')
            assert (await client.get('/account/tokens/api',headers={'Cookie':'rook_account='+p.store.new_session(member)})).status == 403
            response = await client.post('/account/tokens/api',headers=p.headers,json=data)
            assert response.status == 200 and response.headers['Cache-Control'] == 'no-store'
            assert calls == [(p.headers['Cookie'].split('=',1)[1],data)]


@pytest.mark.asyncio
async def test_embedded_account_actions_and_legacy_navigation(portal):
    p = portal
    async with TestClient(TestServer(p.app)) as client:
        response = await client.get('/account?device=ABCDEF12', headers=p.headers, allow_redirects=False)
        assert response.status == 302 and response.headers['Location'] == '/#account?device=ABCDEF12'
        response = await client.get('/account/component', headers=p.headers)
        assert response.status == 200 and 'data-section="profile"' in (await response.json())['html']
        headers = {**p.headers,'X-Rook-View':'account'}
        response = await client.post('/account/action',headers=headers,data={'op':'profile','name':'Updated name','csrf':p.csrf})
        assert (await response.json())['refresh'] is True
        assert p.store.user(p.uid)['name'] == 'Updated name'
        response = await client.post('/account/action',headers=headers,data={'op':'pair','band_id':p.source['id'],'csrf':p.csrf})
        data = await response.json()
        assert 'pairing' in data and data['pairing']['session'] and data['pairing']['code']
        response = await client.post('/account/action',headers=headers,data={'op':'logout','csrf':p.csrf})
        assert (await response.json())['redirect'] == '/account/login'
        assert p.store.session(p.headers['Cookie'].split('=',1)[1]) is None
