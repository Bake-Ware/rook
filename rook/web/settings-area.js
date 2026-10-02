// Settings: one area for hub, band, worker, plugin and personal settings,
// rendered from the hub's settings schema (settings.* on worker "rook").
// Every row shows the effective value, where it came from, what it hides and
// how a change takes effect. Secret values are write-only.
const el=(tag,txt,cls)=>{const e=document.createElement(tag);if(txt!==undefined&&txt!==null)e.textContent=txt;if(cls)e.className=cls;return e;};
const ago=t=>{if(!t)return 'never';const m=Math.round((Date.now()/1000-t)/60);return m<1?'just now':m<60?m+'m ago':m<1440?Math.round(m/60)+'h ago':Math.round(m/1440)+'d ago';};
const show=v=>v===null||v===undefined||v===''?'—':typeof v==='object'?JSON.stringify(v):String(v);
const API='/account/settings/api';
const CSS=`
.st-page h1{margin:0 8px 0 0;font:500 20px var(--sans);color:var(--accent)}
.st-page h2{font:500 15px var(--sans);margin:10px 0 0}
.st-group{border:1px solid var(--line);background:var(--panel);--cols:minmax(0,1.2fr) minmax(0,1.2fr) 130px 84px;min-width:0}
.st-row{display:grid;grid-template-columns:var(--cols);gap:4px 12px;align-items:center;min-height:56px;padding:6px 18px;border-bottom:1px solid #20261d}
.st-row:last-child{border-bottom:0}
.st-label{display:flex;flex-direction:column;min-width:0;font-size:13px;cursor:pointer}.st-label strong{font-weight:500}
.st-label small{font:11px var(--mono);color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st-ctl{display:flex;gap:6px;align-items:center;min-width:0}
.st-ctl input:not([type=checkbox]),.st-ctl select,.st-ctl textarea{flex:1 1 auto;min-width:0;width:100%;font-family:var(--mono)}
.st-ctl textarea{min-height:70px;font-size:12px}
.st-row.dirty .st-ctl input,.st-row.dirty .st-ctl select,.st-row.dirty .st-ctl textarea{border-color:var(--accent)}
.st-src{justify-self:start;padding:3px 8px;border:1px solid var(--dim);color:var(--dim);font:500 10px var(--mono);letter-spacing:1px;text-transform:uppercase;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st-src.out{border-color:var(--accent);color:var(--accent)}.st-src.here{border-color:var(--green);color:var(--green)}
.st-apply{font:11px var(--mono);color:var(--green)}.st-apply.restart{color:var(--accent)}
.st-more{grid-column:1/-1;display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--dim)}
.st-more:empty{display:none}
.st-more .st-help{display:none}.st-row:focus-within .st-help,.st-row.dirty .st-help,.st-row.open .st-help{display:block}
.st-conflict{font-size:12px;color:var(--warn);border-left:2px solid var(--warn);padding-left:8px}
.st-banner{border:1px solid var(--accent-dim);background:#1f1c12;padding:10px 14px;font-size:13px}
.st-banner li{margin:4px 0}
.st-page[data-filter=here] .st-row:not([data-here]),.st-page[data-filter=out] .st-row:not([data-out]),.st-page[data-filter=restart] .st-row:not([data-restart]){display:none}
.st-page[data-filter=here] .st-group:not(:has(.st-row[data-here])),.st-page[data-filter=out] .st-group:not(:has(.st-row[data-out])),.st-page[data-filter=restart] .st-group:not(:has(.st-row[data-restart])){display:none}
.st-group details>summary{padding:10px 18px}
.st-side details>summary{padding:10px 16px;font:500 10px var(--mono);letter-spacing:1px;text-transform:uppercase}
.st-side input[type=search]{width:100%;border-width:0 0 1px}
@media(max-width:800px){.st-group{--cols:minmax(0,1fr) 110px}.st-group>.mg-head{display:none}.st-row .st-label,.st-row .st-ctl{grid-column:1/-1}}
`;

export async function mountSettings(root){
  if(!document.querySelector('link[data-kn]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/account/knowledge/assets/knowledge.css';css.dataset.kn='1';document.head.append(css);}
  if(!document.querySelector('style[data-st]')){const s=document.createElement('style');s.dataset.st='1';s.textContent=CSS;document.head.append(s);}
  if(!document.querySelector('link[href*="manage.css"]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/account/bands/assets/manage.css';document.head.append(css);}
  let csrf='', admin=false, state={view:'',target:''}, overview=null, pending=[], saveButton=null;
  const layout=el('div',undefined,'mg-split');const side=el('aside',undefined,'mg-box mg-nav st-side');side.style.flex='0 1 240px';const page=el('section',undefined,'mg-main st-page');
  const shell=el('div',undefined,'mg');shell.append(layout);layout.append(side,page);root.replaceChildren(shell);
  const status=el('p','','mg-status');status.setAttribute('role','status');
  const FILTERS=[['all','All'],['here','Changed here'],['out','Set outside'],['restart','Needs restart']];
  function updateSave(){if(!saveButton)return;const n=pending.filter(x=>x.dirty()).length;saveButton.textContent='Save '+n+' change'+(n===1?'':'s');saveButton.disabled=!n;}
  async function saveAll(){
    const todo=pending.filter(x=>x.dirty());if(!todo.length)return;saveButton.disabled=true;let ok=0;const notes=[];
    for(const x of todo){try{notes.push(await x.save());ok++;}catch(e){x.err.textContent=e.message;x.box.classList.add('open');}}
    const failed=todo.length-ok;
    if(!failed){await render();status.textContent=notes.join(' · ')+'.';}
    else{status.textContent=ok+' saved, '+failed+' not saved: see the marked rows.';updateSave();}
  }
  // Page chrome: title and filters on top, then the body, then finish().
  function compose(title,meta,intro){
    pending=[];saveButton=null;page.dataset.filter='all';
    const bar=el('div',undefined,'mg-bar');bar.append(el('h1',title));
    const chipHost=el('span',undefined,'mg-actions st-filters');bar.append(chipHost);
    const pick=id=>{page.dataset.filter=id;if(id!=='all')page.querySelectorAll('.st-group details').forEach(d=>{d.open=true;});chipHost.replaceChildren(...FILTERS.map(([f,label])=>{const c=el('button',label,'mg-chip'+(f===id?' on':''));c.type='button';c.setAttribute('aria-pressed',String(f===id));c.onclick=()=>pick(f);return c;}));};
    pick('all');
    page.replaceChildren(bar);
    if(meta)page.append(el('p',meta,'mg-sub mg-mono'));
    if(intro)page.append(el('p',intro,'mg-sub'));
    page.append(status);
  }
  function finish(items){
    const hist=historyTable(items);hist.hidden=true;
    const foot=el('div',undefined,'mg-bar');foot.style.justifyContent='space-between';
    foot.append(el('span',pending.length?'Values set by env or a flag are locked here. Click a setting’s name for its help.':'','mg-sub'));
    const acts=el('div',undefined,'mg-actions');const hb=el('button','History','mg-btn');hb.type='button';hb.onclick=()=>{hist.hidden=!hist.hidden;if(!hist.hidden)hist.scrollIntoView({block:'nearest'});};acts.append(hb);
    if(pending.length){saveButton=el('button','','mg-btn primary');saveButton.type='button';saveButton.onclick=()=>saveAll().catch(e=>{status.textContent=e.message;});acts.append(saveButton);updateSave();}
    else page.querySelector('.st-filters').hidden=true;
    foot.append(acts);page.append(foot,hist);
  }

  async function get(params){const r=await fetch(API+'?'+new URLSearchParams(params));const d=await r.json().catch(()=>({error:'Bad response'}));if(d.csrf)csrf=d.csrf;if(d.admin!==undefined)admin=d.admin;if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const d=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}

  function go(view,target){state={view,target:target||''};const q=new URLSearchParams({view});if(target)q.set('target',target);history.replaceState(null,'','#settings?'+q);render().catch(e=>{page.replaceChildren(el('p',e.message,'kn-error'));});}
  function navItem(text,sub,view,target){const b=el('button',undefined,'mg-navitem');b.type='button';b.append(el('span',text));if(sub){b.append(el('small',sub));b.title=text+' · '+sub;}if(state.view===view&&(state.target||'')===(target||''))b.classList.add('on');b.onclick=()=>go(view,target);return b;}
  function link(text,hash){const a=el('a',text);a.href='#'+hash;a.className='kn-linkish';a.onclick=e=>{e.preventDefault();window.showView&&window.showView(hash);};return a;}

  function renderSide(){
    side.replaceChildren();
    const search=el('input');search.type='search';search.placeholder='Search every setting, e.g. ROOK_PORT';search.setAttribute('aria-label','Search settings');
    const results=el('div');
    let timer=null;search.oninput=()=>{clearTimeout(timer);timer=setTimeout(async()=>{results.replaceChildren();if(!search.value.trim())return;try{const d=await get({view:'search',q:search.value});for(const r of d.results){const b=navItem(r.label,r.env.length?r.env[0]:r.key,r.origin==='core'?(r.scope==='hub'?'hub':r.scope==='user'?'user':'overview'):(r.scope==='user'?'user':'plugin'),r.origin==='core'?'':r.namespace);results.append(b);}if(!d.results.length)results.append(el('div','No match.','mg-empty'));}catch(e){results.append(el('div',e.message,'mg-empty mg-bad'));}},200);};
    if(admin)side.append(search,results);
    if(admin)side.append(navItem('Overview','','overview'),navItem('Hub','','hub'),navItem('Persona','','persona'));
    side.append(navItem('My preferences','','user'));
    if(admin&&overview){
      const fold=(title,view,list,empty)=>{const d=el('details');d.open=state.view===view||list.length<=6;d.append(el('summary',title+' · '+list.length));for(const x of list)d.append(x);if(!list.length)d.append(el('div',empty,'mg-empty'));side.append(d);};
      fold('Bands','band',overview.bands.map(x=>navItem(x.name,x.primary?'primary':x.active?'':'revoked','band',x.id)),'No bands yet.');
      fold('Workers','worker',overview.workers.map(x=>navItem(x.name,x.typed_settings?'':'older build','worker',x.name)),'No workers online.');
      fold('Plugins & services','plugin',overview.plugins.map(x=>navItem(x.title,String(x.count),'plugin',x.namespace)),'None.');
    }
  }

  function badge(text,cls,title){const b=el('span',text,'st-src'+(cls?' '+cls:''));if(title)b.title=title;return b;}
  function sourceBadge(r){
    const s=r.source;if(s==='env')return (r.env||'').startsWith('-')?badge('flag','out','Set on the command line ('+r.env+'): it wins and locks this field.'):badge('env','out','Set in the process environment ('+(r.env||'')+'): it wins and locks this field.');
    if(s==='file')return badge('setup.json','out','From the legacy setup file; a saved value replaces it.');
    if(s==='enrollment')return badge('bands page','here','Managed in the enrollment database.');
    if(s===r.scope_here)return badge('saved','here','Stored at this scope.');
    if(s==='default')return badge('default','','Built-in default.');
    return badge('from '+s,'here','Inherited from the '+s+' value.');
  }

  function control(r){
    const sc=r.schema, t=sc.type;let input;
    const cur=r.value;
    if(sc.secret){input=el('input');input.type='password';input.autocomplete='new-password';input.placeholder=cur?'●●●●●● (set) · new value or {{secret:name}}':'value or {{secret:name}}';}
    else if(t==='bool'){input=el('input');input.type='checkbox';input.checked=!!cur;}
    else if(sc.choices){input=el('select');for(const c of sc.choices){const o=el('option',String(c));o.value=String(c);if(String(c)===String(cur))o.selected=true;input.append(o);}}
    else if(t==='int'||t==='float'){input=el('input');input.type='number';if(t==='float')input.step='any';if(sc.min!==undefined)input.min=sc.min;if(sc.max!==undefined)input.max=sc.max;input.value=cur??'';}
    else if(t==='dict'){input=el('textarea');input.value=JSON.stringify(cur??{},null,1);}
    else if(t==='list'){input=el('input');input.type='text';input.value=(cur||[]).join(', ');input.placeholder='comma separated';}
    else{input=el('input');input.type='text';input.value=cur??'';}
    input.setAttribute('aria-label',sc.label||r.key);
    const value=()=>{if(sc.secret)return input.value;if(t==='bool')return input.checked;if(t==='int')return input.value===''?null:parseInt(input.value,10);if(t==='float')return input.value===''?null:parseFloat(input.value);if(t==='dict'){return JSON.parse(input.value||'{}');}if(t==='list')return input.value.split(',').map(s=>s.trim()).filter(Boolean);return input.value;};
    return {input,value};
  }

  function row(r,{scope,target,onSaved}){
    const sc=r.schema||{};const box=el('div',undefined,'st-row');
    const keyline=r.key+((sc.env_names||[]).length?' · '+sc.env_names.join(', '):'')+(sc.flag?' · '+sc.flag:'');
    const lab=el('div',undefined,'st-label');lab.append(el('strong',sc.label||r.key),el('small',keyline));lab.title=keyline;lab.onclick=()=>box.classList.toggle('open');
    box.append(lab);
    if(r.error){const e=el('span',r.error,'mg-error');e.style.gridColumn='2/-1';box.append(e);return box;}
    const ctl=el('div',undefined,'st-ctl');const more=el('div',undefined,'st-more');const err=el('span','','mg-error');
    const editable=r.editable&&!r.locked;
    const {input,value}=control(r);input.disabled=!editable;ctl.append(input);
    if(editable){
      let initial='';try{initial=JSON.stringify(value());}catch{}
      const dirty=()=>{if(sc.secret)return !!input.value;try{return JSON.stringify(value())!==initial;}catch{return true;}};
      pending.push({box,err,dirty,async save(){err.textContent='';let v;try{v=value();}catch(e){throw Error('Not valid JSON: '+e.message);}const d=await post({action:'set',key:r.key,scope,target,value:v});return (sc.label||r.key)+': '+(d.note||'saved');}});
      const mark=()=>{box.classList.toggle('dirty',dirty());updateSave();};
      input.addEventListener('input',mark);input.addEventListener('change',mark);
      if(r.set_here){const reset=el('button','↺','mg-btn icon');reset.type='button';reset.title='Reset to the inherited value: '+show(r.inherited);reset.setAttribute('aria-label','Reset '+(sc.label||r.key)+' to the inherited value');reset.onclick=async()=>{try{const d=await post({action:'reset',key:r.key,scope,target});await onSaved();status.textContent=(sc.label||r.key)+' reset: '+(d.note||'')+'.';}catch(e){err.textContent=e.message;}};ctl.append(reset);}
    }
    const restart=(sc.apply&&sc.apply!=='live')||r.pending;
    const outside=r.locked||r.source==='env'||r.source==='file';
    if(r.set_here)box.dataset.here='1';if(outside)box.dataset.out='1';if(restart)box.dataset.restart='1';
    const apply=el('span',r.pending?'restart pending':sc.apply&&sc.apply!=='live'?sc.apply:'now','st-apply'+(restart?' restart':''));
    apply.title=r.pending?'Saved, but the process reads it only at start.':'How a change takes effect.';
    box.append(ctl,sourceBadge(r),apply,more);
    more.append(err);
    if(r.conflict)more.append(el('span','⚠ '+r.conflict.note,'st-conflict'));
    for(const bad of r.invalid||[])more.append(el('span','Ignored an invalid '+bad.source+' value: '+bad.error,'st-conflict'));
    if(r.locked){const flag=(r.env||'').startsWith('-');const under=(r.layers||[]).filter(l=>l.source!=='env');more.append(el('span',(r.env||'The environment')+' wins. Stored underneath: '+(under.length?under.map(l=>l.source+' = '+show(l.value)).join(', '):'nothing')+'. '+(flag?'Remove the flag from the '+(sc.owner||'')+' command line':'Remove the variable from the '+(sc.owner||'')+' environment')+' to use it.','st-help'));}
    else if(r.managed==='enrollment'){const s=el('span','Rotate or revoke this key on the Bands page. ','st-help');s.append(link('Open Bands','bands'));more.append(s);}
    else if(!r.editable&&sc.bootstrap){more.append(el('span','Set '+((sc.env_names||[])[0]||sc.flag||'it')+' in the '+sc.owner+' environment; restart to apply.','st-help'));}
    const facts=[sc.help,sc.secret&&r.fingerprint?'Fingerprint '+r.fingerprint+'.':'',sc.bootstrap?'Bootstrap setting: read before the settings store is reachable.':'',sc.deprecated?'Deprecated: '+sc.deprecated:''].filter(Boolean).join(' ');
    if(facts)more.append(el('span',facts,'st-help'));
    return box;
  }

  function groups(d,opts){const out=[];for(const g of d.groups||[]){const box=el('section',undefined,'st-group');const head=el('div',undefined,'mg-head');for(const c of [g.name,'Value','Comes from','Applies'])head.append(el('div',c));box.append(head);const vis=g.rows.filter(r=>!(r.schema&&r.schema.advanced));const adv=g.rows.filter(r=>r.schema&&r.schema.advanced);for(const r of vis)box.append(row(r,opts));if(adv.length){const det=el('details');det.append(el('summary','Show advanced ('+adv.length+')'));for(const r of adv)det.append(row(r,opts));box.append(det);}out.push(box);}return out;}

  function historyTable(items){
    const wrap=el('div',undefined,'kn-history');wrap.append(el('h2','History'));
    if(!items||!items.length){wrap.append(el('p','No changes recorded yet.','kn-dim'));return wrap;}
    const t=el('table',undefined,'kn-table');const h=el('tr');for(const c of ['When','Who','Setting','Scope','Change','Note'])h.append(el('th',c));t.append(h);
    for(const x of items){const tr=el('tr');tr.append(el('td',new Date(x.at*1000).toLocaleString()),el('td',x.actor),el('td',x.key),el('td',x.scope+(x.target?':'+x.target:'')),el('td',show(x.old)+' → '+(x.new===null?'(reset)':show(x.new))),el('td',(x.note||'')+(x.source?' ['+x.source+']':'')));t.append(tr);}
    const s=el('div',undefined,'kn-scroll');s.append(t);wrap.append(s);return wrap;
  }

  function conflictsBanner(list){
    if(!list||!list.length)return null;const b=el('div',undefined,'st-banner');b.append(el('strong',list.length+' setting'+(list.length>1?'s are':' is')+' overridden by the environment'));
    const ul=el('ul');for(const c of list){ul.append(el('li',(c.label||c.key||'')+' ('+(c.owner||'')+'): '+c.note));}b.append(ul);return b;
  }

  async function render(){
    if(!state.view)state.view=admin?'overview':'user';
    renderSide();
    const {view,target}=state;let d;
    if(view==='overview'){d=await get({view});overview=d;renderSide();
      compose('Overview','','Every setting in one place, with where each value comes from. The environment wins over this page, and says so.');
      const c=conflictsBanner(d.conflicts);if(c)page.append(c);
      const rl=d.relay||{};page.append(el('h2','Relay'));
      const rp=el('p','',rl.reachable===false?'st-conflict':'kn-dim');rp.textContent='The MCP dials '+(rl.mcp||'?')+(rl.dashboard?', the dashboard '+rl.dashboard:'')+'. '+(rl.reachable===true?'Reachable: '+rl.peers+' peer(s) seen.':rl.reachable===false?'No peers seen through it yet: check the relay and its address.':'')+' The relay has its own environment (HUB_*): shown on its page, not managed here.';page.append(rp);
      if(rl.note)page.append(el('p',rl.note,'st-conflict'));
      page.append(el('h2','Hub processes'));const t=el('table',undefined,'kn-table');const hr=el('tr');for(const x of ['Process','Started','Reported','Chat database','Settings file'])hr.append(el('th',x));t.append(hr);
      for(const [name,rep] of Object.entries(d.runtime||{})){if(name.startsWith('worker:'))continue;const tr=el('tr');tr.append(el('td',name),el('td',rep.started_at?ago(rep.started_at):'—'),el('td',ago(rep.updated_at)),el('td',(rep.stores||{}).chat_db||'—'),el('td',(rep.stores||{}).settings_db||'—'));t.append(tr);}
      page.append(t);const mc=(d.runtime||{}).mcp,dc=(d.runtime||{}).dashboard;
      if(mc&&dc&&(mc.stores||{}).chat_db&&(dc.stores||{}).chat_db&&mc.stores.chat_db!==dc.stores.chat_db)page.append(el('p','⚠ The dashboard and the MCP use different chat databases; set ROOK_CHAT_DB (or ROOK_DATA_DIR) the same for both.','st-conflict'));
      if(!mc)page.append(el('p','The MCP server has not reported yet.','kn-dim'));if(!dc)page.append(el('p','The dashboard has not reported yet (restart it to record its environment).','kn-dim'));
      page.append(el('h2','Plugins & services'));const pl=el('div',undefined,'kn-sections');for(const p of d.plugins){const b=el('button',undefined,'kn-card');b.type='button';b.append(el('strong',p.title),el('small',p.count+' settings · runs in '+p.owner));b.onclick=()=>go('plugin',p.namespace);pl.append(b);}page.append(pl);
      finish(d.history);return;}
    if(view==='hub'){d=await get({view});compose('Hub');page.append(...groups(d,{scope:'hub',target:'',onSaved:render}));finish(d.history);return;}
    if(view==='band'){d=await get({view,target});const b=d.band;compose('Band: '+b.name,b.label+' · key version '+b.epoch+(b.primary?' · primary':'')+(b.active?'':' · revoked'),'Worker defaults apply to every worker on this band unless a worker overrides them. Apply them from each worker page.');page.append(...groups(d,{scope:'band',target,onSaved:render}));finish(d.history);return;}
    if(view==='plugin'){d=await get({view,target});compose(d.title,'runs in '+d.owner+' · '+d.origin.replace('-',' '));
      if(d.owner.startsWith('service:')){const c=el('div',undefined,'kn-callout');c.append(el('strong','Separate service'),el('span','It reads these with settings.fetch("'+d.namespace+'") on worker rook, using a token listed in core.settings.service_readers.'+d.namespace+'. Readers now: '+(d.service_readers.length?d.service_readers.join(', '):'none')+'. Its own environment still wins.'));page.append(c);}
      if(d.origin==='worker-plugin')page.append(el('p','Per-worker settings: set them on each worker page (Workers in the sidebar).','kn-dim'));
      page.append(...groups(d,{scope:'hub',target:'',onSaved:render}));
      if(d.scoped&&d.scoped.length){page.append(el('h2','Set per band, worker or user'));const t=el('table',undefined,'kn-table');const h=el('tr');for(const c of ['Setting','Home scope','Overrides'])h.append(el('th',c));t.append(h);for(const s of d.scoped){const tr=el('tr');tr.append(el('td',s.label+' ('+s.key+')'),el('td',s.scope),el('td',Object.entries(s.overrides).map(([k,v])=>v+' '+k).join(', ')||'—'));t.append(tr);}page.append(t);}
      finish(d.history);return;}
    if(view==='user'){d=await get({view:'user'});compose('My preferences','','Yours on every device that signs in with this account. A device can still keep its own value.');page.append(...groups(d,{scope:'user',target:d.user,onSaved:render}));finish(d.history);return;}
    if(view==='worker'){d=await get({view,target});const live=d.live||{};
      compose('Worker: '+d.title,(live.online?'● online':'○ offline')+(live.version?' · '+live.version:'')+(live.band?' · band '+live.band:''));
      if(live.online&&!live.typed_settings)page.append(el('div','This worker runs an older build: it takes plain settings, but secrets are not pushed to it (they would land on its disk). Update it to push secrets, which it then fetches at use.','st-banner'));
      // plugins
      page.append(el('h2','Plugins'));
      const plist=(live.plugins||{}).plugins||[];
      if(!plist.length)page.append(el('p',live.plugins_error||(live.online?'This worker did not list its plugins.':'Offline.'),'kn-dim'));
      else{const t=el('table',undefined,'kn-table');const h=el('tr');for(const c of ['Plugin','State','Why',''])h.append(el('th',c));t.append(h);
        for(const p of plist){const tr=el('tr');const st=p.loaded?'on':p.disabled?'off':'not loaded';
          const act=el('td');act.style.whiteSpace='nowrap';
          if(!['selfupdate','config'].includes(p.module)){const b=el('button',p.loaded?'Disable':'Enable');b.type='button';b.onclick=async()=>{b.disabled=true;try{const r=await post({action:'plugin',worker:d.worker,module:p.module,enable:!p.loaded});status.textContent=p.module+(r.ok?(p.loaded?' disabled':' enabled')+' (kept across restarts).':' did not change: '+((r.result||{}).error||''));await render();}catch(e){status.textContent=e.message;}finally{b.disabled=false;}};act.append(b);}
          tr.append(el('td',p.module),el('td',st),el('td',p.loaded?'':(p.reason||''),'kn-dim'),act);t.append(tr);}
        const s=el('div',undefined,'kn-scroll');s.append(t);page.append(s);}
      const ps=Object.entries(d.plugin_settings||{});
      if(ps.length){page.append(el('h2','Plugin settings'),el('p','Per-worker values for plugins that read settings. A plugin that is off because a setting is blank turns on once it is set and applied.','kn-dim'));
        for(const [m,rows] of ps){const p=plist.find(x=>x.module===m);const box=el('section',undefined,'st-group');const head=el('div',undefined,'mg-head');for(const c of [m+(p?(p.loaded?' · on':' · not loaded'):' · not reported by the worker'),'Value','Comes from','Applies'])head.append(el('div',c));box.append(head);for(const r of rows)box.append(row(r,{scope:'worker',target:d.worker,onSaved:render}));page.append(box);}}
      page.append(el('h2','Worker'),...groups(d,{scope:'worker',target:d.worker,onSaved:render}));
      // delivery
      const dl=el('section',undefined,'kn-callout');dl.append(el('strong','Apply to this worker'));
      dl.append(el('span','Stored worker settings are pushed as one config (commit-confirmed: the worker restarts and reverts on its own unless it comes back). Secrets go as {{secret:…}} references that it fetches from the hub at use.'));
      if(d.delivered)dl.append(el('span','Last applied '+ago(d.delivered.updated_at)+' by '+(d.delivered.by||'?')+(d.delivered.epoch?' · epoch '+d.delivered.epoch:''),'kn-dim'));
      if(d.job)dl.append(el('span','Latest apply: '+d.job.state+(d.job.result&&d.job.result.error?' · '+d.job.result.error:''),d.job.state==='failed'?'kn-error':'kn-dim'));
      const ab=el('button','Apply and restart worker','kn-primary');ab.type='button';ab.disabled=!live.online;ab.onclick=async()=>{ab.disabled=true;try{const r=await post({action:'apply_worker',worker:d.worker});status.textContent=r.note||('Applying to '+d.worker+'… it restarts and is confirmed when it returns.');setTimeout(()=>render().catch(()=>{}),8000);}catch(e){status.textContent=e.message;ab.disabled=false;}};
      const row2=el('div',undefined,'kn-row');row2.append(ab);dl.append(row2);page.append(dl);
      if(live.config&&live.config.config){page.append(el('h2','Active overrides on the worker'));const pre=el('pre',JSON.stringify(live.config.config,null,2));pre.className='kn-md';page.append(pre);}
      const rep=live.report;if(rep&&rep.secret_refs&&Object.keys(rep.secret_refs).length){page.append(el('h2','Secrets fetched at use'));const ul=el('ul',undefined,'kn-list');for(const [v,s] of Object.entries(rep.secret_refs))ul.append(el('li',v+' ← vault:'+s.vault+(s.resolved?' (fetched)':' (not fetched yet)')));page.append(ul);}
      finish(d.history);return;}
    if(view==='persona'){d=await get({view});renderPersona(d,target);return;}
    page.replaceChildren(el('p','Unknown settings page.','kn-error'));
  }

  // Persona (hub plugin "persona"): profiles, scoped assignments, history.
  function renderPersona(d,target){
    pending=[];saveButton=null;page.replaceChildren(el('h1','Persona'),el('p','One persona for every agent that uses Rook: it rides in the MCP instructions (trimmed to '+d.mcp_budget+' characters), in work launches, in the skill\'s site notes, in harness files written by persona.apply, and gives the voice assistant its name. The most specific assignment wins: user, then family, then band, then default.','kn-dim'),status);
    // assignments
    page.append(el('h2','Assignments'));
    const at=el('table',undefined,'kn-table');const ah=el('tr');for(const c of ['Scope','Target','Profile','Set by',''])ah.append(el('th',c));at.append(ah);
    for(const a of d.assignments){const tr=el('tr');const td=el('td');const rm=el('button','Remove');rm.type='button';rm.onclick=async()=>{try{await post({action:'persona_assign',scope:a.scope,target:a.target,profile:''});status.textContent='Assignment removed.';await render();}catch(e){status.textContent=e.message;}};td.append(rm);tr.append(el('td',a.scope),el('td',a.target||'(everyone)'),el('td',a.profile),el('td',(a.actor||'')+' · '+ago(a.updated)),td);at.append(tr);}
    if(!d.assignments.length){const tr=el('tr');const td=el('td','Nothing assigned: agents get no persona.','kn-dim');td.colSpan=5;tr.append(td);at.append(tr);}
    page.append(at);
    const af=el('div',undefined,'kn-row');const sc=el('select');for(const s of d.scopes){const o=el('option',s);o.value=s;sc.append(o);}sc.value='default';
    const tg=el('input');tg.type='text';tg.placeholder='target: band id, family ('+d.families.join(', ')+') or user id';tg.style.minWidth='320px';
    const pf=el('select');for(const p of d.profiles){const o=el('option',p.id+(p.name?' ('+p.name+')':''));o.value=p.id;pf.append(o);}
    const ab=el('button','Assign','kn-primary');ab.type='button';ab.disabled=!d.profiles.length;
    ab.onclick=async()=>{try{await post({action:'persona_assign',scope:sc.value,target:tg.value,profile:pf.value});status.textContent='Assigned '+pf.value+' at '+sc.value+(tg.value?':'+tg.value:'')+'.';await render();}catch(e){status.textContent=e.message;}};
    af.append(sc,tg,pf,ab);page.append(af);
    // profiles
    page.append(el('h2','Profiles'));
    const pick=el('div',undefined,'kn-row');
    for(const p of d.profiles){const b=el('button',p.id+' · rev '+p.rev);b.type='button';b.onclick=()=>go('persona',p.id);if(p.id===target)b.className='kn-primary';pick.append(b);}
    const nb=el('button','New profile');nb.type='button';nb.onclick=()=>go('persona','');pick.append(nb);page.append(pick);
    const cur=d.profiles.find(p=>p.id===target);const doc=cur?cur.doc:{id:'',addenda:{}};
    const box=el('section',undefined,'st-group');{const hd=el('div',cur?'Edit '+cur.id:'New profile','mg-head');hd.style.display='block';box.append(hd);}
    const fields={};const add=(key,label,kind,help)=>{const r=el('div',undefined,'st-row');const l=el('div',undefined,'st-label');l.append(el('strong',label));if(help)l.append(el('small',help));const c=el('div',undefined,'st-ctl');c.style.gridColumn='2/-1';let i;if(kind==='text'){i=el('input');i.type='text';i.value=doc[key]||'';}else{i=el('textarea');i.value=kind==='list'?(doc[key]||[]).join('\n'):(doc[key]||'');}i.setAttribute('aria-label',label);c.append(i);r.append(l,c);box.append(r);fields[key]={i,kind};};
    add('id','Profile id','text','slug; cannot change once saved');if(cur)fields.id.i.disabled=true;
    add('name','Persona name','text','e.g. the assistant name agents and the voice use');
    add('owner','Works for','text','optional: whose assistant it is');
    add('voice','Voice and tone','area');
    add('rules','Rules','list','one per line');add('do','Do','list','one per line');add('dont','Don\'t','list','one per line');
    add('formatting','Formatting','area');
    for(const f of d.families)add('addenda.'+f,'Addendum: '+f,'area','added only for '+f);
    for(const f of d.families)fields['addenda.'+f].i.value=(doc.addenda||{})[f]||'';
    add('description','Operator note','text','not shown to agents');
    const collect=()=>{const p={addenda:{}};for(const [k,{i,kind}] of Object.entries(fields)){const v=kind==='list'?i.value.split('\n').map(s=>s.trim()).filter(Boolean):i.value;if(k.startsWith('addenda.')){if(v.trim())p.addenda[k.slice(8)]=v;}else p[k]=v;}return p;};
    const err=el('small','','kn-error');const preview=el('pre',cur?cur.text:'','kn-md');
    const line=el('div',undefined,'kn-row');
    const pv=el('button','Preview');pv.type='button';pv.onclick=async()=>{err.textContent='';try{const r=await post({action:'persona_save',profile:collect(),dry_run:true});preview.textContent=r.text||'(no change)';}catch(e){err.textContent=e.message;}};
    const sv=el('button','Save','kn-primary');sv.type='button';sv.onclick=async()=>{err.textContent='';try{const p=collect();const r=await post({action:'persona_save',profile:p,rev:cur?cur.rev:0});status.textContent=r.unchanged?'No change.':'Saved '+r.id+' rev '+r.rev+'.';go('persona',r.id);}catch(e){err.textContent=e.message;}};
    line.append(pv,sv);
    if(cur){const del=el('button','Delete');del.type='button';del.onclick=async()=>{if(!confirm('Delete profile '+cur.id+'? Its history stays.'))return;try{await post({action:'persona_delete',id:cur.id});status.textContent='Deleted '+cur.id+'.';go('persona','');}catch(e){err.textContent=e.message;}};line.append(del);}
    line.append(err);line.style.padding='12px 18px';box.append(line);page.append(box);
    page.append(el('h3','Rendered (no addendum)'),preview);
    // history
    const wrap=el('div',undefined,'kn-history');wrap.append(el('h2','History'));
    const t=el('table',undefined,'kn-table');const h=el('tr');for(const c of ['When','Who','What','Rev','Note'])h.append(el('th',c));t.append(h);
    for(const x of d.history){const tr=el('tr');tr.append(el('td',new Date(x.ts*1000).toLocaleString()),el('td',x.actor),el('td',x.kind+' '+x.ref+(x.doc===null?' (removed)':x.kind==='assign'?' → '+x.doc.profile:'')),el('td',x.rev??''),el('td',x.note||''));t.append(tr);}
    if(!d.history.length)wrap.append(el('p','No changes recorded yet.','kn-dim'));else{const s=el('div',undefined,'kn-scroll');s.append(t);wrap.append(s);}
    page.append(wrap);
  }

  async function start(){
    try{overview=await get({view:'overview'});}catch(e){overview=null;if(!/operator/i.test(e.message)&&!/Only the operator/.test(e.message))throw e;}
  }
  return {
    async activate(query){await start();const q=new URLSearchParams(query||'');state={view:q.get('view')||state.view||(admin?'overview':'user'),target:q.get('target')||(q.get('view')?'':state.target)};if(!admin)state={view:'user',target:''};await render();},
    deactivate(){},
  };
}
