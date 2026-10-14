// Work > Huddle: a project's (or band's) group room where agents check in.
// A status strip of each agent's latest status, the questions waiting for you
// (pinned until you answer; the answer goes back to the agent and wakes it),
// and the conversation. Talks to /account/huddle/api (operator session +
// CSRF). Stored text is only ever rendered as text.

const search=new URL(import.meta.url).search;
const {h,el,ago}=await import('/account/bands/assets/manage.js'+search);
const {avatar,who}=await import('/account/knowledge/assets/workviews.js'+search);

const API='/account/huddle/api';
const YOU='user:operator';
const STATE={working:'Working',blocked:'Blocked',needs_decision:'Needs you',idle:'Idle',done:'Done'};
const QUIET=2*3600;

function loadCss(){if(document.querySelector('link[data-hd]'))return;const l=document.createElement('link');l.rel='stylesheet';l.href='/account/huddle/assets/huddle.css'+search;l.dataset.hd='1';document.head.append(l);}

let csrf='';
async function api(action,body={}){
  if(!csrf){const r=await fetch(API);const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'Sign in with your operator account to see huddles.');csrf=d.csrf;}
  const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,action,...body})});
  const d=await r.json().catch(()=>({}));if(r.status===403&&/expired/i.test(d.error||'')){csrf='';return api(action,body);}
  if(!r.ok||d.error)throw Error(d.error||'Request failed');return d.result;}

/** Questions waiting for an answer, across every huddle (the Work tab badge). */
export async function openQuestions(){const r=await api('list');return r.huddles.reduce((n,x)=>n+(x.open_questions||0),0);}

const name=id=>id===YOU?'You':who(id);
function button(label,fn,cls,attrs){const b=h('button',{type:'button',class:cls||'',text:label,...attrs});b.onclick=e=>{e.preventDefault();Promise.resolve().then(fn).catch(err=>console.warn(err));};return b;}
const taskLink=slug=>{const a=el('a',slug,'hd-task');a.href='#work?t='+encodeURIComponent(slug);return a;};

export async function mountHuddle(root,{onPins}={}){
  loadCss();
  const wrap=el('div',undefined,'hd-layout');const list=el('aside',undefined,'hd-list');const main=el('section',undefined,'hd-main');wrap.append(list,main);root.append(wrap);
  let cur=null,lastSeq=0,timer=null,active=false,huddles=[],view=null,busy=false;const drafts={};
  const strip=el('div',undefined,'hd-strip');strip.setAttribute('aria-label','Agent status');
  const pins=el('section',undefined,'hd-pins');const log=el('div',undefined,'hd-log');log.setAttribute('role','log');log.setAttribute('aria-live','polite');
  const head=el('header',undefined,'hd-head');
  const compose=el('form',undefined,'hd-compose');const box=el('input');box.placeholder='Message the huddle… (@name to address an agent)';box.setAttribute('aria-label','Message');
  const send=el('button','Send','hd-send');send.type='submit';compose.append(box,send);
  const err=el('p','','kn-error hd-err');

  async function drawList(){
    const r=await api('list');huddles=r.huddles;list.replaceChildren(el('h3','Huddles'));
    const total=huddles.reduce((n,x)=>n+(x.open_questions||0),0);onPins&&onPins(total);
    for(const x of huddles.slice().sort((a,b)=>(b.open_questions-a.open_questions)||((b.last?.ts||0)-(a.last?.ts||0)))){
      const a=el('a',undefined,'hd-item'+(cur&&x.room===cur.room?' hd-current':''));a.href='#work?v=huddle&h='+encodeURIComponent(x.room);
      const top=el('div',undefined,'hd-item-top');top.append(el('strong',x.title.replace(/^Huddle: /,'')));if(x.open_questions)top.append(el('span',String(x.open_questions),'hd-badge'));
      a.append(top,el('small',(x.scope==='project'?'Project':'Band')+' · '+(x.agents||0)+' agent'+(x.agents===1?'':'s')+(x.last?' · '+ago(x.last.ts,''):''),'kn-dim'));
      if(x.last)a.append(el('span',name(x.last.sender)+': '+x.last.text,'hd-item-last'));
      list.append(a);}
    if(!huddles.some(x=>x.scope==='band')){const b=button('Open the band huddle',()=>{location.hash='#work?v=huddle&hb=_';},'kn-linkish hd-start');list.append(b);}
    list.append(el('p','Each project gets a huddle when an agent first reports in, or when you open it from the project page.','kn-dim hd-note'));}

  function drawHead(){head.replaceChildren();const t=el('div');t.append(el('h1',view.title.replace(/^Huddle: /,'')));
    const sub=el('p',undefined,'kn-meta');sub.append(document.createTextNode((view.scope==='project'?'Project huddle':'Band huddle')+' · '+view.participants.filter(p=>p!==YOU).length+' agents · '));
    if(view.scope==='project'){const a=el('a','Project page');a.href='#work?p='+encodeURIComponent(view.ref);sub.append(a,document.createTextNode(' · '));}
    const c=el('a','Open in Chat');c.href='#chat?room='+encodeURIComponent(view.room);sub.append(c);t.append(sub);head.append(t);}

  function drawStrip(){strip.replaceChildren();
    if(!view.statuses.length){strip.append(el('p','No agent has reported in yet. Agents post here with rook_huddle when they start, get stuck or finish.','kn-dim hd-empty'));return;}
    const now=Date.now()/1000;
    for(const s of view.statuses){const quiet=now-s.ts>QUIET&&!['done','idle'].includes(s.state);
      const c=el('article',undefined,'hd-agent hd-st-'+s.state+(quiet?' hd-quiet':''));
      const top=el('div',undefined,'hd-agent-top');top.append(avatar(s.identity),el('b',name(s.identity)),el('span',STATE[s.state]||s.state,'hd-pill'));c.append(top);
      if(s.working_on)c.append(el('p',s.working_on,'hd-doing'));
      if(s.blocked_on)c.append(el('p','Blocked: '+s.blocked_on,'hd-blocked'));
      if(s.needs_decision)c.append(el('p','Needs: '+s.needs_decision,'hd-needs'));
      const foot=el('div',undefined,'hd-agent-foot');if(s.task)foot.append(taskLink(s.task));foot.append(el('span',quiet?'quiet for '+ago(s.ts,'').replace(' ago',''):ago(s.ts,''),'hd-age'));c.append(foot);
      strip.append(c);}}

  function drawPins(){pins.replaceChildren();const open=view.questions.filter(q=>q.state==='open');const done=view.questions.filter(q=>q.state!=='open');
    if(open.length){const h2=el('h2',undefined,'hd-pins-head');h2.append(el('span','Waiting for you'),el('span',String(open.length),'hd-badge'));pins.append(h2);}
    for(const q of open){const card=el('article',undefined,'hd-q');card.id='hd-q-'+q.id;
      const top=el('div',undefined,'hd-q-top');top.append(avatar(q.asker),el('b',name(q.asker)),el('span','asked '+ago(q.asked,''),'kn-dim'));if(q.task)top.append(taskLink(q.task));card.append(top);
      card.append(el('p',q.text,'hd-q-text'));
      const form=el('form',undefined,'hd-answer');const ta=el('textarea');ta.rows=2;ta.placeholder='Your answer';ta.setAttribute('aria-label','Answer to '+name(q.asker));ta.value=drafts[q.id]||'';ta.oninput=()=>{drafts[q.id]=ta.value;};
      if(q.options&&q.options.length){const opts=el('div',undefined,'hd-opts');for(const o of q.options)opts.append(button(o,()=>{ta.value=o;drafts[q.id]=o;ta.focus();},'hd-opt'));form.append(opts);}
      const go=el('button','Answer','hd-send');go.type='submit';const row=el('div',undefined,'hd-answer-row');row.append(ta,go);form.append(row);
      const t=q.wake_target||{};const target=el('p',t.state==='ready'?'Answering wakes the agent on '+t.worker+', with its own permissions.'
        :'The agent sees your answer on its next call'+(t.reason?' ('+t.reason+')':'')+'.','hd-target'+(t.state==='ready'?' hd-target-on':''));form.append(target);
      const note=el('p','','hd-wake kn-dim');form.append(note);
      form.onsubmit=async e=>{e.preventDefault();if(!ta.value.trim())return ta.focus();go.disabled=true;note.textContent='';
        try{const r=await api('answer',{question:q.id,text:ta.value});delete drafts[q.id];
          const w=r.wake||{};note.textContent=w.state==='waking'?'Sent. Waking '+w.worker+'…':w.state==='notice'?'Sent. The agent will see it on its next call ('+(w.reason||'it cannot be woken')+').':'Sent.';
          await refresh(true);}catch(x){note.textContent=x.message;note.classList.add('kn-error');}finally{go.disabled=false;}};
      card.append(form);pins.append(card);}
    if(done.length){const d=el('details',undefined,'hd-answered');d.append(el('summary','Answered lately ('+done.length+')'));
      for(const q of done.slice().reverse()){const r=el('div',undefined,'hd-done');r.append(el('b',name(q.asker)+': '),document.createTextNode(q.text));
        if(q.state==='answered'){const a=el('div',undefined,'hd-done-a');a.append(el('span','↳ '+(q.answered_by||'You')+': ','kn-dim'),document.createTextNode(q.answer));
          if(q.wake)a.append(el('span',' · '+(q.wake.state==='woken'?'woke '+q.wake.worker:q.wake.state==='waking'?'waking '+q.wake.worker:q.wake.state==='failed'?'wake failed':'seen on its next call'),'kn-dim'));r.append(a);}
        else r.append(el('div','withdrawn','kn-dim'));d.append(r);}
      pins.append(d);}
    pins.hidden=!open.length&&!done.length;}

  function message(m){
    const mine=m.sender===YOU;const row=el('div',undefined,'hd-msg'+(mine?' hd-mine':'')+(m.kind?' hd-k-'+m.kind:''));row.dataset.seq=m.seq;
    if(m.kind==='status'){const s=m.data||{};row.append(avatar(m.sender,'sm'));const body=el('div',undefined,'hd-line');
      body.append(el('b',name(m.sender)),el('span',STATE[s.state]||'Status','hd-pill hd-st-'+(s.state||'working')));
      if(s.working_on)body.append(el('span',s.working_on));if(s.blocked_on)body.append(el('span','Blocked: '+s.blocked_on,'hd-blocked'));
      if(s.needs_decision)body.append(el('span','Needs: '+s.needs_decision,'hd-needs'));if(s.task)body.append(taskLink(s.task));
      body.append(el('time',ago(m.ts,''),'hd-time'));row.append(body);return row;}
    row.append(mine?el('span','YOU','wv-av sm hd-you'):avatar(m.sender,'sm'));const body=el('div',undefined,'hd-bubble');const top=el('div',undefined,'hd-by');
    top.append(el('b',name(m.sender)));if(m.kind==='question'){const q=view.questions.find(x=>x.id===m.seq);top.append(el('span',q&&q.state==='answered'?'Question · answered':'Question','hd-tag'));}
    if(m.kind==='answer')top.append(el('span','Answer','hd-tag hd-tag-a'));
    top.append(el('time',ago(m.ts,''),'hd-time'));body.append(top);
    // The stored text carries a prefix (and options) for plain chat clients; the tag and the pinned card say it here.
    let text=m.text;if(m.kind==='question')text=text.replace(/^Question: /,'').replace(/ \(options: [^)]*\)$/,'');
    if(m.kind==='answer')text=text.replace(/^Answer for \S+: /,'');
    body.append(el('p',text));if(m.mentions&&m.mentions.length&&!mine&&m.kind!=='question')body.append(el('small','to '+m.mentions.map(name).join(', '),'kn-dim'));
    if(mine&&m.kind==='answer'&&m.mentions&&m.mentions.length)body.append(el('small','to '+m.mentions.map(name).join(', '),'kn-dim'));
    row.append(body);return row;}

  const seen=new Map();   // seq -> message, to redraw a question once it is answered
  function appendMessages(msgs,reset){const atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<40;if(reset){log.replaceChildren();seen.clear();}
    for(const m of msgs){if(seen.has(m.seq))continue;seen.set(m.seq,m);log.append(message(m));}
    if(!seen.size)log.replaceChildren(el('p','Nothing here yet.','kn-dim hd-empty'));else log.querySelector('.hd-empty')?.remove();
    if(reset||atBottom)log.scrollTop=log.scrollHeight;}

  let ticks=0;
  async function refresh(full){if(!cur||busy)return;busy=true;
    try{const v=await api('view',{room:cur.room,since_seq:full?0:lastSeq,limit:120});
      const reset=full||!view||v.room!==view.room;view=v;lastSeq=v.last_seq;
      drawHead();drawStrip();drawPins();
      appendMessages(v.messages,reset);
      if(!reset&&v.messages.some(m=>m.kind==='answer'))
        for(const n of log.querySelectorAll('.hd-k-question')){const m=seen.get(+n.dataset.seq);if(m)n.replaceWith(message(m));}
      if(full||v.messages.length||++ticks%5===0)await drawList();
      err.textContent='';}
    catch(e){err.textContent=e.message;}finally{busy=false;}}

  compose.onsubmit=async e=>{e.preventDefault();const text=box.value.trim();if(!text||!cur)return;send.disabled=true;
    const mention=[...text.matchAll(/@([\w.:@-]+)/g)].map(x=>x[1]).map(n=>view.participants.find(p=>p===n||who(p)===n||who(p).split(/[_@]/)[0]===n)).filter(Boolean);
    try{await api('send',{room:cur.room,text,mention});box.value='';await refresh(false);}catch(x){err.textContent=x.message;}finally{send.disabled=false;box.focus();}};

  function layoutMain(){main.replaceChildren(head,strip,pins,log,compose,err);}

  return {root:wrap,
    async open(where){active=true;
      const want={room:where.room||undefined,project:where.project||undefined,band:where.band&&where.band!=='_'?where.band:undefined};
      if(!want.room&&!want.project&&!where.band){await drawList();
        const first=huddles.slice().sort((a,b)=>(b.open_questions-a.open_questions)||((b.last?.ts||0)-(a.last?.ts||0)))[0];
        if(first)want.room=first.room;else{main.replaceChildren(el('h1','Huddle'),el('p','A huddle is where your agents check in: what each is working on, what is blocking it, and questions only you can answer. None has started yet. Open a project and choose Huddle, or start the band huddle.','kn-dim'));clearInterval(timer);return;}}
      try{const v=await api('view',{...want,limit:120});cur={room:v.room};view=null;lastSeq=0;layoutMain();await refresh(true);}
      catch(e){main.replaceChildren(el('p',e.message,'kn-error'));}
      clearInterval(timer);
      timer=setInterval(()=>{if(active&&!document.hidden)refresh(false);},4000);},
    deactivate(){active=false;clearInterval(timer);}};
}
