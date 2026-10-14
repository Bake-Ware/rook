// Work views: a board, a list and a timeline of every task, and the project
// page. One read feeds them all (knowledge action "board"); moving a card is
// the same "update" an agent sends, so the saving rules, claims and the
// task's history behave exactly as they do for agents. Stored text is only
// ever rendered as text.

const V=new URL(import.meta.url).search;
const {h,el,ago}=await import('/account/bands/assets/manage.js'+V);

export const COLUMNS=[['todo','To do'],['in_progress','In progress'],['blocked','Blocked'],['paused','Paused'],['done','Done']];
const COLUMN={todo:'todo',in_progress:'in_progress',blocked:'blocked',paused:'paused',done:'done',closed:'done',cancelled:'done'};
const LABEL={todo:'To do',in_progress:'In progress',blocked:'Blocked',paused:'Paused',done:'Done',closed:'Closed',cancelled:'Cancelled',archived:'Archived'};
const FINISHED=new Set(['done','closed','cancelled','archived']);
const DAY=86400000;

export function loadCss(){if(document.querySelector('link[data-wv]'))return;const l=document.createElement('link');l.rel='stylesheet';l.href='/account/knowledge/assets/workviews.css'+V;l.dataset.wv='1';document.head.append(l);}

// -- small helpers ---------------------------------------------------------
const pad=n=>String(n).padStart(2,'0');
export const isoDay=d=>d.getFullYear()+'-'+pad(d.getMonth()+1)+'-'+pad(d.getDate());
export const today=()=>isoDay(new Date());
const parseDay=s=>{const [y,m,d]=s.split('-').map(Number);return new Date(y,m-1,d);};
const fromTs=t=>isoDay(new Date(t*1000));
// Calendar arithmetic, not milliseconds: a day across a DST change is not 24h.
const addDays=(s,n)=>{const d=parseDay(s);d.setDate(d.getDate()+n);return isoDay(d);};
const daysBetween=(a,b)=>Math.round((parseDay(b)-parseDay(a))/DAY);
const fmtDay=s=>parseDay(s).toLocaleDateString(undefined,{month:'short',day:'numeric'});
// "claude.claudecode.laptop@.home.me.rook" -> "claude@laptop"; "human:7" -> "7".
export function who(id){if(!id)return '';if(id.startsWith('human:'))return id.slice(6);if(id.includes(':'))return id.split(':').slice(1).join(':');
  const p=id.split('@')[0].split('.');return p.length>=3?p[0]+'@'+p[2]:id;}
function initials(id){const [a,b]=who(id).split(/[@_]/).map(x=>x.replace(/[^A-Za-z0-9]/g,''));return ((a||'?')[0]+((b||a||'')[b?0:1]||'')).toUpperCase();}
const PAL=['#a4bc92','#d8ad6d','#9fb8c9','#c9a3b8','#b7b276','#8fc1a9','#d39a7a','#b3a3d6'];
function tint(id){let x=0;for(const ch of who(id))x=(x*31+ch.charCodeAt(0))>>>0;return PAL[x%PAL.length];}
export function avatar(id,cls){const a=el('span',initials(id),'wv-av'+(cls?' '+cls:''));a.style.setProperty('--tint',tint(id));a.title=who(id);return a;}
function dueState(t){if(!t.due||FINISHED.has(t.state))return '';const n=daysBetween(today(),t.due);return n<0?'overdue':n<=3?'soon':'';}
function dueChip(t){if(!t.due)return null;const s=dueState(t);const n=daysBetween(today(),t.due);
  const txt=s==='overdue'?'Overdue · '+fmtDay(t.due):n===0?'Due today':n===1?'Due tomorrow':'Due '+fmtDay(t.due);
  return el('span',txt,'wv-due'+(s?' wv-'+s:''));}
function stateChip(state){return el('span',LABEL[state]||state,'wv-state wv-s-'+state);}
function button(label,fn,cls,title){const b=h('button',{type:'button',class:cls||'',text:label,title});b.onclick=e=>{e.preventDefault();e.stopPropagation();Promise.resolve().then(fn).catch(err=>console.warn(err));};return b;}

// A small modal form: fields [{name,label,type:'input'|'textarea'|'date'|'select',value,options,placeholder,required}].
export function ask(host,{title,intro,fields,submit:label='Save'},submit){
  const dlg=el('dialog',undefined,'kn-dialog wv-dialog');const form=el('form');form.method='dialog';
  const err=el('p','','kn-error');const box=el('div',undefined,'kn-fields');
  for(const f of fields){const w=el('label',f.label);let e;
    if(f.type==='select'){e=el('select');for(const [v,t] of f.options){const o=el('option',t);o.value=v;e.append(o);}}
    else{e=document.createElement(f.type==='textarea'?'textarea':'input');if(f.type==='date')e.type='date';}
    e.name=f.name;if(f.value!=null)e.value=f.value;if(f.placeholder)e.placeholder=f.placeholder;if(f.required)e.required=true;
    w.append(e);if(f.hint)w.append(el('small',f.hint,'kn-dim'));box.append(w);}
  const row=el('div',undefined,'kn-row');const cancel=el('button','Cancel');cancel.type='button';cancel.onclick=()=>{dlg.close();dlg.remove();};
  const save=el('button',label,'kn-primary');save.type='submit';row.append(cancel,save);
  form.append(el('h2',title));if(intro)form.append(el('p',intro,'kn-dim wv-intro'));form.append(box,err,row);dlg.append(form);host.append(dlg);
  form.onsubmit=async e=>{e.preventDefault();save.disabled=true;err.textContent='';
    try{await submit(new FormData(form));dlg.close();dlg.remove();}catch(x){err.textContent=x.message;}finally{save.disabled=false;}};
  dlg.addEventListener('cancel',()=>dlg.remove());dlg.showModal();return dlg;}

// Evidence a person can paste: a link, repo@commit, or worker:/path.
function evidence(ref){ref=ref.trim();if(!ref)return null;
  if(/^https?:\/\//.test(ref))return {kind:'url',ref};
  if(/^[\w.\/-]+@[0-9a-f]{7,40}$/i.test(ref))return {kind:'commit',ref};
  if(/^[\w.-]+:(\/|[A-Za-z]:\\)/.test(ref))return {kind:'file',ref};
  throw Error('Evidence is a link (https://…), a commit (repo@sha) or a file (worker:/path).');}

// -- moving a task: the agents' update, with what its rules need -------------
export async function moveTask(kit,t,to){
  const from=t.state;if(COLUMN[from]===to)return false;
  const upd=(patch,extra={})=>kit.api('update',{id:t.id,band:t.band,request_id:crypto.randomUUID(),data:{revision:t.revision,patch,...extra}});
  const stopping=from==='in_progress'&&['todo','paused','blocked'].includes(to);
  const handoff=(state,next)=>({handoff:{goal:t.title,state,next_steps:next?[next]:[],decisions:['Moved to '+LABEL[to]+' from the Work board by '+kit.me()]}});
  const host=kit.host;
  if(to==='done'){
    return new Promise(res=>ask(host,{title:'Finish “'+t.title+'”',submit:'Mark finished',
      intro:'Say what happened. With evidence (a link, commit or file) the task is marked done; without it, it is closed on your word.',
      fields:[{name:'outcome',label:'What happened?',type:'textarea',required:true,value:''},
              {name:'evidence',label:'Evidence (optional)',placeholder:'https://…, repo@abc1234 or worker:/path'}]},
      async d=>{const outcome=d.get('outcome').trim();const ev=evidence(d.get('evidence')||'');
        if(ev){await kit.api('link',{id:t.id,band:t.band,request_id:crypto.randomUUID(),data:{...ev,relation:'evidence',note:'Added when finishing from the Work board'}});
          const cur=await kit.api('get',{id:t.id,band:t.band});
          await kit.api('update',{id:t.id,band:t.band,request_id:crypto.randomUUID(),data:{revision:cur.revision,patch:{state:'done',attrs:{outcome}}}});}
        else await upd({state:'closed',attrs:{outcome}},{closed_by:{who:kit.me(),quote:outcome,session:location.origin+'/#work?t='+encodeURIComponent(t.slug)}});
        res(true);}));}
  if(to==='blocked'){
    return new Promise(res=>ask(host,{title:'Blocked: “'+t.title+'”',submit:'Mark blocked',
      intro:stopping?'An agent is on this. Your note is saved as its handoff, so whoever picks it up knows why it stopped.':'What is it waiting for?',
      fields:[{name:'reason',label:'Waiting on',type:'textarea',required:true,value:''},...(stopping?[{name:'next',label:'Next step once unblocked (optional)'}]:[])]},
      async d=>{const reason=d.get('reason').trim();await upd({state:'blocked',attrs:{blocked_reason:reason}},stopping?handoff(reason,d.get('next')):{});res(true);}));}
  if(stopping){
    return new Promise(res=>ask(host,{title:LABEL[to]+': “'+t.title+'”',submit:'Move',
      intro:'An agent is on this. Stopping it needs a short handoff, so the next one can pick up where it left off.',
      fields:[{name:'state',label:'Where it stands',type:'textarea',required:true,value:''},{name:'next',label:'Next step (optional)'}]},
      async d=>{await upd({state:to},handoff(d.get('state').trim(),d.get('next')));res(true);}));}
  await upd({state:to});return true;}

export function planTask(kit,t,done){
  ask(kit.host,{title:'Plan “'+t.title+'”',intro:'Dates show the task on the timeline. Labels are free-form words for filtering.',
    fields:[{name:'start',label:'Start',type:'date',value:t.start||''},{name:'due',label:'Due',type:'date',value:t.due||''},
            {name:'tags',label:'Labels (comma separated)',value:(t.tags||[]).join(', ')}]},
    async d=>{const tags=d.get('tags').split(',').map(s=>s.trim()).filter(Boolean);
      await kit.api('update',{id:t.id,band:t.band,request_id:crypto.randomUUID(),data:{revision:t.revision,patch:{attrs:{start:d.get('start')||null,due:d.get('due')||null,tags}}}});
      await done();});}

// -- filters ------------------------------------------------------------------
export const FILTER_KEYS=['project','owner','state','label','due','q'];
export function applyFilters(data,f){
  const q=(f.q||'').toLowerCase();const t0=today();
  return data.tasks.filter(t=>(!f.project||t.project===f.project)
    &&(!f.owner||(t.owner||[]).includes(f.owner)||t.creator===f.owner)
    &&(!f.state||COLUMN[t.state]===f.state||t.state===f.state)
    &&(!f.label||(t.tags||[]).includes(f.label))
    &&(!f.due||(f.due==='overdue'?dueState(t)==='overdue':f.due==='week'?(t.due&&!FINISHED.has(t.state)&&daysBetween(t0,t.due)>=0&&daysBetween(t0,t.due)<=7):f.due==='none'?!t.due&&!t.start:f.due==='dated'?!!(t.due||t.start):true))
    &&(!q||(t.title+' '+t.slug+' '+(t.excerpt||'')).toLowerCase().includes(q)));}
export function filterBar(data,f,onChange,{state=true}={}){
  const bar=el('div',undefined,'wv-filters');const sel=(key,label,opts)=>{const s=el('select');s.setAttribute('aria-label',label);s.name=key;
    const all=el('option',label);all.value='';s.append(all);for(const [v,t] of opts){const o=el('option',t);o.value=v;s.append(o);}s.value=f[key]||'';s.onchange=()=>onChange({...f,[key]:s.value});if(f[key])s.classList.add('wv-on');return s;};
  const owners=new Map();for(const t of data.tasks)for(const o of [...(t.owner||[])])owners.set(o,who(o));
  const labels=[...new Set(data.tasks.flatMap(t=>t.tags||[]))].sort();
  const q=el('input');q.type='search';q.placeholder='Search tasks';q.value=f.q||'';q.setAttribute('aria-label','Search tasks');
  let timer=null;q.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>onChange({...f,q:q.value}),250);};
  bar.append(q,sel('project','All projects',data.projects.map(p=>[p.id,p.title])),sel('owner','Anyone',[...owners].sort((a,b)=>a[1].localeCompare(b[1]))));
  if(state)bar.append(sel('state','Any status',COLUMNS.concat([['cancelled','Cancelled']])));
  bar.append(sel('label','Any label',labels.map(x=>[x,x])),sel('due','Any date',[['overdue','Overdue'],['week','Due this week'],['dated','Has dates'],['none','No dates']]));
  if(FILTER_KEYS.some(k=>f[k]))bar.append(button('Clear',()=>onChange({}),'kn-linkish'));
  return bar;}

function projectName(data,id){return (data.projects.find(p=>p.id===id)||{}).title||'';}

// -- board -------------------------------------------------------------------
function card(kit,data,t,{showProject}){
  const c=el('article',undefined,'wv-card wv-c-'+t.state);c.draggable=true;c.dataset.id=t.id;c.tabIndex=0;
  c.setAttribute('aria-label',t.title+', '+(LABEL[t.state]||t.state));
  const top=el('div',undefined,'wv-card-top');const title=el('a',t.title,'wv-card-title');title.href='#work?t='+encodeURIComponent(t.slug)+'&b='+encodeURIComponent(t.band);
  top.append(title);const menuBtn=button('⋯',()=>moveMenu(kit,t,menuBtn),'wv-more','Move to…');menuBtn.setAttribute('aria-label','Move '+t.title);top.append(menuBtn);c.append(top);
  if(showProject){const p=projectName(data,t.project);if(p)c.append(el('div',p,'wv-card-project'));}
  if(t.state==='blocked'&&t.blocked_reason)c.append(el('div','Waiting on: '+t.blocked_reason,'wv-card-note wv-bad'));
  if(t.state==='cancelled')c.append(el('div','Cancelled','wv-card-note'));
  if(t.needs_hygiene)c.append(el('div','Gone quiet with unrecorded work','wv-card-note wv-warn'));
  if(t.dependencies&&!t.unblocked&&!FINISHED.has(t.state)){const n=t.dependencies.filter(d=>d.state!=='done').length;c.append(el('div','Waiting for '+n+' other task'+(n>1?'s':''),'wv-card-note'));}
  const meta=el('div',undefined,'wv-card-meta');
  const people=el('span',undefined,'wv-people');for(const o of (t.claimants&&t.claimants.length?t.claimants.map(x=>x.actor):(t.owner||[])).slice(0,3))people.append(avatar(o,'sm'));
  if(people.childNodes.length)meta.append(people);
  const due=dueChip(t);if(due)meta.append(due);for(const x of (t.tags||[]).slice(0,3))meta.append(el('span',x,'wv-tag'));
  meta.append(el('span',t.state==='in_progress'&&t.claimants&&t.claimants[0]?'active '+ago(t.claimants[0].last_active,''):ago(t.updated,''),'wv-ago'));
  c.append(meta);
  c.ondragstart=e=>{e.dataTransfer.setData('text/plain',t.id);e.dataTransfer.effectAllowed='move';c.classList.add('wv-dragging');};
  c.ondragend=()=>c.classList.remove('wv-dragging');
  c.onclick=e=>{if(e.target.closest('button,a'))return;location.hash=title.getAttribute('href');};
  c.onkeydown=e=>{if(e.key==='Enter')location.hash=title.getAttribute('href');};
  return c;}

function moveMenu(kit,t,anchor){
  document.querySelector('.wv-menu')?.remove();
  const m=el('div',undefined,'wv-menu');m.setAttribute('role','menu');m.append(el('div','Move to','wv-menu-head'));
  for(const [k,label] of COLUMNS){if(COLUMN[t.state]===k)continue;const b=button(label,async()=>{m.remove();await kit.move(t,k);});b.setAttribute('role','menuitem');m.append(b);}
  document.body.append(m);const r=anchor.getBoundingClientRect();
  m.style.top=Math.min(window.innerHeight-m.offsetHeight-8,r.bottom+4)+'px';m.style.left=Math.max(8,Math.min(window.innerWidth-m.offsetWidth-8,r.right-m.offsetWidth))+'px';
  m.querySelector('button')?.focus();
  const close=e=>{if(!m.contains(e.target)){m.remove();document.removeEventListener('pointerdown',close,true);}};
  setTimeout(()=>document.addEventListener('pointerdown',close,true));
  m.onkeydown=e=>{if(e.key==='Escape'){m.remove();anchor.focus();}};}

export function renderBoard(kit,data,tasks,{showProject=true}={}){
  const wrap=el('div',undefined,'wv-board');
  for(const [key,label] of COLUMNS){
    const items=tasks.filter(t=>COLUMN[t.state]===key).sort((a,b)=>(key==='done'?b.updated-a.updated:(a.due||'9999').localeCompare(b.due||'9999')||b.updated-a.updated));
    const col=el('section',undefined,'wv-col wv-col-'+key);col.dataset.state=key;
    const head=el('header',undefined,'wv-col-head');head.append(el('span',label),el('span',String(items.length),'wv-count'));col.append(head);
    const list=el('div',undefined,'wv-col-list');for(const t of items)list.append(card(kit,data,t,{showProject}));
    if(!items.length)list.append(el('p',key==='done'?'Nothing finished lately.':'Empty','wv-empty'));
    col.append(list);
    col.ondragover=e=>{e.preventDefault();e.dataTransfer.dropEffect='move';col.classList.add('wv-drop');};
    col.ondragleave=e=>{if(!col.contains(e.relatedTarget))col.classList.remove('wv-drop');};
    col.ondrop=async e=>{e.preventDefault();col.classList.remove('wv-drop');const t=data.tasks.find(x=>x.id===e.dataTransfer.getData('text/plain'));if(t)await kit.move(t,key);};
    wrap.append(col);}
  return wrap;}

// -- list --------------------------------------------------------------------
const SORTS={title:t=>t.title.toLowerCase(),project:(t,d)=>projectName(d,t.project).toLowerCase(),state:t=>COLUMNS.findIndex(c=>c[0]===COLUMN[t.state])+(t.state==='cancelled'?.5:0),
  owner:t=>who((t.owner||[])[0]||'~'),due:t=>t.due||'9999',updated:t=>-t.updated};
export function renderList(kit,data,tasks,sort,onSort){
  const [key,dir]=(sort||'due:1').split(':');const f=SORTS[key]||SORTS.due;
  const rows=tasks.slice().sort((a,b)=>{const x=f(a,data),y=f(b,data);return (x<y?-1:x>y?1:b.updated-a.updated)*(dir==='-1'?-1:1);});
  const t=el('table',undefined,'wv-table');const head=el('tr');
  for(const [k,label] of [['title','Task'],['project','Project'],['state','Status'],['owner','Owner'],[null,'Labels'],['due','Due'],['updated','Updated']]){
    const th=el('th');if(k){const b=button(label+(k===key?(dir==='-1'?' ↓':' ↑'):''),()=>onSort(k+':'+(k===key&&dir!=='-1'?'-1':'1')),'wv-sort');th.append(b);th.setAttribute('aria-sort',k===key?(dir==='-1'?'descending':'ascending'):'none');}else th.textContent=label;head.append(th);}
  t.append(el('thead'));t.firstChild.append(head);const body=el('tbody');
  for(const x of rows){const tr=el('tr');const a=el('a',x.title);a.href='#work?t='+encodeURIComponent(x.slug)+'&b='+encodeURIComponent(x.band);
    const c1=el('td',undefined,'wv-td-title');c1.append(a);if(x.state==='blocked'&&x.blocked_reason)c1.append(el('small',x.blocked_reason,'wv-bad'));
    const pr=el('td');const pa=el('a',projectName(data,x.project));pa.href='#work?p='+encodeURIComponent((data.projects.find(p=>p.id===x.project)||{}).slug||'')+'&b='+encodeURIComponent(x.band);pr.append(pa);
    const st=el('td');st.append(stateChip(x.state));
    const ow=el('td',undefined,'wv-td-owner');for(const o of (x.owner||[]).slice(0,2)){const s=el('span',undefined,'wv-owner');s.append(avatar(o,'sm'),el('span',who(o)));ow.append(s);}
    const lb=el('td');for(const g of x.tags||[])lb.append(el('span',g,'wv-tag'));
    const du=el('td');const dc=dueChip(x);if(dc)du.append(dc);else du.append(el('span','—','kn-dim'));
    tr.append(c1,pr,st,ow,lb,du,el('td',ago(x.updated,''),'kn-dim'));body.append(tr);}
  t.append(body);const w=el('div',undefined,'wv-scroll');w.append(t);
  if(!rows.length)w.append(el('p','No tasks match.','wv-empty'));return w;}

// -- timeline ----------------------------------------------------------------
function span(t){
  const created=fromTs(t.created);
  if(!t.start&&!t.due)return null;
  const s=t.start||(t.due<created?t.due:created);
  let e=t.due||(FINISHED.has(t.state)?fromTs(t.updated):today());
  if(e<s)e=s;return {s,e,open:!t.due};}
export function renderTimeline(kit,data,tasks,{phone=false,width:avail=0}={}){
  const LBL=phone?132:236,ROW=34;const wrap=el('div',undefined,'wv-timeline');
  const groups=[];const unscheduled=[];
  for(const p of data.projects){const ts=tasks.filter(t=>t.project===p.id);const sched=[];
    for(const t of ts){const sp=span(t);if(sp)sched.push([t,sp]);else unscheduled.push(t);}
    sched.sort((a,b)=>a[1].s.localeCompare(b[1].s)||a[1].e.localeCompare(b[1].e));
    if(sched.length||p.start||p.due)groups.push([p,sched]);}
  if(!groups.length){wrap.append(el('p','Nothing is scheduled yet. Give tasks a start or due date (open a task, then Plan) and they show up here.','wv-empty'));}
  else{
    const all=groups.flatMap(([p,s])=>[...s.flatMap(([,x])=>[x.s,x.e]),p.start,p.due].filter(Boolean));all.push(today());
    let from=addDays(all.reduce((a,b)=>a<b?a:b),-2),to=addDays(all.reduce((a,b)=>a>b?a:b),4);
    let n=daysBetween(from,to)+1;if(n>366){to=addDays(from,365);n=366;}
    // Fit the range to the space there is when it can; otherwise it scrolls sideways.
    const dayW=Math.max(phone?14:10,Math.min(phone?22:34,Math.floor(((avail||(phone?830:1320))-LBL-2)/n)));
    const width=LBL+n*dayW;const x=d=>LBL+Math.max(0,Math.min(n,daysBetween(from,d)))*dayW;
    const scroller=el('div',undefined,'wv-tl-scroll');const inner=el('div',undefined,'wv-tl-inner');inner.style.width=width+'px';inner.style.setProperty('--lbl',LBL+'px');inner.style.setProperty('--row',ROW+'px');
    // Header: month labels and day ticks (every day when there is room, else Mondays).
    const head=el('div',undefined,'wv-tl-head');const corner=el('div','',' wv-tl-label wv-tl-corner');head.append(corner);
    let firstMonth=false;
    for(let i=0;i<n;i++){const d=addDays(from,i),dt=parseDay(d);
      const roomLeft=new Date(dt.getFullYear(),dt.getMonth()+1,0).getDate()-dt.getDate();   // days to the month's end
      if(dt.getDate()===1||(i===0&&roomLeft*dayW>=70)){const m=el('span',dt.toLocaleDateString(undefined,{month:'short',year:dt.getMonth()===0||!firstMonth?'numeric':undefined}),'wv-tl-month');firstMonth=true;m.style.left=(LBL+i*dayW)+'px';head.append(m);}
      if(dayW>=22||dt.getDay()===1||d===today()){const tick=el('span',String(dt.getDate()),'wv-tl-day'+(dt.getDay()===0||dt.getDay()===6?' wv-wkend':'')+(d===today()?' wv-today':''));if(d===today())tick.title='Today';tick.style.left=(LBL+i*dayW)+'px';tick.style.width=dayW+'px';head.append(tick);}}
    inner.append(head);
    const rowsBox=el('div',undefined,'wv-tl-rows');const pos={};let row=0;
    const grid=el('div',undefined,'wv-tl-grid');grid.style.left=LBL+'px';grid.style.backgroundSize=(dayW*7)+'px 100%';
    grid.style.backgroundPosition=(((8-parseDay(from).getDay())%7)*dayW)+'px 0';rowsBox.append(grid);
    for(const [p,sched] of groups){
      const pr=el('div',undefined,'wv-tl-row wv-tl-project');const lab=el('a',p.title,'wv-tl-label');lab.href='#work?p='+encodeURIComponent(p.slug)+'&b='+encodeURIComponent(p.band);pr.append(lab);
      const ps=p.start||(sched[0]&&sched[0][1].s),pe=p.due||(sched.length&&sched.map(([,s])=>s.e).reduce((a,b)=>a>b?a:b));
      if(ps&&pe){const bar=el('div',undefined,'wv-tl-pbar');bar.style.left=x(ps)+'px';bar.style.width=Math.max(dayW,x(addDays(pe,1))-x(ps))+'px';
        const prog=el('i');prog.style.width=Math.round((p.progress||0)*100)+'%';bar.append(prog);bar.title=p.title+': '+fmtDay(ps)+' – '+fmtDay(pe)+' · '+Math.round((p.progress||0)*100)+'% done';pr.append(bar);}
      rowsBox.append(pr);row++;
      for(const [t,s] of sched){const r=el('div',undefined,'wv-tl-row');const a=el('a',t.title,'wv-tl-label wv-tl-task');a.href='#work?t='+encodeURIComponent(t.slug)+'&b='+encodeURIComponent(t.band);a.title=t.title;r.append(a);
        const bar=el('a',undefined,'wv-tl-bar wv-s-'+t.state+(s.open?' wv-open':'')+(dueState(t)==='overdue'?' wv-overdue':''));bar.href=a.href;
        const left=x(s.s),w=Math.max(dayW-2,x(addDays(s.e,1))-left-2);bar.style.left=left+'px';bar.style.width=w+'px';
        bar.title=t.title+' · '+(LABEL[t.state]||t.state)+' · '+fmtDay(s.s)+(t.due?' → due '+fmtDay(t.due):' → no due date');
        if(w>70)bar.append(el('span',(t.owner&&t.owner[0]?who(t.owner[0]):LABEL[t.state])));
        r.append(bar);pos[t.id]={row,left,right:left+w};rowsBox.append(r);row++;}}
    rowsBox.style.height=(row*ROW)+'px';
    // Today, and the dependency arrows (from a dependency's end to the dependent's start).
    const now=el('div',undefined,'wv-tl-today');now.style.left=(x(today())+dayW/2)+'px';now.title='Today';rowsBox.append(now);
    const ns='http://www.w3.org/2000/svg';const svg=document.createElementNS(ns,'svg');svg.setAttribute('class','wv-tl-deps');svg.setAttribute('width',width);svg.setAttribute('height',row*ROW);
    const defs=document.createElementNS(ns,'defs');defs.innerHTML='<marker id="wv-arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0 0L8 4L0 8z" fill="currentColor"/></marker>';svg.append(defs);
    for(const t of tasks)for(const d of t.dependencies||[]){const a=pos[d.id],b=pos[t.id];if(!a||!b)continue;
      const y1=a.row*ROW+ROW/2,y2=b.row*ROW+ROW/2,x1=a.right,x2=b.left;
      // Straight across when the dependent starts after; otherwise around, between the rows.
      const route=x2>=x1+16?`M${x1} ${y1}H${x1+8}V${y2}H${x2-1}`:`M${x1} ${y1}H${x1+8}V${y2-ROW/2+1}H${x2-10}V${y2}H${x2-1}`;
      const path=document.createElementNS(ns,'path');path.setAttribute("d",route);path.setAttribute('marker-end','url(#wv-arrow)');
      path.setAttribute('class',d.state==='done'?'wv-dep-done':x2<x1?'wv-dep-late':'');const tt=document.createElementNS(ns,'title');tt.textContent=t.title+' waits for '+(d.slug||d.id);path.append(tt);svg.append(path);}
    rowsBox.append(svg);inner.append(rowsBox);scroller.append(inner);wrap.append(scroller);
    requestAnimationFrame(()=>{scroller.scrollLeft=Math.max(0,x(today())-LBL-(phone?40:200));});
    const legend=el('p',undefined,'wv-legend');for(const [k,l] of COLUMNS){const s=el('span',l,'wv-leg wv-s-'+k);legend.append(s);}legend.append(el('span','Arrows: waits for','wv-leg wv-leg-dep'));wrap.append(legend);}
  if(unscheduled.length){const d=el('details',undefined,'wv-unscheduled');d.append(el('summary','Not scheduled ('+unscheduled.length+')'));const ul=el('ul',undefined,'kn-list');
    for(const t of unscheduled){const li=el('li');const a=el('a',t.title);a.href='#work?t='+encodeURIComponent(t.slug)+'&b='+encodeURIComponent(t.band);li.append(a,el('span',' · '+projectName(data,t.project)+' · '+(LABEL[t.state]||t.state),'kn-dim'),button('Plan',()=>planTask(kit,t,kit.reload),'kn-linkish wv-plan'));ul.append(li);}
    d.append(ul);wrap.append(d);}
  return wrap;}

// -- project page --------------------------------------------------------------
function stat(n,label,cls){const s=el('div',undefined,'wv-stat'+(cls?' '+cls:''));s.append(el('b',String(n)),el('span',label));return s;}
// One line per event: the actor, then a sentence around the task's title ({t}).
function describe(e){const d=e.data||{};
  if(e.action==='updated'&&d.state)return 'moved {t} to '+(LABEL[d.state]||d.state);
  if(e.action==='updated')return 'edited {t}';
  if(e.action==='linked')return d.kind==='chat'?'opened a room for {t}':d.kind==='commit'?'linked commit '+d.ref+' to {t}':d.kind==='handoff'?'left a handoff on {t}':'linked '+d.kind+' evidence to {t}';
  if(e.action.startsWith('hygiene'))return 'flagged {t} for a write-up';
  return ({created:'created {t}',claimed:'started working on {t}',released:'stepped off {t}',note:'left a note on {t}',
    link_retracted:'retracted a link on {t}',superseded:'superseded {t}',reviewed:'reviewed {t}'})[e.action]||e.action+' {t}';}
export function renderProject(kit,page,opts){
  const p=page.projects[0];const tasks=page.tasks;const out=el('div',undefined,'wv-project');
  const crumbs=el('div',undefined,'kn-crumbs');const back=el('a','Work');back.href='#work';crumbs.append(back,document.createTextNode(' / '+(p.concept?p.concept+' / ':'')+p.title));
  const head=el('header',undefined,'wv-phead');const left=el('div');left.append(el('h1',p.title));
  const meta=el('p',undefined,'kn-meta');meta.append(stateChip(p.state==='active'?'in_progress':p.state),document.createTextNode(' '+kit.bandName(p.band)+' · [['+p.slug+']]'+(p.start||p.due?' · '+(p.start?fmtDay(p.start):'…')+' → '+(p.due?fmtDay(p.due):'…'):'')));left.append(meta);
  const acts=el('div',undefined,'wv-pacts');
  const link=(label,href,cls)=>{const a=el('a',label,'wv-btn'+(cls?' '+cls:''));a.href=href;return a;};
  acts.append(link('Huddle','#work?v=huddle&hp='+encodeURIComponent(p.id),'wv-btn-accent'),link('Board','#work?v=board&project='+encodeURIComponent(p.id)),link('Timeline','#work?v=timeline&project='+encodeURIComponent(p.id)),
    button('New task',()=>opts.create({parent:p.slug,band:p.band,kind:'task'}),'wv-btn'),button('Details',()=>{location.hash='#work?t='+encodeURIComponent(p.slug)+'&b='+encodeURIComponent(p.band);},'wv-btn'));
  head.append(left,acts);out.append(crumbs,head);
  if(p.excerpt)out.append(kit.renderBody(p.excerpt,s=>kit.refOpen(s,p.band)));
  // Progress and counts.
  const c=p.counts||{};const finished=(c.done||0)+(c.closed||0);const overdue=tasks.filter(t=>dueState(t)==='overdue').length;
  const prog=el('div',undefined,'wv-progress');const bar=el('div',undefined,'wv-progress-bar');const fill=el('i');fill.style.width=Math.round(p.progress*100)+'%';bar.append(fill);
  bar.setAttribute('role','progressbar');bar.setAttribute('aria-valuenow',String(Math.round(p.progress*100)));bar.setAttribute('aria-valuemin','0');bar.setAttribute('aria-valuemax','100');bar.setAttribute('aria-label','Project progress');
  prog.append(el('div',finished+' of '+(p.total-(c.cancelled||0))+' tasks finished · '+Math.round(p.progress*100)+'%','wv-progress-label'),bar);
  const stats=el('div',undefined,'wv-stats');stats.append(stat(p.open,'open'),stat(c.in_progress||0,'in progress','wv-st-acc'),stat(c.blocked||0,'blocked',c.blocked?'wv-st-bad':''),stat(overdue,'overdue',overdue?'wv-st-bad':''),stat(finished,'finished','wv-st-ok'));
  out.append(prog,stats);
  const grid=el('div',undefined,'wv-pgrid');const main=el('div',undefined,'wv-pmain');const side=el('aside',undefined,'wv-pside');
  const block=(title,items,empty)=>{const s=el('section',undefined,'wv-psec');s.append(el('h2',title+(items.length?' ('+items.length+')':'')));
    if(!items.length){s.append(el('p',empty,'kn-dim'));return s;}const l=el('div',undefined,'wv-plist');for(const t of items)l.append(card(kit,page,t,{showProject:false}));s.append(l);return s;};
  const by=s=>tasks.filter(t=>t.state===s);
  if(by('blocked').length)main.append(block('Blocked',by('blocked'),''));
  main.append(block('In progress',by('in_progress'),'Nobody is on this project right now.'));
  main.append(block('Up next',by('todo').concat(by('paused')).sort((a,b)=>(a.due||'9999').localeCompare(b.due||'9999')),'Nothing waiting.'));
  const done=tasks.filter(t=>['done','closed','cancelled'].includes(t.state)).sort((a,b)=>b.updated-a.updated);
  if(done.length){const d=el('details',undefined,'wv-psec');d.append(el('summary','Finished recently ('+done.length+')'));const l=el('div',undefined,'wv-plist');for(const t of done)l.append(card(kit,page,t,{showProject:false}));d.append(l);main.append(d);}
  // Side: activity, rooms, knowledge.
  const act=el('section',undefined,'wv-psec');act.append(el('h2','Recent activity'));const feed=el('ol',undefined,'wv-feed');
  for(const e of page.activity.slice(0,14)){const li=el('li');li.append(avatar(e.actor,'sm'));const txt=el('div');
    const a=el('a',e.title);a.href='#work?t='+encodeURIComponent(e.slug)+'&b='+encodeURIComponent(p.band);
    const [pre,post]=describe(e).split('{t}');txt.append(el('b',who(e.actor)),document.createTextNode(' '+pre),a,document.createTextNode(post||''),el('small',ago(e.ts,''),'kn-dim'));li.append(txt);feed.append(li);}
  if(!page.activity.length)feed.append(el('li','Nothing yet.','kn-dim'));act.append(feed);
  const rooms=el('section',undefined,'wv-psec');rooms.append(el('h2','Rooms'));const rl=el('ul',undefined,'wv-links');
  for(const r of page.rooms){const li=el('li');const huddle=r.note==='Huddle';const a=el('a',huddle?'Huddle':(r.note||'Chat room'));
    a.href=huddle?'#work?v=huddle&h='+encodeURIComponent(r.room):'#chat?room='+encodeURIComponent(r.room);li.append(a,el('small',r.records.includes(p.slug)?'this project':'from '+r.records.join(', '),'kn-dim'));rl.append(li);}
  if(!page.rooms.length){const li=el('li');li.append(el('span','No rooms yet. ','kn-dim'));const a=el('a','Start the huddle');a.href='#work?v=huddle&hp='+encodeURIComponent(p.id);li.append(a);rl.append(li);}
  rooms.append(rl);
  const kn=el('section',undefined,'wv-psec');kn.append(el('h2','Knowledge'));const kl=el('ul',undefined,'wv-links');
  for(const k of page.knowledge){const li=el('li');const a=el('a',k.title);a.href='#knowledge?p='+encodeURIComponent(k.slug)+'&b='+encodeURIComponent(p.band);li.append(a,el('small',(k.via==='linked'?'linked from the project':'mentions it')+(k.gist?' · '+k.gist:''),'kn-dim'));kl.append(li);}
  if(!page.knowledge.length)kl.append(el('li','No pages linked yet. Agents link pages with [[slug]].','kn-dim'));kn.append(kl);
  side.append(act,rooms,kn);grid.append(main,side);out.append(grid);
  return out;}
