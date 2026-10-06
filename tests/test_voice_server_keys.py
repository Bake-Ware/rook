"""Exercise live key reload and revocation through HTTP and WebSocket routes."""
import importlib
import json
import sys
import types
import uuid
import pytest
pytest.importorskip('fastapi')
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


@pytest.fixture
def server(tmp_path,monkeypatch):
    class Provider:
        voices=['test'];default_voice='test';system='test'
    monkeypatch.setitem(sys.modules,'services.voice.providers',types.SimpleNamespace(
        Provider=Provider,DIRECT_TOOLS={},ACP_HOST='',ACP_PORT=0))
    monkeypatch.setitem(sys.modules,'webrtcvad',types.SimpleNamespace(Vad=lambda level:None))
    monkeypatch.setitem(sys.modules,'uvicorn',types.SimpleNamespace())
    monkeypatch.setenv('VOICE_MODEL_DIR',str(tmp_path))
    monkeypatch.setenv('VOICE_ADMIN_DB',str(tmp_path/'admin.db'))
    monkeypatch.setenv('VOICE_STATE_DB',str(tmp_path/'state.db'))
    monkeypatch.setenv('VOICE_TOKEN','legacy-owner')
    monkeypatch.delenv('VOICE_IDENTITIES_FILE',raising=False)
    monkeypatch.delenv('VOICE_ALLOW_ANONYMOUS',raising=False)
    import services.voice.server as module
    module=importlib.reload(module)
    module.ADMIN.bootstrap('admin','test-password')
    with TestClient(module.app,base_url='https://voice.test') as client:
        login=client.post('/admin/api/login',json={'username':'admin','password':'test-password'},headers={'X-Voice-Admin':'1'})
        assert login.status_code==200
        headers={'X-Voice-Admin':'1','X-CSRF-Token':login.json()['csrf']}
        yield module,client,headers


def hello(ws):
    ws.send_json({'type':'hello','protocol':2,'conversation':str(uuid.uuid4()),'aec':True})
    assert ws.receive_json()['type']=='session'
    assert ws.receive_json()['type']=='state'


def test_revoke_disconnects_key_and_persists_over_legacy_import(server):
    module,client,headers=server
    with client.websocket_connect('/ws?token=legacy-owner') as ws:
        hello(ws)
        row=client.get('/admin/api/keys').json()['keys'][0]
        assert row['owner']
        assert client.delete('/admin/api/keys/'+row['id'],headers=headers).status_code==200
        with pytest.raises(WebSocketDisconnect):ws.receive_json()
    module.ADMIN.import_legacy(module.IDENTITIES)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws?token=legacy-owner'):pass


def test_created_key_is_live_and_guest_identity_stays_unprivileged(server,monkeypatch):
    module,client,headers=server
    result=client.post('/admin/api/keys',json={'label':'Phone','principal':'Alex','worker':'phone'},headers=headers).json()
    with client.websocket_connect('/ws?token='+result['token']) as ws:
        hello(ws)
        connection=next(iter(module.connections.values()))[0]
        assert connection.identity.worker=='phone' and not connection.identity.owner
    monkeypatch.setenv('VOICE_ALLOW_ANONYMOUS','1')     # guests allowed even though VOICE_TOKEN is set
    with client.websocket_connect('/ws') as ws:
        hello(ws)
        connection=next(iter(module.connections.values()))[0]
        assert not connection.identity.owner and connection.identity.worker is None
    with pytest.raises(WebSocketDisconnect):              # a wrong key is refused, not made a guest
        with client.websocket_connect('/ws?token=not-a-key') as ws: ws.receive_json()
    monkeypatch.delenv('VOICE_ALLOW_ANONYMOUS')
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/ws') as ws: ws.receive_json()


def test_revoke_cancels_jobs_after_socket_disconnect(server):
    module,client,headers=server
    result=client.post('/admin/api/keys',json={'label':'Owner','principal':'Alex','owner':True},headers=headers).json()
    with client.websocket_connect('/ws?token='+result['token']) as ws:
        hello(ws)
        session=next(iter(module.connections))
        job=client.portal.call(module.app.state.store.create_job,session,'test',{})
    assert session in module.credential_sessions[result['id']]
    cancelled=[]
    module.app.state.jobs.cancel=lambda session,jid:cancelled.append((session,jid))
    assert client.delete('/admin/api/keys/'+result['id'],headers=headers).status_code==200
    assert cancelled==[(session,job)]


def test_api_voice_follows_the_guest_rule(server,monkeypatch):
    module,client,headers=server
    body={'text':'Hello there.'}
    assert client.post('/api/voice',json=body).status_code==401                   # no guests configured
    monkeypatch.setenv('VOICE_ALLOW_ANONYMOUS','1')
    assert client.post('/api/voice',json=body).status_code!=401                   # keyless guest admitted
    bad=client.post('/api/voice',json=body,headers={'Authorization':'Bearer not-a-key'})
    assert bad.status_code==401                                                   # a wrong key is never a guest
    ok=client.post('/api/voice',json=body,headers={'Authorization':'Bearer legacy-owner'})
    assert ok.status_code!=401
