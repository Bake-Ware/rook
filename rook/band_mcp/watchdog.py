"""Rook health watchdog: tell the operator on Telegram before Rook fails them.

Stdlib only, so it runs anywhere. Run it once a minute (systemd timer):

    python3 -m rook.band_mcp.watchdog --mode hub       # on the hub
    python3 watchdog.py --mode remote                   # on another machine

hub mode: an end-to-end MCP probe on 127.0.0.1 (initialize -> rook_whoami ->
DELETE), /healthz session-table counters and worker count, the hub services,
and free memory. When sessions are being evicted or refused it names the
busiest client (IP, token hash) and the busiest caller identity from the
journal. remote mode: the same probe through the public URL, so a dead hub or
tunnel is still reported.

Alerts go to Telegram (ROOK_WATCHDOG_TELEGRAM_TOKEN / _CHAT) once per
condition, repeat every ROOK_WATCHDOG_REPEAT_MIN while it lasts, and a
"recovered" follows when it clears. A probe must fail twice in a row before it
alerts, so a deploy restart doesn't page. State lives in --state.
Config comes from the environment (an EnvironmentFile), never arguments.
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

UA = 'rook-watchdog/1'
SERVICES = ('rook-band-mcp', 'rook-remote', 'telesthete-hub', 'cloudflared')


def env(name, default=''):
    return os.environ.get(name, default)


def http(url, method='GET', body=None, headers=None, timeout=15):
    """(status, headers, text); status 0 on a network failure."""
    h = {'User-Agent': UA, **(headers or {})}
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read().decode(errors='replace')
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode(errors='replace')
    except Exception as e:  # noqa: BLE001 - any failure is a failed probe
        return 0, {}, f'{type(e).__name__}: {e}'


def probe(url, token, host=None, timeout=15):
    """A real client round trip. Returns None if healthy, else what failed."""
    hdr = {'Authorization': f'Bearer {token}', 'Accept': 'application/json, text/event-stream'}
    if host:
        hdr['Host'] = host
    t0 = time.monotonic()
    st, h, txt = http(url, 'POST', {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'rook-watchdog', 'version': '1'}}}, hdr, timeout)
    sid = h.get('mcp-session-id') if st == 200 else None
    if not sid:
        return f'initialize failed: HTTP {st} {txt[:120]}'
    hdr['mcp-session-id'] = sid
    try:
        http(url, 'POST', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, hdr, timeout)
        st, _, txt = http(url, 'POST', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                        'params': {'name': 'rook_whoami', 'arguments': {}}}, hdr, timeout)
        if st != 200 or '"error"' in txt[:400] and '"result"' not in txt:
            return f'tools/call failed: HTTP {st} {txt[:120]}'
    finally:
        http(url, 'DELETE', None, hdr, timeout)
    slow = time.monotonic() - t0
    if slow > 10:
        return f'slow: round trip took {slow:.1f}s'
    return None


def busiest_identity(journal_db, window=300):
    try:
        db = sqlite3.connect(f'file:{journal_db}?mode=ro', uri=True, timeout=2)
        row = db.execute('SELECT actor, COUNT(*) FROM calls WHERE ts > ? GROUP BY actor '
                         'ORDER BY 2 DESC LIMIT 1', (time.time() - window,)).fetchone()
        return f'{row[0]} ({row[1]} calls in {window // 60} min)' if row else None
    except Exception:  # noqa: BLE001
        return None


def check_hub(state):
    """Conditions as {key: message}; also updates the worker baseline in state."""
    found = {}
    token = env('ROOK_MCP_STATIC_TOKEN')
    base = env('ROOK_WATCHDOG_MCP_URL', 'http://127.0.0.1:8765')
    host = env('ROOK_WATCHDOG_HOST')                # the MCP's public hostname (allowed-hosts check)
    if not host:
        return {'config': 'ROOK_WATCHDOG_HOST is not set (the MCP public hostname), so the hub cannot be checked'}
    for svc in SERVICES:
        r = subprocess.run(['systemctl', 'is-active', svc], capture_output=True, text=True)
        if r.stdout.strip() != 'active':
            found[f'svc:{svc}'] = f'{svc} is {r.stdout.strip() or "unknown"}'
    fail = probe(base + '/mcp', token, host)
    strikes = state.get('probe_strikes', 0) + 1 if fail else 0
    state['probe_strikes'] = strikes
    if fail and strikes >= 2:
        found['probe'] = f'MCP probe failing ({strikes} in a row): {fail}'
    st, _, txt = http(base + '/healthz', headers={'Authorization': f'Bearer {token}', 'Host': host})
    if st != 200:
        found['healthz'] = f'/healthz returned HTTP {st}'
        return found
    hz = json.loads(txt)
    mcp = hz.get('mcp', {})
    prev = state.get('mcp') or {}
    if prev.get('uptime_secs', 0) > mcp.get('uptime_secs', 0):
        prev = {}                                   # restarted: counters reset
    minutes = max(1.0, (time.time() - state.get('at', time.time() - 60)) / 60)
    refused = mcp.get('refused', 0) - prev.get('refused', 0)
    evicted = (mcp.get('evicted', 0) + mcp.get('key_evicted', 0)
               - prev.get('evicted', 0) - prev.get('key_evicted', 0))
    who = (f"top client {mcp.get('top_ips', [['?']])[0][0]}, token #{mcp.get('top_keys', [['?']])[0][0]}"
           if mcp.get('top_keys') else 'no recent sessions')
    ident = busiest_identity(env('ROOK_JOURNAL_DB', os.path.join(env('ROOK_DATA_DIR', '/var/lib/rook-band-mcp'), 'journal.db')))
    if refused > 0:
        found['refused'] = (f'MCP refused {refused} connection(s) in the last {minutes:.0f} min '
                            f"({mcp.get('sessions')}/{mcp.get('max_sessions')} sessions). {who}."
                            + (f' Busiest caller: {ident}.' if ident else ''))
    if evicted / minutes >= float(env('ROOK_WATCHDOG_EVICT_PER_MIN', '10')):
        found['leak'] = (f'A client is leaking MCP sessions: {evicted} evicted in {minutes:.0f} min. '
                         f'{who}.' + (f' Busiest caller: {ident}.' if ident else ''))
    workers = hz.get('workers', 0)
    base_w = state.get('workers_baseline')
    if mcp.get('uptime_secs', 0) > 180:            # roster refills ~60s after a restart
        if base_w and workers < base_w * 0.8:
            found['workers'] = f'Workers dropped to {workers} (usually {base_w})'
        if not base_w or workers > base_w:
            state['workers_baseline'] = workers
        elif workers >= base_w * 0.8:
            state['workers_baseline'] = round(base_w * 0.99 + workers * 0.01, 1)   # slow drift
    try:
        with open('/proc/meminfo') as f:
            mem = {l.split(':')[0]: int(l.split()[1]) for l in f}
        avail = mem['MemAvailable'] // 1024
        if avail < int(env('ROOK_WATCHDOG_MIN_MEM_MB', '80')):
            found['memory'] = f'Hub memory low: {avail} MB available'
    except Exception:  # noqa: BLE001
        pass
    state['mcp'] = mcp
    state['last'] = {'workers': workers, 'sessions': mcp.get('sessions'), 'refused': refused, 'evicted': evicted}
    return found


def check_remote(state):
    if not env('ROOK_WATCHDOG_MCP_URL'):
        return {'config': 'ROOK_WATCHDOG_MCP_URL is not set (e.g. https://mcp.example.com)'}
    fail = probe(env('ROOK_WATCHDOG_MCP_URL') + '/mcp',              # e.g. https://mcp.example.com
                 env('ROOK_MCP_STATIC_TOKEN'), timeout=20)
    strikes = state.get('probe_strikes', 0) + 1 if fail else 0
    state['probe_strikes'] = strikes
    return {'public': f'Rook MCP unreachable from outside ({strikes} checks in a row): {fail}'} \
        if fail and strikes >= 2 else {}


def telegram(text):
    tok, chat = env('ROOK_WATCHDOG_TELEGRAM_TOKEN'), env('ROOK_WATCHDOG_TELEGRAM_CHAT')
    if not tok or not chat:
        print('ALERT (telegram not configured):', text, file=sys.stderr)
        return False
    st, _, txt = http(f'https://api.telegram.org/bot{tok}/sendMessage', 'POST',
                      {'chat_id': chat, 'text': text, 'disable_web_page_preview': True})
    if st != 200:
        print(f'telegram send failed: HTTP {st} {txt[:200]}', file=sys.stderr)
    return st == 200


def notify(state, found, where, send=telegram):
    """Alert new conditions, repeat long-running ones, announce recoveries."""
    now = time.time()
    active = state.setdefault('active', {})
    repeat = float(env('ROOK_WATCHDOG_REPEAT_MIN', '60')) * 60
    for key, msg in found.items():
        a = active.get(key)
        if a is None:
            if send(f'⚠️ Rook [{where}]: {msg}'):
                active[key] = {'since': now, 'sent': now}
        elif now - a['sent'] >= repeat:
            mins = round((now - a['since']) / 60)
            if send(f'⚠️ Rook [{where}] still failing after {mins} min: {msg}'):
                a['sent'] = now
    for key in [k for k in active if k not in found]:
        mins = round((now - active[key]['since']) / 60)
        if send(f'✅ Rook [{where}] recovered: {key} (after {mins} min)'):
            del active[key]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--mode', choices=('hub', 'remote'), default='hub')
    ap.add_argument('--state', default=env('ROOK_WATCHDOG_STATE', '/var/lib/rook-watchdog/state.json'))
    ap.add_argument('--test-alert', action='store_true', help='send one Telegram message and exit')
    a = ap.parse_args(argv)
    where = env('ROOK_WATCHDOG_NAME', 'hub' if a.mode == 'hub' else 'external')
    if a.test_alert:
        return 0 if telegram(f'🔔 Rook watchdog [{where}] test message: alerts reach you.') else 1
    try:
        with open(a.state) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    try:
        found = check_hub(state) if a.mode == 'hub' else check_remote(state)
    except Exception as e:  # noqa: BLE001 - the watchdog itself must never go quiet
        found = {'watchdog': f'watchdog check crashed: {type(e).__name__}: {e}'}
    notify(state, found, where)
    state['at'] = time.time()
    os.makedirs(os.path.dirname(a.state) or '.', exist_ok=True)
    tmp = a.state + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f)
    os.replace(tmp, a.state)
    print(json.dumps({'found': found, **state.get('last', {})}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
