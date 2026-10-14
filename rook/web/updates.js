// Manage > Updates: what the hub, every worker, app and firmware device runs
// against what is available; release notes; the hub update; staged worker
// rollouts; the breaking-update gate; history and rollback. Everything goes
// through /account/updates/api (operator only; rook/hub/plugins/updates/web.py).
// Release notes and names are rendered as text.
const {h, useCss, btn, chips, dialog, copy, ago} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API='/account/updates/api';
const CSS=`
.up h1{margin:0 8px 0 0;font:500 20px var(--sans);color:var(--accent)}
.up-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.up-card{border:1px solid var(--line);background:var(--panel);padding:14px 16px;display:flex;flex-direction:column;gap:6px;min-width:0}
.up-card .k{font:500 22px var(--mono);color:var(--fg)}
.up-card .k.warn{color:var(--accent)}.up-card .k.good{color:var(--green)}.up-card .k.bad{color:var(--bad)}
.up-card small{color:var(--dim);font-size:12px;line-height:1.45}
.up-step{display:grid;grid-template-columns:32px minmax(0,1fr);gap:4px 12px;align-items:start}
.up-step>.n{width:28px;height:28px;display:grid;place-items:center;border:1px solid var(--line2);font:500 12px var(--mono);color:var(--dim)}
.up-step.now>.n{border-color:var(--accent);color:var(--accent)}
.up-step.done>.n{border-color:#3d5a33;color:var(--green)}
.up-step>div{display:flex;flex-direction:column;gap:8px;min-width:0}
.up-step h3{margin:4px 0 0;font:500 15px var(--sans)}
.up-step p{margin:0;color:var(--dim);font-size:13px;line-height:1.5}
.up-notes{max-height:240px}
.up-warn{border:1px solid #6b4336;background:#211512;padding:14px 16px;display:flex;flex-direction:column;gap:10px}
.up-warn h3{margin:0;font:500 15px var(--sans);color:var(--bad)}
.up-warn ul{margin:0;padding-left:18px;font-size:13px;line-height:1.55}
.up-warn label{display:flex;gap:10px;align-items:flex-start;font-size:13px;line-height:1.45;cursor:pointer}
.up-state{font:500 10px var(--mono);letter-spacing:1px;text-transform:uppercase;padding:2px 6px;border:1px solid var(--line2);color:var(--dim);white-space:nowrap}
.up-state.current,.up-state.done{border-color:#3d5a33;color:var(--green)}
.up-state.behind,.up-state.waiting,.up-state.pushing,.up-state.running{border-color:#514b35;color:var(--accent)}
.up-state.cannot,.up-state.failed,.up-state.stopped,.up-state.old{border-color:#6b4336;color:var(--bad)}
.up-state.held,.up-state.skipped,.up-state.cancelled,.up-state.unknown{border-style:dashed}
.up-note{color:var(--bad);font-size:12px;line-height:1.4;white-space:normal}
.up-prog{display:flex;flex-direction:column;gap:10px}
.up-bar{height:6px;background:var(--line);display:flex}.up-bar i{display:block;height:6px}
.up-bar .d{background:var(--green)}.up-bar .f{background:var(--bad)}.up-bar .w{background:var(--accent)}
.up-row-detail{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.up-hist{display:grid;grid-template-columns:110px 90px minmax(0,1fr);gap:4px 14px;padding:10px 0;border-top:1px solid #20261d;font-size:13px;align-items:baseline}
.up-hist small{color:var(--dim);font:11px var(--mono)}
.up-hist .d{color:#c9cdb8;line-height:1.45;overflow-wrap:anywhere}
.up-sel{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.up-warn .mg-two small,.up .mg-two small.up-note{white-space:normal}
@media(max-width:700px){.up-hist{grid-template-columns:1fr}.up-step{grid-template-columns:1fr}.up-step>.n{display:none}}
`;
function css(){
  useCss();
  if(!document.querySelector('style[data-up]')){const s=document.createElement('style');s.dataset.up='1';s.textContent=CSS;document.head.append(s);}
}

let csrf='';
async function get(params){
  const r=await fetch(API+'?'+new URLSearchParams(params||{}));
  const d=await r.json().catch(()=>({error:'Bad response'}));
  if(d.csrf)csrf=d.csrf;
  if(!r.ok||d.error)throw Error(d.error||'Request failed');
  return d;
}
async function post(body){
  if(!csrf)await get();
  const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});
  const d=await r.json().catch(()=>({error:'Bad response'}));
  if(!r.ok){const e=Error(d.error||'Request failed');e.data=d;throw e;}
  return d.result;
}

const STATE={current:'Up to date',behind:'Update available',cannot:'Update by hand',unknown:'Unknown',
  done:'Done',failed:'Failed',pushing:'Sending',waiting:'Restarting',pending:'Waiting',skipped:'Skipped',held:'Pinned',cancelled:'Not started',
  running:'In progress',stopped:'Stopped'};
const KIND={worker:'Worker',app:'App',firmware:'Firmware'};
const stateTag=(s,text)=>h('span',{class:'up-state '+s,text:text||STATE[s]||s});
const osLabel=o=>({linux:'Linux',windows:'Windows',darwin:'macOS',android:'Android',firmware:'Firmware'}[o]||o||'—');
const verOf=m=>m.kind==='app'?'app '+(m.app_version||'?')+(m.build?' · build '+m.build:''):m.kind==='firmware'?(m.fw_version||'unknown'):(m.build?'build '+m.build:'unknown');

export async function mountUpdates(root){
  css();
  const page=h('div',{class:'mg up'});root.replaceChildren(page);
  const status=h('p',{class:'mg-status',role:'status'});
  let o=null, tab='overview', timer=null, busy=false, picked=new Set(), sel={mode:'all',value:''}, canary='';
  let relTag=null, ackBox=false;
  const say=(t,bad)=>{status.className='mg-status'+(bad?' bad':'');status.textContent=t||'';};

  async function load(){o=await get({});return o;}
  async function act(body,done,button){
    if(busy)return;busy=true;if(button)button.disabled=true;say('Working…');
    try{const r=await post(body);await load(false);render();say(typeof done==='function'?done(r):done);}
    catch(e){say(e.message,true);if(e.data&&e.data.manual)manualDialog(e.message,e.data.manual);await load(false).catch(()=>{});render();}
    finally{busy=false;if(button)button.disabled=false;}
  }
  function manualDialog(why,cmd){
    const c=btn('Copy','',e=>copy(cmd,e.target));
    dialog('Update the hub by hand',h('p',{text:why}),h('div',{class:'mg-bar'},h('code',{class:'mg-code',text:cmd}),c));
  }

  // ---- header -------------------------------------------------------------
  function header(){
    const check=btn('Check now','',()=>act({action:'check'},'Checked for new releases.',check));
    const set=btn('Settings','',settingsDialog);
    const ck=o.check||{};
    const line=(o.mode==='off'?'Checking is off':'Checked '+ago(ck.ok_at||ck.checked_at))+
      ' · '+({off:'Off',notify:'Tell me',auto:'Install automatically'}[o.mode])+' · '+(o.channel==='prerelease'?'Test releases too':'Stable releases')+
      (o.mode==='auto'?' · window '+(o.window||'any time'):'');
    return [h('div',{class:'mg-bar'},h('h1',{text:'Updates'}),h('span',{class:'mg-sub',text:line}),h('span',{class:'mg-grow'}),set,check),
      ck.error?h('p',{class:'mg-error',text:'Last check failed: '+ck.error+'. Rook tries again later on its own.'}):null];
  }
  function settingsDialog(){
    let mode=o.mode;const modeRow=h('div',{class:'mg-actions'});
    const drawModes=()=>modeRow.replaceChildren(...chips([['off','Off'],['notify','Tell me'],['auto','Install automatically']],mode,m=>{mode=m;drawModes();}));
    drawModes();
    const ch=h('select',{'aria-label':'Release channel'},h('option',{value:'stable',text:'Stable releases',selected:o.channel==='stable'}),h('option',{value:'prerelease',text:'Stable and test releases',selected:o.channel==='prerelease'}));
    const win=h('input',{value:o.window||'',placeholder:'02:00-05:00','aria-label':'Maintenance window'});
    const save=btn('Save','primary',async()=>{d.close();await act({action:'settings',mode,channel:ch.value,window:win.value.trim()},'Update settings saved.');});
    const d=dialog('Update settings',
      h('p',{text:'Tell me: Rook checks for new releases and tells you here and in chat. Install automatically: Rook also installs them during the maintenance window, the hub first, then your workers one small group at a time. Updates that would cut off a device always wait for you.'}),
      h('div',{class:'mg-field'},h('span',{class:'mg-label',text:'When a new release comes out'}),modeRow),
      h('label',{class:'mg-field'},h('span',{class:'mg-label',text:'Releases'}),ch),
      h('label',{class:'mg-field'},h('span',{class:'mg-label',text:'Maintenance window (hub time)'}),win),
      h('div',{class:'mg-actions end'},save));
  }

  // ---- overview -------------------------------------------------------------
  function counts(kind){
    const ms=o.members.filter(m=>m.kind===kind);
    return {all:ms.length,behind:ms.filter(m=>m.state==='behind').length,cannot:ms.filter(m=>m.state==='cannot'||m.note).length};
  }
  function summaryCards(){
    const hub=o.hub.current, w=counts('worker'), a=counts('app'), f=counts('firmware');
    const card=(label,k,cls,sub)=>h('div',{class:'up-card'},h('span',{class:'mg-label',text:label}),h('span',{class:'k '+cls,text:k}),h('small',{text:sub}));
    return h('div',{class:'up-cards'},
      card('Hub',hub.version||hub.name||'dev build',o.hub.update_available?'warn':'good',o.hub.update_available?'Rook '+o.hub.latest.replace(/^v/,'')+' is available':'Up to date'+(hub.name?' · '+hub.name:'')),
      card('Workers',w.all?(w.all-w.behind)+' / '+w.all:'none',w.behind?'warn':'good',w.behind?w.behind+' can update to build '+o.feed.newest:(w.all?'All on the newest build':'No workers yet')),
      card('Phones and apps',a.all?(a.all-a.behind)+' / '+a.all:'none',a.cannot?'bad':a.behind?'warn':'good',a.cannot?a.cannot+' too old for this hub':a.behind?a.behind+' can update':'Nothing to do'),
      card('Firmware',f.all?(f.all-f.behind)+' / '+f.all:'none',f.behind?'warn':'good',f.behind?f.behind+' can be flashed with newer firmware':'Nothing to do'),
      card('Relay',o.relay.pinned||'—','', o.relay.latest&&o.relay.latest!==o.relay.pinned?'telesthete '+o.relay.latest+' exists; it comes with the Rook release that pins it':'Pinned by this Rook release'));
  }
  function progressBanner(){
    const out=[];
    if(o.hub_apply){
      const a=o.hub_apply;
      out.push(h('div',{class:'mg-box callout'},h('div',{class:'mg-pad'},
        h('div',{class:'mg-bar'},stateTag('running'),h('b',{text:'Updating the hub to '+a.version}),h('span',{class:'mg-sub',text:'started '+ago(a.started_at)+' by '+String(a.by||'').replace(/^human:/,'')})),
        h('p',{class:'mg-sub',text:'The hub restarts during the update, so this page may lose its connection for a minute. If the new release does not come up healthy, the hub goes back to '+(a.from_version||'the previous release')+' on its own.'}),
        a.log?h('pre',{text:a.log}):null)));
    }
    if(o.rollout&&o.rollout.status==='running')out.push(rolloutPanel(o.rollout));
    return out;
  }
  function releasePicker(rels){
    if(rels.length<2)return null;
    const s=h('select',{'aria-label':'Release',onchange:()=>{relTag=s.value;ackBox=false;render();}},
      rels.map(r=>h('option',{value:r.tag,text:r.tag+(r.prerelease?' (test release)':''),selected:r.tag===relTag})));
    return h('label',{class:'mg-field',style:'max-width:260px'},h('span',{class:'mg-label',text:'Release'}),s);
  }
  function breakingBox(rel){
    const a=rel.analysis;
    if(!a.breaking&&a.known)return null;
    if(!a.known)return h('div',{class:'up-warn'},h('h3',{text:'Rook cannot tell what this update changes'}),
      h('p',{class:'mg-sub',text:'Some update information is missing ('+a.note+'), so Rook cannot tell which devices it affects. Read the release notes first.'}),
      a.reasons&&a.reasons.length?h('ul',null,a.reasons.map(r=>h('li',{text:r}))):null,
      ackControls(rel,a.cut_off||[]));
    const fixFirst=a.cut_off.filter(c=>c.fix==='update_first');
    return h('div',{class:'up-warn'},
      h('h3',{text:'This update changes which devices Rook supports'}),
      a.reasons.length?h('ul',null,a.reasons.map(r=>h('li',{text:r}))):null,
      a.cut_off.length?h('p',{class:'mg-sub',text:a.cut_off.length+' of your devices would stop working with the new hub until they are updated:'}):h('p',{class:'mg-sub',text:'None of the devices on your band right now is affected.'}),
      a.cut_off.length?h('div',{class:'mg-box'},a.cut_off.map(c=>h('div',{class:'mg-row',style:'--cols:minmax(0,1fr)'},h('span',{class:'mg-two'},h('b',{text:c.name+(c.online===false?' (offline)':'')}),h('small',{class:'up-note',text:c.why}),h('small',{text:'Fix: '+c.how}))))):null,
      fixFirst.length?h('div',{class:'mg-actions'},btn('Update '+fixFirst.length+' worker'+(fixFirst.length===1?'':'s')+' first','soft',e=>act({action:'rollout',selection:{mode:'ids',value:fixFirst.map(c=>c.worker_id)}},'Updating those workers first; see the progress above.',e.target))):null,
      ackControls(rel,a.cut_off));
  }
  function ackControls(rel,cut){
    const a=rel.analysis;
    if(a.acked)return h('p',{class:'mg-sub',text:'Acknowledged by '+String(a.acked.by||'').replace(/^human:/,'')+' '+ago(a.acked.at)+'.'});
    if(a.known&&!a.breaking&&!cut.length)return null;
    const box=h('input',{type:'checkbox',checked:ackBox,onchange:()=>{ackBox=box.checked;b.disabled=!box.checked;}});
    const b=btn('Acknowledge','danger',e=>act({action:'acknowledge',tag:rel.tag},'Acknowledged. You can update the hub now.',e.target),{disabled:!ackBox});
    return h('div',{class:'mg-actions'},h('label',null,box,h('span',{text:cut.length?'I understand that '+cut.map(c=>c.name).join(', ')+' will stop working until '+(cut.length===1?'it is':'they are')+' updated.':'I have read the release notes and want to install it anyway.'})),b);
  }
  function hubStep(rel){
    const a=rel.analysis, blocked=(a.breaking||!a.known)&&!a.acked;
    const inst=o.hub.install;
    let action;
    if(o.hub_apply)action=h('p',{class:'mg-sub',text:'In progress.'});
    else if(rel.can_apply){
      action=h('div',{class:'mg-actions'},btn('Update the hub to '+rel.version,'primary',e=>{
        const d=dialog('Update the hub to '+rel.version+'?',h('p',{text:rel.apply_note}),
          h('div',{class:'mg-actions end'},btn('Cancel','',()=>d.close()),btn('Update now','primary',async()=>{d.close();await act({action:'apply_hub',tag:rel.tag},'The hub update has started.');})));
      },{disabled:blocked}),blocked?h('span',{class:'mg-sub',text:'Update or acknowledge the devices above first.'}):null);
    }else{
      const cmd=(inst.manual||'').replaceAll('{tag}',rel.tag).replaceAll('{version}',rel.version);
      action=h('div',{class:'mg',style:'gap:8px'},h('p',{class:'mg-sub',text:inst.why}),cmd?h('div',{class:'mg-bar'},h('code',{class:'mg-code',text:cmd}),btn('Copy','',e=>copy(cmd,e.target))):null);
    }
    return h('div',{class:'up-step now'},h('span',{class:'n',text:'1'}),h('div',null,
      h('h3',{text:'Update the hub'}),h('p',{text:'This hub runs '+(o.hub.current.version||o.hub.current.name||'a development build')+'. Your workers get their updates from the hub, so it goes first.'}),action));
  }
  function workerStep(n){
    const w=counts('worker');
    const pending=o.feed.newest>o.feed.released;
    return h('div',{class:'up-step '+(pending||w.behind?'now':'done')},h('span',{class:'n',text:String(n)}),h('div',null,
      h('h3',{text:'Update your workers'}),
      h('p',{text:w.behind?w.behind+' worker'+(w.behind===1?' is':'s are')+' behind the newest build on this hub ('+o.feed.newest+'). Rook updates one test worker first, then the rest a few at a time, and stops if one does not come back healthy.':'Every worker is on the newest build this hub has.'}),
      w.behind?h('div',{class:'mg-actions'},btn('Choose workers…','soft',()=>{tab='workers';render();})):null));
  }
  function overview(){
    const body=[summaryCards(),...progressBanner()];
    const rels=o.releases.filter(r=>r.newer);
    if(rels.length){
      if(!relTag||!rels.some(r=>r.tag===relTag))relTag=rels[0].tag;
      const rel=rels.find(r=>r.tag===relTag);
      body.push(h('div',{class:'mg-box strong'},h('div',{class:'mg-panel-head'},h('h2',{text:rel.title||('Rook '+rel.version)}),
          h('span',{class:'mg-actions'},rel.prerelease?stateTag('held','test release'):null,rel.analysis.breaking?stateTag('failed','breaking'):null,rel.html_url?h('a',{href:rel.html_url,target:'_blank',rel:'noopener',text:'On GitHub'}):null)),
        h('div',{class:'mg-pad'},releasePicker(rels),
          rel.notes?h('pre',{class:'up-notes quote',text:rel.notes}):h('p',{class:'mg-sub',text:'No release notes.'}),
          breakingBox(rel),hubStep(rel),workerStep(2))));
    }else{
      body.push(h('div',{class:'mg-box'},h('div',{class:'mg-pad'},h('h3',{style:'margin:0;font:500 15px var(--sans)',text:'The hub is up to date'}),
        h('p',{class:'mg-sub',text:o.mode==='off'?'Checking for new releases is off (Settings).':'Rook checks for new releases every few hours. Nothing newer than '+(o.hub.current.version||o.hub.current.name)+' is out.'}),
        workerStep(1))));
    }
    return body;
  }

  // ---- workers --------------------------------------------------------------
  function rolloutPanel(ro){
    const n=ro.targets.length, d=ro.targets.filter(t=>t.status==='done').length, f=ro.targets.filter(t=>t.status==='failed').length;
    const w=ro.targets.filter(t=>t.status==='waiting'||t.status==='pushing').length;
    const pct=x=>(100*x/Math.max(1,n))+'%';
    const stop=ro.status==='running'?btn('Stop rollout','danger',e=>act({action:'cancel'},'Stopping after the worker in progress.',e.target)):null;
    const rows=ro.targets.map(t=>h('div',{class:'mg-row',style:'--cols:minmax(0,1.2fr) 90px 110px minmax(0,2fr)'},
      h('span',{class:'mg-two'},h('b',{text:t.name}),h('small',{text:t.phase==='canary'?'test worker (first)':'build '+(t.from_build??'?')+' → '+ro.build})),
      h('span',{class:'mg-sub',text:t.phase==='canary'?'Canary':'Group'}),stateTag(t.status),h('span',{class:'up-row-detail',text:t.detail,title:t.detail})));
    return h('div',{class:'mg-box '+(ro.status==='running'?'callout':'')},h('div',{class:'mg-pad up-prog'},
      h('div',{class:'mg-bar'},stateTag(ro.status),h('b',{text:'Worker rollout to build '+ro.build}),h('span',{class:'mg-sub',text:d+' of '+n+' done'+(f?', '+f+' failed':'')}),h('span',{class:'mg-grow'}),stop),
      h('div',{class:'up-bar',role:'progressbar','aria-valuenow':String(d),'aria-valuemax':String(n)},h('i',{class:'d',style:'width:'+pct(d)}),h('i',{class:'f',style:'width:'+pct(f)}),h('i',{class:'w',style:'width:'+pct(w)})),
      ro.reason?h('p',{class:ro.status==='stopped'?'mg-error':'mg-sub',text:ro.reason}):null,
      h('div',{class:'mg-table',style:'--min:560px'},h('div',null,rows))));
  }
  function selection(){
    const ws=o.members.filter(m=>m.kind==='worker');
    const bands=[...new Set(ws.map(m=>m.band).filter(Boolean))], oses=[...new Set(ws.map(m=>m.os))];
    const by=h('div',{class:'up-sel'},...chips([['all','All workers'],['band','By band'],['os','By system'],['ids','Pick']],sel.mode,m=>{sel={mode:m,value:m==='band'?bands[0]:m==='os'?oses[0]:''};render();}));
    if(sel.mode==='band')by.append(h('select',{'aria-label':'Band',onchange:e=>{sel.value=e.target.value;render();}},bands.map(b=>h('option',{value:b,text:'Band '+b,selected:b===sel.value}))));
    if(sel.mode==='os')by.append(h('select',{'aria-label':'System',onchange:e=>{sel.value=e.target.value;render();}},oses.map(x=>h('option',{value:x,text:osLabel(x),selected:x===sel.value}))));
    return by;
  }
  function chosen(){
    const ws=o.members.filter(m=>m.kind==='worker');
    if(sel.mode==='all')return ws;
    if(sel.mode==='band')return ws.filter(m=>m.band===sel.value);
    if(sel.mode==='os')return ws.filter(m=>m.os===sel.value);
    return ws.filter(m=>picked.has(m.worker_id));
  }
  function workerRow(m){
    const inSel=chosen().includes(m);
    const pick=sel.mode==='ids'?h('input',{type:'checkbox',checked:picked.has(m.worker_id),'aria-label':'Update '+m.name,onchange:e=>{e.target.checked?picked.add(m.worker_id):picked.delete(m.worker_id);render();}}):h('span',{text:inSel?'●':'',class:'mg-warn'});
    const st=m.note?stateTag('old','Too old'):stateTag(m.state);
    return h('div',{class:'mg-row',style:'--cols:28px minmax(0,1.4fr) 90px 120px 130px'},pick,
      h('span',{class:'mg-two'},h('b',{text:m.name+(m.online?'':' (offline)')}),h('small',{class:m.note||m.rolled_back||m.pending_note?'up-note':'',text:m.note||m.rolled_back||(m.pending_note?'Update first: '+m.pending_note:'')||(m.band?'band '+m.band:'')})),
      h('span',{class:'mg-sub',text:osLabel(m.os)}),h('span',{class:'mg-mono',text:verOf(m)}),st);
  }
  function workersTab(){
    const ws=o.members.filter(m=>m.kind==='worker');
    const out=[];
    if(o.rollout)out.push(rolloutPanel(o.rollout));
    out.push(h('p',{class:'mg-sub',text:'Workers that check in on their own get build '+o.feed.released+' (released to everyone). The newest build on this hub is '+o.feed.newest+'. Pick who gets it: Rook updates one test worker first, then the rest '+'a few at a time, and stops if any worker does not come back healthy. Each worker also undoes the update by itself if it cannot reach the band.'}));
    out.push(selection());
    const targets=chosen().filter(m=>m.state==='behind'&&m.can_push);
    const can=h('select',{'aria-label':'Test worker',onchange:e=>{canary=e.target.value;}},h('option',{value:'',text:'Test worker: picked for me'}),targets.map(t=>h('option',{value:t.worker_id,text:'Test worker: '+t.name,selected:t.worker_id===canary})));
    const running=o.rollout&&o.rollout.status==='running';
    const start=btn(targets.length?'Update '+targets.length+' worker'+(targets.length===1?'':'s')+' to build '+o.feed.newest:'Nothing to update','primary',e=>act({action:'rollout',selection:sel.mode==='ids'?{mode:'ids',value:[...picked]}:sel,canaries:canary?[canary]:undefined},'Rollout started.',e.target),{disabled:!targets.length||running});
    const release=o.feed.newest>o.feed.released?btn('Release build '+o.feed.newest+' to everyone','',e=>{
      const d=dialog('Release build '+o.feed.newest+' to everyone?',h('p',{text:'Every worker that checks in moves to it on its own, without a test worker first. Use this after a partial rollout went well.'}),
        h('div',{class:'mg-actions end'},btn('Cancel','',()=>d.close()),btn('Release','primary',async()=>{d.close();await act({action:'release',build:o.feed.newest},'Released to everyone.');})));
    }):null;
    out.push(h('div',{class:'mg-actions'},targets.length?can:null,start,release));
    out.push(h('div',{class:'mg-box'},h('div',{class:'mg-table',style:'--min:620px'},h('div',null,
      h('div',{class:'mg-head',style:'--cols:28px minmax(0,1.4fr) 90px 120px 130px'},['','Worker','System','Version','Status'].map(x=>h('div',{text:x}))),
      ws.length?ws.map(workerRow):h('div',{class:'mg-empty',text:'No workers on the band.'})))));
    return out;
  }

  // ---- apps -----------------------------------------------------------------
  function appsTab(){
    const apps=o.members.filter(m=>m.kind==='app');
    const out=[h('p',{class:'mg-sub',text:'The Android app updates itself from this hub\'s app page (/apk), with Android asking the owner to confirm when it has to. Rook can only tell the app an update is waiting. Phones on an app older than 0.4.1 must install the new app by hand once.'})];
    out.push(h('div',{class:'mg-bar'},h('span',{class:'mg-sub',text:o.app?'Available here: Rook app '+o.app.version_name+' ('+(o.app.source==='release'?'from a release':'uploaded on the hub')+')':'No app file is published on this hub yet.'}),h('span',{class:'mg-grow'}),
      btn('Ask phones to update','soft',e=>act({action:'notify_apps'},r=>'Asked '+r.sent.length+' app'+(r.sent.length===1?'':'s')+' to update'+(r.skipped.length?'; not reached: '+r.skipped.join(', '):'')+'.',e.target),{disabled:!apps.some(a=>a.state==='behind'&&a.can_notify)})));
    for(const r of o.releases.filter(r=>r.has_apk).slice(0,1))
      out.push(h('div',{class:'mg-actions'},btn('Publish the app from '+r.tag,'',e=>act({action:'publish_apk',tag:r.tag,notify:true},'Published; phones were asked to update.',e.target))));
    out.push(h('div',{class:'mg-box'},h('div',{class:'mg-table',style:'--min:560px'},h('div',null,
      h('div',{class:'mg-head',style:'--cols:minmax(0,1.4fr) 140px 120px 130px'},['Device','Installed','Available','Status'].map(x=>h('div',{text:x}))),
      apps.length?apps.map(m=>h('div',{class:'mg-row',style:'--cols:minmax(0,1.4fr) 140px 120px 130px'},
        h('span',{class:'mg-two'},h('b',{text:m.name+(m.online?'':' (offline)')}),h('small',{class:m.note||m.pending_note?'up-note':'',text:m.note?m.note+'. Install the new app on it by hand.':m.pending_note?'Update first: '+m.pending_note:osLabel(m.platform)})),
        h('span',{class:'mg-mono',text:verOf(m)}),h('span',{class:'mg-mono',text:m.target||'—'}),m.note?stateTag('old','Too old'):stateTag(m.state))):h('div',{class:'mg-empty',text:'No phones or apps on the band.'})))));
    out.push(h('p',{class:'mg-sub',text:'Desktop app: it asks this hub for updates at /updates/desktop.json.'}));
    return out;
  }

  // ---- firmware -------------------------------------------------------------
  function firmwareTab(){
    const fw=o.members.filter(m=>m.kind==='firmware');
    return [h('p',{class:'mg-sub',text:'Rook shows firmware versions but does not flash devices: firmware images are not part of Rook releases yet. Flash a device by hand when it is behind.'}),
      h('div',{class:'mg-box'},h('div',{class:'mg-table',style:'--min:560px'},h('div',null,
        h('div',{class:'mg-head',style:'--cols:minmax(0,1.4fr) 110px 110px 130px'},['Device','Installed','Newest','Status'].map(x=>h('div',{text:x}))),
        fw.length?fw.map(m=>h('div',{class:'mg-row',style:'--cols:minmax(0,1.4fr) 110px 110px 130px'},
          h('span',{class:'mg-two'},h('b',{text:m.name}),h('small',{text:m.firmware==='rp2040'?'RP2040 USB dongle':'ESP32 KVM dongle'})),
          h('span',{class:'mg-mono',text:m.fw_version||'unknown'}),h('span',{class:'mg-mono',text:m.target||'—'}),stateTag(m.state))):h('div',{class:'mg-empty',text:'No firmware devices on the band.'})))),
      h('details',null,h('summary',{text:'How to flash firmware by hand'}),h('div',{class:'mg-pad'},
        h('p',{class:'mg-sub',text:'ESP32 dongle: open the device\'s own page (http://<device address>/config, with its admin password) and upload firmware.bin, or connect it by USB and run: pio run -d firmware -t upload'}),
        h('p',{class:'mg-sub',text:'RP2040 dongle: hold BOOT while plugging it in, then copy firmware.uf2 (pio run -d firmware-rp2040) to the RPI-RP2 drive.'})))];
  }

  // ---- history ---------------------------------------------------------------
  function historyTab(){
    const ev=o.history||[];
    const words={started:'Started',finished:'Finished',acknowledged:'Acknowledged',rollback:'Roll back',released:'Released',published:'Published',notified:'Asked to update',announced:'Announced',cancel:'Stop asked','auto-skipped':'Auto: waited'};
    const kinds={hub:'Hub',workers:'Workers',app:'App',notify:'Notice'};
    const out=[];
    const inst=o.hub.install;
    if(inst.can_apply&&(inst.kind==='rook-hub'||ev.some(e=>e.kind==='hub'&&e.event==='finished'&&e.result==='ok'))){
      out.push(h('div',{class:'mg-actions'},btn('Roll the hub back to its previous release','danger',e=>{
        const d=dialog('Roll the hub back?',h('p',{text:'The hub goes back to the release it ran before the last update and restarts. Your data is kept.'}),
          h('div',{class:'mg-actions end'},btn('Cancel','',()=>d.close()),btn('Roll back','danger',async()=>{d.close();await act({action:'rollback_hub'},'Rollback started.');})));
      })));
    }
    out.push(h('div',{class:'mg-box'},h('div',{class:'mg-pad',style:'gap:0'},
      ev.length?ev.map(e=>h('div',{class:'up-hist'},h('small',{text:new Date(e.ts*1000).toLocaleString(undefined,{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'})}),
        h('span',null,h('b',{text:kinds[e.kind]||e.kind}),' ',e.result?stateTag(e.result==='ok'?'done':e.result):null),
        h('span',{class:'d',text:[words[e.event]||e.event,e.tag||(e.build?'build '+e.build:''),e.from&&e.to?e.from+' → '+e.to:'',e.detail,e.by?'('+String(e.by).replace(/^human:/,'')+')':''].filter(Boolean).join(' · ')}))):h('p',{class:'mg-sub',text:'No updates yet.'}))));
    const dh=o.hub.history||[];
    if(dh.length)out.push(h('details',null,h('summary',{text:'Hub deploy log (rook hub history)'}),h('pre',{text:dh.map(x=>new Date(x.ts*1000).toISOString().slice(0,16)+'  '+x.event+' '+(x.result||'')+' '+Object.entries(x.services||{}).map(([n,c])=>n+' '+(c.from||'-')+'->'+(c.to||'-')).join(', ')).join('\n')})));
    return out;
  }

  async function render(){
    const tabs=h('div',{class:'mg-tabs',role:'tablist'});
    const w=counts('worker'),a=counts('app'),f=counts('firmware');
    for(const [id,label] of [['overview','Overview'],['workers','Workers'+(w.behind?' ('+w.behind+')':'')],['apps','Phones and apps'+(a.behind+a.cannot?' ('+(a.behind+a.cannot)+')':'')],['firmware','Firmware'+(f.behind?' ('+f.behind+')':'')],['history','History']])
      tabs.append(h('button',{type:'button',class:id===tab?'on':'',text:label,role:'tab','aria-selected':String(id===tab),onclick:()=>{tab=id;say('');render();}}));
    const body=h('div',{class:'mg',style:'gap:14px'});
    body.append(...({overview,workers:workersTab,apps:appsTab,firmware:firmwareTab,history:historyTab}[tab]()).filter(Boolean));
    page.replaceChildren(...header().filter(Boolean),tabs,status,body);
  }

  let ticks=0;
  function tick(){
    if(busy||!root.isConnected||document.hidden||document.querySelector('dialog[open]'))return;
    // Every 4 s while something runs, else about once a minute.
    const live=(o&&o.rollout&&o.rollout.status==='running')||(o&&o.hub_apply);
    if(!live&&++ticks%15)return;
    load(false).then(render).catch(()=>{});
  }
  return {
    async activate(){
      try{await load(false);await render();}
      catch(e){page.replaceChildren(h('h1',{text:'Updates'}),h('p',{class:'mg-error',text:e.message}));}
      clearInterval(timer);timer=setInterval(tick,4000);
    },
    deactivate(){clearInterval(timer);timer=null;},
  };
}
