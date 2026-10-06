import asyncio
import hashlib
import json
import time

import pytest
from services.voice import providers
from services.voice.identity import Identity, current_identity, identity_for
from services.voice.workers import WorkerInventory


def test_mapping_is_credential_based():
    digest = hashlib.sha256(b'private').hexdigest()
    mappings = {digest: {'principal': 'Alex', 'worker': 'Alexphone', 'owner': True}}
    assert identity_for('private', mappings) == Identity('Alex', 'Alexphone', True)
    assert identity_for('wrong', mappings) == Identity()


@pytest.mark.parametrize('cap', ['sms.list', 'notify.list', 'calllog.list', 'contacts.search', 'location.get'])
@pytest.mark.parametrize('identity,target,allowed', [
    (Identity('Alex', 'Alexphone', True), 'Sams phone', True),
    (Identity('Sam', 'Sams phone'), 'Sams phone', True),
    (Identity('Sam', 'Sams phone'), 'Alexphone', False),
    (Identity(), 'Alexphone', False),
])
def test_personal_reads_enforced_before_mcp(monkeypatch, cap, identity, target, allowed):
    requests = []
    cache = WorkerInventory()
    cache.rows = [{'name': name, 'caps': [cap]} for name in ('Alexphone', 'Sams phone')]
    cache.updated = time.monotonic()
    monkeypatch.setattr(providers, 'inventory', cache)
    class MCP:
        async def call(self, name, args):
            requests.append(args)
            return json.dumps({'ok': True, 'result': []})
    monkeypatch.setattr(providers, 'RookMCP', MCP)
    async def scenario():
        token = current_identity.set(identity)
        try:
            if allowed:
                await providers.tool_rook_read({'worker': target, 'cap': cap})
            else:
                with pytest.raises(PermissionError, match='own device|Which device'):
                    await providers.tool_rook_read({'worker': target, 'cap': cap, 'owner': True})
            assert len(requests) == int(allowed)
        finally:
            current_identity.reset(token)
    asyncio.run(scenario())


def test_live_catalog_and_argument_validation(monkeypatch):
    cache = WorkerInventory()
    cache.rows = [{'name': 'Alexphone', 'caps': ['sms.list', 'shell.exec']}]
    cache.updated = time.monotonic()
    cache.schemas = {'Alexphone': {'sms.list': {'params': [{'name': 'limit', 'type': 'int', 'required': False}]}}}
    caps, description = cache.read_catalog(providers.READ_CAPS)
    assert caps == ['sms.list'] and 'Alexphone: sms.list(limit?)' in description
    assert 'shell.exec' not in description
    monkeypatch.setattr(providers, 'inventory', cache)
    async def scenario():
        token = current_identity.set(Identity('Alex', 'Alexphone', True))
        try:
            with pytest.raises(providers.Handoff, match='accepted: limit'):
                await providers.tool_rook_read({'worker': 'Alexphone', 'cap': 'sms.list', 'args': {'thread': 1}})
        finally:
            current_identity.reset(token)
    asyncio.run(scenario())


def test_job_inherits_connection_identity_without_cross_connection_leak(tmp_path, monkeypatch):
    from services.voice.jobs import Jobs
    from services.voice.state import Store
    async def scenario():
        seen = []
        ready = asyncio.Event()
        async def read(args):
            await ready.wait()
            seen.append(current_identity.get())
            return 'ok'
        store = Store(tmp_path/'identity.db')
        jobs = Jobs(store, {'rook_read': read}, '', 0, lambda *a: None)
        try:
            for principal in ('Alex', 'Sam'):
                token = current_identity.set(Identity(principal, principal+'phone', principal=='Alex'))
                jobs.start(principal, 'rook_read', {}, [])
                current_identity.reset(token)
            ready.set()
            await asyncio.gather(*list(jobs.tasks.values()))
            assert {i.principal for i in seen} == {'Alex', 'Sam'}
            assert sum(i.owner for i in seen) == 1
            assert current_identity.get() == Identity()
        finally:
            await jobs.close()
            store.db.close()
    asyncio.run(scenario())


def test_capability_failure_inside_successful_transport_is_readable(monkeypatch):
    cache = WorkerInventory()
    cache.rows = [{'name':'gpu-box','caps':['info.uptime']}]
    cache.updated = time.monotonic()
    monkeypatch.setattr(providers, 'inventory', cache)
    class MCP:
        async def call(self, *args):
            return json.dumps({'ok':True,'result':{'ok':False,'error':'Permission was denied'}})
    monkeypatch.setattr(providers, 'RookMCP', MCP)
    from services.voice.identity import Identity, current_identity
    async def read():
        current_identity.set(Identity('Alex', owner=True))
        await providers.tool_rook_read({'worker':'gpu-box','cap':'info.uptime'})
    with pytest.raises(providers.Handoff, match='Permission was denied'):
        asyncio.run(read())
