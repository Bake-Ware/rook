"""Local OAuth callbacks work without allowing plaintext remote callbacks."""
import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from rook.band_mcp.oauth_shim import OAuthShim


@pytest.mark.parametrize('redirect', [
    'http://localhost:19876/mcp/oauth/callback',
    'http://127.0.0.1:19876/callback?existing=yes',
    'http://[::1]:19876/callback',
    'https://client.example/callback',
])
def test_callback_code_exchange(redirect):
    store = Mock()
    store.verify_bearer.return_value = object()
    shim = OAuthShim(Starlette(), store, 'https://rook.example')
    verifier = 'v' * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    with TestClient(shim) as client:
        response = client.get('/authorize', params={
            'response_type': 'code', 'redirect_uri': redirect,
            'code_challenge': challenge, 'code_challenge_method': 'S256',
            'client_id': 'opencode', 'state': 'test-state',
        }, follow_redirects=False)
        assert response.status_code == 302
        query = parse_qs(urlsplit(response.headers['location']).query)
        assert query['state'] == ['test-state']
        if '?' in redirect:
            assert query['existing'] == ['yes']
        form = dict(grant_type='authorization_code', code=query['code'][0],
                    redirect_uri=redirect, code_verifier=verifier,
                    client_id='opencode', client_secret='rook-test-token')
        assert client.post('/token', data={**form, 'redirect_uri': redirect + '/other'}).status_code == 400
        assert client.post('/token', data={**form, 'code_verifier': 'wrong'}).status_code == 400
        result = client.post('/token', data=form)
        assert result.status_code == 200
        assert result.json()['access_token'] == 'rook-test-token'
        store.verify_bearer.assert_called_with('rook-test-token')
        assert client.post('/token', data=form).status_code == 400


@pytest.mark.parametrize('redirect', [
    'http://example.com/callback', 'http://192.168.1.2/callback',
    'http://localhost.evil.example/callback', 'http://localhost@evil.example/callback',
    'http://evil.example@localhost/callback', 'http://localhost\\@evil.example/callback',
    'http://localhost:bad/callback', 'http://localhost:99999/callback',
    'http://[::1/callback', 'http://localhost/callback#fragment',
    'http://local\nhost/callback', 'https:///callback', '//localhost/callback', '',
])
def test_reject_invalid_or_remote_plaintext_callback(redirect):
    shim = OAuthShim(Starlette(), Mock(), 'https://rook.example')
    with TestClient(shim) as client:
        response = client.get('/authorize', params={
            'response_type': 'code', 'redirect_uri': redirect, 'code_challenge': 'challenge',
        }, follow_redirects=False)
    assert response.status_code == 400
    assert response.json()['error'] == 'invalid_request'
    assert not shim._codes
