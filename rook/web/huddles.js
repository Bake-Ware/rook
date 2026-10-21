// Huddles: your threads with the house agent in the chair and the roles it
// calls on (docs/design/huddles.md 8). A narrow list on the left (chaired
// huddles by recent activity, then project and band status huddles; archived
// huddles and the old chat rooms, read-only, under the filter), the thread on
// the right; on a phone the list and the thread take turns. Each post starts
// a round: the chair routes it, roles answer (compact "answering" lines while
// they run; replies are posted whole, so the view polls), and action cards wait
// inline for Approve or Dismiss. Talks to /account/huddle/api (operator
// session + CSRF) and, for the old chat rooms, the dashboard's /api/chat/*.
// Stored text is only ever rendered as text.

const search=new URL(import.meta.url).search;
const {h,el,more,copyChip}=await import('/account/bands/assets/manage.js'+search);

const API='/account/huddle/api';
export const YOU='user:operator';
let csrf='';
/** One action on the huddle account API: its result, or an Error with the reason. */
export async function hapi(action,body={},retry=true){
  if(!csrf){const r=await fetch(API);const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'Sign in with your operator account.');csrf=d.csrf;}
  const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,action,...body})});
  const d=await r.json().catch(()=>({}));
  if(r.status===403&&retry&&/expired/i.test(d.error||'')){csrf='';return hapi(action,body,false);}
  if(!r.ok||d.error)throw Error(d.error||'Request failed');
  return d.result;
}

function loadCss(){if(document.querySelector('link[data-hd]'))return;const l=document.createElement('link');l.rel='stylesheet';l.href='/account/huddle/assets/huddle.css'+search;l.dataset.hd='1';document.head.append(l);}

/** Text cut to n characters with a visible marker, as the hub cuts. */
export const clip=(t,n)=>{t=String(t??'');return t.length<=n?t:t.slice(0,n)+'\n[truncated: '+n+' of '+t.length+' chars]';};
export const short=id=>{id=String(id||'');return id.includes(':')?id.split(':').slice(1).join(':'):id;};
const RUNNING=['chair','roles','closing'];
const FILTERS=[['active','Huddles'],['archived','Archived'],['chat','Old chats']];
// What the chair and the roles post when a turn fails (rook/hub/plugins/huddle/chair.py).
const FAILED=/^\((?:The chair could not finish this round|.+ could not answer): /;
const KIND={'task.create':'New task','task.update':'Task change','job.run':'Run a job','job.create':'New job',
  'session.send':'Nudge a session','session.start':'Start a session',note:'Note',ask_bake:'Question for you'};
const CARD_STATE={proposed:'Waiting',approved:'Running',dismissed:'Dismissed',done:'Done',failed:'Failed'};
const NARROW=760;

function when(ts){const d=new Date(ts*1000),now=new Date();const t=d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
  return d.toDateString()===now.toDateString()?t:d.toLocaleDateString([],{month:'short',day:'numeric'})+' '+t;}
const chip=(text,cls)=>el('span',text,'hx-chip'+(cls?' '+cls:''));
// "Brave otter_lamp" -> "brave-otter-lamp": a speakable name as the hub stores it.
const norm=s=>String(s||'').trim().toLowerCase().split(/[\s_.-]+/).filter(Boolean).join('-');

export async function mountHuddles(root){
  loadCss();
  let filter='active',huddles=[],rooms=[],cur=null,view=null,msgs=[],lastSeq=0,sig='';
  let roles={},defaults=[],chairOn=true,chairId='agent:home';
  let active=false,timer=null,busy=false,again=false,ticks=0,sending=false;

  const wrap=el('div',undefined,'hx');
  const side=el('aside',undefined,'hx-side');
  const filterSel=h('select',{'aria-label':'Show'},FILTERS.map(([v,l])=>h('option',{value:v,text:l})));
  const newBtn=h('button',{type:'button',class:'hx-btn primary',text:'+ New','aria-label':'New huddle',title:'New huddle'});
  const list=h('nav',{class:'hx-list','aria-label':'Huddles'});
  side.append(h('div',{class:'hx-side-head'},filterSel,newBtn),list);
  const main=el('section',undefined,'hx-main');
  wrap.append(side,main);root.replaceChildren(wrap);

  const head=el('header',undefined,'hx-head');
  const notice=el('div',undefined,'hx-notice');
  const log=h('div',{class:'hx-log',role:'log','aria-live':'polite'});
  const pop=h('ul',{class:'hx-pop',role:'listbox',hidden:true});
  const box=h('textarea',{rows:'1','aria-label':'Message',autocomplete:'off'});
  const sendB=h('button',{type:'submit',class:'hx-btn primary hx-send',text:'Send'});
  const compose=h('form',{class:'hx-compose'},pop,box,sendB);
  const err=h('p',{class:'hx-err',role:'alert'});

  filterSel.onchange=()=>{filter=filterSel.value;list.replaceChildren();loadList().catch(showErr);};
  newBtn.onclick=()=>startForm().catch(showErr);
  function showErr(e){err.textContent=e.message||String(e);if(!err.isConnected)main.append(err);}

  async function loadRoles(){
    try{const r=await hapi('roles');roles=Object.fromEntries(r.roles.map(x=>[x.id,x]));defaults=r.default_roles||[];chairOn=r.chair_on;chairId=r.chair||chairId;}
    catch(e){/* the list says why */}
  }
  const roleName=id=>(roles[id]&&roles[id].name)||id;
  const chairOf=()=>(view&&view.chair)||chairId;
  function nameOf(id){if(id===YOU)return 'You';if(String(id).startsWith('role:'))return roleName(id.slice(5));return short(id);}

  // -- the list -------------------------------------------------------------
  const lastTs=x=>(x.last&&x.last.ts)||x.created||0;
  async function loadList(){
    if(filter==='chat'){
      const [all,res]=await Promise.all([hapi('list',{archived:true}),fetch('/api/chat/rooms',{headers:{Accept:'application/json'}})]);
      const d=res.ok?await res.json().catch(()=>({})):{};
      const taken=new Set(all.huddles.map(x=>x.room));
      rooms=(d.rooms||[]).filter(r=>!taken.has(r.room));
    }else{
      const r=await hapi('list',{archived:filter==='archived'});
      huddles=r.huddles.filter(x=>filter==='archived'?!!x.archived:!x.archived)
        .sort((a,b)=>(Number(b.chaired)-Number(a.chaired))||(lastTs(b)-lastTs(a)));
    }
    drawList();
  }
  function item(room,kind,title,meta,last,badge,name){
    const a=h('a',{href:'#huddles?room='+encodeURIComponent(name||room),class:'hx-item'+(cur&&cur.room===room?' on':'')});
    const top=el('div',undefined,'hx-item-top');top.append(el('b',title));if(badge)top.append(h('span',{class:'hx-badge',text:String(badge),title:kind==='chat'?'':'Waiting for you'}));
    a.append(top,el('small',meta,'hx-item-meta'));if(last)a.append(el('span',last,'hx-item-last'));
    a.onclick=e=>{e.preventDefault();open(room,kind).catch(showErr);};
    return a;
  }
  function drawList(){
    list.replaceChildren();
    if(filter==='chat'){
      for(const r of rooms)list.append(item(r.room,'chat',r.title||r.name,r.name?r.name+' · read-only':'read-only',r.last_sender?short(r.last_sender)+': '+(r.last_text||''):'',0));
      if(!rooms.length)list.append(el('p','No old chat rooms.','hx-empty'));
      return;
    }
    for(const x of huddles){
      const kind=x.chaired?(x.roles.length?x.roles.map(roleName).join(', '):'with '+short(chairId)):(x.scope==='project'?'Project status':'Band status');
      const last=x.last?nameOf(x.last.sender)+': '+x.last.text:'';
      list.append(item(x.room,'huddle',x.title.replace(/^Huddle: /,''),kind,last,x.chaired?x.open_cards:x.open_questions,x.name));
    }
    if(!huddles.length)list.append(el('p',filter==='archived'?'Nothing archived.':'No huddles yet.','hx-empty'));
  }

  // -- a thread ----------------------------------------------------------------
  async function open(room,kind,name){
    name=name||(huddles.find(x=>x.room===room)||rooms.find(x=>x.room===room)||{}).name;
    cur={room,kind};view=null;msgs=[];lastSeq=0;sig='';err.textContent='';
    wrap.classList.add('hx-open');drawList();
    history.replaceState(null,'','#huddles?room='+encodeURIComponent(name||room));
    head.replaceChildren();notice.replaceChildren();log.replaceChildren();
    main.replaceChildren(head,notice,log,...(kind==='chat'?[]:[compose]),err);
    box.value='';grow();closePop();
    await refresh();
    schedule();
    if(kind!=='chat'&&innerWidth>NARROW&&!box.disabled)box.focus();
  }
  function add(ms){for(const m of ms||[])if(!msgs.length||m.seq>msgs[msgs.length-1].seq)msgs.push(m);}
  async function refresh(){
    if(!cur)return;
    if(busy){again=true;return;}
    busy=true;const want=cur;
    try{
      let v;
      if(want.kind==='chat'){
        const r=await fetch('/api/chat/read?room='+encodeURIComponent(want.room)+'&since='+lastSeq,{headers:{Accept:'application/json'}});
        const d=await r.json().catch(()=>null);
        if(!r.ok||!d||d.ok===false)throw Error((d&&d.error)||'Could not read that room.');
        v={room:d.room||want.room,name:d.name,title:d.title,chat:true,messages:d.messages,last_seq:d.last_seq};
      }else v=await hapi('view',{room:want.room,since_seq:lastSeq,limit:150});
      if(cur!==want)return;
      view=v;add(v.messages);lastSeq=Math.max(lastSeq,v.last_seq||0);
      draw();err.textContent='';
    }catch(e){if(cur===want)showErr(e);}
    finally{busy=false;if(again){again=false;refresh();}}
  }
  function draw(){
    const r=(view.rounds||[]).at(-1);
    const s=JSON.stringify([view.title,view.archived,lastSeq,msgs.length,chairOn,(view.cards||[]).map(c=>c.id+c.state),
      r&&[r.id,r.state,(r.replies||[]).map(x=>x.state)],(view.questions||[]).filter(q=>q.state==='open').length]);
    if(s===sig)return;
    const first=!sig;sig=s;
    drawHead();drawNotice();drawLog(first);drawCompose();
  }
  function drawHead(){
    const back=h('button',{type:'button',class:'hx-back',text:'‹','aria-label':'Back to the list',title:'Back to the list'});
    back.onclick=()=>{wrap.classList.remove('hx-open');history.replaceState(null,'','#huddles');};
    const t=el('div',undefined,'hx-title');t.append(el('h2',(view.title||'').replace(/^Huddle: /,'')));
    const meta=el('div',undefined,'hx-meta');
    if(view.name)meta.append(copyChip(view.name,view.name,'hx-name'));
    if(view.chat)meta.append(el('span','Old chat · read-only','hx-dim'));
    else if(view.roles){meta.append(chip(short(chairOf())+' · chair','hx-chair'));for(const id of view.roles)meta.append(chip(roleName(id),'hx-role'));}
    else meta.append(el('span',view.scope==='project'?'Project status':'Band status','hx-dim'));
    t.append(meta);
    const acts=el('div',undefined,'hx-acts');
    if(view.roles){
      const room=view.room,archived=!!view.archived;
      const b=more([{label:archived?'Unarchive':'Archive',run:()=>busyDo(b,async()=>{await hapi('archive',{room,archived:!archived});sig='';await refresh();await loadList();})}],'Huddle actions','hx-btn hx-more');
      acts.append(b);
    }else if(!view.chat)acts.append(h('a',{class:'hx-btn',href:'#work?v=huddle&h='+encodeURIComponent(view.room),text:'Board',title:'Status board'}));
    head.replaceChildren(back,t,acts);
  }
  // Only notices you can act on.
  function drawNotice(){
    notice.replaceChildren();
    if(view.roles&&!chairOn)notice.append(el('span','Chair is off.'),h('a',{href:'#home',text:'Set up'}));
    const open=(view.questions||[]).filter(q=>q.state==='open').length;
    if(!view.roles&&open)notice.append(el('span',open+' question'+(open===1?'':'s')+' for you.'),
      h('a',{href:'#work?v=huddle&h='+encodeURIComponent(view.room),text:'Answer'}));
    notice.hidden=!notice.children.length;
  }
  function drawCompose(){
    if(view.chat)return;
    box.disabled=sendB.disabled=!!view.archived;
    box.placeholder=view.archived?'Archived':view.roles?(view.roles.length?'Ask… (@role for one)':'Message '+short(chairOf())):'Message';
  }
  // Cards attach to their message; a card without one goes after the last
  // message of its round (cards older than the loaded messages go first).
  function anchors(){
    const rounds=view.rounds||[],out=new Map(),put=(k,c)=>{if(!out.has(k))out.set(k,[]);out.get(k).push(c);};
    const firstSeq=msgs.length?msgs[0].seq:Infinity;
    for(const c of view.cards||[]){
      let seq=c.seq;
      if(!seq){
        const i=rounds.findIndex(r=>r.id===c.round),r=rounds[i];
        const until=i>=0&&rounds[i+1]?rounds[i+1].trigger_seq:Infinity;
        const inRound=msgs.filter(m=>m.seq<until&&(!r||m.seq>=r.trigger_seq));
        seq=inRound.length?inRound[inRound.length-1].seq:(msgs.length?msgs[msgs.length-1].seq:0);
      }
      put(seq<firstSeq?'top':seq,c);
    }
    return out;
  }
  function drawLog(first){
    const atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<80;
    log.replaceChildren();
    const at=view.chat?new Map():anchors();
    for(const c of at.get('top')||[])log.append(card(c));
    for(const m of msgs){log.append(message(m));for(const c of at.get(m.seq)||[])log.append(card(c));}
    if(!msgs.length)log.append(el('p',view.chat?'Empty.':view.roles?(view.roles.length?'Ask something.':'Say hi to '+short(chairOf())+'.'):'Nothing yet.','hx-empty'));
    const r=(view.rounds||[]).at(-1);
    if(r&&RUNNING.includes(r.state))log.append(pending(r));
    if(first||atBottom)log.scrollTop=log.scrollHeight;
  }
  function message(m){
    const sender=String(m.sender||''),mine=sender===YOU,role=sender.startsWith('role:'),chair=!view.chat&&sender===chairOf();
    const failed=(role||chair)&&FAILED.test(m.text);
    const route=chair&&m.text.split('\n').every(l=>/^@[a-z][\w-]*: /.test(l));
    const row=el('div',undefined,'hx-msg'+(mine?' me':'')+(failed?' failed':'')+(route?' route':''));row.dataset.seq=m.seq;
    const by=el('div',undefined,'hx-by');
    if(mine)by.append(el('b','You'));
    else if(role)by.append(chip(roleName(sender.slice(5)),'hx-role'));
    else if(chair)by.append(chip(short(sender)+' · chair','hx-chair'));
    else by.append(el('b',short(sender)));
    by.append(h('time',{class:'hx-time',text:when(m.ts),title:new Date(m.ts*1000).toLocaleString()}));
    row.append(by,el('div',m.text,'hx-text'));
    return row;
  }
  function card(c){
    const b=el('div',undefined,'hx-card hx-c-'+c.state);b.dataset.card=c.id;
    const top=el('div',undefined,'hx-card-top');
    top.append(el('span',KIND[c.kind]||c.kind,'hx-card-kind'),el('span',nameOf(c.by),'hx-dim'),el('span',CARD_STATE[c.state]||c.state,'hx-card-state'));
    if(c.name)top.append(copyChip(c.name,c.name,'hx-card-name'));
    b.append(top,el('p',c.summary,'hx-card-sum'));
    if(c.relayed_text)b.append(el('p',c.relayed_text,'hx-card-relay hx-dim'));
    else if(c.decided_by&&c.state!=='proposed')b.append(el('p',(c.state==='dismissed'?'Dismissed by ':'Approved by ')+nameOf(c.decided_by),'hx-card-relay hx-dim'));
    if(c.state==='dismissed'&&c.result&&c.result.dismissed)b.append(el('p','Reason: '+c.result.dismissed,'hx-card-relay hx-dim'));
    if(c.call){
      const call=el('div',undefined,'hx-card-call');const what=c.call.worker+' · '+c.call.cap;
      call.append(h('code',{text:what,title:what+' (tap to copy)','data-copy':what}));
      if(c.call.args&&Object.keys(c.call.args).length){const d=h('details');d.append(h('summary',{text:'Args'}),el('pre',JSON.stringify(c.call.args,null,2)));call.append(d);}
      b.append(call);
    }
    if(c.state==='proposed'&&!view.archived){
      const ok=h('button',{type:'button',class:'hx-btn primary',text:'Approve'}),no=h('button',{type:'button',class:'hx-btn',text:'Dismiss'});
      no.classList.add('quiet');ok.onclick=()=>decide(c,'approve',ok,no);no.onclick=()=>decide(c,'dismiss',ok,no);
      b.append(h('div',{class:'hx-card-acts'},ok,no));
    }else if(c.state==='failed'||c.result){
      // A failed card says why (the hub's c.error, else the reply's own).
      const res=c.result||{},inner=(res.result&&typeof res.result==='object')?res.result:{};
      const bad=c.error||res.denied||res.error||inner.error||((c.state==='failed'||res.ok===false)&&'the call failed');
      if(bad)b.append(el('p','Failed: '+String(bad),'hx-card-err'));
      else if(res.result!==undefined||res.truncated){
        const d=h('details',{class:'hx-card-res'});
        d.append(h('summary',{text:'Result'}),el('pre',clip(typeof res.result==='string'?res.result:JSON.stringify(res.result??res.truncated,null,2),4000)));
        b.append(d);
      }
    }
    return b;
  }
  async function decide(c,decision,...bs){
    bs.forEach(x=>x.disabled=true);
    try{await hapi('card',{room:cur.room,card:c.id,decision});sig='';await refresh();loadList().catch(()=>{});}
    catch(e){showErr(e);bs.forEach(x=>x.disabled=false);}
  }
  function pending(r){
    const p=el('div',undefined,'hx-pending');
    const line=(who,cls,what)=>{const d=el('div',undefined,'hx-wait');d.append(chip(who,cls),el('span',what,'hx-dots'));return d;};
    if(r.state==='chair')p.append(line(short(chairOf())+' · chair','hx-chair','thinking'));
    if(r.state==='roles')for(const x of r.replies||[])if(x.state==='running')p.append(line(roleName(x.role),'hx-role','answering'));
    if(r.state==='closing')p.append(line(short(chairOf())+' · chair','hx-chair','summing up'));
    return p;
  }
  async function busyDo(b,work){b.disabled=true;try{await work();}catch(e){showErr(e);}finally{b.disabled=false;}}

  // -- the composer: Enter sends, Shift+Enter is a new line, @ completes a role
  let opts=[],pick=0;
  function grow(){box.style.height='auto';box.style.height=Math.min(box.scrollHeight,200)+'px';}
  function suggest(){
    const ids=(view&&view.roles)||[],before=box.value.slice(0,box.selectionStart);
    const m=before.match(/(?:^|[^\w@])@([a-z0-9_-]*)$/i);
    if(!m||!ids.length)return closePop();
    const q=m[1].toLowerCase();
    opts=ids.filter(id=>id.startsWith(q)||roleName(id).toLowerCase().startsWith(q));
    if(!opts.length)return closePop();
    pick=Math.min(pick,opts.length-1);drawPop();
  }
  function drawPop(){
    pop.replaceChildren(...opts.map((id,i)=>{const li=h('li',{role:'option',class:i===pick?'on':'','aria-selected':String(i===pick)});
      li.append(el('b','@'+id),el('span',roleName(id)));li.onmousedown=e=>{e.preventDefault();pick=i;choose();};return li;}));
    pop.hidden=false;
  }
  function closePop(){pop.hidden=true;opts=[];pick=0;}
  function choose(){
    const id=opts[pick];if(!id)return closePop();
    const pos=box.selectionStart,before=box.value.slice(0,pos).replace(/@([a-z0-9_-]*)$/i,'@'+id+' ');
    box.value=before+box.value.slice(pos);box.selectionStart=box.selectionEnd=before.length;closePop();grow();box.focus();
  }
  box.addEventListener('keydown',e=>{
    if(!pop.hidden){
      if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();pick=(pick+(e.key==='ArrowDown'?1:-1)+opts.length)%opts.length;drawPop();return;}
      if(e.key==='Enter'||e.key==='Tab'){e.preventDefault();choose();return;}
      if(e.key==='Escape'){e.preventDefault();closePop();return;}
    }
    if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();submit();}
  });
  box.addEventListener('input',()=>{grow();suggest();});
  box.addEventListener('blur',()=>setTimeout(closePop,150));
  compose.onsubmit=e=>{e.preventDefault();submit();};
  async function submit(){
    const text=box.value.trim();
    if(!text||!cur||cur.kind==='chat'||sending||box.disabled)return;
    sending=true;sendB.disabled=true;err.textContent='';
    const ids=(view&&view.roles)||[];
    const mention=[...new Set([...text.matchAll(/(?:^|[^\w@])@([a-z][a-z0-9_-]*)/gi)].map(x=>x[1].toLowerCase()))].filter(id=>ids.includes(id)).map(id=>'role:'+id);
    try{await hapi('send',{room:cur.room,text,mention});box.value='';grow();await refresh();schedule();}
    catch(e){showErr(e);}
    finally{sending=false;sendB.disabled=!!(view&&view.archived);box.focus();}
  }

  // -- a new huddle --------------------------------------------------------------
  async function startForm(){
    const back=cur;cur=null;sig='';clearTimeout(timer);
    wrap.classList.add('hx-open');drawList();
    await loadRoles();
    const title=h('input',{placeholder:'Topic',autocomplete:'off',maxlength:'200'});
    const all=Object.values(roles),want=new Set(defaults.filter(id=>roles[id]&&roles[id].enabled!==false));
    const checks=all.map(r=>{const on=r.enabled!==false;
      const c=h('input',{type:'checkbox',value:r.id,checked:on&&(!want.size||want.has(r.id)),disabled:!on});
      return h('label',{class:'hx-check'},c,el('span',r.name||r.id),el('code','@'+r.id),on?null:el('small','off','hx-dim'));});
    const fe=h('p',{class:'hx-err',role:'alert'});
    const go=h('button',{type:'submit',class:'hx-btn primary',text:'Start'});
    const cancel=h('button',{type:'button',class:'hx-btn',text:'Cancel'});
    const form=h('form',{class:'hx-form'},h('h2',{text:'New huddle'}),
      h('label',{class:'hx-field'},'Title',title),
      h('fieldset',{class:'hx-roles'},h('legend',{text:checks.length?'Roles (none: just '+short(chairId)+')':'Roles'}),
        checks.length?checks:h('p',{class:'hx-dim'},'No roles: just '+short(chairId)+'. ',h('a',{href:'#roles',text:'Add roles'}))),
      h('div',{class:'hx-form-acts'},cancel,go),fe);
    cancel.onclick=()=>{if(back)open(back.room,back.kind).catch(showErr);else{wrap.classList.remove('hx-open');empty();}};
    form.onsubmit=async e=>{e.preventDefault();go.disabled=true;fe.textContent='';
      try{const picked=checks.map(l=>l.querySelector('input')).filter(c=>c.checked).map(c=>c.value);
        const r=await hapi('start',{title:title.value.trim()||'Huddle',roles:picked});
        if(filter!=='active'){filter='active';filterSel.value='active';}
        await loadList();await open(r.room,'huddle');}
      catch(x){fe.textContent=x.message;go.disabled=false;}};
    main.replaceChildren(h('header',{class:'hx-head'},h('button',{type:'button',class:'hx-back',text:'‹','aria-label':'Back to the list',onclick:()=>{wrap.classList.remove('hx-open');}})),form);
    title.focus();
  }
  function empty(){main.replaceChildren(el('p',huddles.length?'Pick a huddle.':'No huddles yet.','hx-empty hx-pick'));}

  // -- polling: 1.5 s while a round runs, slower otherwise, never while hidden
  function running(){const r=(view&&view.rounds||[]).at(-1);return !!r&&RUNNING.includes(r.state);}
  function schedule(){clearTimeout(timer);if(active&&cur)timer=setTimeout(tick,running()?1500:6000);}
  async function tick(){
    if(active&&!document.hidden){await refresh();if(++ticks%5===0)loadList().catch(()=>{});}
    schedule();
  }
  document.addEventListener('visibilitychange',()=>{if(active&&cur&&!document.hidden){clearTimeout(timer);tick();}});

  return {root:wrap,
    /** ``query``: the hash's query (room=<id or name>: open that huddle, or that old chat room). */
    async activate(query){
      active=true;
      await loadRoles();
      const room=new URLSearchParams(query||'').get('room');
      const same=cur&&(cur.room===room||(view&&view.name&&view.name===norm(room)));
      if(room&&!same){
        const all=(await hapi('list',{archived:true}).catch(()=>({huddles:[]}))).huddles;
        let hit=all.find(x=>x.room===room||x.name===norm(room));
        // A near-miss name: the hub resolves it for a read.
        if(!hit){const v=await hapi('view',{room}).catch(()=>null);if(v)hit=all.find(x=>x.room===v.room);}
        filter=hit?(hit.archived?'archived':'active'):'chat';filterSel.value=filter;
        await loadList().catch(showErr);
        await open(hit?hit.room:room,hit?'huddle':'chat',hit&&hit.name);
        return;
      }
      await loadList().catch(e=>list.replaceChildren(el('p',e.message,'hx-err')));
      if(cur){sig='';await refresh();schedule();return;}
      const first=huddles.find(x=>x.chaired)||huddles[0];
      if(first&&innerWidth>NARROW)await open(first.room,'huddle');else empty();
    },
    deactivate(){active=false;clearTimeout(timer);}};
}
