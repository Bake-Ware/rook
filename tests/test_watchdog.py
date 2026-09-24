"""The watchdog alerts once per condition, repeats while it lasts, announces
recovery, ignores a single failed probe, and names the leaking client."""
import json
import subprocess
from types import SimpleNamespace

from rook.band_mcp import watchdog


def test_alert_repeat_and_recovery(monkeypatch):
    sent, clock = [], [1000.0]
    monkeypatch.setattr(watchdog.time, 'time', lambda: clock[0])
    monkeypatch.setenv('ROOK_WATCHDOG_REPEAT_MIN', '60')
    send = lambda text: sent.append(text) or True
    state = {}
    watchdog.notify(state, {'probe': 'MCP probe failing'}, 'hub', send)
    watchdog.notify(state, {'probe': 'MCP probe failing'}, 'hub', send)      # still failing: quiet
    assert len(sent) == 1 and 'MCP probe failing' in sent[0]
    clock[0] += 3601
    watchdog.notify(state, {'probe': 'MCP probe failing'}, 'hub', send)      # an hour on: reminder
    assert len(sent) == 2 and 'still failing after 60 min' in sent[1]
    watchdog.notify(state, {}, 'hub', send)
    assert 'recovered: probe' in sent[2] and state['active'] == {}
    # A failed send is retried next run rather than silently marked as sent.
    watchdog.notify(state, {'x': 'boom'}, 'hub', lambda t: False)
    assert 'x' not in state['active']


def hub(monkeypatch, healthz, probe_fail=None, services_ok=True):
    monkeypatch.setenv('ROOK_MCP_STATIC_TOKEN', 't')
    monkeypatch.setenv('ROOK_JOURNAL_DB', '/nonexistent')
    monkeypatch.setenv('ROOK_WATCHDOG_HOST', 'mcp.example.com')
    monkeypatch.setattr(watchdog, 'probe', lambda *a, **k: probe_fail)
    monkeypatch.setattr(watchdog, 'http', lambda url, *a, **k: (200, {}, json.dumps(healthz)))
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='active\n' if services_ok else 'failed\n'))


def test_single_probe_failure_does_not_page(monkeypatch):
    hz = {'workers': 30, 'mcp': {'uptime_secs': 900, 'refused': 0, 'evicted': 0, 'key_evicted': 0, 'sessions': 5, 'max_sessions': 128}}
    hub(monkeypatch, hz, probe_fail='initialize failed: HTTP 502')
    state = {}
    assert 'probe' not in watchdog.check_hub(state)          # a deploy restart
    assert 'probe' in watchdog.check_hub(state)              # still down a minute later
    hub(monkeypatch, hz)
    assert watchdog.check_hub(state) == {} and state['probe_strikes'] == 0


def test_leak_and_refusals_name_the_client(monkeypatch):
    base = {'uptime_secs': 900, 'refused': 0, 'evicted': 0, 'key_evicted': 0, 'sessions': 128, 'max_sessions': 128,
            'top_ips': [['2600:1700:10de:b000::43', 180]], 'top_keys': [['ab12cd34', 180]]}
    hub(monkeypatch, {'workers': 30, 'mcp': base})
    state = {}
    watchdog.check_hub(state)
    state['at'] = watchdog.time.time() - 60
    hub(monkeypatch, {'workers': 30, 'mcp': {**base, 'evicted': 40, 'refused': 3}})
    found = watchdog.check_hub(state)
    assert '40 evicted' in found['leak'] and '2600:1700:10de:b000::43' in found['leak'] and 'ab12cd34' in found['leak']
    assert 'refused 3' in found['refused']


def test_worker_drop_and_dead_service(monkeypatch):
    mcp = {'uptime_secs': 900, 'refused': 0, 'evicted': 0, 'key_evicted': 0}
    hub(monkeypatch, {'workers': 30, 'mcp': mcp})
    state = {}
    watchdog.check_hub(state)
    hub(monkeypatch, {'workers': 12, 'mcp': mcp}, services_ok=False)
    found = watchdog.check_hub(state)
    assert 'dropped to 12 (usually 30' in found['workers'] and 'svc:rook-remote' in found
    # Right after a restart the roster is still refilling: no worker alert.
    hub(monkeypatch, {'workers': 3, 'mcp': {**mcp, 'uptime_secs': 30}})
    assert 'workers' not in watchdog.check_hub(state)
