"""Controlled on-host old/fixed lifecycle A/B; real band, isolated ASGI stores.

No service changes. Existing PSK is read into memory only. Output is scalars.
"""
import asyncio
import importlib.util
import json
import logging
import os
import random
import secrets
import subprocess
import sqlite3
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path

import httpx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from rook.band_mcp.client import MultiBandClient
from rook.band_mcp.server import build_server


async def run(output):
    logging.disable(logging.CRITICAL)
    pid=subprocess.check_output(['systemctl','show','rook-band-mcp','-p','MainPID','--value'],text=True).strip()
    env=dict(s.split('=',1) for s in Path('/proc/'+pid+'/environ').read_text().split('\0') if '=' in s)
    with sqlite3.connect('file:'+env['ROOK_ENROLLMENT_DB']+'?mode=ro',uri=True) as db:
        psks=[r[0] for r in db.execute('SELECT psk FROM bands WHERE active=1')]
    client=MultiBandClient(psks)
    spec=importlib.util.spec_from_file_location('rook.band_mcp.legacy_probe_client','/opt/rook-releases/voice-20260910-84f2e66/rook/band_mcp/client.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    class Variant:
        def __init__(self,legacy):self.legacy=legacy
        @property
        def workers(self):return client.workers
        async def call(self,*a,**kw):
            if self.legacy:
                target_client=client._client_for(kw['target'])
                return await module.BandClient.call(target_client,*a,**kw)
            return await client.call(*a,**kw)
    await client.start()
    try:
        for _ in range(45):
            if any(w.get('name')=='bakenetcanada' for w in client.workers.values()):break
            await asyncio.sleep(1)
        else:raise RuntimeError('target worker unavailable to isolated probe')
        with tempfile.TemporaryDirectory() as d, os.fdopen(os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as out:
            async with AsyncExitStack() as stack:
                clients={}
                for variant in ['old','fixed']:
                    token=secrets.token_urlsafe(32);Path(d,variant).mkdir()
                    m,_=build_server(Variant(variant=='old'),public_url='http://localhost',static_token=token,journal_path=d+'/'+variant+'/journal.db')
                    if variant=='old':m._session_manager=StreamableHTTPSessionManager(m._mcp_server,security_settings=m.settings.transport_security)
                    app=m.streamable_http_app();await stack.enter_async_context(app.router.lifespan_context(app))
                    h=await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'}))
                    r=await h.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'controlled-probe','version':'1'}}})
                    h.headers['mcp-session-id']=r.headers['mcp-session-id']
                    await h.post('/mcp',json={'jsonrpc':'2.0','method':'notifications/initialized'})
                    clients[variant]=h
                rng=random.Random(20260921)
                for pair in range(100):
                    order=['old','fixed'];rng.shuffle(order)
                    for variant in order:
                        t=time.perf_counter();ok=False
                        r=await clients[variant].post('/mcp',json={'jsonrpc':'2.0','id':pair+2,'method':'tools/call','params':{'name':'rook_call','arguments':{'cap':'shell.exec','worker':'bakenetcanada','args':{'cmd':'printf controlled-probe'},'timeout':10}}})
                        ms=(time.perf_counter()-t)*1000
                        if r.status_code==200:
                            body=json.loads([s[6:] for s in r.text.splitlines() if s.startswith('data: ')][-1])['result']
                            reply=json.loads(body['content'][0]['text'])
                            ok=reply.get('ok') and reply.get('result',{}).get('stdout')=='controlled-probe'
                        out.write(json.dumps({'variant':variant,'pair':pair,'ms':round(ms,3),'ok':bool(ok)})+'\n');out.flush()
                        await asyncio.sleep(max(0,1-(time.perf_counter()-t)))
    finally:
        await client.stop()

if __name__=='__main__':
    import sys
    try:
        asyncio.run(run(sys.argv[1]))
    except Exception as error:
        import traceback
        Path(sys.argv[1]+'.error').write_text(json.dumps({'type':type(error).__name__,'sites':[f'{f.filename}:{f.lineno}' for f in traceback.extract_tb(error.__traceback__)]})+'\n')
        raise SystemExit(1)
