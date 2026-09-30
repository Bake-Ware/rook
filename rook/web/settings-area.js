// Settings: one area for hub, band, worker, plugin and personal settings,
// rendered from the hub's settings schema (settings.* on worker "rook").
// Every row shows the effective value, where it came from, what it hides and
// how a change takes effect. Secret values are write-only.
const el=(tag,txt,cls)=>{const e=document.createElement(tag);if(txt!==undefined&&txt!==null)e.textContent=txt;if(cls)e.className=cls;return e;};
const ago=t=>{if(!t)return 'never';const m=Math.round((Date.now()/1000-t)/60);return m<1?'just now':m<60?m+'m ago':m<1440?Math.round(m/60)+'h ago':Math.round(m/1440)+'d ago';};
const show=v=>v===null||v===undefined||v===''?'—':typeof v==='object'?JSON.stringify(v):String(v);
const API='/account/settings/api';
const CSS=`
.st-row{display:grid;grid-template-columns:minmax(180px,260px) minmax(0,1fr);gap:6px 18px;padding:12px 0;border-bottom:1px solid var(--line)}
.st-row:last-child{border-bottom:0}
.st-label{font-size:13px}.st-label small{display:block;color:var(--dim);font-size:11px;margin-top:3px;font-family:var(--mono);overflow-wrap:anywhere}
.st-ctl{display:flex;flex-direction:column;gap:6px;min-width:0}
.st-ctl .kn-row{margin:0}
.st-ctl input[type=text],.st-ctl input[type=number],.st-ctl input[type=password],.st-ctl select,.st-ctl textarea{padding:7px 9px;background:var(--panel2);color:var(--fg);border:1px solid var(--line2);font:inherit;min-width:220px;max-width:100%}
.st-ctl textarea{font-family:var(--mono);font-size:12px;min-height:70px}
.st-ctl input[type=password]{min-width:340px}
.st-ctl input:disabled,.st-ctl select:disabled,.st-ctl textarea:disabled{opacity:.7}
.st-help{font-size:12px;color:var(--dim)}
.st-conflict{font-size:12px;color:var(--warn);border-left:2px solid var(--warn);padding-left:8px}
.st-src-env,.st-src-file{border-color:var(--warn)!important;color:var(--warn)!important}
.st-src-hub,.st-src-band,.st-src-worker,.st-src-user{border-color:var(--accent)!important;color:var(--accent)!important}
.st-group{border:1px solid var(--line);background:var(--panel2);padding:4px 16px;margin:0 0 16px}
.st-group>h3{font-family:var(--mono);font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:12px 0 4px}
.st-nav button{text-align:left}
.st-nav .kn-index-item.st-current{border-color:var(--accent-dim)!important;background:var(--chip)!important}
.st-banner{border:1px solid var(--warn);color:var(--fg);background:var(--panel2);padding:10px 12px;margin:0 0 14px;font-size:13px}
.st-banner li{margin:4px 0}
@media(max-width:800px){.st-row{grid-template-columns:1fr}}
`;

export async function mountSettings(root){
  if(!document.querySelector('link[data-kn]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/account/knowledge/assets/knowledge.css';css.dataset.kn='1';document.head.append(css);}
  if(!document.querySelector('style[data-st]')){const s=document.createElement('style');s.dataset.st='1';s.textContent=CSS;document.head.append(s);}
  let csrf='', admin=false, state={view:'',target:''}, overview=null;
  const layout=el('div',undefined,'kn-layout');const side=el('aside',undefined,'kn-side st-nav');const page=el('section',undefined,'kn-page');
  layout.append(side,page);root.replaceChildren(layout);
  const status=el('p','','kn-dim');status.setAttribute('role','status');

  async function get(params){const r=await fetch(API+'?'+new URLSearchParams(params));const d=await r.json().catch(()=>({error:'Bad response'}));if(d.csrf)csrf=d.csrf;if(d.admin!==undefined)admin=d.admin;if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const d=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}

  function go(view,target){state={view,target:target||''};const q=new URLSearchParams({view});if(target)q.set('target',target);history.replaceState(null,'','#settings?'+q);render().catch(e=>{page.replaceChildren(el('p',e.message,'kn-error'));});}
  function navItem(text,sub,view,target){const b=el('button',undefined,'kn-index-item');b.type='button';b.append(el('span',text));if(sub)b.append(el('small',sub));if(state.view===view&&(state.target||'')===(target||''))b.classList.add('st-current');b.onclick=()=>go(view,target);return b;}
  function link(text,hash){const a=el('a',text);a.href='#'+hash;a.className='kn-linkish';a.onclick=e=>{e.preventDefault();window.showView&&window.showView(hash);};return a;}

  function renderSide(){
    side.replaceChildren();
    const search=el('input');search.type='search';search.placeholder='Search settings, e.g. ROOK_KNOWLEDGE';search.setAttribute('aria-label','Search settings');
    const results=el('div',undefined,'kn-index');
    let timer=null;search.oninput=()=>{clearTimeout(timer);timer=setTimeout(async()=>{results.replaceChildren();if(!search.value.trim())return;try{const d=await get({view:'search',q:search.value});for(const r of d.results){const b=navItem(r.label,r.key+(r.env.length?' · '+r.env[0]:''),r.origin==='core'?(r.scope==='hub'?'hub':r.scope==='user'?'user':'overview'):(r.scope==='user'?'user':'plugin'),r.origin==='core'?'':r.namespace);results.append(b);}if(!d.results.length)results.append(el('small','No match.','kn-dim'));}catch(e){results.append(el('small',e.message,'kn-error'));}},200);};
    if(admin)side.append(search,results);
    const nav=el('div',undefined,'kn-index');
    if(admin){nav.append(navItem('Overview','conflicts, processes, recent changes','overview'),navItem('Hub','addresses, network, sign-in, storage','hub'));}
    nav.append(navItem('My preferences','your account only','user'));side.append(nav);
    if(admin&&overview){
      side.append(el('h3','Bands'));const b=el('div',undefined,'kn-index');for(const x of overview.bands)b.append(navItem(x.name,(x.primary?'primary · ':'')+(x.active?'active':'revoked'),'band',x.id));if(!overview.bands.length)b.append(el('small','No bands yet.','kn-dim'));side.append(b);
      side.append(el('h3','Workers'));const w=el('div',undefined,'kn-index');for(const x of overview.workers)w.append(navItem(x.name,(x.version||'')+(x.typed_settings?'':' · older build'),'worker',x.name));if(!overview.workers.length)w.append(el('small','No workers online.','kn-dim'));side.append(w);
      side.append(el('h3','Plugins & services'));const p=el('div',undefined,'kn-index');for(const x of overview.plugins)p.append(navItem(x.title,x.count+' settings · '+x.origin.replace('-',' '),'plugin',x.namespace));side.append(p);
      side.append(el('h3','Also here'));const also=el('div',undefined,'kn-list');for(const [t,h] of [['Agent instructions','guidance'],['Secrets (vault)','vault'],['API tokens','tokens'],['Band keys, members, moves','bands'],['Account & access','account']]){const d=el('div');d.append(link(t,h));also.append(d);}side.append(also);
    }
  }

  function badge(text,cls,title){const b=el('span',text,'kn-badge'+(cls?' '+cls:''));if(title)b.title=title;return b;}
  function sourceBadge(r){
    const s=r.source;if(s==='env')return (r.env||'').startsWith('-')?badge('flag '+r.env,'st-src-env','Set on the command line: it wins and locks this field.'):badge('env '+(r.env||''),'st-src-env','Set in the process environment: it wins and locks this field.');
    if(s==='file')return badge('setup.json','st-src-file','From the legacy setup file; a saved value replaces it.');
    if(s==='enrollment')return badge('bands page','st-src-hub','Managed in the enrollment database.');
    if(s===r.scope_here)return badge('set here','st-src-'+s,'Stored at this scope.');
    if(s==='default')return badge('default','','Built-in default.');
    return badge('from '+s,'st-src-'+s,'Inherited from the '+s+' value.');
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
    const lab=el('div',undefined,'st-label');lab.append(el('strong',sc.label||r.key));lab.append(el('small',r.key+((sc.env_names||[]).length?' · '+sc.env_names.join(', '):'')+(sc.flag?' · '+sc.flag:'')));
    const ctl=el('div',undefined,'st-ctl');box.append(lab,ctl);
    if(r.error){ctl.append(el('span',r.error,'kn-error'));return box;}
    const meta=el('div',undefined,'kn-row');meta.append(sourceBadge(r));
    if(sc.apply&&sc.apply!=='live')meta.append(badge(sc.apply,'','How a change takes effect: '+sc.apply));
    if(r.pending)meta.append(badge('restart required','st-src-env','Saved, but the process reads it only at start.'));
    if(sc.secret&&r.fingerprint)meta.append(badge('fp '+r.fingerprint,'','Fingerprint of the current value (first 8 hex of SHA-256).'));
    if(sc.bootstrap)meta.append(badge('bootstrap','', 'Read before the settings store is reachable: set it in the environment or on the command line.'));
    if(sc.deprecated)meta.append(badge('deprecated','st-src-env',sc.deprecated));
    const editable=r.editable&&!r.locked;
    const {input,value}=control(r);input.disabled=!editable;
    const line=el('div',undefined,'kn-row');line.append(input);
    const err=el('small','','kn-error');
    if(editable){
      const save=el('button',sc.secret?'Replace':'Save','kn-primary');save.type='button';
      save.onclick=async()=>{err.textContent='';let v;try{v=value();}catch(e){err.textContent='Not valid JSON: '+e.message;return;}if(sc.secret&&!v){err.textContent='Enter the new value first.';return;}save.disabled=true;try{const d=await post({action:'set',key:r.key,scope,target,value:v});status.textContent=(sc.label||r.key)+': '+(d.note||'saved')+'.';await onSaved();}catch(e){err.textContent=e.message;}finally{save.disabled=false;}};
      line.append(save);
      if(r.set_here){const reset=el('button','Reset to inherited');reset.type='button';reset.title='Inherited value: '+show(r.inherited);reset.onclick=async()=>{try{const d=await post({action:'reset',key:r.key,scope,target});status.textContent=(sc.label||r.key)+' reset: '+(d.note||'')+'.';await onSaved();}catch(e){err.textContent=e.message;}};line.append(reset);}
    }
    line.append(err);ctl.append(meta,line);
    if(r.locked){const flag=(r.env||'').startsWith('-');const under=(r.layers||[]).filter(l=>l.source!=='env');ctl.append(el('span',(r.env||'The environment')+' wins. Stored underneath: '+(under.length?under.map(l=>l.source+' = '+show(l.value)).join(', '):'nothing')+'. '+(flag?'Remove the flag from the '+(sc.owner||'')+' command line':'Remove the variable from the '+(sc.owner||'')+' environment')+' to use it.','st-help'));}
    else if(r.managed==='enrollment'){const s=el('span','Rotate or revoke this key on the Bands page. ','st-help');s.append(link('Open Bands','bands'));ctl.append(s);}
    else if(!r.editable&&sc.bootstrap){ctl.append(el('span','Set '+((sc.env_names||[])[0]||sc.flag||'it')+' in the '+sc.owner+' environment; restart to apply.','st-help'));}
    if(r.conflict)ctl.append(el('span','⚠ '+r.conflict.note,'st-conflict'));
    for(const bad of r.invalid||[])ctl.append(el('span','Ignored an invalid '+bad.source+' value: '+bad.error,'st-conflict'));
    if(sc.help)ctl.append(el('span',sc.help,'st-help'));
    return box;
  }

  function groups(d,opts){const out=[];for(const g of d.groups||[]){const box=el('section',undefined,'st-group');box.append(el('h3',g.name));const vis=g.rows.filter(r=>!(r.schema&&r.schema.advanced));const adv=g.rows.filter(r=>r.schema&&r.schema.advanced);for(const r of vis)box.append(row(r,opts));if(adv.length){const det=el('details');det.append(el('summary','Show advanced ('+adv.length+')'));for(const r of adv)det.append(row(r,opts));box.append(det);}out.push(box);}return out;}

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
      page.replaceChildren(el('h1','Settings'),el('p','Every hub, band, worker and plugin setting in one place, with where each value comes from. The environment wins over the Settings page, and says so.','kn-dim'),status);
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
      page.append(historyTable(d.history));return;}
    if(view==='hub'){d=await get({view});page.replaceChildren(el('h1','Hub'),el('p','Addresses, network, sign-in, updates and storage. Bootstrap settings are read before the store is reachable; they show their source here and change in the environment.','kn-dim'),status,...groups(d,{scope:'hub',target:'',onSaved:render}),historyTable(d.history));return;}
    if(view==='band'){d=await get({view,target});const b=d.band;page.replaceChildren(el('h1',b.name),el('p','Band '+b.label+' · epoch '+b.epoch+(b.primary?' · primary':'')+(b.active?'':' · revoked'),'kn-meta'),el('p','Worker defaults apply to every worker on this band unless a worker overrides them. Apply them from each worker page.','kn-dim'),status,...groups(d,{scope:'band',target,onSaved:render}),historyTable(d.history));return;}
    if(view==='plugin'){d=await get({view,target});page.replaceChildren(el('h1',d.title),el('p','Runs in: '+d.owner+' · '+d.origin.replace('-',' '),'kn-meta'),status);
      if(d.owner.startsWith('service:')){const c=el('div',undefined,'kn-callout');c.append(el('strong','Separate service'),el('span','It reads these with settings.fetch("'+d.namespace+'") on worker rook, using a token listed in core.settings.service_readers.'+d.namespace+'. Readers now: '+(d.service_readers.length?d.service_readers.join(', '):'none')+'. Its own environment still wins.'));page.append(c);}
      if(d.origin==='worker-plugin')page.append(el('p','Per-worker settings: set them on each worker page (Workers in the sidebar).','kn-dim'));
      page.append(...groups(d,{scope:'hub',target:'',onSaved:render}));
      if(d.scoped&&d.scoped.length){page.append(el('h2','Set per band, worker or user'));const t=el('table',undefined,'kn-table');const h=el('tr');for(const c of ['Setting','Home scope','Overrides'])h.append(el('th',c));t.append(h);for(const s of d.scoped){const tr=el('tr');tr.append(el('td',s.label+' ('+s.key+')'),el('td',s.scope),el('td',Object.entries(s.overrides).map(([k,v])=>v+' '+k).join(', ')||'—'));t.append(tr);}page.append(t);}
      page.append(historyTable(d.history));return;}
    if(view==='user'){d=await get({view:'user'});page.replaceChildren(el('h1','My preferences'),el('p','Yours on every device that signs in with this account. A device can still keep its own value.','kn-dim'),status,...groups(d,{scope:'user',target:d.user,onSaved:render}),historyTable(d.history));return;}
    if(view==='worker'){d=await get({view,target});const live=d.live||{};
      page.replaceChildren(el('h1',d.title),el('p',(live.online?'● online':'○ offline')+(live.version?' · '+live.version:'')+(live.band?' · band '+live.band:''),'kn-meta'),status);
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
        for(const [m,rows] of ps){const p=plist.find(x=>x.module===m);const box=el('section',undefined,'st-group');box.append(el('h3',m+(p?(p.loaded?' · on':' · not loaded'):' · not reported by the worker')));for(const r of rows)box.append(row(r,{scope:'worker',target:d.worker,onSaved:render}));page.append(box);}}
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
      page.append(historyTable(d.history));return;}
    page.replaceChildren(el('p','Unknown settings page.','kn-error'));
  }

  async function start(){
    try{overview=await get({view:'overview'});}catch(e){overview=null;if(!/operator/i.test(e.message)&&!/Only the operator/.test(e.message))throw e;}
  }
  return {
    async activate(query){await start();const q=new URLSearchParams(query||'');state={view:q.get('view')||state.view||(admin?'overview':'user'),target:q.get('target')||(q.get('view')?'':state.target)};if(!admin)state={view:'user',target:''};await render();},
    deactivate(){},
  };
}
