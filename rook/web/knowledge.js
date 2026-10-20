// Knowledge (a wiki) and Work (projects and tasks): human views over the
// records agents keep. Mostly read-only: agents write; the user observes and
// directs. All stored text is rendered as text nodes, never as HTML.

// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {el, ago: agoOr, more, copyChip, info, useCss} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);
const date=t=>t?new Date(t*1000).toLocaleString():'';
const ago=t=>agoOr(t,'');
const STATE_LABEL={in_progress:'In progress',todo:'To do',blocked:'Blocked',paused:'Paused',done:'Done',closed:'Closed',cancelled:'Cancelled',archived:'Archived',active:'Active',superseded:'Superseded'};

function loadCss(){useCss();if(document.querySelector('link[data-kn]'))return;const css=document.createElement('link');css.rel='stylesheet';css.href='/account/knowledge/assets/knowledge.css'+new URL(import.meta.url).search;css.dataset.kn='1';document.head.append(css);}

function client(){
  let csrf='',bands=[],actor={};
  async function boot(){if(csrf)return;const r=await fetch('/account/knowledge/api');if(!r.ok)throw Error('Sign in with your operator account to view this.');const d=await r.json();csrf=d.csrf;bands=d.bands;actor=d.actor||{};}
  async function api(action,body={}){await boot();const r=await fetch('/account/knowledge/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,action,...body})});const d=await r.json();if(!r.ok||d.error||d.ok===false)throw Error(d.error||'Request failed');return d.result;}
  async function listAll(kind){await boot();const out=[];for(const b of bands){const r=await api('list',{band:b.id,kind,data:{limit:200}});for(const x of r.records)out.push({...x,band:x.band||b.id});}return out;}
  async function searchAll(query,kind){await boot();const out=[];let semantic=false;for(const b of bands){const r=await api('search',{band:b.id,query,kind});semantic=semantic||r.semantic;for(const x of r.results)out.push({...x,band:x.band||b.id,excerpt:x.body});}return {results:out,semantic};}
  return {api,boot,listAll,searchAll,bands:()=>bands,bandName:id=>(bands.find(b=>b.id===id)||{}).name||id,me:()=>actor.label||actor.id||'the operator'};
}

function btn(label,fn,cls){const b=el('button',label,cls);b.type='button';b.onclick=e=>{e.preventDefault();Promise.resolve().then(fn).catch(err=>console.warn(err));};return b;}

// Minimal, safe markdown: #/##/### headings, - and 1. lists, ``` code blocks, `code`, **bold**, [[slug]] and http(s) links.
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
    const li=line.match(/^\s*[-*]\s+(.*)$/)||line.match(/^\s*\d+[.)]\s+(.*)$/);if(li){flush();const tag=/^\s*\d/.test(line)?'OL':'UL';if(!list||list.tagName!==tag){list=el(tag.toLowerCase());out.append(list);}const e=el('li');inline(li[1],e);list.append(e);continue;}
    // An indented line goes on with the list item above it (the user manual wraps them).
    if(list&&/^\s{2,}\S/.test(line)){inline(' '+line.trim(),list.lastChild);continue;}
    if(!line.trim()){flush();list=null;continue;}
    list=null;para.push(line.trim());}
  if(code!==null)out.append(el('pre',code.join('\n')));flush();
  if(!out.childNodes.length)out.append(el('p','(empty)','kn-dim'));return out;
}

// One-line breadcrumbs: [label, onclick] links, the last part plain text; long parts ellipsize.
function crumbsNav(parts){const n=el('nav',undefined,'kn-crumbs');n.setAttribute('aria-label','Breadcrumbs');parts.forEach((x,i)=>{if(i)n.append(el('span','/','kn-sep'));
  if(Array.isArray(x)){const b=btn(x[0],x[1],'kn-linkish kn-crumb');b.title=x[0];n.append(b);}else{const t=el('span',x,'kn-crumb kn-here');t.title=x;n.append(t);}});return n;}
// The side list folds behind a toggle on phones (knowledge.css); always open on desktop.
function foldSide(side,label,body){const tog=el('button',label,'kn-side-toggle');tog.type='button';tog.setAttribute('aria-expanded','false');
  tog.onclick=()=>{const o=side.classList.toggle('kn-open');tog.setAttribute('aria-expanded',String(o));};
  const box=el('div',undefined,'kn-side-body');box.append(...body);side.append(tog,box);return {tog,box,close(){side.classList.remove('kn-open');tog.setAttribute('aria-expanded','false');}};}

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
  const add=btn('+',()=>edit(null,null),'kn-primary kn-add');add.setAttribute('aria-label','New page');add.title='New page';
  const showOld=el('label',' Old','kn-dim kn-old-toggle');showOld.title='Show superseded and archived pages';const chk=el('input');chk.type='checkbox';chk.setAttribute('aria-label','Show superseded pages');showOld.prepend(chk);
  const index=el('nav',undefined,'kn-index');index.setAttribute('aria-label','All pages');
  const reviewBtn=btn('Review',()=>startReview(),'kn-review-btn');
  const tools=el('div',undefined,'kn-tools');tools.append(q,reviewBtn,add);side.append(tools);
  const pagesHead=el('div',undefined,'kn-side-head');pagesHead.append(el('h3','Pages'),showOld);
  const fold=foldSide(side,'Pages',[pagesHead,index]);
  let pages=[],active=false,timer=null,cur=null,searching=false,reviewing=false;
  // Pages nest under pages (record.parent), like folders. Which folders are
  // open is remembered per browser.
  let open_=new Set();try{open_=new Set(JSON.parse(localStorage.getItem('kn-open')||'[]'));}catch{}
  const saveOpen=()=>{try{localStorage.setItem('kn-open',JSON.stringify([...open_]));}catch{}};
  const byId=id=>pages.find(p=>p.id===id);
  const ancestors=p=>{const out=[],seen=new Set();let x=p&&p.parent&&byId(p.parent);while(x&&!seen.has(x.id)){seen.add(x.id);out.unshift(x);x=x.parent&&byId(x.parent);}return out;};
  const pathOf=p=>[...ancestors(p),p].map(x=>x.title).join(' / ');
  const visible=p=>chk.checked||!['superseded','archived'].includes(p.state);
  const byTitle=(a,b)=>a.title.localeCompare(b.title);
  const go=(slug,band,review=reviewing)=>{fold.close();setHash('knowledge',{p:slug,b:band,r:review&&slug?'1':''});route();};
  const ref=(slug,band)=>{const hit=pages.find(p=>p.slug===slug&&(!band||p.band===band));if(hit||!slug)return go(slug,band);
    // [[slug]] may name a task/project: send it to Work.
    c.api('get',{id:slug,...(band?{band}:{})}).then(r=>{if(r.kind==='knowledge')go(slug,r.band);else jump('work',{t:r.slug,b:r.band});}).catch(()=>go(slug,band));};
  async function loadIndex(){pages=await c.listAll('knowledge');if(!searching)drawTree();
    const n=queue().length;reviewBtn.textContent=n?'Review '+n:'Review';reviewBtn.title=n?n+' pages nobody has verified yet':'Everything is verified';
    fold.tog.textContent='Pages · '+pages.filter(visible).length;}
  // Review mode walks the unverified pages in tree order.
  const vstate=p=>p.verification||p.attrs?.verification||'unverified';
  function treeOrder(){const out=[];const walk=(parent,b)=>{for(const p of folders(kids(parent,b))){out.push(p);walk(p.id,b);}};for(const b of [...new Set(pages.map(p=>p.band))])walk(null,b);return out;}
  const queue=()=>treeOrder().filter(p=>p.state==='active'&&vstate(p)==='unverified');
  function startReview(){const q_=queue();if(q_.length)go(q_[0].slug,q_[0].band,true);else{setHash('knowledge',{});reviewing=false;drawHome();}}
  function nextInQueue(afterId){const q_=queue().filter(p=>p.id!==afterId);if(!q_.length)return null;const order=treeOrder().map(p=>p.id);const i=order.indexOf(afterId);return q_.find(p=>order.indexOf(p.id)>i)||q_[0];}
  function advance(afterId){const n=nextInQueue(afterId);if(n)go(n.slug,n.band,true);else{reviewing=false;setHash('knowledge',{});drawHome(true);}}
  async function review(r,verdict,note){await c.api('review',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,verdict,...(note?{note}:{})}});await loadIndex();}
  function dispute(r,then){form('Dispute “'+r.title+'”',f=>{f('note',"What's wrong? (an agent fixes it)",r.attrs.dispute_reason||'','textarea');},
    async d=>{await review(r,'disputed',d.get('note'));then();});}
  const folders=list=>list.map(p=>[kids(p.id,p.band).length>0,p]).sort((x,y)=>y[0]-x[0]||byTitle(x[1],y[1])).map(x=>x[1]);
  function kids(parent,band){return pages.filter(p=>visible(p)&&p.band===band&&(parent?p.parent===parent:!(p.parent&&byId(p.parent)&&visible(byId(p.parent))))).sort(byTitle);}
  function drawTree(){searching=false;index.replaceChildren();
    const cp=cur&&pages.find(p=>p.slug===cur.slug&&(!cur.band||p.band===cur.band));
    if(cp)for(const a of ancestors(cp))open_.add(a.id);
    const bands=[...new Set(pages.filter(visible).map(p=>p.band))];
    if(!bands.length){index.append(el('p','No pages yet.','kn-dim'));return;}
    const node=(p,depth)=>{const ch=kids(p.id,p.band),isOpen=open_.has(p.id);
      const row=el('div',undefined,'kn-tree-row'+(cp&&cp.id===p.id?' kn-current':''));row.style.paddingLeft=(depth*14)+'px';
      const tog=btn(ch.length?(isOpen?'▾':'▸'):'',()=>{if(!ch.length)return;isOpen?open_.delete(p.id):open_.add(p.id);saveOpen();drawTree();},'kn-tog');
      tog.setAttribute('aria-label',ch.length?(isOpen?'Collapse ':'Expand ')+p.title:'');if(!ch.length)tog.tabIndex=-1;
      const a=btn('',()=>go(p.slug,p.band),'kn-tree-item');const v=vstate(p);const dot=el('i',undefined,'kn-dot kn-dot-'+v);dot.title=v;a.append(dot,el('span',p.title));if(ch.length)a.append(el('small',String(ch.length)));
      if(['superseded','archived'].includes(p.state))a.classList.add('kn-old');
      row.append(tog,a);index.append(row);if(isOpen)for(const k of folders(ch))node(k,depth+1);};

    for(const b of bands){if(bands.length>1)index.append(el('h4',c.bandName(b),'kn-tree-band'));for(const p of folders(kids(null,b)))node(p,0);}}
  function drawResults(list){searching=true;index.replaceChildren();const shown=list.filter(visible).sort(byTitle);
    if(!shown.length){index.append(el('p','No matches.','kn-dim'));return;}
    for(const p of shown){const full=byId(p.id)||p;const a=btn('',()=>go(p.slug,p.band),'kn-index-item');a.append(el('span',p.title));const anc=ancestors(full);if(anc.length)a.append(el('small',anc.map(x=>x.title).join(' / ')));index.append(a);}}
  chk.onchange=()=>searching?null:drawTree();
  q.onkeydown=async e=>{if(e.key!=='Enter')return;const term=q.value.trim();if(!term){drawTree();return;}const r=await c.searchAll(term,'knowledge');drawResults(r.results);};
  q.oninput=()=>{if(!q.value.trim()&&searching)drawTree();};
  // Parent picker: every page in tree order, indented, minus the page itself and its subpages.
  function parentSelect(f,value,band,exclude){const s=f('parent','Parent','','select');const none=el('option','— Top level —');none.value='';s.append(none);
    const walk=(parent,b,depth)=>{for(const p of pages.filter(x=>x.band===b&&x.state!=='archived'&&(parent?x.parent===parent:!(x.parent&&byId(x.parent)))).sort(byTitle)){if(exclude&&p.id===exclude)continue;
      const o=el('option','  '.repeat(depth)+p.title+(bands().length>1?' ('+c.bandName(b)+')':''));o.value=p.id;s.append(o);walk(p.id,b,depth+1);}};
    const bands=()=>[...new Set(pages.map(p=>p.band))];for(const b of (band?[band]:bands()))walk(null,b,0);s.value=value||'';return s;}
  function edit(r,parentId){form(r?'Edit page':'New page',f=>{f('title','Title',r?.title||'');
      parentSelect(f,r?r.parent:parentId,r?.band,r?.id);
      if(!r)f('slug','Slug (optional)','');
      f('body','Body (markdown, [[slug]] links)',r?.body||'','textarea');
      if(!r&&c.bands().length>1){const b=f('band','Band','', 'select');for(const x of c.bands()){const o=el('option',x.name);o.value=x.id;if(x.primary)o.selected=true;b.append(o);}}},
    async d=>{const parent=d.get('parent')||null;
      if(r){const patch={title:d.get('title'),body:d.get('body')};if((r.parent||null)!==parent)patch.parent=parent;
        await c.api('update',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,patch}});await loadIndex();await open(r.slug,r.band);}
      else{const band=parent?byId(parent).band:(d.get('band')||c.bands()[0]?.id);
        const n=await c.api('create',{kind:'knowledge',band,request_id:crypto.randomUUID(),data:{title:d.get('title'),body:d.get('body'),...(parent?{parent}:{}),...(d.get('slug')?{slug:d.get('slug')}:{})}});
        if(parent){open_.add(parent);saveOpen();}await loadIndex();go(n.slug,n.band);}});}
  function reviewBlock(r){const v=r.attrs.verification||'unverified',who=r.attrs.reviewed_label,when=r.attrs.reviewed_at;const box=el('div',undefined,'kn-review kn-review-'+v);
    const reopen=()=>open(r.slug,r.band);
    if(v==='verified'){box.append(el('span',who?'✓ Verified · '+who+' · '+ago(when):'✓ Verified (evidence)'),el('span',undefined,'kn-grow'),btn('Unverify',async()=>{await review(r,'unverified');reopen();},'kn-linkish'));}
    else if(v==='disputed'){const t=el('div');t.append(el('strong','Disputed'+(who?' by '+who:'')),el('div',r.attrs.dispute_reason||''));box.append(t,el('span',undefined,'kn-grow'),btn('✓ Verify',async()=>{await review(r,'verified');reopen();},'kn-ok'),btn('Clear',async()=>{await review(r,'unverified');reopen();}));}
    else{const t=el('div');t.append(el('span','Unverified'));if(r.attrs.review_note)t.append(el('div',r.attrs.review_note,'kn-warn'));
      box.append(t,el('span',undefined,'kn-grow'));if(!reviewing)box.append(btn('✓ Verify',async()=>{await review(r,'verified');reopen();},'kn-ok'),btn('Dispute',()=>dispute(r,reopen)));}
    return box;}
  function move(r){form('Move “'+r.title+'”',f=>{parentSelect(f,r.parent,r.band,r.id);},
    async d=>{const parent=d.get('parent')||null;if((r.parent||null)===parent)return;
      await c.api('update',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,patch:{parent}}});await loadIndex();await open(r.slug,r.band);});}
  async function open(slug,band){const r=await c.api('get',{id:slug,...(band?{band}:{})});cur={slug:r.slug,band:r.band};if(!searching)drawTree();page.replaceChildren();
    page.append(crumbsNav([['Knowledge',()=>go(null)],...ancestors(byId(r.id)||r).map(a=>[a.title,()=>go(a.slug,a.band)]),r.title]));
    const head=el('header',undefined,'kn-head');const titleRow=el('div',undefined,'kn-titlerow');titleRow.append(el('h1',r.title));
    const pageActs=el('div',undefined,'kn-acts');pageActs.append(btn('Edit',()=>edit(r),'kn-primary'),more(()=>[{label:'New subpage',run:()=>edit(null,r.id)},{label:'Move…',run:()=>move(r)},
      r.state!=='archived'&&{label:'Archive',danger:true,run:async()=>{await c.api('update',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,patch:{state:'archived'}}});await loadIndex();go(null);}}],'More actions for this page'));
    titleRow.append(pageActs);head.append(titleRow);const meta=el('p',undefined,'kn-meta');
    meta.append(badge(r.attrs.verification||'unverified'),el('span',r.attrs.knowledge_kind||'page'),copyChip('[['+r.slug+']]'),el('span',c.bandName(r.band)+' · '+r.creator+' · '+ago(r.updated)));head.append(meta);
    if(reviewing){const bar=el('div',undefined,'kn-reviewbar');const left=queue().filter(p=>p.id!==r.id).length;
      const exit=btn('✕',()=>go(r.slug,r.band,false),'kn-icon');exit.setAttribute('aria-label','Exit review');exit.title='Exit review';
      bar.append(el('strong','Review'),el('span',left?left+' left':'last','kn-dim'),el('span',undefined,'kn-grow'),
        btn('✓ Verify',async()=>{await review(r,'verified');advance(r.id);},'kn-ok'),
        btn('Dispute',()=>dispute(r,()=>advance(r.id))),btn('Skip',()=>advance(r.id)),exit);
      page.prepend(bar);}
    page.append(head,reviewBlock(r));
    if(r.state==='superseded'){const w=el('p','This page is superseded','kn-warn');if(r.superseded_by){w.append(document.createTextNode(' by '));const a=el('a',r.superseded_by.title);a.href='#';a.onclick=e=>{e.preventDefault();go(r.superseded_by.slug,r.band);};w.append(a);}page.append(w);}
    page.append(renderBody(r.body,s=>ref(s,r.band)));
    if((r.attrs.tags||[]).length){const t=el('p',undefined,'kn-tags');for(const x of r.attrs.tags)t.append(badge(x,'tag'));page.append(t);}
    const sub=r.children.filter(x=>x.kind==='knowledge'&&(chk.checked||!['superseded','archived'].includes(x.state))).sort(byTitle);
    if(sub.length){page.append(el('h2','Subpages'));const ul=el('ul',undefined,'kn-list');for(const b of sub){const li=el('li');const a=el('a',b.title);a.href='#';a.onclick=e=>{e.preventDefault();go(b.slug,r.band);};li.append(a);const n=kids(b.id,r.band).length;if(n)li.append(el('span',' · '+n+' page'+(n>1?'s':''),'kn-dim'));ul.append(li);}page.append(ul);}
    if(r.backlinks.length){page.append(el('h2','What links here'));const ul=el('ul',undefined,'kn-list');for(const b of r.backlinks){const li=el('li');const a=el('a',b.title);a.href='#';a.onclick=e=>{e.preventDefault();ref(b.slug,r.band);};li.append(a,el('span',' · '+b.kind,'kn-dim'));ul.append(li);}page.append(ul);}
    if(r.links.length){page.append(el('h2','Sources'),linksTable(r.links));}
    page.append(historyBlock(r.events));page.focus?.();}
  async function drawHome(done){cur=null;if(!searching)drawTree();page.replaceChildren();
    const recent=pages.filter(p=>p.state==='active').sort((a,b)=>b.updated-a.updated).slice(0,12);
    const counts={};for(const p of pages)counts[p.verification||'unverified']=(counts[p.verification||'unverified']||0)+1;
    if(done)page.append(el('p','✓ All verified.','kn-review kn-review-verified'));
    const stats=el('p',undefined,'kn-meta');stats.append(document.createTextNode(pages.length+' pages'));for(const [k,n] of Object.entries(counts))stats.append(badge(n+' '+k,k),document.createTextNode(' '));page.append(stats);
    const sections=[...new Set(pages.map(p=>p.band))].flatMap(b=>kids(null,b).filter(p=>kids(p.id,b).length));
    if(sections.length){page.append(el('h2','Sections'));const grid=el('div',undefined,'kn-sections');for(const s of sections){const n=kids(s.id,s.band).length;const b=btn('',()=>go(s.slug,s.band),'kn-card');const top=el('div',undefined,'kn-card-top');top.append(el('strong',s.title));b.append(top,el('small',n+' page'+(n>1?'s':'')+': '+kids(s.id,s.band).slice(0,4).map(x=>x.title).join(', ')+(n>4?'…':'')));grid.append(b);}page.append(grid);}
    page.append(el('h2','Recent'));const ul=el('ul',undefined,'kn-list');for(const p of recent){const li=el('li');const a=el('a',p.title);a.href='#';a.onclick=e=>{e.preventDefault();go(p.slug,p.band);};li.append(a);const anc=ancestors(p);li.append(el('span',(anc.length?' · '+anc.map(x=>x.title).join(' / '):'')+' · '+ago(p.updated)+' · '+(p.creator||''),'kn-dim'));if(p.excerpt)li.append(el('div',p.excerpt.slice(0,160),'kn-dim kn-excerpt'));ul.append(li);}
    if(!recent.length)ul.append(el('li','Nothing yet.','kn-dim'));page.append(ul);}
  async function route(){const p=hashParams('knowledge');if(!p)return;reviewing=p.get('r')==='1';try{const slug=p.get('p');if(slug)await open(slug,p.get('b'));else await drawHome();}catch(e){page.replaceChildren(el('p',e.message,'kn-error'));}}
  const onHash=()=>{if(active)route();};
  return {async activate(){active=true;window.addEventListener('hashchange',onHash);window.addEventListener('popstate',onHash);try{await loadIndex();await route();}catch(e){page.replaceChildren(el('p',e.message,'kn-error'));}timer=setInterval(()=>{if(active&&!document.hidden)loadIndex().catch(()=>{});},60000);},
    deactivate(){active=false;window.removeEventListener('hashchange',onHash);window.removeEventListener('popstate',onHash);clearInterval(timer);}};
}

// ---------------------------------------------------------------- Work (projects and tasks)
// Views: Overview (the deck), Board, List, Timeline (workviews.js, over the
// "board" read), Huddle (huddle.js), a project page and a task page. The hash
// carries the view and its filters: #work?v=board&project=<id>&label=…
const WORK_TABS=[['','Overview'],['board','Board'],['list','List'],['timeline','Timeline'],['huddle','Huddle']];
export async function mountTasks(root){
  loadCss();const c=client();const search=new URL(import.meta.url).search;
  const wv=await import('/account/knowledge/assets/workviews.js'+search);wv.loadCss();
  root.replaceChildren();
  const bar=el('div',undefined,'wv-bar');const tabs=el('nav',undefined,'wv-tabs');tabs.setAttribute('aria-label','Work views');
  const msg=el('span','','wv-msg');msg.setAttribute('role','status');
  const newBtn=btn('+ New',()=>create(),'kn-primary wv-new');newBtn.setAttribute('aria-label','New task or project');newBtn.title='New task or project';
  bar.append(tabs,msg,newBtn);
  const layout=el('div',undefined,'kn-layout');const side=el('aside',undefined,'kn-side');const main=el('section',undefined,'kn-page');layout.append(side,main);
  const full=el('section',undefined,'wv-full');full.hidden=true;root.append(bar,layout,full);
  const form=dialog(root);
  let deck=[],concepts=[],active=false,timer=null,version=0,board=null,boardKey='',huddle=null,huddleUI=null,pins=0,msgTimer=null;
  const go=params=>{setHash('work',params);route();};
  const say=(text,bad)=>{msg.textContent=text;msg.classList.toggle('wv-err',!!bad);clearTimeout(msgTimer);msgTimer=setTimeout(()=>{msg.textContent='';},bad?9000:4000);};
  const kit={api:c.api,bandName:c.bandName,me:()=>c.me(),host:root,renderBody,refOpen:(s,b)=>refOpen(s,b),
    reload:async()=>{await refresh(true);},
    move:async(t,to)=>{try{if(await wv.moveTask(kit,t,to)){say('Moved “'+t.title+'” to '+(wv.COLUMNS.find(x=>x[0]===to)||[0,to])[1]+'.');await refresh(true);}}
      catch(e){say(e.message,true);if(/changed|revision/i.test(e.message))await refresh(true);}}};
  function drawTabs(current){tabs.replaceChildren();for(const [v,label] of WORK_TABS){const a=el('a',label);a.href='#work'+(v?'?v='+v:'');
      if(v===current)a.setAttribute('aria-current','page');if(v==='huddle'&&pins){const b=el('span',String(pins),'wv-pin');b.title=pins+' question'+(pins>1?'s':'')+' waiting for you';a.append(b);}tabs.append(a);}}
  function show(which){layout.hidden=which!=='layout';full.hidden=which!=='full';}
  function taskCard(t,band){const b=btn('',()=>go({t:t.slug,b:band}),'kn-card');const top=el('div',undefined,'kn-card-top');top.append(badge(STATE_LABEL[t.state]||t.state,t.state),el('strong',t.title));b.append(top);
    if(t.claimants&&t.claimants.length)b.append(el('small','On it: '+t.claimants.map(x=>x.actor+' ('+ago(x.last_active)+')').join(', ')));
    if(t.needs_hygiene)b.append(el('small','Idle, work unrecorded','kn-warn'));
    if(t.blocked_reason)b.append(el('small','Blocked: '+t.blocked_reason));
    if(t.latest_handoff)b.append(el('small','Handoff '+ago(t.latest_handoff.ts)));
    if(t.outcome)b.append(el('small','Outcome: '+t.outcome));
    b.append(el('small',ago(t.updated)+(t.creator?' · by '+t.creator:''),'kn-dim'));return b;}
  function section(title,items,band){if(!items.length)return null;const s=el('div',undefined,'kn-section');s.append(el('h3',title+' ('+items.length+')'));for(const t of items)s.append(taskCard(t,t._band||band));return s;}
  async function load(){const v=++version;const d=await c.api('deck');let cs=[];try{cs=await c.listAll('concept');}catch{}if(v!==version)return;deck=d.deck;concepts=cs;drawSide();loadPins();}
  async function huddleModule(){return huddle||(huddle=await import('/account/huddle/assets/huddle.js'+search));}
  async function loadPins(){try{const n=await (await huddleModule()).openQuestions();if(n!==pins){pins=n;const p=hashParams('work');drawTabs(currentTab(p));}}catch{}}
  function drawSide(){const wasOpen=side.classList.contains('kn-open');side.replaceChildren();const body=[];
    const byConcept={};for(const p of deck){const k=p.project.parent||'';(byConcept[k]=byConcept[k]||[]).push(p);}
    const cname=id=>(concepts.find(x=>x.id===id)||{}).title||'Other projects';
    for(const [cid,ps] of Object.entries(byConcept)){body.push(el('h3',cname(cid)));for(const p of ps){const n=p.in_progress.length,b2=p.blocked.length,t=p.todo.length;const a=btn('',()=>go({p:p.project.slug,b:p.band}),'kn-index-item');a.append(el('span',p.project.title),el('small',[n&&n+' active',b2&&b2+' blocked',t&&t+' to do'].filter(Boolean).join(' · ')||'nothing open'));body.push(a);}}
    if(!deck.length)body.push(el('p','No projects yet.','kn-dim'));
    foldSide(side,'Projects · '+deck.length,body);if(wasOpen){side.classList.add('kn-open');side.querySelector('.kn-side-toggle').setAttribute('aria-expanded','true');}}
  function drawOverview(){main.replaceChildren();
    const all=k=>deck.flatMap(p=>p[k].map(t=>({...t,_band:p.band,_project:p.project.title})));
    const hygiene=all('in_progress').filter(t=>t.needs_hygiene);
    for(const [title,items] of [['Needs hygiene',hygiene],['In progress',all('in_progress')],['Blocked',all('blocked')],['Paused',all('paused')],['Recently done',all('recently_done')]]){const s=section(title,items);if(s)main.append(s);}
    if(!main.children.length)main.append(el('p','Nothing in flight.','kn-dim'));}
  // Board, list and timeline share one read and the filters in the hash.
  async function fetchBoard(){const d=await c.api('board',{data:{done_days:30}});const key=JSON.stringify(d);const changed=key!==boardKey;board=d;boardKey=key;return changed;}
  async function refresh(force){const p=hashParams('work');if(!p)return;await load().catch(()=>{});
    const v=p.get('v');if(['board','list','timeline'].includes(v)){if(await fetchBoard()||force)drawPM(v,p);}
    else if(p.get('p'))await drawProject(p.get('p'),p.get('b'));else if(p.get('t')){if(force)await drawTask(p.get('t'),p.get('b'));}else drawOverview();}
  const INTRO={board:'Drag a card to another column to change its status, or use ⋯ on a card. Agents see the change right away; stopping work an agent is on asks you for a short handoff.',
    list:'Every task across your projects. Sort by any column; the filters narrow every view.',
    timeline:'Tasks with a start or due date, by project. Arrows show what waits for what; the dashed line is today.'};
  function drawPM(v,p){const f=Object.fromEntries(wv.FILTER_KEYS.map(k=>[k,p.get(k)||'']));const sort=p.get('sort')||'';
    const keep=(extra)=>{const q={v,...Object.fromEntries(Object.entries(f).filter(([,x])=>x)),...extra};go(q);};
    const tasks=wv.applyFilters(board,f);
    const filters=wv.filterBar(board,f,nf=>go({v,...Object.fromEntries(Object.entries(nf).filter(([,x])=>x)),...(sort?{sort}:{})}),{state:v!=='board',help:INTRO[v],sort:v==='list'?{value:sort,set:s=>keep({sort:s})}:null});
    let view;
    if(v==='board')view=wv.renderBoard(kit,board,tasks,{showProject:!f.project});
    else if(v==='list')view=wv.renderList(kit,board,tasks,sort,s=>keep({sort:s}));
    else view=wv.renderTimeline(kit,board,tasks,{phone:matchMedia('(max-width: 800px)').matches,width:full.clientWidth-(matchMedia('(max-width: 800px)').matches?26:38)});
    const keepX=full.querySelector('.wv-tl-scroll,.wv-board')?.scrollLeft;
    full.replaceChildren(filters,view);
    if(keepX&&v!=='timeline'){const sc=full.querySelector('.wv-board');if(sc)sc.scrollLeft=keepX;}
    if(board.truncated)full.append(el('p','First '+board.tasks.length+' tasks only.','kn-dim'));}
  async function drawProject(slug,band){const page=await c.api('board',{id:slug,...(band?{band}:{}),data:{done_days:30}});
    full.replaceChildren(wv.renderProject(kit,page,{create}));}
  async function drawHuddle(p){const hm=await huddleModule();
    if(!huddleUI||!full.contains(huddleUI.root)){full.replaceChildren();huddleUI=await hm.mountHuddle(full,{onPins:n=>{if(n!==pins){pins=n;drawTabs('huddle');}}});}
    await huddleUI.open({room:p.get('h'),project:p.get('hp'),band:p.get('hb')});}
  async function drawTask(slug,band){const r=await c.api('get',{id:slug,...(band?{band}:{})});main.replaceChildren();
    const proj=deck.find(p=>p.band===r.band&&(p.project.id===r.parent||p.project.id===r.id||[...p.in_progress,...p.blocked,...p.paused,...p.todo,...p.recently_done].some(t=>t.id===r.id)));
    const parts=[['Work',()=>go({})]];if(proj&&proj.project.id!==r.id)parts.push([proj.project.title,()=>go({p:proj.project.slug,b:proj.band})]);parts.push(r.title);
    const titleRow=el('div',undefined,'kn-titlerow');titleRow.append(el('h1',r.title));
    const acts=el('div',undefined,'kn-acts');acts.append(btn('+ Subtask',()=>create({parent:r.slug,band:r.band,kind:'task'})));
    if(r.state!=='archived')acts.append(more([{label:'Archive',danger:true,run:async()=>{await c.api('update',{id:r.id,band:r.band,request_id:crypto.randomUUID(),data:{revision:r.revision,patch:{state:'archived'}}});await load();go({});}}],'More actions for this task'));
    titleRow.append(acts);main.append(crumbsNav(parts),titleRow);
    const meta=el('p',undefined,'kn-meta');meta.append(badge(STATE_LABEL[r.state]||r.state,r.state),el('span',r.kind),copyChip('[['+r.slug+']]'),el('span',c.bandName(r.band)+' · by '+r.creator+' · '+ago(r.updated)));main.append(meta);
    if(r.kind==='task'||r.kind==='project'){const plan=el('div',undefined,'wv-taskplan');
      // Only real YYYY-MM-DD dates: older records may hold free text there.
      const day=v=>/^\d{4}-\d{2}-\d{2}$/.test(v||'')?v:'';const a={...r.attrs,start:day(r.attrs.start),due:day(r.attrs.due)};
      const t={...r,start:a.start,due:a.due,tags:a.tags};
      if(r.kind==='task'&&r.state!=='archived'){const s=el('select');s.setAttribute('aria-label','Status');for(const [k,l] of wv.COLUMNS){const o=el('option',l);o.value=k;s.append(o);}
        s.value={closed:'done',cancelled:'done'}[r.state]||r.state;s.onchange=async()=>{const to=s.value;s.value={closed:'done',cancelled:'done'}[r.state]||r.state;await kit.move(t,to);await drawTask(slug,band);};plan.append(s);}
      const planBtn=btn('Plan',()=>wv.planTask(kit,t,async()=>{await drawTask(slug,band);}),'kn-linkish');planBtn.title='Plan dates and labels';planBtn.setAttribute('aria-label','Plan dates and labels');
      plan.append(el('span',(a.start||a.due)?((a.start||'…')+' → '+(a.due||'…')):'No dates','kn-dim'),planBtn);
      main.append(plan);}
    const live=r.claims.filter(x=>!x.released);if(live.length){const s=el('div',undefined,'kn-callout');s.append(el('strong','On it'));for(const x of live)s.append(el('div',x.actor+' · since '+date(x.started)+' · last active '+ago(x.last_active)+(x.dirty?' · needs hygiene':'')));main.append(s);}
    if(r.attrs.closed_by){const c=r.attrs.closed_by,s=el('div',undefined,'kn-callout');s.append(el('strong','Closed on '+c.who+"'s word"),el('div','“'+c.quote+'”'),el('div','Session: '+c.session+' · recorded by '+c.recorded_by));main.append(s);}
    if(r.attrs.outcome){const s=el('div',undefined,'kn-callout');s.append(el('strong',r.state==='cancelled'?'Why cancelled':'Outcome'),el('div',r.attrs.outcome));main.append(s);}
    if(r.attrs.blocked_reason)main.append(el('p','Blocked: '+r.attrs.blocked_reason,'kn-warn'));
    main.append(renderBody(r.body,s=>refOpen(s,r.band)));
    for(const [key,label] of [['criteria','Done when'],['dependencies','Depends on'],['workers','Workers'],['tags','Labels']]){const v=r.attrs[key]||[];if(v.length){main.append(el('h2',label));const ul=el('ul',undefined,'kn-list');for(const x of v){const li=el('li');if(key==='dependencies'){const a=el('a',x);a.href='#';a.onclick=e=>{e.preventDefault();refOpen(x,r.band);};li.append(a);}else li.textContent=x;ul.append(li);}main.append(ul);}}
    if(r.children.length){main.append(el('h2','Subtasks'));for(const k of r.children)main.append(taskCard(k,r.band));}
    if(r.links.length){main.append(el('h2','Audit trail'),linksTable(r.links));}
    if(r.claims.length>live.length){const d=el('details');d.append(el('summary','Past claims'));for(const x of r.claims.filter(x=>x.released))d.append(el('div',x.actor+' · '+date(x.started)+' → '+date(x.released),'kn-dim'));main.append(d);}
    if(r.backlinks.length){main.append(el('h2','Referenced by'));const ul=el('ul',undefined,'kn-list');for(const b of r.backlinks){const li=el('li');const a=el('a',b.title);a.href='#';a.onclick=e=>{e.preventDefault();refOpen(b.slug,r.band);};li.append(a,el('span',' · '+b.kind,'kn-dim'));ul.append(li);}main.append(ul);}
    main.append(historyBlock(r.events));}
  function refOpen(slug,band){c.api('get',{id:slug,...(band?{band}:{})}).then(r=>{if(r.kind==='knowledge')jump('knowledge',{p:r.slug,b:r.band});else if(r.kind==='project')go({p:r.slug,b:r.band});else go({t:r.slug,b:r.band});}).catch(()=>{});}
  function create(opts={}){form('New '+(opts.kind||'record'),f=>{const k=f('kind','Type',opts.kind||'task','select');for(const x of ['task','project','concept']){const o=el('option',x);o.value=x;k.append(o);}k.value=opts.kind||'task';
      f('title','Title');f('body','Details (markdown, [[slug]] links)','','textarea');f('parent','Parent slug (concept for a project, project or task for a task)',opts.parent||'');
      const b=f('band','Band','','select');for(const x of c.bands()){const o=el('option',x.name);o.value=x.id;if(opts.band?x.id===opts.band:x.primary)o.selected=true;b.append(o);}},
    async d=>{const n=await c.api('create',{kind:d.get('kind'),band:d.get('band'),request_id:crypto.randomUUID(),data:{title:d.get('title'),body:d.get('body'),...(d.get('parent')?{parent:d.get('parent')}:{})}});await load();go(d.get('kind')==='project'?{p:n.slug,b:n.band}:{t:n.slug,b:n.band});});}
  function currentTab(p){if(!p)return '';const v=p.get('v');return v||(p.get('t')||p.get('p')?null:'');}
  async function route(){const p=hashParams('work');if(!p)return;const v=p.get('v');drawTabs(currentTab(p));
    if(v!=='huddle')huddleUI?.deactivate();
    try{
      if(['board','list','timeline'].includes(v)){show('full');if(!board)await fetchBoard();drawPM(v,p);}
      else if(v==='huddle'){show('full');await drawHuddle(p);}
      else if(p.get('t')){show('layout');await drawTask(p.get('t'),p.get('b'));}
      else if(p.get('p')){show('full');await drawProject(p.get('p'),p.get('b'));}
      else{show('layout');drawOverview();}
    }catch(e){(full.hidden?main:full).replaceChildren(el('p',e.message,'kn-error'));}}
  const onHash=()=>{if(active)route();};
  return {async activate(){active=true;window.addEventListener('hashchange',onHash);window.addEventListener('popstate',onHash);board=null;try{await load();await route();}catch(e){main.replaceChildren(el('p',e.message,'kn-error'));}
      timer=setInterval(async()=>{if(!active||document.hidden||document.querySelector('.kn-dialog[open],.wv-dragging,.wv-menu'))return;const p=hashParams('work');if(!p||p.get('v')==='huddle'){loadPins();return;}try{await refresh(false);}catch{}},30000);},
    deactivate(){active=false;window.removeEventListener('hashchange',onHash);window.removeEventListener('popstate',onHash);clearInterval(timer);huddleUI?.deactivate();}};
}
