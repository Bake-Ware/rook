"""Synthetic ASGI MCP churn; emits scalars/allocation sites, never credentials.

Run with PYTHONPATH=. python tools/diagnostics/band_mcp_memory.py.
Transport replies are synthetic; HTTP/session/auth/tool/store paths are real.
"""
import argparse
import asyncio
import gc
import json
import logging
import os
import secrets
import statistics
import tempfile
import time
import tracemalloc
from collections import Counter

import httpx
from rook.band_mcp.client import BandClient
from rook.band_mcp.server import build_server
from rook.band_mcp.oauth_shim import OAuthShim


def rss():
    with open('/proc/self/status') as f:
        return {k: int(v.split()[0]) for k, v in (l.split(':', 1) for l in f if ':' in l) if k in ('VmRSS', 'VmHWM')}


async def exercise(batches=5, sessions=40, trace=True, settle=0, idle=None):
    logging.disable(logging.CRITICAL)
    if trace:
        tracemalloc.start(5)
    with tempfile.TemporaryDirectory() as tmp:
        client = BandClient(secrets.token_hex(32))
        client._handle_announce({'worker_id': 'synthetic', 'name': 'synthetic', 'caps': ['echo']})
        async def send(payload):
            msg = json.loads(payload)
            mode = msg['args'].get('mode')
            if mode == 'failure':
                raise OSError('synthetic send failure')
            if mode != 'timeout':
                client._handle_reply({'id': msg['id'], 'from': 'synthetic', 'ok': True, 'result': 'x' * 8192})
        client.transport.send = send
        token = secrets.token_urlsafe(32)
        mcp, store = build_server(client, public_url='http://localhost', static_token=token, journal_path=tmp+'/journal.db')
        app = mcp.streamable_http_app()
        manager = mcp.session_manager
        if idle is not None:
            manager.idle_seconds = idle
        shim = OAuthShim(app, store, 'https://example.test')
        headers = {'Authorization': 'Bearer '+token, 'Accept': 'application/json, text/event-stream'}
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=shim), base_url='http://localhost', headers=headers) as http:
            samples = []
            gc.collect()
            base = tracemalloc.take_snapshot() if trace else None
            for batch in range(batches):
                timings=[]
                for n in range(sessions):
                    r = await http.post('/mcp', json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'memory-harness','version':'1'}}})
                    if r.status_code == 503:
                        continue
                    assert r.status_code == 200, r.status_code
                    sid=r.headers['mcp-session-id']
                    h={'mcp-session-id':sid}
                    await http.post('/mcp', headers=h, json={'jsonrpc':'2.0','method':'notifications/initialized'})
                    calls=[('rook_workers',{}),('rook_console_list',{}),('rook_chat_rooms',{}),('rook_console_read',{'room':'missing'}),('rook_chat_read',{'room':'missing'})]
                    calls += [('rook_call', {'cap':'echo','worker':'synthetic','args':{'mode':mode},'timeout':0.001}) for mode in ['ok']*5+['timeout','failure']]
                    for i,(name,args) in enumerate(calls,2):
                        t=time.perf_counter()
                        r=await http.post('/mcp', headers=h, json={'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':name,'arguments':args}})
                        assert r.status_code==200, r.status_code
                        timings.append((time.perf_counter()-t)*1000)
                    await http.get('/authorize', params={'response_type':'code','redirect_uri':'http://localhost/callback','code_challenge':'synthetic','client_id':'harness'})
                    if n % 2 == 0:
                        await http.delete('/mcp', headers=h)
                await asyncio.sleep(settle)
                gc.collect()
                row={'batch':batch+1, **rss(), 'sessions':len(manager._server_instances),'pending':len(client._pending),'oauth_codes':len(shim._codes),'tasks':len(asyncio.all_tasks()),'calls':len(timings),'p50_ms':statistics.median(timings) if timings else None,'p95_ms':sorted(timings)[int(.95*(len(timings)-1))] if timings else None}
                if trace:
                    row['traced_bytes']=tracemalloc.get_traced_memory()[0]
                samples.append(row)
                print(json.dumps(row), flush=True)
            if trace:
                diffs=tracemalloc.take_snapshot().compare_to(base,'lineno')[:10]
                print(json.dumps({'allocations':[{'site':str(s.traceback),'bytes':s.size_diff,'count':s.count_diff} for s in diffs]}),flush=True)
                import objgraph
                print(json.dumps({'objects':{t:objgraph.count(t) for t in ['StreamableHTTPServerTransport','ServerSession','Future','Task']},'ownership':'manager._server_instances -> transport; task group -> run_server -> ServerSession; client._pending -> Future'}),flush=True)
            return samples

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--batches',type=int,default=5)
    p.add_argument('--sessions',type=int,default=40)
    p.add_argument('--no-trace',action='store_true')
    p.add_argument('--idle',type=float)
    p.add_argument('--settle',type=float,default=0)
    a=p.parse_args()
    asyncio.run(exercise(a.batches,a.sessions,not a.no_trace,a.settle,a.idle))
