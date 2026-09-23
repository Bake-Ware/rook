// Knowledge (a wiki) and Work (projects and tasks): human views over the
// records agents keep. Mostly read-only: agents write; the user observes and
// directs. All stored text is rendered as text nodes, never as HTML.

const el=(tag,txt,cls)=>{const e=document.createElement(tag);if(txt!==undefined&&txt!==null)e.textContent=txt;if(cls)e.className=cls;return e;};
const date=t=>t?new Date(t*1000).toLocaleString():'';
const ago=t=>{if(!t)return '';const m=Math.round((Date.now()/1000-t)/60);return m<1?'just now':m<60?m+'m ago':m<1440?Math.round(m/60)+'h ago':Math.round(m/1440)+'d ago';};
const STATE_LABEL={in_progress:'In progress',todo:'To do',blocked:'Blocked',paused:'Paused',done:'Done',cancelled:'Cancelled',archived:'Archived',active:'Active',superseded:'Superseded'};

function loadCss(){if(document.querySelector('link[data-kn]'))return;const css=document.createElement('link');css.rel='stylesheet';css.href='/account/knowledge/assets/knowledge.css'+new URL(import.meta.url).search;css.dataset.kn='1';document.head.append(css);}

function client(){
  let csrf='',bands=[];
  async function boot(){if(csrf)return;const r=await fetch('/account/knowledge/api');if(!r.ok)throw Error('Sign in with your operator account to view this.');const d=await r.json();csrf=d.csrf;bands=d.bands;}
  async function api(action,body={}){await boot();const r=await fetch('/account/knowledge/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,action,...body})});const d=await r.json();if(!r.ok||d.error||d.ok===false)throw Error(d.error||'Request failed');return d.result;}
  async function listAll(kind){await boot();const out=[];for(const b of bands){const r=await api('list',{band:b.id,kind,data:{limit:200}});for(const x of r.records)out.push({...x,band:x.band||b.id});}return out;}
  async function searchAll(query,kind){await boot();const out=[];let semantic=false;for(const b of bands){const r=await api('search',{band:b.id,query,kind});semantic=semantic||r.semantic;for(const x of r.results)out.push({...x,band:x.band||b.id,excerpt:x.body});}return {results:out,semantic};}
  return {api,boot,listAll,searchAll,bands:()=>bands,bandName:id=>(bands.find(b=>b.id===id)||{}).name||id};
}

function btn(label,fn,cls){const b=el('button',label,cls);b.type='button';b.onclick=e=>{e.preventDefault();Promise.resolve().then(fn).catch(err=>console.warn(err));};return b;}

// Minimal, safe markdown: #/##/### headings, - lists, ``` code blocks, `code`, **bold**, [[slug]] and http(s) links.
function renderBody(text,onRef){
  const out=el('div',undefined,'kn-md');let list=null,para=[],code=null;
  const inline=(s,into)=>{for(const part of s.split(/(\[\[[a-z0-9][a-z0-9-]{0,79}\]\]|`[^`]+`|\*\*[^*]+\*\*|https?:\/\/[^\s)<>"]+)/)){if(!part)continue;
    let m;if((m=part.match(/^\[\[(.+)\]\]$/))){const a=el('a','','kn-ref');a.href='#';a.textContent=m[1];a.onclick=e=>{e.preventDefault();onRef(m[1]);};into.append(a);}
    else if(part.startsWith('`')&&part.endsWith('`')&&part.length>1)into.append(el('code',part.slice(1,-1)));
    else if(/^\*\*[^*]+\*\*$/.test(part))into.append(el('strong',part.slice(2,-2)));
    else if(/^https?:\/\//.test(part)){const a=el('a',part);a.href=part;a.target='_blank';a.rel='noopener noreferrer';into.append(a);}
    else into.append(document.createTextNode(part));}};
  const flush=()=>{if(para.length){const p=el('p');inline(para.join(' '),p);out.append(p);para=[];}};
  for(const line of (text||'').split('\n')){
    if(code!==null){if(line.startsWith('```')){out.append(el('pre',code.join('\n')));code=null;}else code.push(line);continue;}
    if(line.startsWith('```')){flush();list=null;code=[];continue;}
    const h=line.match(/^(#{1,3})\s+(.*)$/);if(h){flush();list=null;const e=el('h'+(h[1].length+2));inline(h[2],e);out.append(e);continue;}
    const li=line.match(/^\s*[-*]\s+(.*)$/);if(li){flush();if(!list){list=el('ul');out.append(list);}const e=el('li');inline(li[1],e);list.append(e);continue;}
    if(!line.trim()){flush();list=null;continue;}
    list=null;para.push(line.trim());}
  if(code!==null)out.append(el('pre',code.join('\n')));flush();
  if(!out.childNodes.length)out.append(el('p','(empty)','kn-dim'));return out;
}

function badge(text,kind){return el('span',text,'kn-badge kn-'+(kind||text).replace(/[^a-z_]/g,''));}

function linksTable(links){const t=el('table',undefined,'kn-table');const h=el('tr');for(const c of ['When','Relation','Kind','Reference','By',''])h.append(el('th',c));t.append(h);
  for(const l of links.slice().reverse()){const tr=el('tr');tr.append(el('td',date(l.ts)),el('td',l.relation),el('td',l.kind),el('td',l.ref+(l.note?' — '+l.note:'')),el('td',l.actor),el('td',l.auto?'auto':''));t.append(tr);}
  const wrap=el('div',undefined,'kn-scroll');wrap.append(t);return wrap;}

function historyBlock(events){const d=el('details',undefined,'kn-history');d.append(el('summary','History ('+events.length+')'));for(const e of events){const x=el('details');x.append(el('summary',date(e.ts)+' · '+e.action+' · '+e.actor),el('pre',JSON.stringify(e.data,null,2)));d.append(x);}return d;}

function dialog(root){const dlg=el('dialog',undefined,'kn-dialog');const form=el('form');const title=el('h2');const fields=el('div',undefined,'kn-fields');const err=el('p','','kn-error');const row=el('div',undefined,'kn-row');
  const cancel=el('button','Cancel');cancel.type='button';cancel.onclick=()=>dlg.close();const save=el('button','Save');save.type='submit';row.append(cancel,save);form.append(title,fields,err,row);dlg.append(form);root.append(dlg);
  return (heading,build,submit)=>{title.textContent=heading;fields.replaceChildren();err.textContent='';build((name,label,value='',type='input')=>{const w=el('label',label);const e=document.createElement(type);e.name=name;e.value=value;w.append(e);fields.append(w);return e;});
    form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{await submit(new FormData(form));dlg.close();}catch(x){err.textContent=x.message;}finally{save.disabled=false;}};dlg.showModal();};}

function hashParams(view){const [v,q]=location.hash.slice(1).split('?');return v===view?new URLSearchParams(q||''):null;}
function hashFor(view,params){const q=new URLSearchParams(Object.entries(params).filter(([,v])=>v)).toString();return '#'+view+(q?'?'+q:'');}
// Switching to another dashboard view must fire hashchange so the shell routes it.
function jump(view,params){location.hash=hashFor(view,params);}
function setHash(view,params){const q=new URLSearchParams(Object.entries(params).filter(([,v])=>v)).toString();const h='#'+view+(q?'?'+q:'');if(location.hash!==h)history.pushState(null,'',h);}

// ---------------------------------------------------------------- Knowledge (wiki)
export async function mountKnowledge(root){
  loadCss();const c=client();
  root.replaceChildren();const layout=el('div',undefined,'kn-layout');const side=el('aside',undefined,'kn-side');const page=el('article',undefined,'kn-page');layout.append(side,page);root.append(layout);
  const form=dialog(root);
  const q=el('input');q.type='search';q.placeholder='Search the wiki';q.setAttribute('aria-label','Search the wiki');
  const home=btn('Knowledge home',()=>go(null),'kn-linkish');const add=btn('New page',()=>edit(null),'kn-primary');
  const showOld=el('label',' Show superseded','kn-dim');const chk=el('input');chk.type='checkbox';showOld.prepend(chk);
  const index=el('nav',undefined,'kn-index');index.setAttribute('aria-label','All pages');
  const tools=el('div',undefined,'kn-row');tools.append(home,add);side.append(q,tools,el('h3','All pages'),showOld,index);
  let pages=[],active=false,timer=null;
  const go=(slug,band)=>{setHash('knowledge',{p:slug,b:band});route();};
  const ref=(slug,band)=>{const hit=pages.find(p=>p.slug===slug&&(!band||p.band===band));if(hit||!slug)return go(slug,band);
    // [[slug]] may name a task/project: send it to Work.
    c.api('get',{id:slug,...(band?{band}:{})}).then(r=>{if(r.kind==='knowledge')go(slug,r.band);else jump('work',{t:r.slug,b:r.band});}).catch(()=>go(slug,band));};
  async function loadIndex(){pages=await c.listAll('knowledge');drawIndex(pages);}
  function drawIndex(list){index.replaceChildren();const shown=list.filter(p=>chk.checked||!['superseded','archived'].includes(p.state)).sort((a,b)=>a.title.localeCompare(b.title));
    if(!shown.length){index.append(el('p','No pages yet.','kn-dim'));return;}const multi=new Set(shown.map(p=>p.band)).size>1;
    for(const p of shown){const a=btn('',()=>go(p.slug,p.band),'kn-index-item');a.append(el('span',p.title),el('small',(multi?c.bandName(p.band)+' · ':'')+(p.verification||p.attrs?.verification||'unverified')));index.append(a);}}
  chk.onchange=()=>drawIndex(pages);
  q.onkeydown=async e=>{if(e.key!=='Enter')return;const term=q.value.trim();if(!term){drawIndex(pages);return;}const r=await c.searchAll(term,'knowledge');drawIndex(r.results);};
  function edit(r){form(r?'Edit page':'New page',f=>{f('title','Title',r?.title||'');if(!r)f('slug','Slug (optional, lowercase-with-dashes)','');
      f('body','Body — markdown-lite; link pages with [[slug]]',r?.body||'','textarea');if(!r){const b=f('band','Band','', 'select');for(const x of c.bands()){const o=el('option',x.name);o.value=x.id;if(x.primary)o.selected=true;b.append(o);}}},
    async d=>{if(r){await c.api('update',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,patch:{title:d.get('title'),body:d.get('body')}}});await open(r.slug,r.band);}
      else{const n=await c.api('create',{kind:'knowledge',band:d.get('band'),request_id:crypto.randomUUID(),data:{title:d.get('title'),body:d.get('body'),...(d.get('slug')?{slug:d.get('slug')}:{})}});await loadIndex();go(n.slug,n.band);}});}
  async function open(slug,band){const r=await c.api('get',{id:slug,...(band?{band}:{})});page.replaceChildren();
    const crumbs=el('nav',undefined,'kn-crumbs');crumbs.append(btn('Knowledge',()=>go(null),'kn-linkish'),document.createTextNode(' / '+r.title));page.append(crumbs);
    const head=el('header',undefined,'kn-head');head.append(el('h1',r.title));const meta=el('p',undefined,'kn-meta');
    meta.append(badge(r.attrs.verification||'unverified'),document.createTextNode(' '+(r.attrs.knowledge_kind||'page')+' · [['+r.slug+']] · '+c.bandName(r.band)+' · by '+r.creator+' · updated '+ago(r.updated)));head.append(meta);page.append(head);
    if(r.state==='superseded'){const w=el('p','This page is superseded','kn-warn');if(r.superseded_by){w.append(document.createTextNode(' by '));const a=el('a',r.superseded_by.title);a.href='#';a.onclick=e=>{e.preventDefault();go(r.superseded_by.slug,r.band);};w.append(a);}page.append(w);}
    page.append(renderBody(r.body,s=>ref(s,r.band)));
    if((r.attrs.tags||[]).length){const t=el('p',undefined,'kn-tags');for(const x of r.attrs.tags)t.append(badge(x,'tag'));page.append(t);}
    const acts=el('div',undefined,'kn-row');acts.append(btn('Edit',()=>edit(r)));if(r.state!=='archived')acts.append(btn('Archive',async()=>{await c.api('update',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,patch:{state:'archived'}}});await loadIndex();go(null);}));page.append(acts);
    if(r.backlinks.length){page.append(el('h2','What links here'));const ul=el('ul',undefined,'kn-list');for(const b of r.backlinks){const li=el('li');const a=el('a',b.title);a.href='#';a.onclick=e=>{e.preventDefault();ref(b.slug,r.band);};li.append(a,el('span',' · '+b.kind,'kn-dim'));ul.append(li);}page.append(ul);}
    if(r.links.length){page.append(el('h2','Sources and evidence'),linksTable(r.links));}
    page.append(historyBlock(r.events));page.focus?.();}
  async function drawHome(){page.replaceChildren(el('h1','Knowledge'),el('p','What the agent team knows, with sources. Agents write these pages as they work; everything starts unverified until someone links evidence with a traceable id.','kn-dim'));
    const recent=pages.filter(p=>p.state==='active').sort((a,b)=>b.updated-a.updated).slice(0,12);
    const counts={};for(const p of pages)counts[p.verification||'unverified']=(counts[p.verification||'unverified']||0)+1;
    const stats=el('p',undefined,'kn-meta');stats.append(document.createTextNode(pages.length+' pages · '));for(const [k,n] of Object.entries(counts))stats.append(badge(n+' '+k,k),document.createTextNode(' '));page.append(stats);
    page.append(el('h2','Recently updated'));const ul=el('ul',undefined,'kn-list');for(const p of recent){const li=el('li');const a=el('a',p.title);a.href='#';a.onclick=e=>{e.preventDefault();go(p.slug,p.band);};li.append(a,el('span',' · '+ago(p.updated)+' · '+(p.creator||''),'kn-dim'));if(p.excerpt)li.append(el('div',p.excerpt.slice(0,160),'kn-dim'));ul.append(li);}
    if(!recent.length)ul.append(el('li','Nothing yet.','kn-dim'));page.append(ul);}
  async function route(){const p=hashParams('knowledge');if(!p)return;try{const slug=p.get('p');if(slug)await open(slug,p.get('b'));else await drawHome();}catch(e){page.replaceChildren(el('p',e.message,'kn-error'));}}
  const onHash=()=>{if(active)route();};
  return {async activate(){active=true;window.addEventListener('hashchange',onHash);window.addEventListener('popstate',onHash);try{await loadIndex();await route();}catch(e){page.replaceChildren(el('p',e.message,'kn-error'));}timer=setInterval(()=>{if(active&&!document.hidden)loadIndex().catch(()=>{});},60000);},
    deactivate(){active=false;window.removeEventListener('hashchange',onHash);window.removeEventListener('popstate',onHash);clearInterval(timer);}};
}

// ---------------------------------------------------------------- Work (projects and tasks)
export async function mountTasks(root){
  loadCss();const c=client();
  root.replaceChildren();const layout=el('div',undefined,'kn-layout');const side=el('aside',undefined,'kn-side');const main=el('section',undefined,'kn-page');layout.append(side,main);root.append(layout);
  const form=dialog(root);
  let deck=[],concepts=[],active=false,timer=null,version=0;
  const go=params=>{setHash('work',params);route();};
  function taskCard(t,band){const b=btn('',()=>go({t:t.slug,b:band}),'kn-card');const top=el('div',undefined,'kn-card-top');top.append(badge(STATE_LABEL[t.state]||t.state,t.state),el('strong',t.title));b.append(top);
    if(t.claimants&&t.claimants.length)b.append(el('small','On it: '+t.claimants.map(x=>x.actor+' ('+ago(x.last_active)+')').join(', ')));
    if(t.needs_hygiene)b.append(el('small','Needs hygiene: idle with unrecorded work','kn-warn'));
    if(t.blocked_reason)b.append(el('small','Blocked: '+t.blocked_reason));
    if(t.latest_handoff)b.append(el('small','Last handoff '+ago(t.latest_handoff.ts)));
    if(t.outcome)b.append(el('small','Outcome: '+t.outcome));
    b.append(el('small',ago(t.updated)+(t.creator?' · by '+t.creator:''),'kn-dim'));return b;}
  function section(title,items,band){if(!items.length)return null;const s=el('div',undefined,'kn-section');s.append(el('h3',title+' ('+items.length+')'));for(const t of items)s.append(taskCard(t,t._band||band));return s;}
  async function load(){const v=++version;const d=await c.api('deck');let cs=[];try{cs=await c.listAll('concept');}catch{}if(v!==version)return;deck=d.deck;concepts=cs;drawSide();}
  function drawSide(){side.replaceChildren();const row=el('div',undefined,'kn-row');row.append(btn('All work',()=>go({}),'kn-linkish'),btn('New',()=>create(),'kn-primary'));side.append(row);
    const byConcept={};for(const p of deck){const k=p.project.parent||'';(byConcept[k]=byConcept[k]||[]).push(p);}
    const cname=id=>(concepts.find(x=>x.id===id)||{}).title||'Other projects';
    for(const [cid,ps] of Object.entries(byConcept)){side.append(el('h3',cname(cid)));for(const p of ps){const n=p.in_progress.length,b2=p.blocked.length,t=p.todo.length;const a=btn('',()=>go({p:p.project.slug,b:p.band}),'kn-index-item');a.append(el('span',p.project.title),el('small',[n&&n+' active',b2&&b2+' blocked',t&&t+' to do'].filter(Boolean).join(' · ')||'nothing open'));side.append(a);}}
    if(!deck.length)side.append(el('p','No projects yet. Agents create them as they work.','kn-dim'));}
  function drawOverview(){main.replaceChildren(el('h1','Work'),el('p','What the agents are tracking across all bands. Ask an agent about a project and tell it what to pick up; it records the work here.','kn-dim'));
    const all=k=>deck.flatMap(p=>p[k].map(t=>({...t,_band:p.band,_project:p.project.title})));
    const hygiene=all('in_progress').filter(t=>t.needs_hygiene);
    for(const [title,items] of [['Needs hygiene',hygiene],['In progress',all('in_progress')],['Blocked',all('blocked')],['Paused',all('paused')],['Recently done',all('recently_done')]]){const s=section(title,items);if(s)main.append(s);}
    if(main.children.length===2)main.append(el('p','Nothing in flight right now.','kn-dim'));}
  function drawProject(slug,band){const p=deck.find(x=>x.project.slug===slug&&(!band||x.band===band));if(!p){main.replaceChildren(el('p','Project not found (it may be archived).','kn-error'));return;}
    main.replaceChildren();const crumbs=el('nav',undefined,'kn-crumbs');crumbs.append(btn('Work',()=>go({}),'kn-linkish'),document.createTextNode(' / '+p.project.title));main.append(crumbs,el('h1',p.project.title));
    const meta=el('p',undefined,'kn-meta');meta.append(badge(STATE_LABEL[p.project.state]||p.project.state,p.project.state),document.createTextNode(' '+c.bandName(p.band)+' · [['+p.project.slug+']] · by '+(p.project.creator||'')));main.append(meta);
    if(p.project.excerpt)main.append(renderBody(p.project.excerpt,x=>refOpen(x,p.band)));
    const acts=el('div',undefined,'kn-row');acts.append(btn('Open details',()=>go({t:p.project.slug,b:p.band})),btn('New task',()=>create({parent:p.project.slug,band:p.band,kind:'task'})));main.append(acts);
    for(const [k,title] of [['in_progress','In progress'],['blocked','Blocked'],['paused','Paused'],['todo','To do'],['recently_done','Recently done']]){const s=section(title,p[k],p.band);if(s)main.append(s);}}
  async function drawTask(slug,band){const r=await c.api('get',{id:slug,...(band?{band}:{})});main.replaceChildren();
    const crumbs=el('nav',undefined,'kn-crumbs');crumbs.append(btn('Work',()=>go({}),'kn-linkish'));
    const proj=deck.find(p=>p.band===r.band&&(p.project.id===r.parent||p.project.id===r.id||[...p.in_progress,...p.blocked,...p.paused,...p.todo,...p.recently_done].some(t=>t.id===r.id)));
    if(proj&&proj.project.id!==r.id){crumbs.append(document.createTextNode(' / '));crumbs.append(btn(proj.project.title,()=>go({p:proj.project.slug,b:proj.band}),'kn-linkish'));}
    crumbs.append(document.createTextNode(' / '+r.title));main.append(crumbs,el('h1',r.title));
    const meta=el('p',undefined,'kn-meta');meta.append(badge(STATE_LABEL[r.state]||r.state,r.state),document.createTextNode(' '+r.kind+' · [['+r.slug+']] · '+c.bandName(r.band)+' · created by '+r.creator+' · updated '+ago(r.updated)));main.append(meta);
    const live=r.claims.filter(x=>!x.released);if(live.length){const s=el('div',undefined,'kn-callout');s.append(el('strong','On it'));for(const x of live)s.append(el('div',x.actor+' · since '+date(x.started)+' · last active '+ago(x.last_active)+(x.dirty?' · needs hygiene':'')));main.append(s);}
    if(r.attrs.outcome){const s=el('div',undefined,'kn-callout');s.append(el('strong',r.state==='cancelled'?'Why cancelled':'Outcome'),el('div',r.attrs.outcome));main.append(s);}
    if(r.attrs.blocked_reason)main.append(el('p','Blocked: '+r.attrs.blocked_reason,'kn-warn'));
    main.append(renderBody(r.body,s=>refOpen(s,r.band)));
    for(const [key,label] of [['criteria','Done when'],['dependencies','Depends on'],['workers','Workers'],['tags','Tags']]){const v=r.attrs[key]||[];if(v.length){main.append(el('h2',label));const ul=el('ul',undefined,'kn-list');for(const x of v){const li=el('li');if(key==='dependencies'){const a=el('a',x);a.href='#';a.onclick=e=>{e.preventDefault();refOpen(x,r.band);};li.append(a);}else li.textContent=x;ul.append(li);}main.append(ul);}}
    const acts=el('div',undefined,'kn-row');acts.append(btn('New subtask',()=>create({parent:r.slug,band:r.band,kind:'task'})));if(r.state!=='archived')acts.append(btn('Archive',async()=>{await c.api('update',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,patch:{state:'archived'}}});await load();go({});}));main.append(acts);
    if(r.children.length){main.append(el('h2','Subtasks'));for(const k of r.children)main.append(taskCard(k,r.band));}
    if(r.links.length){main.append(el('h2','Audit trail'),linksTable(r.links));}
    if(r.claims.length>live.length){const d=el('details');d.append(el('summary','Past claims'));for(const x of r.claims.filter(x=>x.released))d.append(el('div',x.actor+' · '+date(x.started)+' → '+date(x.released),'kn-dim'));main.append(d);}
    if(r.backlinks.length){main.append(el('h2','Referenced by'));const ul=el('ul',undefined,'kn-list');for(const b of r.backlinks){const li=el('li');const a=el('a',b.title);a.href='#';a.onclick=e=>{e.preventDefault();refOpen(b.slug,r.band);};li.append(a,el('span',' · '+b.kind,'kn-dim'));ul.append(li);}main.append(ul);}
    main.append(historyBlock(r.events));}
  function refOpen(slug,band){c.api('get',{id:slug,...(band?{band}:{})}).then(r=>{if(r.kind==='knowledge')jump('knowledge',{p:r.slug,b:r.band});else go({t:r.slug,b:r.band});}).catch(()=>{});}
  function create(opts={}){form('New '+(opts.kind||'record'),f=>{const k=f('kind','Type',opts.kind||'task','select');for(const x of ['task','project','concept']){const o=el('option',x);o.value=x;k.append(o);}k.value=opts.kind||'task';
      f('title','Title');f('body','Details (markdown-lite, [[slug]] links)','','textarea');f('parent','Parent: a concept for a project; a project or task for a task (slug)',opts.parent||'');
      const b=f('band','Band','','select');for(const x of c.bands()){const o=el('option',x.name);o.value=x.id;if(opts.band?x.id===opts.band:x.primary)o.selected=true;b.append(o);}},
    async d=>{const n=await c.api('create',{kind:d.get('kind'),band:d.get('band'),request_id:crypto.randomUUID(),data:{title:d.get('title'),body:d.get('body'),...(d.get('parent')?{parent:d.get('parent')}:{})}});await load();go(d.get('kind')==='project'?{p:n.slug,b:n.band}:{t:n.slug,b:n.band});});}
  async function route(){const p=hashParams('work');if(!p)return;try{if(p.get('t'))await drawTask(p.get('t'),p.get('b'));else if(p.get('p'))drawProject(p.get('p'),p.get('b'));else drawOverview();}catch(e){main.replaceChildren(el('p',e.message,'kn-error'));}}
  const onHash=()=>{if(active)route();};
  return {async activate(){active=true;window.addEventListener('hashchange',onHash);window.addEventListener('popstate',onHash);try{await load();await route();}catch(e){main.replaceChildren(el('p',e.message,'kn-error'));}
      timer=setInterval(async()=>{if(!active||document.hidden||document.querySelector('.kn-dialog[open]'))return;try{await load();const p=hashParams('work');if(p&&!p.get('t'))route();}catch{}},30000);},
    deactivate(){active=false;window.removeEventListener('hashchange',onHash);window.removeEventListener('popstate',onHash);clearInterval(timer);}};
}
