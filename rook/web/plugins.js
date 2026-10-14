// Manage > Plugins: every plugin on the hub and on each online worker in one
// grid, with switches, band defaults for new workers, presets, and the
// marketplace preview. Everything goes through /account/plugins/api (operator
// only; rook/hub/plugins_web.py). Stored text is rendered as text.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, useCss, btn, chips, dialog} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API='/account/plugins/api';
const RISK_NOTE={read:'reads only',write:'changes data',exec:'runs code or controls the machine',admin:'administers the band'};
const STATE_LABEL={on:'on',off:'off',unavailable:'n/a',error:'error',na:'',absent:'—',unknown:'?'};
const CSS=`
.pl h1{margin:0 8px 0 0;font:500 20px var(--sans);color:var(--accent)}
.pl-filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.pl-filters input[type=search]{flex:1 1 200px;min-width:0}
.pl-grid{border:1px solid var(--line);background:var(--panel);overflow-x:auto}
.pl-grid>div{min-width:var(--min,640px)}
.pl-r{display:grid;grid-template-columns:var(--cols);gap:0;align-items:stretch;border-bottom:1px solid #20261d}
.pl-r:last-child{border-bottom:0}
.pl-r.head{position:sticky;top:0;background:var(--panel);z-index:1;font:500 10px var(--mono);letter-spacing:1px;color:var(--dim);text-transform:uppercase}
.pl-r.group{background:#161b13;font:500 10px var(--mono);letter-spacing:1px;color:var(--dim);text-transform:uppercase}
.pl-r.group>div{padding:8px 14px;grid-column:1/-1}
.pl-r>div{padding:8px 6px;display:flex;align-items:center;justify-content:center;min-width:0}
.pl-r>div:first-child{justify-content:flex-start;padding-left:14px;position:sticky;left:0;background:var(--panel);z-index:1}
.pl-r.head>div{flex-direction:column;gap:2px;text-align:center;overflow:hidden}
.pl-r.head small{font:10px var(--mono);letter-spacing:0;text-transform:none;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.pl-name{display:flex;flex-direction:column;gap:2px;min-width:0;text-align:left;background:none;border:0;color:var(--fg);cursor:pointer;padding:0;font:13px var(--sans)}
.pl-name b{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pl-name small{color:var(--dim);font:11px var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pl-name:hover b{color:var(--accent)}
.pl-tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:2px}
.pl-risk{font:500 9px var(--mono);letter-spacing:1px;padding:1px 5px;border:1px solid var(--line2);color:var(--dim);text-transform:uppercase}
.pl-risk.exec,.pl-risk.admin{border-color:#6b4336;color:var(--bad)}
.pl-risk.write{border-color:#514b35;color:var(--accent)}
.pl-risk.hi{background:#241614;border-color:#6b4336;color:var(--bad);text-transform:none;letter-spacing:0}
.pl .pl-cell{min-width:58px;min-height:30px;padding:0 8px;border:1px solid var(--line);background:transparent;color:var(--dim);font:500 11px var(--mono);cursor:default}
.pl .pl-cell.on{border-color:#3d5a33;color:var(--green);background:#16200f}
.pl .pl-cell.off{border-color:var(--line2);color:var(--dim)}
.pl .pl-cell.unavailable{border-style:dashed;color:#8a8d7a}
.pl .pl-cell.error{border-color:#6b4336;color:var(--bad);background:#241614}
.pl .pl-cell.na,.pl .pl-cell.absent{border-color:transparent}
.pl .pl-cell.can{cursor:pointer}.pl .pl-cell.can:hover{border-color:var(--accent);color:var(--accent)}
.pl .pl-cell.busy{opacity:.5;cursor:progress}
.pl-sum{display:none;font:11px var(--mono);color:var(--dim)}
.pl-def input{width:18px;height:18px}
.pl-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.pl-card{border:1px solid var(--line);background:var(--panel);padding:14px 16px;display:flex;flex-direction:column;gap:8px;min-width:0}
.pl-card.on{border-color:var(--accent-dim)}
.pl-card h3{margin:0;font:500 15px var(--sans);display:flex;gap:10px;align-items:center}
.pl-card p{margin:0;color:var(--dim);font-size:13px;line-height:1.5}
.pl-card small{color:var(--dim);font:11px var(--mono);overflow-wrap:anywhere}
.pl-detail dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:6px 14px;margin:0;font-size:13px}
.pl-detail dt{color:var(--dim);font:11px var(--mono)}
.pl-detail dd{margin:0;overflow-wrap:anywhere}
.pl-where{display:grid;grid-template-columns:minmax(0,1fr) auto auto;gap:6px 10px;align-items:center;font-size:13px}
.pl-where small{color:var(--dim);display:block;font-size:11px}
.pl-caps{display:flex;flex-direction:column;gap:6px;max-height:260px;overflow:auto;font-size:12px}
.pl-caps div{display:grid;grid-template-columns:minmax(0,max-content) auto;gap:2px 8px;align-items:baseline}
.pl-caps code{font:12px var(--mono);color:var(--fg)}
.pl-caps span.doc{grid-column:1/-1;color:var(--dim)}
.pl-banner{padding:10px 14px;border:1px solid var(--accent-dim);background:#1f1c12;font-size:13px;line-height:1.5}
@media(max-width:760px){.pl-grid .pl-r{grid-template-columns:minmax(0,1fr) 120px}.pl-grid .pl-c{display:none}.pl-grid .pl-sum{display:flex}.pl-grid>div{min-width:0}}
.pp{display:flex;flex-direction:column;gap:10px}
.pp-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
.pp-item{display:grid;grid-template-columns:20px minmax(0,1fr);gap:4px 10px;align-items:start;padding:10px 12px;border:1px solid var(--line);background:var(--panel);cursor:pointer;font-size:13px}
.pp-item.on{border-color:var(--accent-dim);background:#1f1c12}
.pp-item input{margin-top:2px}
.pp-item b{font-weight:500}
.pp-item small{grid-column:2;color:var(--dim);font-size:12px;line-height:1.45}
.pp-item .pl-tags{grid-column:2}
`;

function css(){
  useCss();
  if(!document.querySelector('style[data-pl]')){const s=document.createElement('style');s.dataset.pl='1';s.textContent=CSS;document.head.append(s);}
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
  if(!csrf)await get({view:'presets'});
  const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});
  const d=await r.json().catch(()=>({error:'Bad response'}));
  if(!r.ok&&!('ok' in d))throw Error(d.error||'Request failed');
  return d;
}

function riskTags(risk,high){
  const box=h('span',{class:'pl-tags'});
  for(const t of (risk&&risk.tiers)||[])box.append(h('span',{class:'pl-risk '+t,text:t,title:RISK_NOTE[t]||''}));
  for(const x of high||[])box.append(h('span',{class:'pl-risk hi',text:x}));
  return box;
}

// A preset picker: "What do you want Rook to do?". Used on the Plugins page
// and as the first step of the onboarding pairing flow (index.html).
// opts: {band, intro, applyLabel, onApplied(result)}. Returns {node, refresh}.
export function presetPicker(opts={}){
  css();
  const node=h('div',{class:'pp mg'});
  const status=h('p',{class:'mg-status',role:'status'});
  let data=null;
  async function refresh(){
    node.replaceChildren(h('p',{class:'mg-sub',text:'Loading presets…'}));
    try{data=await get({view:'presets',...(opts.band?{band:opts.band}:{})});}
    catch(e){node.replaceChildren(h('p',{class:'mg-sub',text:'Plugin presets are unavailable: '+e.message}));if(opts.onError)opts.onError(e);return;}
    render();
  }
  function render(){
    const list=h('div',{class:'pp-list'});
    const boxes=[];
    for(const p of data.presets){
      const cb=h('input',{type:'checkbox',checked:p.selected,'aria-label':p.title});
      const item=h('label',{class:'pp-item'+(p.selected?' on':'')},cb,h('b',{text:p.title}),
        h('small',{text:p.summary}),riskTags(p.risk||{tiers:p.tiers},p.high_risk));
      cb.onchange=()=>item.classList.toggle('on',cb.checked);
      boxes.push([p.id,cb]);list.append(item);
    }
    const band=h('select',{'aria-label':'Band'});
    for(const b of data.bands||[])band.append(h('option',{value:b.id,text:b.name,selected:b.id===data.band}));
    const apply=btn(opts.applyLabel||'Apply','primary',async()=>{
      apply.disabled=true;status.className='mg-status';status.textContent='Applying…';
      try{
        const r=await post({action:'apply_presets',presets:boxes.filter(([,c])=>c.checked).map(([id])=>id),band:band.value||data.band||''});
        const bad=(r.hub||[]).filter(x=>!x.ok);
        status.textContent=(r.error?r.error:'Saved. '+(r.band?'New workers on this band get these plugins. ':'')+(bad.length?'Hub plugins that did not load: '+bad.map(x=>x.module+' ('+x.error+')').join('; '):''));
        if(bad.length||r.error)status.className='mg-status bad';
        if(opts.onApplied)opts.onApplied(r);
      }catch(e){status.className='mg-status bad';status.textContent=e.message;}
      finally{apply.disabled=false;}
    });
    const bar=h('div',{class:'mg-actions'},(data.bands||[]).length>1?band:null,apply,opts.skip||null);
    node.replaceChildren(opts.intro?h('p',{class:'mg-sub',text:opts.intro}):null,list,
      h('p',{class:'mg-sub',text:'Unchecked groups are turned off on new workers (and their hub plugins on the hub). Red tags mark plugins that can run code, control input or capture the screen.'}),bar,status);
  }
  refresh();
  return {node,refresh};
}

export async function mountPlugins(root){
  css();
  const page=h('div',{class:'mg pl'});root.replaceChildren(page);
  const status=h('p',{class:'mg-status',role:'status'});
  let tab='installed', m=null, timer=null, busy=false, filter={side:'all',q:'',os:'',band:''};
  let catalog=null;

  function say(text,bad){status.className='mg-status'+(bad?' bad':'');status.textContent=text||'';}

  async function load(refresh){
    const q={view:'matrix'};if(filter.band)q.band=filter.band;if(refresh)q.refresh='1';
    m=await get(q);
    return m;
  }

  function cellButton(row,col){
    const c=row.cells[col.id]||{state:'na'};
    const label=c.state==='on'&&c.toggle===null&&row.protected?'on':STATE_LABEL[c.state]??c.state;
    const b=h('button',{type:'button',class:'pl-cell '+c.state+(c.toggle?' can':''),text:label,
      title:(col.name+': '+c.state+(c.reason?' — '+c.reason:''))+(c.toggle?'. Click to '+c.toggle+'.':''),
      'aria-label':row.title+' on '+col.name+': '+c.state+(c.toggle?', click to '+c.toggle:'')});
    if(!c.toggle){b.disabled=c.state==='na'||c.state==='absent';b.tabIndex=c.state==='na'?-1:0;return b;}
    b.onclick=()=>toggle(row,col,c.toggle==='enable',b);
    return b;
  }

  async function toggle(row,col,enable,button){
    if(busy)return;busy=true;if(button){button.classList.add('busy');button.textContent='…';}
    say((enable?'Turning on ':'Turning off ')+row.module+' on '+col.name+'…');
    try{
      const r=await post({action:'toggle',target:col.id==='hub'?'hub':col.id,module:row.module,enable});
      await load(true);render();
      if(r.ok)say(row.module+(enable?' is on':' is off')+' on '+col.name+(col.id==='hub'?' (kept across restarts).':' (live, kept across restarts).'));
      else say(row.module+' on '+col.name+': '+(r.error||'did not change'),true);
    }catch(e){say(e.message,true);await load(true).catch(()=>{});render();}
    finally{busy=false;}
  }

  function columns(){
    let cols=m.columns.slice(1);
    if(filter.os)cols=cols.filter(c=>(c.app==='android'?'android':c.os)===filter.os);
    if(filter.band)cols=cols.filter(c=>c.band_id===filter.band||!c.band_id);
    cols.sort((a,b)=>(a.band_name||'').localeCompare(b.band_name||'')||a.name.localeCompare(b.name));
    return [m.columns[0],...cols];
  }

  function rowsShown(){
    const q=filter.q.trim().toLowerCase();
    return m.rows.filter(r=>(filter.side==='all'||r.side===filter.side)&&
      (!q||[r.module,r.title,r.namespace,r.description,r.origin].join(' ').toLowerCase().includes(q)));
  }

  function summary(row,cols){
    const n={};for(const c of cols){const s=(row.cells[c.id]||{}).state;if(s&&s!=='na'&&s!=='absent')n[s]=(n[s]||0)+1;}
    return Object.entries(n).map(([k,v])=>v+' '+k).join(' · ')||'—';
  }

  function grid(){
    const cols=columns();
    const tmpl='minmax(220px,1.6fr) '+cols.map(()=>'78px').join(' ')+' 92px';
    const wrap=h('div',{class:'pl-grid',style:'--min:'+(240+cols.length*78+92)+'px'});
    const inner=h('div');wrap.append(inner);
    const head=h('div',{class:'pl-r head',style:'--cols:'+tmpl},h('div',{text:'Plugin'}));
    for(const c of cols)head.append(h('div',{class:'pl-c',title:c.name+(c.band_name?' · band '+c.band_name:'')+(c.os?' · '+c.os:'')+(c.version?' · '+c.version:'')+(c.error?' · '+c.error:'')},
      h('span',{text:c.kind==='hub'?'hub':c.name}),h('small',{text:c.kind==='hub'?'rook':[c.app==='android'?'android':c.os,c.band_name].filter(Boolean).join(' · ')})));
    head.append(h('div',{class:'pl-c',title:'Default for workers enrolled on this band from now on'},h('span',{text:'new'}),h('small',{text:'workers'})));
    head.append(h('div',{class:'pl-sum',text:'Where'}));
    inner.append(head);
    const shown=rowsShown();
    let group=null;
    for(const r of shown){
      const g=r.side==='hub'?'On the hub':'On workers';
      if(g!==group){group=g;inner.append(h('div',{class:'pl-r group',style:'--cols:'+tmpl},h('div',{text:g})));}
      const name=h('button',{type:'button',class:'pl-name',onclick:()=>openDetail(r)},
        h('b',{text:r.title}),h('small',{text:r.module+(r.namespace&&r.namespace!==r.module?' · '+r.namespace+'.*':'')+' · '+r.origin+(r.settings?' · '+r.settings+' settings':'')}),
        riskTags(r.risk,r.protected?['core']:[]));
      const line=h('div',{class:'pl-r',style:'--cols:'+tmpl},h('div',null,name));
      for(const c of cols)line.append(h('div',{class:'pl-c'},cellButton(r,c)));
      const def=h('div',{class:'pl-c pl-def'});
      if(r.side==='worker'){
        const cb=h('input',{type:'checkbox',checked:r.default!==false,disabled:r.protected||!m.band,
          title:r.default===undefined||r.default===null?'Built-in: on where available':'Set: '+(r.default?'on':'off'),
          'aria-label':'Turn '+r.module+' on for new workers'});
        cb.onchange=async()=>{cb.disabled=true;try{await post({action:'default',band:m.band,module:r.module,value:cb.checked});say('New workers on this band: '+r.module+(cb.checked?' on.':' off.'));await load(false);render();}catch(e){say(e.message,true);cb.checked=!cb.checked;cb.disabled=false;}};
        def.append(cb);
      }else def.append(h('span',{class:'mg-sub',text:'—'}));
      line.append(def,h('div',{class:'pl-sum',text:summary(r,cols)}));
      inner.append(line);
    }
    if(!shown.length)inner.append(h('div',{class:'mg-empty',text:'No plugins match.'}));
    return wrap;
  }

  function openDetail(r){
    const cols=columns();
    const where=h('div',{class:'pl-where'});
    for(const c of cols){
      const cell=r.cells[c.id]||{};if(cell.state==='na')continue;
      const label=h('span',null,h('span',{text:c.kind==='hub'?'hub (rook)':c.name}),h('small',{text:(cell.state||'')+(cell.reason?' — '+cell.reason:'')}));
      const sw=cell.toggle?btn(cell.toggle==='enable'?'Turn on':'Turn off',cell.toggle==='enable'?'soft':'',async()=>{d.close();await toggle(r,c,cell.toggle==='enable');}):h('span');
      const rl=cell.state==='on'&&c.can_reload?btn('Reload','',async()=>{sw.disabled=true;try{const x=await post({action:'reload',target:c.id==='hub'?'hub':c.id,module:r.module});say(r.module+' on '+c.name+(x.ok?' reloaded.':': '+(x.error||'reload failed')),!x.ok);d.close();await load(true);render();}catch(e){say(e.message,true);}}):h('span');
      where.append(label,sw,rl);
    }
    const caps=h('div',{class:'pl-caps'});
    for(const c of r.caps||[])caps.append(h('div',null,h('code',{text:c.cap}),h('span',{class:'pl-risk '+c.risk,text:c.risk}),c.doc?h('span',{class:'doc',text:c.doc}):null));
    if(!(r.caps||[]).length)caps.append(h('span',{class:'mg-sub',text:'No capabilities reported yet (load it somewhere to see them).'}));
    const facts=h('dl',null,
      h('dt',{text:'module'}),h('dd',{text:r.module+' ('+r.side+')'}),
      h('dt',{text:'namespace'}),h('dd',{text:r.namespace?r.namespace+'.*':'—'}),
      h('dt',{text:'origin'}),h('dd',{text:r.origin}),
      h('dt',{text:'placement'}),h('dd',{text:r.placement||'—'}),
      r.depends&&r.depends.length?h('dt',{text:'needs'}):null,r.depends&&r.depends.length?h('dd',{text:r.depends.join(', ')}):null,
      h('dt',{text:'risk'}),h('dd',null,riskTags(r.risk)),
      h('dt',{text:'settings'}),h('dd',null,r.settings_link?h('a',{href:'#'+r.settings_link,text:r.settings+' setting'+(r.settings===1?'':'s')+' →',onclick:e=>{e.preventDefault();d.close();history.pushState(null,'','#'+r.settings_link);window.showView?.('settings');}}):h('span',{text:'none'})));
    const bulk=h('div',{class:'mg-actions'});
    if(r.side==='worker'&&!r.protected){
      const all=on=>btn(on?'Turn on on all eligible workers':'Turn off on all workers',on?'soft':'danger',async()=>{
        if(!on&&!confirm('Turn '+r.module+' off on every online worker that has it?'))return;
        d.close();say((on?'Turning on ':'Turning off ')+r.module+' everywhere…');
        try{const x=await post({action:'toggle_all',module:r.module,enable:on});await load(true);render();
          const bad=(x.results||[]).filter(y=>!y.ok);
          say(x.note||(((x.results||[]).length-bad.length)+' worker(s) switched'+(bad.length?'; failed: '+bad.map(y=>y.target+' ('+y.error+')').join('; '):'.')),!!bad.length);}
        catch(e){say(e.message,true);}});
      bulk.append(all(true),all(false));
    }
    const d=dialog(r.title,h('div',{class:'pl-detail mg'},
      r.description?h('p',{text:r.description}):null,
      r.protected?h('p',{class:'mg-sub',text:'Core plugin: it carries the controls to switch everything else back on, so it cannot be turned off here.'}):null,
      facts,h('h3',{class:'mg-label',text:'Where it runs'}),where,bulk,
      h('h3',{class:'mg-label',text:'Capabilities'}),caps));
  }

  function filters(){
    const bar=h('div',{class:'pl-filters'});
    bar.append(...chips([['all','All'],['hub','Hub'],['worker','Workers']],filter.side,id=>{filter.side=id;render();}));
    const oses=[...new Set(m.columns.slice(1).map(c=>c.app==='android'?'android':c.os).filter(Boolean))].sort();
    if(oses.length>1){const s=h('select',{'aria-label':'Operating system'},h('option',{value:'',text:'Every OS'}),oses.map(o=>h('option',{value:o,text:o,selected:o===filter.os})));s.onchange=()=>{filter.os=s.value;render();};bar.append(s);}
    const bands=(m.bands||[]).filter(b=>b.active);
    if(bands.length>1){const s=h('select',{'aria-label':'Band'},bands.map(b=>h('option',{value:b.id,text:'Band '+b.name,selected:b.id===m.band})));s.onchange=async()=>{filter.band=s.value;await load(false);render();};bar.append(s);}
    const q=h('input',{type:'search',placeholder:'Find a plugin',value:filter.q,'aria-label':'Find a plugin'});
    q.oninput=()=>{filter.q=q.value;const g=page.querySelector('.pl-grid');if(g)g.replaceWith(grid());};
    bar.append(q);
    return bar;
  }

  async function marketplace(){
    if(!catalog)catalog=await get({view:'catalog'});
    const box=h('div',{class:'mg',style:'gap:12px'});
    box.append(h('div',{class:'pl-banner'},h('strong',{text:'Preview. '}),h('span',{text:'Installing from a catalog is coming soon. Today a third-party plugin is a Python package with a "rook.plugins" entry point: pip install it into the hub\'s or a worker\'s environment and restart it; it then shows up under Installed.'})));
    if(!catalog.configured.length)box.append(h('p',{class:'mg-sub',text:'No catalog configured (core.plugins.catalogs in Settings > Hub). Showing the bundled example catalog: its entries are fictional.'}));
    for(const cat of catalog.catalogs){
      box.append(h('h2',{class:'mg-label',text:cat.name+(cat.example?' · examples':'')}));
      for(const err of cat.errors||[])box.append(h('p',{class:'mg-error',text:err}));
      const cards=h('div',{class:'pl-cards'});
      for(const e of cat.plugins){
        const install=btn('Coming soon','',null,{disabled:true,title:catalog.install_note});
        cards.append(h('div',{class:'pl-card'},
          h('h3',null,h('span',{text:e.name}),h('small',{text:'v'+e.version})),
          h('small',{text:[e.author,e.license,e.namespace+'.*',(e.compatible?'':'needs core_api '+e.min_core_api)].filter(Boolean).join(' · ')}),
          riskTags(e.risk,cat.example?['example']:[]),
          h('p',{text:e.description}),
          h('div',{class:'mg-actions'},btn('Details','soft',()=>catalogDetail(e,cat)),install)));
      }
      if(!cat.plugins.length)cards.append(h('p',{class:'mg-sub',text:'No usable entries.'}));
      box.append(cards);
    }
    return box;
  }

  function catalogDetail(e,cat){
    const pkg=e.package||{};
    const caps=h('div',{class:'pl-caps'});
    for(const c of e.caps||[])caps.append(h('div',null,h('code',{text:c.cap}),h('span',{class:'pl-risk '+c.risk,text:c.risk}),c.description?h('span',{class:'doc',text:c.description}):null));
    const install=btn('Install (coming soon)','primary',null,{disabled:true});
    const out=h('p',{class:'mg-sub',text:catalog.install_note});
    dialog(e.name,h('div',{class:'pl-detail mg'},
      cat.example?h('div',{class:'pl-banner',text:'Example entry from the bundled catalog: not a real package.'}):null,
      h('p',{text:e.description}),
      h('dl',null,
        h('dt',{text:'id'}),h('dd',{text:e.id}),h('dt',{text:'version'}),h('dd',{text:e.version}),
        h('dt',{text:'author'}),h('dd',{text:e.author||'—'}),h('dt',{text:'license'}),h('dd',{text:e.license||'—'}),
        h('dt',{text:'homepage'}),h('dd',{text:e.homepage||'—'}),
        h('dt',{text:'runs'}),h('dd',{text:e.placement}),
        h('dt',{text:'package'}),h('dd',{text:pkg.kind==='wheel'?pkg.url+' (sha256 '+(pkg.sha256||'?').slice(0,12)+'…)':pkg.requirement||'—'}),
        h('dt',{text:'signature'}),h('dd',{text:e.signature?'present (not verified yet)':'none'}),
        h('dt',{text:'core api'}),h('dd',{text:'≥ '+e.min_core_api+(e.compatible?'':' (this hub is older)')}),
        h('dt',{text:'risk'}),h('dd',null,riskTags(e.risk))),
      (e.problems||[]).length?h('p',{class:'mg-error',text:'Catalog problems: '+e.problems.join('; ')}):null,
      h('h3',{class:'mg-label',text:'Capabilities'}),caps,
      h('div',{class:'mg-actions'},install),out));
  }

  async function render(){
    const bar=h('div',{class:'mg-bar'},h('h1',{text:'Plugins'}));
    const tabs=h('div',{class:'mg-tabs',role:'tablist'});
    for(const [id,label] of [['installed','Installed'],['presets','Presets'],['marketplace','Marketplace']]){
      tabs.append(h('button',{type:'button',class:id===tab?'on':'',text:label,role:'tab','aria-selected':String(id===tab),onclick:()=>{tab=id;render().catch(e=>say(e.message,true));}}));
    }
    const refresh=btn('Refresh','',async()=>{refresh.disabled=true;try{if(tab==='marketplace'){catalog=await get({view:'catalog',refresh:'1'});}else await load(true);await render();say('Refreshed.');}catch(e){say(e.message,true);}finally{refresh.disabled=false;}});
    bar.append(h('span',{class:'mg-grow'}),refresh);
    const body=h('div',{class:'mg',style:'gap:12px'});
    page.replaceChildren(bar,tabs,status,body);
    if(tab==='installed'){
      if(!m)await load(false);
      const online=m.columns.length-1;
      body.append(h('p',{class:'mg-sub',text:'Click a cell to turn a plugin on or off there: it loads or unloads live and stays that way across restarts. '+online+' worker'+(online===1?'':'s')+' online; offline workers are not shown. "New workers" is what a worker enrolled on '+(((m.bands||[]).find(b=>b.id===m.band)||{}).name||'this band')+' gets on its first start.'}));
      for(const s of m.synced||[])if(s&&!s.ok)body.append(h('p',{class:'mg-error',text:'Applying a stored hub switch for '+s.module+' failed: '+s.error}));
      body.append(filters(),grid());
    }else if(tab==='presets'){
      body.append(presetPicker({band:filter.band||(m&&m.band)||'',intro:'Pick what Rook should do. This switches the hub\'s plugins now and sets what new workers on the band turn on; workers already on the band keep their plugins (switch them under Installed).',onApplied:async()=>{m=null;}}).node);
    }else{
      body.append(h('p',{class:'mg-sub',text:'Loading catalogs…'}));
      try{const mk=await marketplace();body.replaceChildren(mk);}catch(e){body.replaceChildren(h('p',{class:'mg-error',text:e.message}));}
    }
  }

  function tick(){
    if(tab!=='installed'||busy||!root.isConnected||document.hidden||document.querySelector('dialog[open]'))return;
    load(false).then(()=>{if(tab==='installed'&&!busy){const g=page.querySelector('.pl-grid');if(g)g.replaceWith(grid());}}).catch(()=>{});
  }
  return {
    async activate(){
      try{await load(false);await render();}
      catch(e){page.replaceChildren(h('h1',{text:'Plugins'}),h('p',{class:'mg-error',text:e.message}));}
      clearInterval(timer);timer=setInterval(tick,15000);
    },
    deactivate(){clearInterval(timer);timer=null;},
  };
}
