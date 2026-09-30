"""Voice reads its configuration from the hub (settings.fetch("voice")):
environment > hub > cached last-known-good > defaults, secrets in memory only,
restart-apply values frozen at start, and a schema that matches the code in
both directions."""
from __future__ import annotations

import ast
import asyncio
import importlib
import json
import logging
from pathlib import Path
import sys
import types

import httpx
import pytest

from rook.core.service_settings import DECISION, VOICE, VOICE_CLIENT_SETTINGS
from services.voice import config as vc
from services.voice.config import ServiceConfig

REPO = Path(__file__).resolve().parents[1]
VOICE_DIR = REPO / 'services' / 'voice'
DECLARED = {s.name for s in VOICE}
SECRET = 'model-key-' + 'x' * 12


def run(coro):
    return asyncio.run(coro)


# -- the schema matches the code ----------------------------------------------

def _reads() -> dict[str, set]:
    """Setting names each voice module reads: cfg("name") anywhere, and
    <config>.get("name") inside config.py."""
    out: dict[str, set] = {}
    for path in sorted(VOICE_DIR.glob('*.py')):
        if path.name == 'smoke.py':          # a client-side test tool, not the service
            continue
        tree = ast.parse(path.read_text())
        names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            f = node.func
            is_cfg = isinstance(f, ast.Name) and f.id == 'cfg'
            is_get = (path.name == 'config.py' and isinstance(f, ast.Attribute) and f.attr == 'get'
                      and isinstance(f.value, ast.Name) and f.value.id in ('self', 'via', 'CONFIG'))
            if not (is_cfg or is_get):
                continue
            arg = node.args[0]
            if path.name == 'config.py' and isinstance(arg, ast.Name):
                continue                     # the accessors themselves (cfg, source)
            assert isinstance(arg, ast.Constant) and isinstance(arg.value, str), \
                f'{path.name}:{node.lineno}: settings are read with a literal name'
            names.add(arg.value)
        out[path.name] = names
    return out


def test_every_setting_voice_reads_is_declared_and_every_declared_one_is_read():
    reads = _reads()
    read = set().union(*reads.values())
    undeclared = read - DECLARED
    assert not undeclared, f'read but not declared in rook/core/service_settings.py: {undeclared}'
    unread = DECLARED - VOICE_CLIENT_SETTINGS - read
    assert not unread, f'declared but never read by services/voice: {unread}'
    assert not (read & VOICE_CLIENT_SETTINGS), 'client-side settings are read by the app, not voice'


def test_voice_reads_no_environment_outside_config():
    for path in sorted(VOICE_DIR.glob('*.py')):
        if path.name in ('config.py', 'smoke.py'):
            continue
        text = path.read_text()
        assert 'os.environ' not in text and 'getenv' not in text, \
            f'{path.name} reads the environment; declare a setting and use cfg()'


def test_legacy_variables_still_work():
    names = {n for s in VOICE for n in s.env_names()}
    legacy = {'VOICE_BIND', 'VOICE_PORT', 'VOICE_TLS_KEY', 'VOICE_TLS_CERT', 'VOICE_TOKEN',
              'VOICE_ALLOW_ANONYMOUS', 'VOICE_MODEL_DIR', 'VOICE_STATE_DB', 'VOICE',
              'WHISPER_MODEL', 'WHISPER_DEVICE', 'WHISPER_COMPUTE', 'MIN_SPEECH_MS', 'MIN_RMS',
              'MAX_NO_SPEECH', 'MIN_LOGPROB', 'VLLM_URL', 'VLLM_MODEL', 'ACP_HOST', 'ACP_PORT',
              'ACP_AUTO_APPROVE', 'ROOK_MCP_URL', 'ROOK_MCP_TOKEN', 'ROOK_VOICE_ASSISTANT_NAME',
              'ROOK_VOICE_OWNER'}
    assert legacy <= names, legacy - names
    # Every setting also takes its canonical name.
    assert all(f'ROOK_VOICE_{s.name.upper()}' in s.env_names() for s in VOICE)
    assert all(f'ROOK_DECISION_{s.name.upper()}' in s.env_names() for s in DECISION)


def test_hub_schema_is_the_shared_declaration():
    from rook.hub.settings_schema import Schema
    schema = Schema(worker_package=None, hub_package=None)
    hub_voice = {e.setting.name for e in schema if e.namespace == 'voice'}
    assert hub_voice == DECLARED
    assert schema.get('voice.assistant_name').env_names()[0] == 'ROOK_VOICE_ASSISTANT_NAME'
    assert schema.get('voice.owner').env_names()[0] == 'ROOK_VOICE_OWNER'
    assert schema.get('voice.mcp_token').setting.bootstrap   # needed to reach the hub at all
    assert schema.get('decision.mode').setting.choices == ('off', 'shadow')


# -- resolution ----------------------------------------------------------------

def _reply(values: dict, stored=None, users=None) -> dict:
    out = {'namespace': 'voice', 'values': values, 'users': users or {}}
    if stored is not None:
        out['stored'] = stored
    return out


def _cfg(tmp_path, environ=None, replies=None) -> ServiceConfig:
    c = ServiceConfig('voice', VOICE, environ=environ or {}, cache_path=tmp_path / 'cache.json')
    queue = list(replies or [])

    async def fetcher():
        item = queue.pop(0) if queue else RuntimeError('hub unreachable')
        if isinstance(item, Exception):
            raise item
        return item
    c.fetcher = fetcher
    return c


def test_env_beats_hub_beats_default(tmp_path):
    c = _cfg(tmp_path, {'WHISPER_MODEL': 'medium.en', 'ROOK_VOICE_OWNER': 'Alex'},
             [_reply({'whisper_model': 'base.en', 'assistant_name': 'Ada', 'owner': 'Hub',
                      'tts_speed': 1.2, 'llm_api_key': SECRET},
                     stored=['whisper_model', 'assistant_name', 'owner', 'tts_speed', 'llm_api_key'])])
    run(c.load())
    assert c.resolve('whisper_model') == ('medium.en', 'env', 'WHISPER_MODEL')
    assert c.resolve('owner') == ('Alex', 'env', 'ROOK_VOICE_OWNER')
    assert c.resolve('assistant_name')[:2] == ('Ada', 'hub')
    assert c.resolve('tts_speed')[:2] == (1.2, 'hub')
    assert c.resolve('llm_api_key')[:2] == (SECRET, 'hub')
    assert c.resolve('stt_language')[:2] == ('en', 'default')
    snap = c.snapshot()
    assert SECRET not in json.dumps(snap) and snap['llm_api_key']['value'].startswith('***')


def test_values_the_hub_only_defaults_are_defaults(tmp_path):
    c = _cfg(tmp_path, replies=[_reply({'whisper_model': 'small.en', 'port': 1},
                                       stored=[])])
    run(c.load())
    assert c.resolve('whisper_model')[1] == 'default'
    assert c.get('port') == 8900              # bootstrap: never from the hub


def test_older_hub_without_stored_list(tmp_path):
    c = _cfg(tmp_path, replies=[_reply({'assistant_name': 'Ada', 'owner': None})])
    run(c.load())
    assert c.resolve('assistant_name')[:2] == ('Ada', 'hub')
    assert c.resolve('owner')[1] == 'default'


def test_invalid_values_fall_through(tmp_path, caplog):
    c = _cfg(tmp_path, {'VOICE_PORT': 'eighty', 'ROOK_VOICE_TTS_SPEED': '9'},
             [_reply({'tts_speed': 'fast', 'turn_silence_ms': 800},
                     stored=['tts_speed', 'turn_silence_ms'])])
    with caplog.at_level(logging.WARNING, logger='rook.voice.config'):
        run(c.load())
        assert c.get('port') == 8900 and c.get('tts_speed') == 1.0 and c.get('turn_silence_ms') == 800
        c.get('port')
    assert sum('VOICE_PORT' in r.getMessage() for r in caplog.records) == 1   # warned once


def test_cache_is_last_known_good_without_secrets(tmp_path):
    c = _cfg(tmp_path, replies=[_reply({'assistant_name': 'Ada', 'llm_api_key': SECRET},
                                       stored=['assistant_name', 'llm_api_key'])])
    run(c.load())
    cache = tmp_path / 'cache.json'
    assert cache.stat().st_mode & 0o777 == 0o600
    assert SECRET not in cache.read_text() and 'Ada' in cache.read_text()
    # Next start: the hub is down, the cache supplies the value, secrets are gone.
    c2 = _cfg(tmp_path, replies=[ConnectionError('refused')])
    run(c2.load())
    assert c2.resolve('assistant_name')[:2] == ('Ada', 'cache')
    assert c2.get('llm_api_key') is None
    assert 'ConnectionError' in c2.last_error
    # Once the hub answers, it is the truth: a key no longer stored is a default.
    c2.fetcher = _cfg(tmp_path, replies=[_reply({}, stored=[])]).fetcher
    changed = run(c2.refresh())
    assert 'assistant_name' in changed and c2.resolve('assistant_name')[:2] == ('Rook', 'default')


def test_no_token_means_environment_and_defaults_only(tmp_path):
    (tmp_path / 'voice-settings.json').write_text(json.dumps(
        {'namespace': 'voice', 'values': {'assistant_name': 'Stale'}}))
    c = ServiceConfig('voice', VOICE, environ={'VOICE_MODEL_DIR': str(tmp_path)})
    assert vc.connect_hub(c) is False
    run(c.load())
    assert c.resolve('assistant_name')[:2] == ('Rook', 'default')
    assert c.cache_path() == tmp_path / 'voice-settings.json'


def test_unreachable_hub_keeps_the_last_fetched_values(tmp_path, caplog):
    c = _cfg(tmp_path, replies=[_reply({'owner': 'Alex'}, stored=['owner']),
                                TimeoutError('slow'), TimeoutError('slow')])
    run(c.load())
    with caplog.at_level(logging.WARNING, logger='rook.voice.config'):
        assert run(c.refresh()) is None and run(c.refresh()) is None
    assert c.get('owner') == 'Alex'
    assert sum('could not fetch' in r.getMessage() for r in caplog.records) == 1


def test_restart_settings_are_frozen_and_reported_pending(tmp_path):
    c = _cfg(tmp_path, replies=[_reply({'whisper_model': 'base.en'}, stored=['whisper_model']),
                                _reply({'whisper_model': 'large-v3', 'owner': 'Alex'},
                                       stored=['whisper_model', 'owner'])])
    reports = []

    async def reporter(body):
        reports.append(body)
    c.reporter = reporter
    run(c.load())
    c.freeze()
    changed = run(c.refresh())
    assert set(changed) >= {'whisper_model', 'owner'}
    assert c.get('whisper_model') == 'base.en' and c.pending == {'whisper_model'}
    assert c.get('owner') == 'Alex'                       # live settings follow at once
    assert reports[-1]['pending'] == ['whisper_model']
    assert c.snapshot()['whisper_model']['pending'] == 'restart'


def test_report_names_env_locks_without_secret_values(tmp_path):
    c = _cfg(tmp_path, {'VOICE_TOKEN': 'client-secret', 'WHISPER_DEVICE': 'cuda'})
    body = c.report_body()
    assert body['env'] == {'token': 'VOICE_TOKEN', 'whisper_device': 'WHISPER_DEVICE'}
    assert body['values'] == {'whisper_device': 'cuda'}
    assert 'client-secret' not in json.dumps(body)


def test_refresh_loop_wakes_on_request(tmp_path):
    c = _cfg(tmp_path, replies=[_reply({'owner': 'A'}, stored=['owner']),
                                _reply({'owner': 'B'}, stored=['owner'])])
    seen = []

    async def scenario():
        await c.load()
        task = asyncio.create_task(c.run(interval=0, on_refresh=seen.append))
        for _ in range(100):
            if c._wake is not None:
                break
            await asyncio.sleep(0.01)
        c.request_refresh()                 # what SIGHUP does
        for _ in range(100):
            if seen:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())
    assert seen == [['owner']] and c.get('owner') == 'B'


# -- against the real hub service ------------------------------------------------

def _hub(tmp_path):
    from rook.hub.settings_schema import Schema
    from rook.hub.settings_service import SettingsService
    from rook.hub.settings_store import SettingsStore

    class Vault:
        def __init__(self):
            self.data = {}

        def set(self, name, value, description, actor):
            self.data[name] = value

        def get(self, name, actor, via='get', task=None):
            return self.data[name]

        def delete(self, name, actor):
            return self.data.pop(name, None) is not None
    svc = SettingsService(SettingsStore(tmp_path / 'settings.db'), Schema(), vault=Vault(),
                          environ={}, setup_loader=dict)
    svc.set('core.settings.service_readers', {'voice': ['voice-box']})
    return svc


PRINCIPAL = {'kind': 'agent', 'label': 'voice-box', 'agent_id': 'agent_v', 'verified': True}


def test_hub_edit_reaches_voice_on_refresh(tmp_path):
    svc = _hub(tmp_path)
    c = ServiceConfig('voice', VOICE, environ={'ROOK_VOICE_OWNER': 'EnvOwner'},
                      cache_path=tmp_path / 'cache.json')

    async def fetcher():
        return svc.fetch('voice', PRINCIPAL)

    async def reporter(body):
        return svc.report_service('voice', PRINCIPAL, body['env'], started_at=body['started_at'],
                                  pending=body['pending'], values=body['values'])
    c.fetcher, c.reporter = fetcher, reporter
    svc.set('voice.assistant_name', 'Ada', actor='human:operator')
    svc.set('voice.llm_api_key', SECRET, actor='human:operator')
    svc.set('voice.owner', 'HubOwner', actor='human:operator')
    run(c.load())
    c.freeze()
    assert c.resolve('assistant_name')[:2] == ('Ada', 'hub')
    assert c.get('llm_api_key') == SECRET
    assert c.resolve('owner')[:2] == ('EnvOwner', 'env')
    # The hub shows the env lock the voice process reported.
    row = svc.resolve('voice.owner')
    assert row['locked'] and row['env'] == 'ROOK_VOICE_OWNER' and row['value'] == 'EnvOwner'
    assert row['conflict']['hidden'] == 'hub'
    # Edit on the hub; voice picks it up on its next refresh.
    svc.set('voice.assistant_name', 'Bea', actor='human:operator')
    svc.set('voice.whisper_model', 'large-v3', actor='human:operator')
    changed = run(c.refresh())
    assert 'assistant_name' in changed and c.get('assistant_name') == 'Bea'
    assert c.get('whisper_model') == 'small.en' and c.pending == {'whisper_model'}
    page = svc.plugin_page('voice')
    assert page['runtime']['pending'] == ['voice.whisper_model']
    assert page['runtime']['reporter'] == 'voice-box'
    assert SECRET not in json.dumps(page)


def test_persona_follows_the_hub(tmp_path, monkeypatch):
    for mod in ('numpy', 'faster_whisper', 'kokoro_onnx'):
        if mod not in sys.modules:
            try:
                importlib.import_module(mod)
            except ImportError:
                stub = types.ModuleType(mod)
                stub.WhisperModel = stub.Kokoro = object
                monkeypatch.setitem(sys.modules, mod, stub)
    for var in ('ROOK_VOICE_ASSISTANT_NAME', 'ROOK_VOICE_OWNER'):
        monkeypatch.delenv(var, raising=False)
    import services.voice.providers as providers
    c = _cfg(tmp_path, replies=[_reply({'assistant_name': 'Ada', 'owner': 'Alex'},
                                       stored=['assistant_name', 'owner'])])
    monkeypatch.setattr(vc, 'CONFIG', c)
    assert providers.mouthpiece_system().startswith("You are Rook, the user's personal")
    run(c.load())
    assert providers.mouthpiece_system().startswith("You are Ada, Alex's personal voice assistant.")
    desc = {t['function']['name']: t['function']['description'] for t in providers.tools()}
    assert "Alex's Rook band" in desc['rook_devices']
    url, body, headers = providers.llm_request({'messages': []})
    assert body['model'] == 'qwopus3.6-35b-a3b-v1-mtp' and headers == {}


# -- over the MCP (rook_call -> settings.fetch) -----------------------------------

def test_fetch_and_report_go_through_rook_call(monkeypatch, tmp_path):
    from mcp.server.fastmcp import FastMCP
    from services.voice import rookmcp
    from services.voice.rookmcp import RookMCP
    calls = []
    mcp = FastMCP('hub')

    @mcp.tool()
    async def rook_call(cap: str, worker: str, args: dict | None = None) -> str:
        calls.append((cap, worker, args))
        if cap == 'settings.fetch':
            return json.dumps({'ok': True, 'id': 'j1', 'from': 'rook', 'result': _reply(
                {'assistant_name': 'Ada', 'llm_api_key': SECRET},
                stored=['assistant_name', 'llm_api_key'])})
        return json.dumps({'ok': False, 'error': 'nope'})
    app = mcp.streamable_http_app()
    Real = httpx.AsyncClient

    class Client(Real):
        def __init__(self, **kw):
            super().__init__(transport=httpx.ASGITransport(app=app), base_url='http://localhost:8000', **kw)
    monkeypatch.setattr(rookmcp.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(RookMCP, '_sid', None)
    c = ServiceConfig('voice', VOICE, environ={'ROOK_MCP_URL': 'http://localhost:8000/mcp',
                                               'ROOK_MCP_TOKEN': 'svc-token'},
                      cache_path=tmp_path / 'cache.json')
    assert vc.connect_hub(c)

    async def scenario():
        async with app.router.lifespan_context(app):
            await c.load()
    run(scenario())
    assert c.resolve('assistant_name')[:2] == ('Ada', 'hub') and c.get('llm_api_key') == SECRET
    assert calls[0] == ('settings.fetch', 'rook', {'namespace': 'voice'})
    assert calls[1][0] == 'settings.report'      # failed report: logged, not fatal
    monkeypatch.setattr(RookMCP, '_sid', None)
