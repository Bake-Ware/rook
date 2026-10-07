// Home agent: the hub's own LLM. Configured here, stored through the settings
// service (keys home.*), answered by the "home" hub plugin. The API key is only
// ever a vault reference ({{secret:name}}); this page never sees a key.
// The chat panel talks to the agent through the dashboard's chat API
// (/api/chat/*), in the same two-person room the Chat view uses, so the
// conversation shows in both places.
import {h,ago,useCss,btn,table,two} from '/account/bands/assets/manage.js';

const API='/account/settings/api';
const VIA={chat:'Chat',ask:'home.ask',test:'Test'};

export async function mountHome(root){
  useCss();
  let csrf='',d=null,models=[],busy=false;
  const status=h('p',{class:'mg-status',role:'status'}),summary=h('p',{class:'mg-sub'});
  const top=h('div'),acts=h('div',{style:'display:flex;flex-direction:column;gap:6px'});
  const panel=h('div',{class:'mg-box strong mg-side'});
  const chat=homeChat({
    toForm(){panel.scrollIntoView({behavior:'smooth',block:'start'});panel.querySelector('input,select')?.focus({preventScroll:true});},
    async status(){const j=await get();d.status=j.status;d.activity=j.activity;drawList();return j;}});
  const list=h('div',{class:'mg-main'},top,chat.el,acts);
  root.replaceChildren(h('div',{class:'mg'},summary,status,h('div',{class:'mg-split'},list,panel)));

  async function get(){const r=await fetch(API+'?view=home');const j=await r.json().catch(()=>({error:'Bad response'}));if(j.csrf)csrf=j.csrf;if(!r.ok||j.error)throw Error(j.error||'Sign in with your operator account to set up the home agent.');return j;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const j=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||j.error)throw Error(j.error||'Request failed');return j;}
  async function refresh(){d=await get();draw();}

  const val=k=>d.settings[k]?.value;
  function state(s){
    if(!s.enabled)return ['Off','mg-dimtext'];
    if(s.missing.length)return ['Needs '+s.missing.join(' and ').replace('base_url','a base URL'),'mg-warn'];
    if(s.last_error)return ['On · last error','mg-bad'];
    return ['On','mg-good'];
  }
  function draw(){drawList();drawPanel();chat.update(d.status,Number(val('timeout_s'))||90);}
  // The table and the activity list only: the chat panel and the form keep
  // what the operator is typing.
  function drawList(){
    const s=d.status,[label,cls]=state(s);
    summary.textContent='The home agent is an LLM that lives at the hub. People reach it in Chat as @'+s.name+'; agents ask it with home.ask on worker rook. It acts as '+s.identity+' and everything it does is journaled under that name.';
    const row=h('div',{class:'mg-row sel'},two(s.name,s.identity,true),two(s.model||'no model',s.endpoint_host||'no endpoint',true),h('span',{text:label,class:cls}),h('span',{class:'mg-dimtext',text:s.tools?'knowledge search':'no tools'}));
    const rows=d.activity.map(a=>h('div',{class:'mg-row',title:a.error||''},
      h('span',{class:'mg-dimtext',text:ago(a.ts)}),h('span',{text:VIA[a.via]||a.via}),
      two(a.sender||'',a.room||'',false),
      h('span',{class:a.ok?'mg-good':'mg-bad',text:a.ok?(a.latency_ms!=null?(a.latency_ms/1000).toFixed(1)+' s':'ok')+(a.tools&&a.tools.length?' · '+a.tools.length+' tool call'+(a.tools.length>1?'s':''):''):(a.error||'failed').slice(0,80)})));
    top.replaceChildren(table('minmax(0,1fr) minmax(0,1.2fr) 150px 120px',['Agent','Model · endpoint','State','Tools'],[row]));
    acts.replaceChildren(
      h('div',{class:'mg-label',style:'margin-top:6px',text:'Recent activity (since the MCP server started)'}),
      table('90px 80px minmax(0,1fr) minmax(0,1fr)',['When','Via','From · room','Result'],rows.length?rows:[h('div',{class:'mg-empty',text:'Nothing yet. Use Test, or say hello in the chat above.'})]));
  }
  function field(label,ctl,k,note){
    const r=d.settings[k]||{};
    if(r.locked){ctl.disabled=true;}
    const help=r.locked?'Set by '+(r.env||'the environment')+' on the MCP server; change it there.':(note||r.help||'');
    return h('label',{class:'mg-field'},label,ctl,help?h('small',{class:'mg-sub',style:'white-space:normal',text:help}):null);
  }
  function drawPanel(){
    const err=h('small',{class:'mg-error',role:'alert'}),out=h('pre',{hidden:true,style:'white-space:pre-wrap'});
    const enabled=h('input',{type:'checkbox',checked:!!val('enabled')});
    const name=h('input',{value:val('name')||'home',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const provider=h('select',null,d.providers.map(p=>h('option',{value:p,text:p==='openai'?'OpenAI-compatible':p,selected:p===val('provider')})));
    const base=h('input',{value:val('base_url')||'',placeholder:'http://llm.example:1234/v1',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const dl=h('datalist',{id:'home-models'},models.map(m=>h('option',{value:m})));
    const model=h('input',{value:val('model')||'',list:'home-models',placeholder:'model id',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const ref=String(val('api_key')||''),refName=(ref.match(/^\{\{secret:(.+)\}\}$/)||[])[1]||'';
    const key=h('select',null,h('option',{value:'',text:'No key'}),d.secrets.map(n=>h('option',{value:n,text:n,selected:n===refName})),refName&&!d.secrets.includes(refName)?h('option',{value:refName,text:refName+' (missing from the vault)',selected:true}):null);
    const persona=h('select',null,h('option',{value:'',text:'The persona assigned to family "home" (or the default)'}),d.personas.map(p=>h('option',{value:p.id,text:p.name+' ('+p.id+')',selected:p.id===val('persona')})));
    const prompt=h('textarea',{rows:'4',placeholder:'Extra instructions for the home agent',style:'font:13px/1.6 var(--mono)'});prompt.value=val('system_prompt')||'';
    const tools=h('input',{type:'checkbox',checked:!!val('tools')});
    const num=(k,step)=>h('input',{type:'number',step:step||'1',value:val(k)??'',style:'font-family:var(--mono)'});
    const timeout=num('timeout_s','1'),maxTok=num('max_tokens'),temp=num('temperature','0.05'),ctx=num('context_messages');
    const values=()=>({enabled:enabled.checked,name:name.value.trim().toLowerCase(),provider:provider.value,base_url:base.value.trim(),model:model.value.trim(),
      api_key:key.value?'{{secret:'+key.value+'}}':'',persona:persona.value,system_prompt:prompt.value,tools:tools.checked,
      timeout_s:Number(timeout.value||90),max_tokens:Number(maxTok.value||1024),temperature:temp.value===''?null:Number(temp.value),context_messages:Number(ctx.value||20)});
    const run=async(b,work)=>{if(busy)return;busy=true;b.disabled=true;err.textContent='';try{await work();}catch(e){err.textContent=e.message;}finally{busy=false;b.disabled=false;}};
    const fetchModels=btn('List models','',()=>run(fetchModels,async()=>{
      const r=await post({action:'home_models',values:values()});
      if(!r.ok)throw Error(r.error+(r.note?' '+r.note:''));models=r.models;dl.replaceChildren(...models.map(m=>h('option',{value:m})));
      status.textContent=models.length+' model'+(models.length===1?'':'s')+' at '+(base.value||'the endpoint')+'.'+(models.length&&!model.value?' Pick one in Model.':'')+(r.note?' '+r.note:'');
      if(!model.value&&models.length===1)model.value=models[0];}));
    const test=btn('Test','soft',()=>run(test,async()=>{
      out.hidden=false;out.textContent='Asking '+(model.value||'the model')+'…';
      const r=await post({action:'home_test',values:values()});
      out.textContent=(r.ok?r.model+' answered in '+(r.latency_ms/1000).toFixed(1)+' s:\n\n'+r.reply:'Test failed: '+r.error)+(r.note?'\n\n'+r.note:'');
      out.className=r.ok?'':'mg-bad';refresh().catch(()=>{});}));
    const save=btn('Save','primary',()=>run(save,async()=>{
      const r=await post({action:'home_save',values:values()});
      d=r;draw();status.textContent=r.saved.length?'Saved '+r.saved.map(k=>k.slice(5)).join(', ')+'. It takes effect now.':'No changes.';}));
    const hist=d.history.slice(0,10);
    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Home agent'}),h('small',{text:'Stored as home.* in Settings · hub-wide'}))),
      h('div',{class:'mg-pad'},
        h('label',{class:'mg-actions'},enabled,'Enabled: answer in chat and to home.ask'),
        field('Name',name,'name','Chat identity agent:<name>; people write @'+(val('name')||'home')+'.'),
        field('Provider',provider,'provider'),
        field('Base URL',base,'base_url','The endpoint\'s /v1 base. Any OpenAI-compatible server works.'),
        field('Model',h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},model,fetchModels),'model','List models reads /v1/models with the values on this form.'),dl,
        field('API key',key,'api_key','A secret from Secrets, used as {{secret:name}}. Add the key there first; it is never shown or stored here.'),
        field('Persona',persona,'persona'),
        field('System prompt addition',prompt,'system_prompt'),
        h('label',{class:'mg-actions'},tools,'Read-only tools: let it search the knowledge wiki (each search is journaled as '+d.status.identity+')'),
        h('details',null,h('summary',{text:'Limits'}),h('div',{class:'mg-pad',style:'padding-left:0;padding-right:0'},
          field('Request timeout (s)',timeout,'timeout_s'),field('Longest reply (tokens)',maxTok,'max_tokens'),
          field('Temperature',temp,'temperature','Blank: the endpoint\'s default.'),field('Room messages sent as context',ctx,'context_messages'))),
        h('div',{class:'mg-actions end'},err,test,save),out,
        d.status.last_error?h('p',{class:'mg-sub mg-bad',style:'white-space:normal',text:'Last error: '+d.status.last_error}):null,
        h('div',null,h('div',{class:'mg-label',style:'margin-bottom:8px',text:'Changes'}),
          hist.length?hist.map(e=>h('div',{class:'mg-log'},h('span',{class:'mg-mono',text:e.key.slice(5)}),h('span',{text:(e.actor||e.actor_label||'')}),h('span',{class:'mg-sub',style:'font-size:12px',text:ago(e.at||e.ts)}))):h('p',{class:'mg-sub',text:'Not configured yet.'}))));
  }
  return {
    activate(){chat.el.hidden=false;refresh().then(()=>chat.start()).catch(e=>{top.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));acts.replaceChildren();chat.el.hidden=true;panel.replaceChildren();});},
    deactivate(){chat.stop();}};
}

// -- chat with the home agent ------------------------------------------------
// The dashboard chats as user:operator (see /api/chat/* in the hub). The room is
// the newest two-person room between that identity and agent:<name>, which is
// what the Chat view opens when you click the agent in Presence; it is created
// on the first message, not by visiting the page. The plugin posts whole
// replies (no streaming), so the panel polls the room like the Chat view does.
const OP='user:operator';
const UNAVAILABLE='The home agent is unavailable right now.',BUSY='(Busy: I will answer shortly.)';
async function cj(url,body){
  const r=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{headers:{Accept:'application/json'}});
  const j=await r.json().catch(()=>null);
  if(r.status===503)throw Error('Chat is not available on this hub (no chat store).');
  if(r.status===401||r.status===403||!j)throw Error('Chat needs the dashboard admin login; sign in again.');
  if(!r.ok||j.ok===false||j.error)throw Error(j.error||'Chat request failed');
  return j;
}
export function homeChat({toForm,status}){
  let ident='',name='home',on=false,timeout=90,room=null,seq=0,timer=null,live=false,waiting=null,ticks=0,loaded=false,sending=false;
  const note=h('div',{class:'hc-note',role:'status',hidden:true});
  const log=h('div',{class:'hc-log','aria-live':'polite','aria-label':'Conversation'});
  const typing=h('div',{class:'hc-typing',hidden:true});
  const box=h('textarea',{rows:'1',placeholder:'Message the home agent',autocomplete:'off','aria-label':'Message'});
  const send=btn('Send','primary',()=>submit());
  const title=h('h2'),sub=h('small');
  const el=h('section',{class:'mg-box strong hc','aria-label':'Chat with the home agent'},
    h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},title,sub)),
    note,log,typing,h('div',{class:'hc-input'},box,send));
  box.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();submit();}});
  box.addEventListener('input',()=>{box.style.height='auto';box.style.height=Math.min(box.scrollHeight,160)+'px';});

  function say(text,cls,action){
    note.hidden=!text;note.className='hc-note'+(cls?' '+cls:'');
    note.replaceChildren(...(text?[h('span',{text}),action||null].filter(Boolean):[]));
  }
  const formBtn=()=>btn('Go to the form','soft',toForm);
  function blocked(s){
    if(!s.enabled)return 'The home agent is off. Turn on Enabled in the form and Save to chat with it.';
    if(s.missing.length)return 'The home agent needs '+s.missing.join(' and ').replace('base_url','a base URL')+' before it can answer. Fill in the form and Save.';
    if(!s.chat)return 'The home agent cannot reach the hub\'s chat store, so it cannot answer here.';
    return '';
  }
  // Called whenever the page has fresh settings (load, Save).
  function update(s,t){
    timeout=t;title.textContent='Chat with @'+s.name;
    sub.textContent='The same conversation is in Chat, in the room "'+s.name+'".';
    if(s.identity!==ident){ident=s.identity;name=s.name;room=null;seq=0;loaded=false;stopWaiting();log.replaceChildren();}
    const why=blocked(s);on=!why;
    box.disabled=send.disabled=!on;
    if(why){say(why,'warn',s.enabled&&s.missing.length||!s.enabled?formBtn():null);stopWaiting();}
    else if(note.classList.contains('warn'))say('');
  }
  function empty(){log.replaceChildren(h('div',{class:'mg-empty',text:'No conversation yet. Say hello to @'+name+'.'}));}
  function render(msgs){
    if(seq===0)log.replaceChildren();
    const atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<60;
    for(const m of msgs){
      if(m.seq<=seq)continue;seq=m.seq;
      const me=m.sender===OP,quiet=m.sender===ident&&(m.text===BUSY||m.text===UNAVAILABLE);
      log.append(h('div',{class:'hc-msg'+(me?' me':'')+(quiet?' quiet':'')},
        h('div',{class:'hc-who'},h('b',{text:me?'You':m.sender===ident?name:m.sender.replace(/^[a-z]+:/,'')}),
          h('span',{text:new Date(m.ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})})),
        h('div',{class:'hc-text',text:m.text})));
      if(waiting&&m.sender===ident&&m.seq>waiting.after){
        if(m.text===UNAVAILABLE)failed('');
        else if(m.text!==BUSY)stopWaiting();
      }
    }
    if(!log.children.length)empty();
    if(msgs.length&&(atBottom||waiting))log.scrollTop=log.scrollHeight;
  }
  async function findRoom(){
    const rooms=(await cj('/api/chat/rooms')).rooms||[];
    const r=rooms.find(r=>r.participants&&r.participants.length===2&&r.participants.includes(OP)&&r.participants.includes(ident));
    return r?r.room:null;
  }
  const read=()=>cj('/api/chat/read?room='+encodeURIComponent(room)+'&since='+seq);
  async function load(){
    room=await findRoom();seq=0;loaded=true;
    if(!room){empty();return;}
    const got=await read(),msgs=got.messages||[];
    render(msgs);log.scrollTop=log.scrollHeight;
    // Opened while a question is still out: keep showing that it is working.
    const last=msgs.at(-1);
    if(on&&!waiting&&last&&last.sender===OP&&Date.now()/1000-last.ts<limit())wait(last.seq,last.ts*1000);
  }
  async function poll(){
    if(!live||!ident)return schedule();
    try{
      if(!loaded)await load();
      else if(room)render((await read()).messages||[]);
      else if(++ticks%3===0&&await findRoom())loaded=false;   // started from the Chat view
      if(waiting)await watch();
      if(note.classList.contains('lost'))say('');
    }catch(e){if(on)say(e.message,'bad lost');}
    schedule();
  }
  function schedule(){clearTimeout(timer);if(live)timer=setTimeout(poll,waiting?1500:4000);}
  const limit=()=>timeout*2+30;
  function wait(after,since){waiting={after,since,checks:0};typing.hidden=false;tick();}
  function tick(){
    if(!waiting)return;
    const s=Math.round((Date.now()-waiting.since)/1000);
    typing.textContent=name+' is thinking'+(s>=3?' · '+s+' s':'')+'…';
  }
  function stopWaiting(){waiting=null;typing.hidden=true;}
  // While it works: every few polls, look at the agent's activity for a failed
  // reply to the operator (the room only gets a generic line, at most once per
  // ten minutes), and stop waiting after the timeout.
  async function watch(){
    tick();
    const age=(Date.now()-waiting.since)/1000;
    if(age>limit()){stopWaiting();say('No reply after '+Math.round(age)+' s. Check Recent activity below; the home agent runs in the hub\'s MCP server and only answers while that is up.','warn');return;}
    if(++waiting.checks%3)return;
    const j=await status();
    const bad=(j.activity||[]).find(a=>a.via==='chat'&&!a.ok&&a.sender===OP&&a.ts*1000>=waiting.since-2000);
    if(bad)failed(j.status.last_error);
  }
  function failed(err){
    stopWaiting();
    say('The home agent could not answer'+(err?': '+err:'')+'. Check the endpoint, model and key in the form, then use Test.','bad',formBtn());
  }
  async function submit(){
    const text=box.value.trim();if(!text||!on||sending)return;
    sending=true;send.disabled=true;say('');
    try{
      if(!room){room=(await cj('/api/chat/start',{title:name,invite:[ident]})).room;seq=0;loaded=true;}
      const t0=Date.now();
      await cj('/api/chat/send',{room,text,mention:[ident],expects_reply:true});
      box.value='';box.style.height='';
      // Wait from our own message, then render: a reply already in this batch ends the wait.
      const msgs=(await read()).messages||[],mine=msgs.filter(m=>m.sender===OP).at(-1);
      wait(mine?mine.seq:seq,t0);render(msgs);log.scrollTop=log.scrollHeight;schedule();
    }catch(e){say('Not sent: '+e.message,'bad');}
    finally{sending=false;send.disabled=!on;box.focus();}
  }
  return {el,update,
    start(){if(live)return;live=true;loaded=false;poll();},
    stop(){live=false;clearTimeout(timer);}};
}
