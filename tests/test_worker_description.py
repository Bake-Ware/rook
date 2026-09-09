import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from rook.worker import core, admin
from rook.worker.metadata import WorkerMetadata
from rook.band_mcp.client import BandClient
from rook.band_mcp.server import build_server
from rook.remote.bootstrap import CombinedServer


@pytest.fixture
def worker(tmp_path, monkeypatch):
    monkeypatch.setattr(core, '_WORKER_ID_FILE', tmp_path/'worker_id')
    monkeypatch.setattr(admin, '_PLUGIN_STATE', tmp_path/'plugins.json')
    monkeypatch.setattr(admin, '_CUSTOM_STATE', tmp_path/'custom_caps.json')
    transport = SimpleNamespace(send=AsyncMock())
    return core.Worker(transport, enabled=[], name='test-worker')


@pytest.mark.asyncio
async def test_description_persists_reannounces_and_clears(worker):
    value = '  Living-room\n tablet for music & Home Assistant.  '
    result = await worker.registry.call('worker.description_set', description=value)
    assert result == {'ok': True, 'description':'Living-room tablet for music & Home Assistant.', 'announced':True}
    announce = json.loads(worker.transport.send.call_args.args[0])
    assert announce['description'] == result['description']
    assert 'worker.description_set' in announce['caps']
    # A fresh worker/transport models reboot, bundle update, or band move.
    restarted = core.Worker(SimpleNamespace(send=AsyncMock()), enabled=[], name='renamed-worker')
    assert restarted.worker_id == worker.worker_id
    assert (await restarted.registry.call('worker.description_get'))['description'] == result['description']
    await restarted.announce()
    assert json.loads(restarted.transport.send.call_args.args[0])['description'] == result['description']
    await restarted.registry.call('worker.description_set', description='')
    assert WorkerMetadata(restarted.metadata.path).description == ''


@pytest.mark.asyncio
async def test_description_validation_and_failed_announcements(worker, monkeypatch):
    await worker.registry.call('worker.description_set', description='Original role')
    for invalid in (None, 123, ['role'], 'x'*281, 'bad\x00text'):
        with pytest.raises(ValueError):
            await worker.registry.call('worker.description_set', description=invalid)
    assert worker.metadata.description == 'Original role'
    worker.transport.send.side_effect = OSError('offline')
    result = await worker.registry.call('worker.description_set', description='New role')
    assert result['ok'] and result['announced'] is False
    assert WorkerMetadata(worker.metadata.path).description == 'New role'
    def fail(*args):raise OSError('disk full')
    monkeypatch.setattr('rook.worker.metadata.os.replace', fail)
    with pytest.raises(OSError):
        await worker.registry.call('worker.description_set', description='Not saved')
    assert worker.metadata.description == 'New role'
    assert WorkerMetadata(worker.metadata.path).description == 'New role'
    assert not list(worker.metadata.path.parent.glob('.metadata-*'))


@pytest.mark.asyncio
async def test_announce_description_reaches_mcp_and_web(worker, tmp_path, monkeypatch):
    worker.app_release = {'platform':'android','version':'0.4.0','code':4}
    await worker.registry.call('worker.description_set', description='Build host <&> release signing')
    message = json.loads(worker.transport.send.call_args.args[0])
    client = BandClient('test-band')
    client._handle_announce(message)
    entry = client.workers[worker.worker_id]
    assert entry['description'] == message['description']
    assert entry['app_release'] == worker.app_release
    mcp, _ = build_server(client, public_url='https://mcp.example.com', persist_path=str(tmp_path/'tokens.json'))
    result = await mcp.call_tool('rook_workers', {})
    blocks = result[0] if isinstance(result, tuple) else result
    assert json.loads(blocks[0].text)[0]['description'] == message['description']
    assert json.loads(blocks[0].text)[0]['app_release'] == worker.app_release
    monkeypatch.setenv('ROOK_SETUP_PATH',str(tmp_path/'setup.json'))
    monkeypatch.setenv('ROOK_ENROLLMENT_DB',str(tmp_path/'enrollment.db'))
    monkeypatch.setenv('ROOK_CHAT_DB',str(tmp_path/'chat.db'))
    from rook.remote import setup_store
    setup_store.save({'band_name':'test','band_psk':'test-band','hub_public':'hub.example.com:443','pyz_domain':'rook.example.com'})
    server = CombinedServer(band_psk='test-band')
    server._band = client
    async with TestClient(TestServer(server._app)) as http:
        response = await http.get('/api/band/workers', headers={'Accept':'application/json'})
        assert response.status == 200
        rows=await response.json()
        assert rows[0]['description'] == message['description']
        assert rows[0]['app_release'] == worker.app_release
    # Clearing, old workers, and malformed remote values must not retain stale text.
    for replacement in ('', None, {'invalid':True}):
        client._handle_announce({**message,'description':replacement})
        assert entry['description'] == ''
    client._handle_announce({k:v for k,v in message.items() if k!='description'})
    assert entry['description'] == ''
