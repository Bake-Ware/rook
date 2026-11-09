"""Token management shares account auth without duplicating the live token store."""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from starlette.applications import Starlette
from starlette.testclient import TestClient as ASGIClient

from test_band_management import portal
from rook.remote.token_web import TokenWeb


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
            response = await client.get('/account/tokens/api',headers={'Cookie':'rook_account='+p.store.new_session(member)})
            assert response.status == 403
            assert (await response.json())['error'] == 'API tokens require an owner account.'
            assert (await client.get('/account/tokens/api')).status == 401
            assert (await client.post('/account/tokens/api',headers=p.headers,json=['x'])).status == 400
            response = await client.post('/account/tokens/api',headers=p.headers,json=data)
            assert response.status == 200 and response.headers['Cache-Control'] == 'no-store'
            assert calls == [(p.headers['Cookie'].split('=',1)[1],data)]
            for path in ('/tokens', '/account/tokens'):
                response = await client.get(path, headers=p.headers, allow_redirects=False)
                assert response.status == 302 and response.headers['Location'] == '/#tokens'
            assert (await client.get('/account/tokens/assets/tokens.js')).status == 404


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
