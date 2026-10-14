// Memory: what the home agent remembers (people, facts, conversations,
// skills), served by the "memory" hub plugin through the settings account API
// (view=memory, actions memory_*). Everything here is the operator's to read,
// correct and forget. A change never edits a memory in place: new text
// replaces it and the old one stays in its history, as the agent does itself.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, ago, useCss, btn, table, two, chips} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API='/account/settings/api';
const STATES=[['active','In use'],['superseded','Replaced'],['archived','Archived'],['forgotten','Forgotten'],['all','All']];
const SOURCES={chat:'from chat',tool:'saved by the agent',page:'added here',agent:'added by an agent',job:'learned from a job',consolidation:'summarised',ask:'from home.ask'};
const KIND_ONE={profile:'About a person',fact:'Fact',episode:'Conversation',procedure:'Skill'};
const who=s=>s==='shared'?'everyone':String(s||'').replace(/^[a-z]+:/,'');

// Phone width: one column; kind, person and age move under the text.
const CSS='#view-memory .mem-meta{display:none}@media(max-width:700px){#view-memory .mem-tbl .mg-table>div{min-width:0!important}'
  +'#view-memory .mem-tbl .mg-head,#view-memory .mem-tbl .mg-row{grid-template-columns:minmax(0,1fr)!important}'
  +'#view-memory .mem-tbl .mg-head div:not(:first-child),#view-memory .mem-tbl .mem-c{display:none}#view-memory .mem-meta{display:block}}';

export async function mountMemory(root){
  useCss();
  if(!document.getElementById('memory-css'))document.head.append(h('style',{id:'memory-css',text:CSS}));
  let csrf='',d=null,items=[],total=0,sel=null,busy=false;
  const f={kind:'',state:'active',query:''};
  const status=h('p',{class:'mg-status',role:'status'}),summary=h('p',{class:'mg-sub'});
  const note=h('div'),bar=h('div',{class:'mg-bar'}),kinds=h('div',{class:'mg-actions'});
  const list=h('div'),more=h('div',{class:'mg-actions'}),foot=h('p',{class:'mg-sub',style:'white-space:normal'});
  const panel=h('div',{class:'mg-box strong mg-side'});
  const search=h('input',{type:'search',placeholder:'Search what it remembers',autocomplete:'off','aria-label':'Search memories',class:'mg-grow'});
  const state=h('select',{'aria-label':'Show'},STATES.map(([v,l])=>h('option',{value:v,text:l})));
  let timer=null;
  search.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{f.query=search.value.trim();load();},250);};
  state.onchange=()=>{f.state=state.value;load();};
  const add=btn('Add a memory','primary',()=>{sel={new:true};drawList();drawPanel();});
  const tidy=btn('Tidy up now','',()=>run(tidy,async()=>{
    const r=await post({action:'memory_consolidate'});
    const s=r.stats;status.textContent='Tidied up: '+[s.episodes&&s.episodes+' conversation'+(s.episodes>1?'s':'')+' summarised',s.rolled_up&&s.rolled_up+' rolled up by week',s.merged&&s.merged+' duplicate'+(s.merged>1?'s':'')+' merged',s.faded&&s.faded+' faded',s.indexed&&s.indexed+' indexed'].filter(Boolean).join(', ')||'Tidied up: nothing to do.';
    await reload();}));
  bar.append(search,state,tidy,add);
  const main=h('div',{class:'mg-main'},kinds,bar,list,more,foot);
  root.replaceChildren(h('div',{class:'mg'},summary,note,status,h('div',{class:'mg-split'},main,panel)));

  async function get(){const r=await fetch(API+'?view=memory');const j=await r.json().catch(()=>({error:'Bad response'}));if(j.csrf)csrf=j.csrf;if(!r.ok||j.error)throw Error(j.error||'Sign in with your operator account to see what the home agent remembers.');return j;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const j=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||j.error||j.ok===false)throw Error(j.error||'Request failed');return j;}
  async function run(b,work){if(busy)return;busy=true;b.disabled=true;status.classList.remove('bad');try{await work();}catch(e){status.textContent=e.message;status.classList.add('bad');}finally{busy=false;b.disabled=false;}}
  async function refresh(){d=await get();items=d.items||[];total=d.total||0;drawTop();drawList();drawPanel();}
  // After a change: counts and status again, the list with the current filters.
  async function reload(){d=await get();drawTop();if(d.enabled)await load();}
  async function load(offset=0){
    const r=await post({action:'memory_list',kind:f.kind,state:f.state,query:f.query,limit:50,offset});
    items=offset?items.concat(r.items):r.items;total=r.total;drawList();
  }

  function drawTop(){
    const s=d.status||{active:{}},counts=s.active||{},n=Object.values(counts).reduce((a,b)=>a+b,0);
    summary.textContent='What the home agent remembers. It reads the relevant parts before it answers and adds to them afterwards. Correct anything here, or make it forget. Passwords, codes and keys are never kept: those belong in Secrets.';
    note.replaceChildren();
    if(!d.enabled){note.append(h('div',{class:'mg-banner'},h('span',{text:'Memory is off on this hub. Turn on the memory plugin under Plugins (Home & voice agent) and restart.'})));}
    else if(!d.home.enabled){note.append(h('div',{class:'mg-banner'},h('span',{text:'The home agent is off, so nothing new is being remembered. You can still add memories here.'}),h('a',{href:'#home',text:'Set up the home agent'})));}
    else if(d.settings.auto_remember===false){note.append(h('div',{class:'mg-banner'},h('span',{text:'"Remember what people tell it" is off: only memories added here, by agents or by its own tools are kept.'})));}
    kinds.replaceChildren(...chips([['','All · '+n],...d.kinds.map(k=>[k.id,k.label+' · '+(counts[k.id]||0)])],f.kind,id=>{f.kind=id;drawTop();load();}));
    const e=s.embeddings||{},last=s.last_consolidation;
    foot.textContent=(e.model?'Meaning-based search is on ('+e.model+').':e.sources&&e.sources.length?(e.last_error?'Meaning-based search: '+e.sources[0]+' is not answering; keyword search meanwhile.':'Meaning-based search via '+e.sources[0]+' (it starts with the next question).'):'Keyword search only (set an embedding model in Settings > Memory for meaning-based search).')
      +' Last tidy-up: '+(last?ago(last.ts):'not yet')+'.'+(s.waiting_turns?' '+s.waiting_turns+' message'+(s.waiting_turns>1?'s':'')+' waiting to be summarised.':'');
  }
  function drawList(){
    const rows=items.map(m=>{
      const r=h('div',{class:'mg-row pick'+(sel&&sel.id===m.id?' sel':''),tabindex:'0',role:'button','aria-label':m.text},
        h('span',{class:'mg-two'},h('b',{text:m.text,style:'white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical'}),
          h('small',{class:'mem-meta',text:[KIND_ONE[m.kind]||m.kind,'about '+who(m.about),ago(m.updated)].join(' · ')}),
          h('small',{text:[SOURCES[m.source]||m.source,m.state&&m.state!=='active'?STATES.find(x=>x[0]===m.state)?.[1].toLowerCase():null,m.pinned?'kept':null].filter(Boolean).join(' · ')})),
        h('span',{class:'mg-dimtext mem-c',text:KIND_ONE[m.kind]||m.kind}),
        h('span',{class:'mg-dimtext mem-c',text:who(m.about)}),
        h('span',{class:'mg-sub mem-c',text:ago(m.updated)}));
      const open=()=>show(m.id);r.onclick=open;r.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};
      return r;});
    const empty=f.query?'Nothing matches "'+f.query+'".':f.state!=='active'?'Nothing here.':'Nothing remembered yet. Chat with the home agent, or add a memory.';
    list.replaceChildren(h('div',{class:'mg-box mem-tbl'},table('minmax(0,1fr) 130px 110px 90px',['What it remembers','Kind','About','Updated'],rows.length?rows:[h('div',{class:'mg-empty',text:empty})],520)));
    more.replaceChildren(items.length<total?btn('Show more ('+(total-items.length)+')','',()=>load(items.length)):h('span',{class:'mg-sub',text:total?total+' shown':''}));
  }
  async function show(id){
    try{const r=await post({action:'memory_show',id});sel=r.memory;drawList();drawPanel();
      if(innerWidth<900)panel.scrollIntoView({behavior:'smooth',block:'start'});}
    catch(e){status.textContent=e.message;status.classList.add('bad');}
  }
  function drawPanel(){
    if(!sel){panel.replaceChildren(h('div',{class:'mg-pad'},h('p',{class:'mg-sub',text:'Pick a memory to see where it came from, correct it or forget it.'}),
      h('div',null,h('div',{class:'mg-label',style:'margin-bottom:8px',text:'How it remembers'}),
        h('p',{class:'mg-sub',style:'white-space:normal;line-height:1.6',text:'About people: names and preferences people tell it. Facts: things about your home and setup. Conversations: short summaries, rolled up by week. Skills: ways of doing things that worked. Memories it does not use fade over time; keep one to stop that.'}))));return;}
    const isNew=!!sel.new,live=isNew||sel.state==='active';
    const err=h('small',{class:'mg-error',role:'alert'});
    const text=h('textarea',{rows:'4',disabled:!live,'aria-label':'Memory text'});text.value=isNew?'':sel.text;
    const kind=h('select',{disabled:!live},d.kinds.map(k=>h('option',{value:k.id,text:KIND_ONE[k.id],selected:isNew?k.id==='fact':k.id===sel.kind})));
    const about=h('input',{value:isNew?'':sel.scope,placeholder:'shared, or a person such as '+d.settings.default_person,disabled:!live,autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const pinned=h('input',{type:'checkbox',checked:!isNew&&!!sel.pinned,disabled:!live});
    const act=async(b,body,done)=>run(b,async()=>{err.textContent='';try{const r=await post(body);sel=r.memory||sel;status.textContent=done(r);await reload();drawPanel();}catch(e){err.textContent=e.message;throw e;}});
    const save=btn(isNew?'Remember':'Save','primary',()=>act(save,isNew?{action:'memory_add',text:text.value,kind:kind.value,about:about.value.trim(),pinned:pinned.checked}
      :{action:'memory_edit',id:sel.id,text:text.value,kind:kind.value,about:about.value.trim(),pinned:pinned.checked},
      r=>r.verdict==='duplicate'?'It already remembered that; kept the existing memory.':isNew?'Remembered.':r.verdict==='updated'?'Saved.':'Saved as a new version; the old one is kept in its history.'));
    const forget=btn('Forget','danger',()=>act(forget,{action:'memory_forget',id:sel.id},()=>'Forgotten. It will not be used again.'));
    const erase=btn('Forget and erase','danger',()=>{if(!confirm('Erase the text of this memory for good? Its history will only say that something was forgotten.'))return;act(erase,{action:'memory_forget',id:sel.id,purge:true},()=>'Forgotten and erased.');});
    const close=btn('×','icon',()=>{sel=null;drawList();drawPanel();},{'aria-label':'Close'});
    const facts=isNew?null:h('div',{class:'mg-pad',style:'padding-top:0'},
      h('div',{class:'mg-label',text:'Where it came from'}),
      kv('Source',(SOURCES[sel.source]||sel.source)+(sel.author?' · '+sel.author:'')),
      sel.room?kv('Chat room',sel.room_title||sel.room):null,
      kv('First noted',new Date(sel.created*1000).toLocaleString()),
      kv('Strength',Math.round(sel.strength*100)+'% · used '+sel.uses+' time'+(sel.uses===1?'':'s')+(sel.reinforced?' · said again '+sel.reinforced+'×':'')),
      sel.state!=='active'?kv('State',(STATES.find(x=>x[0]===sel.state)||[0,sel.state])[1]+(sel.reason?' · '+sel.reason:'')):null,
      sel.superseded_by?h('div',null,btn('Open the newer version','',()=>show(sel.superseded_by))):null,
      (sel.supersedes||[]).length?h('div',{class:'mg-actions'},h('span',{class:'mg-sub',text:'Replaced:'}),sel.supersedes.slice(0,4).map((id,i)=>btn('earlier version'+(sel.supersedes.length>1?' '+(i+1):''),'',()=>show(id)))):null,
      h('div',null,h('div',{class:'mg-label',style:'margin:6px 0 8px',text:'History'}),
        (sel.history||[]).slice(0,12).map(e=>h('div',{class:'mg-log'},h('span',{class:'mg-mono',text:e.action}),h('span',{text:e.actor}),h('span',{class:'mg-sub',style:'font-size:12px',text:ago(e.ts)})))));
    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:isNew?'Add a memory':KIND_ONE[sel.kind]||sel.kind}),h('small',{text:isNew?'Write it as one plain statement':'About '+who(sel.scope)})),close),
      h('div',{class:'mg-pad'},
        h('label',{class:'mg-field'},'What it remembers',text,live?null:h('small',{class:'mg-sub',text:'Only memories in use can be changed.'})),
        h('div',{class:'mg-actions',style:'flex-wrap:wrap;gap:12px'},h('label',{class:'mg-field',style:'flex:1 1 150px'},'Kind',kind),h('label',{class:'mg-field',style:'flex:1 1 180px'},'About (a person, or "shared")',about)),
        h('label',{class:'mg-actions'},pinned,'Keep: never let it fade'),
        live?h('div',{class:'mg-actions end'},err,isNew?null:erase,isNew?null:forget,save):h('div',{class:'mg-actions end'},err,sel.state!=='forgotten'||sel.text!=='[forgotten]'?erase:null)),
      facts);
  }
  function kv(k,v){return h('div',{class:'mg-log',style:'grid-template-columns:96px minmax(0,1fr)'},h('span',{class:'mg-sub',text:k}),h('span',{text:v,style:'white-space:normal;overflow-wrap:anywhere'}));}
  return {
    activate(){refresh().catch(e=>{list.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));panel.replaceChildren();});},
    deactivate(){clearTimeout(timer);}};
}
