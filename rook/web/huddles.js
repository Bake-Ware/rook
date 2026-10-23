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
export async function hapi(action,body={},retry=true,signal){
  if(!csrf){const r=await fetch(API);const d=await r.json().catch(()=>({}));if(!r.ok)throw Error(d.error||'Sign in with your operator account.');csrf=d.csrf;}
  const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,action,...body}),signal});
  const d=await r.json().catch(()=>({}));
  if(r.status===403&&retry&&/expired/i.test(d.error||'')){csrf='';return hapi(action,body,false,signal);}
  if(!r.ok||d.error)throw Error(d.error||'Request failed');
  return d.result;
}

// -- reading a message aloud ------------------------------------------------------
// The Speak button on a bubble asks the hub to read it (speech.py on the hub:
// markdown, code, links and ids made speakable; the role's voice, else the
// band's default) and plays the audio here. The hub cuts the text into
// sentence-sized chunks: the first plays as soon as it arrives, the next two
// load while it plays, and they follow each other without a gap. Chunks are
// kept per (voice, text), so a replay is instant; a chunk that fails twice is
// skipped with a short note. With no voice engine the hub can reach, this
// browser's own speech reads it, and the button's tooltip says so. One
// message speaks at a time; tapping again stops it. A long press (or
// right-click) offers "Play on <device>" for the devices the hub allows.
let speaking=null,devicesP=null,actx=null;
const speakKey=(room,ref)=>room+'|'+(ref.card?'c:'+ref.card:ref.seq);
function speakMark(b,state,tip){
  b.classList.toggle('on',state==='playing');b.classList.toggle('wait',state==='loading');
  b.setAttribute('aria-pressed',String(state==='playing'||state==='loading'));
  b.textContent=state?'■':'▶';
  b.title=tip||(state?'Stop':b.dataset.tip||'Speak');
  if(!state&&b.style)b.style.removeProperty('--p');
}
/** Stop whatever message is speaking. */
export function stopSpeaking(){
  const s=speaking;if(!s)return;speaking=null;s.stopped=true;
  try{s.ctl&&s.ctl.abort();}catch(e){}
  for(const src of s.sources||[])try{src.stop();}catch(e){}
  try{s.audio&&s.audio.pause();}catch(e){}
  if(s.browser&&window.speechSynthesis)speechSynthesis.cancel();
  if(s.url)URL.revokeObjectURL(s.url);
  s.done&&s.done();
  if(s.btn)speakMark(s.btn,'',s.tip);
}
const b64buf=b64=>{const bin=atob(b64),buf=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)buf[i]=bin.charCodeAt(i);return buf.buffer;};
function playClip(s,buf,mime){
  return new Promise((resolve,reject)=>{
    if(s.url)URL.revokeObjectURL(s.url);
    s.url=URL.createObjectURL(new Blob([buf],{type:mime||'audio/wav'}));
    const a=new Audio(s.url);s.audio=a;s.done=resolve;
    a.onended=()=>resolve();a.onerror=()=>reject(Error('this browser could not play the audio'));
    a.play().catch(reject);
  });
}
// Played chunks by (voice, text), least recently used out first.
const CLIPS=new Map(),CLIP_LIMIT=48*1024*1024;let clipBytes=0;
function clipGet(k){const v=CLIPS.get(k);if(v){CLIPS.delete(k);CLIPS.set(k,v);}return v;}
function clipPut(k,buf){
  if(!buf||buf.byteLength>CLIP_LIMIT)return;
  if(CLIPS.has(k))clipBytes-=CLIPS.get(k).byteLength;
  CLIPS.delete(k);CLIPS.set(k,buf);clipBytes+=buf.byteLength;
  for(const [old,v] of CLIPS){if(clipBytes<=CLIP_LIMIT)break;CLIPS.delete(old);clipBytes-=v.byteLength;}
}
/** Where chunks play, one after another: Web Audio schedules each to start
 *  where the last ends (no gap); without it, audio elements in turn. Make it
 *  inside the click, so the browser lets it play. ``out(buf)`` resolves to
 *  {started, ended} promises for that chunk. */
function audioOut(s){
  const AC=window.AudioContext||window.webkitAudioContext;
  if(AC){
    try{actx=actx||new AC();if(actx.state==='suspended')actx.resume().catch(()=>{});}catch(e){actx=null;}
  }
  if(actx){
    const ctx=actx;let at=0;s.sources=[];
    return async buf=>{
      const ab=await ctx.decodeAudioData(buf.slice(0));
      if(s.stopped)return {started:Promise.resolve(),ended:Promise.resolve()};
      const src=ctx.createBufferSource();src.buffer=ab;src.connect(ctx.destination);
      const start=Math.max(at,ctx.currentTime+0.03);src.start(start);at=start+ab.duration;s.sources.push(src);
      return {started:new Promise(r=>setTimeout(r,Math.max(0,(start-ctx.currentTime)*1000))),
              ended:new Promise(r=>{src.onended=()=>r();})};
    };
  }
  let chain=Promise.resolve();
  return async buf=>{
    const started=chain,ended=chain.then(()=>s.stopped?null:playClip(s,buf,'audio/wav'));
    chain=ended.catch(()=>{});return {started,ended};
  };
}
/** Play ``count`` chunks in order. ``load(i)`` fetches one (a failure is
 *  tried once more, then skipped with ``onSkip(i, error)``; two skipped in a
 *  row end it with that error); at most ``ahead`` load at once while earlier
 *  ones play. ``play(clip, i)`` queues a chunk after the last and resolves to
 *  {started, ended}. Ends early once ``stopped()``. */
export async function playChunks({count,load,play,ahead=2,stopped=()=>false,onSkip=()=>{}}){
  const loads=[];let ended=Promise.resolve(),skipped=0;
  const want=i=>{if(i<count&&!loads[i])loads[i]=load(i).catch(()=>{if(stopped())throw Error('stopped');return load(i);})
    .then(c=>({c}),e=>({e}));};
  for(let i=0;i<count;i++){
    for(let j=i;j<i+ahead;j++)want(j);
    const r=await loads[i];
    if(stopped())return;
    if(r.e){
      if(++skipped>=2)throw r.e;
      onSkip(i,r.e);continue;
    }
    skipped=0;
    const h=await play(r.c,i);
    ended=h.ended;
    await h.started;
    if(stopped())return;
  }
  await ended;
}
/** Read ``text`` with this browser's speech; resolves when it ends. */
export function browserSpeak(s,text){
  return new Promise((resolve,reject)=>{
    if(!window.speechSynthesis||!window.SpeechSynthesisUtterance)return reject(Error('this browser cannot speak'));
    // Short utterances: some browsers stop a long one after about 15 s.
    const bits=(String(text).match(/[^.!?]+[.!?]*\s*/g)||[String(text)]).reduce((out,x)=>{
      if(out.length&&(out[out.length-1]+x).length<220)out[out.length-1]+=x;else out.push(x);return out;},[]);
    s.browser=true;s.done=resolve;speechSynthesis.cancel();
    bits.forEach((t,i)=>{const u=new SpeechSynthesisUtterance(t.trim());
      if(i===bits.length-1){u.onend=()=>resolve();u.onerror=e=>e.error==='interrupted'||e.error==='canceled'?resolve():reject(Error(e.error||'speech failed'));}
      speechSynthesis.speak(u);});
  });
}
/** A short note beside the button that goes away by itself. */
function speakNote(b,text){
  if(!b||!b.after)return;
  b.parentNode?.querySelector('.hx-speak-note')?.remove();
  const n=h('small',{class:'hx-speak-note',text});b.after(n);setTimeout(()=>n.remove(),8000);
}
async function speakNow(b,room,ref){
  stopSpeaking();
  const s={key:speakKey(room,ref),btn:b,stopped:false,tip:b.dataset.tip,
           ctl:typeof AbortController==='function'?new AbortController():null};speaking=s;
  speakMark(b,'loading');
  const out=audioOut(s);
  try{
    const p=await hapi('speak',{room,...ref,plan:true},true,s.ctl?.signal);
    if(s.stopped)return;
    if(p.mode==='browser'){
      // No voice engine the hub can reach: this browser reads it.
      s.tip='Spoken by this browser: '+(p.reason||'no voice engine')+'.';
      if(s.btn){s.btn.dataset.tip=s.tip;speakMark(s.btn,'playing','Stop. '+s.tip);}
      if(p.text)await browserSpeak(s,p.text);
      return;
    }
    const chunks=p.chunks||[],n=chunks.length;
    if(!n)return;
    s.tip=p.source==='role'?'Speak (voice '+p.voice+')':'Speak (the band\'s voice'+(p.label?', '+p.label:'')+')';
    if(p.reason)s.tip+=': '+p.reason;
    const vkey=(p.source==='role'?'role:'+p.voice:'default:'+(p.label||''))+'|';
    await playChunks({count:n,ahead:2,stopped:()=>s.stopped,
      load:async i=>{
        const hit=clipGet(vkey+chunks[i]);if(hit)return hit;
        const r=await hapi('speak',{room,...ref,part:i},true,s.ctl?.signal);
        if(r.mode!=='hub'||!r.audio)throw Error(r.reason||'the voice engine failed');
        const buf=b64buf(r.audio);
        if(r.source===p.source&&r.voice===p.voice)clipPut(vkey+chunks[i],buf);   // not a fallback voice
        return buf;},
      play:async(buf,i)=>{
        const hd=await out(buf);
        hd.started.then(()=>{if(s.stopped||!s.btn)return;
          if(n>1)s.btn.style?.setProperty('--p',String((i+1)/n));
          speakMark(s.btn,'playing',n>1?'Stop (part '+(i+1)+' of '+n+')':'Stop');});
        return hd;},
      onSkip:(i,e)=>{speakNote(s.btn,'Skipped part '+(i+1)+' of '+n+': '+e.message);}});
  }catch(e){if(!s.stopped&&s.btn){s.tip='Speak. Last try failed: '+e.message;s.btn.dataset.tip=s.tip;}}
  finally{if(speaking===s){speaking=null;if(s.url)URL.revokeObjectURL(s.url);if(s.btn){s.btn.dataset.tip=s.tip||'';speakMark(s.btn,'',s.tip);}}}
}
function deviceMenu(b,room,ref){
  devicesP=devicesP||hapi('speak_devices').then(r=>r.devices||[]).catch(()=>[]);
  devicesP.then(list=>{
    document.querySelector('.hx-speak-menu')?.remove();
    if(!list.length){b.title='No device to play on.';return;}
    const menu=h('ul',{class:'hx-speak-menu',role:'menu'});
    const close=()=>{menu.remove();document.removeEventListener('pointerdown',away,true);document.removeEventListener('keydown',esc,true);};
    const away=e=>{if(!menu.contains(e.target))close();};
    const esc=e=>{if(e.key==='Escape'){close();b.focus();}};
    for(const d of list){const it=h('button',{type:'button',role:'menuitem',text:'Play on '+d.worker});
      it.onclick=async()=>{close();b.title='Sending to '+d.worker+'…';
        try{await hapi('speak_on',{room,...ref,worker:d.worker});b.title='Playing on '+d.worker+'.';}
        catch(e){b.title='Play on '+d.worker+' failed: '+e.message;}};
      menu.append(h('li',{role:'none'},it));}
    const r=b.getBoundingClientRect();
    menu.style.top=Math.min(r.bottom+4,innerHeight-60*list.length-8)+'px';menu.style.left=Math.max(8,Math.min(r.left,innerWidth-220))+'px';
    document.body.append(menu);menu.querySelector('button').focus();
    setTimeout(()=>{document.addEventListener('pointerdown',away,true);document.addEventListener('keydown',esc,true);});
  });
}
// -- picking a voice ----------------------------------------------------------------
// The voice pickers on the Roles, Home agent and Voice pages: the voice
// engine's voices (Chatterbox first, then Kokoro), a blank choice for the
// band's default, and a Preview button that plays a short sample here.
let voicesP=null;
const ENGINE_NAMES={chatterbox:'Chatterbox (natural voices)',kokoro:'Kokoro (quick voices)'};
/** The hub's voices (kept for the page's life; ``fresh`` asks again). */
export function voiceList(fresh){
  // A failed or empty list is not kept: the next picker asks again.
  if(fresh||!voicesP)voicesP=hapi('voices').then(v=>{if(!(v.voices||[]).length)voicesP=null;return v;})
    .catch(e=>{voicesP=null;return {available:false,voices:[],catalog:[],reason:e.message};});
  return voicesP;
}
/** A voice select with a Preview button. ``blank`` labels the empty choice
 *  (given the band's default name). Returns {el, select, note}. */
const BAND_DEFAULT=d=>'Band default'+(d?' ('+d+')':'');
/** Fill ``sel`` with the hub's voices, grouped by engine (Chatterbox first),
 *  ``current`` selected and a blank choice labelled ``blank(default)``.
 *  Resolves to the voice list. The Settings page uses it too. */
export async function fillVoiceSelect(sel,current,blank=BAND_DEFAULT){
  current=String(current||'');
  const v=await voiceList();
  const cat=(v.catalog&&v.catalog.length)?v.catalog:(v.voices||[]).map(x=>({id:x,label:x}));
  const opt=c=>h('option',{value:c.id,text:c.label&&c.label.toLowerCase()!==c.id.toLowerCase()?c.label+' ('+c.id+')':(c.label||c.id),selected:c.id===current});
  const groups={};for(const c of cat)(groups[c.engine||'']=groups[c.engine||'']||[]).push(c);
  const rank=k=>k==='chatterbox'?0:k==='kokoro'?1:2;
  const order=Object.keys(groups).sort((a,b)=>rank(a)-rank(b)||a.localeCompare(b));
  sel.replaceChildren(h('option',{value:'',text:blank(v.default||'')}),
    ...(order.length>1||order[0]?order.map(k=>h('optgroup',{label:ENGINE_NAMES[k]||k||'Other'},groups[k].map(opt))):cat.map(opt)));
  if(current&&!cat.some(c=>c.id===current))sel.append(h('option',{value:current,text:current+' (not on the voice engine)',selected:true}));
  return v;
}
export function voicePicker(current,{blank=BAND_DEFAULT}={}){
  current=String(current||'');
  const sel=h('select',{'aria-label':'Voice'},h('option',{value:'',text:blank('')}));
  if(current)sel.append(h('option',{value:current,text:current,selected:true}));
  const note=h('small',{class:'mg-sub',style:'white-space:normal'});
  const play=h('button',{type:'button',class:'mg-btn',text:'Preview',title:'Hear this voice'});
  fillVoiceSelect(sel,current,blank).then(v=>{
    if(!v.available)note.textContent='No voice engine on this hub now ('+(v.reason||'voice is off')+'): the browser reads messages aloud.';
    else if(!(v.voices||[]).length)note.textContent='The voice engine did not list its voices'+(v.error?' ('+v.error+')':'')+'.';
  });
  play.onclick=async e=>{
    e.preventDefault();
    if(speaking&&speaking.key==='preview'){stopSpeaking();return;}
    stopSpeaking();
    const s={key:'preview',btn:null,stopped:false};speaking=s;play.textContent='Stop';
    try{
      const r=await hapi('preview',{voice:sel.value});
      if(s.stopped)return;
      if(r.mode==='browser'){note.textContent='Spoken by this browser: '+(r.reason||'no voice engine')+'.';await browserSpeak(s,r.text);}
      else{note.textContent='';await playClip(s,b64buf(r.audio),r.mime);}
    }catch(err){if(!s.stopped)note.textContent='Preview failed: '+err.message;}
    finally{if(speaking===s){speaking=null;if(s.url)URL.revokeObjectURL(s.url);}play.textContent='Preview';}
  };
  return {el:h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},sel,play),select:sel,note};
}

/** The Speak button for a message (``ref`` {seq}) or card ({card}) in ``room``. */
export function speakButton(room,ref){
  const b=h('button',{type:'button',class:'hx-speak','aria-label':'Speak'});
  speakMark(b,'');
  if(speaking&&speaking.key===speakKey(room,ref)){speaking.btn=b;b.dataset.tip=speaking.tip||'';speakMark(b,'playing','Stop');}
  let held=null,long=false;
  b.addEventListener('pointerdown',e=>{if(e.button)return;long=false;clearTimeout(held);held=setTimeout(()=>{long=true;deviceMenu(b,room,ref);},600);});
  for(const ev of ['pointerup','pointerleave','pointercancel'])b.addEventListener(ev,()=>clearTimeout(held));
  b.addEventListener('contextmenu',e=>{e.preventDefault();clearTimeout(held);if(!long)deviceMenu(b,room,ref);long=true;});
  b.onclick=e=>{e.preventDefault();e.stopPropagation();if(long){long=false;return;}
    if(speaking&&speaking.key===speakKey(room,ref)){stopSpeaking();return;}
    speakNow(b,room,ref);};
  return b;
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
    if(!cur||cur.room!==room)stopSpeaking();
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
      r&&[r.id,r.state,(r.replies||[]).map(x=>x.state)],(view.questions||[]).filter(q=>q.state==='open').length,
      view.table&&[(view.table.artifacts||[]).map(a=>a.id+a.state),(view.table.motions||[]).map(m=>m.id+m.state+(m.ballots||[]).length)]]);
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
    const tbl=tableStrip();if(tbl)t.append(tbl);
    const acts=el('div',undefined,'hx-acts');
    if(view.roles){
      const room=view.room,archived=!!view.archived;
      const b=more([{label:archived?'Unarchive':'Archive',run:()=>busyDo(b,async()=>{await hapi('archive',{room,archived:!archived});sig='';await refresh();await loadList();})}],'Huddle actions','hx-btn hx-more');
      acts.append(b);
    }else if(!view.chat)acts.append(h('a',{class:'hx-btn',href:'#work?v=huddle&h='+encodeURIComponent(view.room),text:'Board',title:'Status board'}));
    head.replaceChildren(back,t,acts);
  }
  // The conference table: what is pinned (each at its exact version) and the
  // votes on it; ballots stay sealed until every required voter has voted.
  const VOTE_WORD={yes:'yes',no:'no',cant_verify:"couldn't verify"};
  function tableStrip(){
    const tb=view.table;if(!tb)return null;
    const arts=(tb.artifacts||[]).filter(a=>a.state==='pinned'),mots=tb.motions||[];
    if(!arts.length&&!mots.length)return null;
    const d=h('details',{class:'hx-table'});
    const live=mots.filter(m=>!['executed','void','rejected','withdrawn','gates_failed','failed'].includes(m.state));
    d.append(h('summary',{text:'Table · '+arts.length+' pinned'+(live.length?' · '+live.length+' vote'+(live.length===1?'':'s')+' open':'')}));
    for(const a of tb.artifacts||[]){
      const row=el('div',undefined,'hx-tb-row'+(a.state==='pinned'?'':' hx-dim'));
      const ver=['pr','commit','file'].includes(a.kind)?String(a.version).slice(0,7):String(a.version).slice(0,12);
      const url=(a.ref&&(a.ref.url||''))||'';
      row.append(el('span','A'+a.n,'hx-tb-n'),el('span',a.kind,'hx-card-kind'));
      row.append(url?h('a',{href:url,target:'_blank',rel:'noopener',text:a.title}):el('span',a.title));
      row.append(h('code',{text:'@'+ver,title:String(a.version)}));
      if(a.state!=='pinned')row.append(el('span',a.state,'hx-dim'));
      d.append(row);
    }
    for(const m of mots){
      const row=el('div',undefined,'hx-tb-row hx-tb-vote');
      row.append(el('span','vote','hx-card-kind'),el('span',m.kind+' @'+String(m.version).slice(0,7)),el('b',m.state.replace('_',' ')));
      if(m.name)row.append(copyChip(m.name,m.name,'hx-card-name'));
      const bs=el('div',undefined,'hx-tb-ballots');
      for(const s of m.seats||[]){
        const b=(m.ballots||[]).filter(x=>x.voter===s.role||x.seat===s.seat).at(-1);
        const word=!b?'waiting':b.sealed?'sealed':(VOTE_WORD[b.vote]||b.vote);
        bs.append(chip((s.role?roleName(s.role):s.seat)+': '+word,'hx-role'+(b&&!b.sealed&&b.vote!=='yes'?' hx-tb-no':'')));
      }
      row.append(bs);d.append(row);
    }
    return d;
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
    if(String(m.text||'').trim())by.append(speakButton(cur.room,{seq:m.seq}));
    row.append(by,el('div',m.text,'hx-text'));
    return row;
  }
  function card(c){
    const b=el('div',undefined,'hx-card hx-c-'+c.state);b.dataset.card=c.id;
    const top=el('div',undefined,'hx-card-top');
    top.append(el('span',KIND[c.kind]||c.kind,'hx-card-kind'),el('span',nameOf(c.by),'hx-dim'),el('span',CARD_STATE[c.state]||c.state,'hx-card-state'));
    if(c.name)top.append(copyChip(c.name,c.name,'hx-card-name'));
    if(String(c.summary||'').trim())top.append(speakButton(cur.room,{card:c.id}));
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
    deactivate(){active=false;clearTimeout(timer);stopSpeaking();}};
}
