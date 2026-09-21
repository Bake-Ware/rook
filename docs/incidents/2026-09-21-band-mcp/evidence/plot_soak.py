"""Plot scalar production memory data, including swap so RSS cannot mislead."""
import os
os.environ.setdefault('MPLCONFIGDIR','/tmp/rook-mpl-cache')
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

base=Path(__file__).resolve().parent
rows=[json.loads(l) for l in (base/'production-final.jsonl').read_text().splitlines()]
m=[r for r in rows if r['type']=='memory']
x=[r['elapsed']/60 for r in m]
rss=[r['VmRSS']/1024 for r in m]
swap=[r['VmSwap']/1024 for r in m]
fig,axes=plt.subplots(2,1,figsize=(9,6),sharex=True,gridspec_kw={'height_ratios':[2,1]})
axes[0].plot(x,rss,'o-',label='Process RSS',markersize=3,color='#2463a6')
axes[0].plot(x,[a+b for a,b in zip(rss,swap)],'--',label='RSS + process swap',color='#b06813')
axes[0].axhline(150_000_000/2**20,color='#708070',linestyle=':',label='Idle target 150 MB')
axes[0].set_ylim(0,170);axes[0].set_ylabel('MiB');axes[0].legend(loc='lower right');axes[0].grid(alpha=.2)
axes[0].set_title('rook-band-mcp — final 30-minute production soak\n450 MiB containment cap unchanged; historical OOM anon-RSS ~543–558 MB')
axes[1].plot(x,swap,'o-',color='#b06813',markersize=3,label='Process swap')
axes[1].set_ylabel('Swap MiB');axes[1].set_xlabel('Minutes since probe start');axes[1].grid(alpha=.2)
axes[1].text(.01,.92,'Host-pressure interval retained in evidence; one worker-call timeout.',transform=axes[1].transAxes,va='top',fontsize=9)
fig.tight_layout();fig.savefig(base/'production-soak.png',dpi=160);fig.savefig(base/'production-soak.svg');plt.close(fig)
