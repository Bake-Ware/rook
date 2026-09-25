"""On-host production probe. Root reads existing credentials into memory only.

Emits only PID/RSS/time/latency/status scalars. Never emits HTTP bodies or secrets.
Run: sudo venv/bin/python band_mcp_probe.py --seconds 1800 --output /tmp/soak.jsonl
"""
import argparse
import json
import os
import subprocess
import time
import httpx

# Worker the probe round-trips shell.exec through (normally the hub host's own worker).
PROBE_WORKER = os.environ.get('ROOK_PROBE_WORKER', 'hub')


def pid():
    return subprocess.check_output(['systemctl','show','rook-band-mcp','-p','MainPID','--value'],text=True).strip()


def decode(response):
    response.raise_for_status()
    if 'text/event-stream' in response.headers.get('content-type',''):
        return json.loads([s[6:] for s in response.text.splitlines() if s.startswith('data: ')][-1])
    return response.json()


def run(seconds, output):
    proc = pid()
    env=dict(s.split('=',1) for s in open('/proc/'+proc+'/environ').read().split('\0') if '=' in s)
    token=env.get('ROOK_MCP_STATIC_TOKEN')
    if not token:
        entries=json.load(open(env.get('ROOK_MCP_PERSIST','/var/lib/rook-band-mcp/oauth.json')))['api_tokens']
        token=next(e['token'] for e in entries if not e.get('expires_at') or e['expires_at']>time.time())
    headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'}
    fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as out, httpx.Client(base_url='http://127.0.0.1:8765',headers=headers,timeout=20) as http:
        def emit(row):
            out.write(json.dumps({'utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),**row})+'\n');out.flush()
        start=time.monotonic(); sample_at=0; seq=0; sid=None; errors=0
        while time.monotonic()-start <= seconds:
            elapsed=time.monotonic()-start
            if elapsed>=sample_at:
                current=pid()
                fields={}
                for line in open('/proc/'+current+'/status'):
                    k,_,v=line.partition(':')
                    if k in ('VmRSS','VmHWM','VmSwap','Threads'): fields[k]=int(v.split()[0])
                emit({'type':'memory','elapsed':round(elapsed,2),'pid':int(current),**fields})
                sample_at+=60
            if sid is None:
                r=http.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'rook-memory-probe','version':'1'}}})
                decode(r);sid=r.headers['mcp-session-id']
                http.post('/mcp',headers={'mcp-session-id':sid},json={'jsonrpc':'2.0','method':'notifications/initialized'})
            name='rook_call' if seq%2 else 'rook_workers'
            args={'cap':'shell.exec','worker':PROBE_WORKER,'args':{'cmd':'printf memory-probe'},'timeout':10} if seq%2 else {}
            t=time.perf_counter();ok=False
            try:
                result=decode(http.post('/mcp',headers={'mcp-session-id':sid},json={'jsonrpc':'2.0','id':seq+2,'method':'tools/call','params':{'name':name,'arguments':args}}))
                body=result.get('result',{})
                ok='error' not in result and not body.get('isError')
                content=json.loads(body['content'][0]['text'])
                if name=='rook_call': ok=ok and content.get('ok') and content['result'].get('stdout')=='memory-probe'
                else: ok=ok and any(w.get('name')==PROBE_WORKER for w in content)
            except Exception:
                ok=False
            emit({'type':'call','tool':name,'ms':round((time.perf_counter()-t)*1000,3),'ok':bool(ok)})
            errors+=not ok;seq+=1
            if seq%60==0:
                if seq%120==0: http.delete('/mcp',headers={'mcp-session-id':sid})
                sid=None
            time.sleep(max(0,1-(time.perf_counter()-t)))
        if sid: http.delete('/mcp',headers={'mcp-session-id':sid})
        current=pid()
        fields={line.split(':')[0]:int(line.split(':')[1].split()[0]) for line in open('/proc/'+current+'/status') if line.startswith(('VmRSS:','VmHWM:','VmSwap:'))}
        emit({'type':'memory','elapsed':round(time.monotonic()-start,2),'pid':int(current),**fields})
        emit({'type':'complete','calls':seq,'errors':errors})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--seconds',type=int,default=1800);p.add_argument('--output',required=True)
    a=p.parse_args();run(a.seconds,a.output)
