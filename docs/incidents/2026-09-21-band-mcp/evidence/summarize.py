"""Summarize scalar production probe data; no response bodies are accepted."""
import json
import statistics
from pathlib import Path

BASE=Path(__file__).resolve().parent

def load(name):
    return [json.loads(line) for line in (BASE/name).read_text().splitlines()]

def summary(rows):
    mem=[r for r in rows if r['type']=='memory']
    calls=[r for r in rows if r['type']=='call']
    result={'duration_seconds':mem[-1]['elapsed'],'samples':len(mem),
            'calls':len(calls),'errors':sum(not r['ok'] for r in calls),
            'pids':sorted({r['pid'] for r in mem}),
            'peak_rss_kib':max(r['VmRSS'] for r in mem),
            'peak_hwm_kib':max(r['VmHWM'] for r in mem),
            'max_swap_kib':max(r['VmSwap'] for r in mem),
            'calls_per_second':len(calls)/mem[-1]['elapsed']}
    for tool in ['rook_call','rook_workers']:
        vals=sorted(r['ms'] for r in calls if r['tool']==tool)
        result[tool]={'n':len(vals),'p50_ms':statistics.median(vals),'p95_ms':vals[int(.95*(len(vals)-1))]}
    recent=[r for r in mem if r['elapsed']>=mem[-1]['elapsed']-900]
    x=[r['elapsed']/3600 for r in recent];y=[r['VmRSS']/1024 for r in recent]
    mx,my=statistics.mean(x),statistics.mean(y)
    result['last15_rss_mib']={'min':min(y),'max':max(y),'slope_mib_per_hour':sum((a-mx)*(b-my) for a,b in zip(x,y))/sum((a-mx)**2 for a in x)}
    tail=[r for r in mem if r['elapsed']>=mem[-1]['elapsed']-600]
    result['last10_rss_plus_swap_mib']={'min':min((r['VmRSS']+r['VmSwap'])/1024 for r in tail),'max':max((r['VmRSS']+r['VmSwap'])/1024 for r in tail)}
    result['complete']=any(r['type']=='complete' for r in rows)
    return result

if __name__=='__main__':
    before=load('production-before.jsonl');after=load('production-final.jsonl')
    result={'before':summary(before),'after':summary(after)}
    (BASE/'production-summary.json').write_text(json.dumps(result,indent=2)+'\n')
    table=['| UTC | Elapsed min | PID | RSS MiB | HWM MiB | Swap MiB |','|---|---:|---:|---:|---:|---:|']
    for r in after:
        if r['type']=='memory':table.append(f"| {r['utc']} | {r['elapsed']/60:.2f} | {r['pid']} | {r['VmRSS']/1024:.2f} | {r['VmHWM']/1024:.2f} | {r['VmSwap']/1024:.2f} |")
    (BASE/'production-soak-table.md').write_text('\n'.join(table)+'\n')
    print(json.dumps(result,indent=2))
