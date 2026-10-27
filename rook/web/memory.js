// Memory: what the home agent remembers (people, facts, conversations,
// skills), served by the "memory" hub plugin through the settings account API
// (view=memory, actions memory_*). Everything here is the operator's to read,
// correct and forget. A change never edits a memory in place: new text
// replaces it and the old one stays in its history, as the agent does itself.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, ago, useCss, btn, table, two, info, more: moreBtn, seg} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

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
  const status=h('p',{class:'mg-status',role:'status'}),summary=h('span',{class:'mem-help'});
  const note=h('div'),bar=h('div',{class:'mg-bar'}),kinds=h('div',{class:'mem-kinds'});
  const list=h('div'),more=h('div',{class:'mg-actions'}),foot=h('p',{class:'mg-sub mem-foot',style:'white-space:normal'});
  const panel=h('div',{class:'mg-box strong mg-side'});
  const search=h('input',{type:'search',placeholder:'Search',autocomplete:'off','aria-label':'Search memories',class:'mg-grow'});
  const state=h('select',{'aria-label':'Show'},STATES.map(([v,l])=>h('option',{value:v,text:l})));
  let timer=null;
  search.oninput=()=>{clearTimeout(timer);timer=setTimeout(()=>{f.query=search.value.trim();role?roleLoad():load();},250);};
  state.onchange=()=>{f.state=state.value;load();};
  const add=btn('+ Add','primary',()=>{sel={new:true};drawList();drawPanel();if(innerWidth<900)panel.scrollIntoView({behavior:'smooth',block:'start'});},{'aria-label':'Add a memory',title:'Add a memory'});
  // Whose memory: the home agent's (the memory plugin), or a huddle role's
  // (kept in the role's home on its agent host; role.memory / role.memory_edit
  // through the huddle API). Shown only when there are roles.
  let role='',roles=[],hapi=null;
  const whose=h('select',{'aria-label':'Whose memory',hidden:true},h('option',{value:'',text:'The home agent'}));
  whose.onchange=()=>{history.replaceState(null,'',whose.value?'#memory?role='+encodeURIComponent(whose.value):'#memory');
    refresh().catch(e=>{status.textContent=e.message;status.classList.add('bad');});};
  async function loadRoles(){
    try{if(!hapi)hapi=(await import('/account/huddle/assets/huddles.js'+new URL(import.meta.url).search)).hapi;
      roles=(await hapi('role_list')).roles||[];}catch{roles=[];}
    whose.replaceChildren(h('option',{value:'',text:'The home agent'}),...roles.map(r=>h('option',{value:r.id,text:(r.name||r.id)+' (@'+r.id+')'})));
    if(role&&!roles.some(r=>r.id===role))whose.append(h('option',{value:role,text:'@'+role}));
    whose.value=role;whose.hidden=!roles.length&&!role;
  }
  const tidyNow=()=>run(null,async()=>{
    const r=await post({action:'memory_consolidate'});
    const s=r.stats;status.textContent='Tidied up: '+[s.episodes&&s.episodes+' conversation'+(s.episodes>1?'s':'')+' summarised',s.rolled_up&&s.rolled_up+' rolled up by week',s.merged&&s.merged+' duplicate'+(s.merged>1?'s':'')+' merged',s.faded&&s.faded+' faded',s.indexed&&s.indexed+' indexed'].filter(Boolean).join(', ')||'Tidied up: nothing to do.';
    await reload();});
  const tidy=moreBtn([{label:'Tidy up now',run:tidyNow}],'More');
  bar.append(whose,search,state,tidy,add);
  const main=h('div',{class:'mg-main'},kinds,bar,list,more,h('div',{class:'mg-actions mem-footrow'},summary,foot));
  root.replaceChildren(h('div',{class:'mg'},note,status,h('div',{class:'mg-split'},main,panel)));

  async function get(){const r=await fetch(API+'?view=memory');const j=await r.json().catch(()=>({error:'Bad response'}));if(j.csrf)csrf=j.csrf;if(!r.ok||j.error)throw Error(j.error||'Sign in with an owner account to see what the home agent remembers.');return j;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const j=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||j.error||j.ok===false)throw Error(j.error||'Request failed');return j;}
  async function run(b,work){if(busy)return;busy=true;if(b)b.disabled=true;status.classList.remove('bad');try{await work();}catch(e){status.textContent=e.message;status.classList.add('bad');}finally{busy=false;if(b)b.disabled=false;}}
  async function refresh(){
    const want=new URLSearchParams(location.hash.split('?')[1]||'').get('role')||'';
    if(want!==role){role=want;sel=null;items=[];total=0;}
    state.hidden=tidy.hidden=!!role;
    await loadRoles();
    if(role)return roleLoad();
    d=await get();items=d.items||[];total=d.total||0;drawTop();drawList();drawPanel();}
  // After a change: counts and status again, the list with the current filters.
  async function reload(){if(role)return roleLoad();d=await get();drawTop();if(d.enabled)await load();}

  // -- a role's memory ---------------------------------------------------------
  const ROLE_KINDS=[['fact','Fact'],['procedure','Skill'],['episode','Conversation']];
  const roleKind=k=>(ROLE_KINDS.find(x=>x[0]===k)||[k,k])[1];
  const roleDoc=()=>roles.find(r=>r.id===role)||{id:role,name:role};
  async function roleLoad(){
    const r=roleDoc();
    summary.replaceChildren(info('What '+(r.name||role)+' (@'+role+') remembers. It is kept in the role\'s own folder on its agent host and read before each of its answers. Forget anything here, or add something it should know.','About this memory'));
    note.replaceChildren();kinds.replaceChildren();foot.textContent='';
    try{const res=await hapi('role_memory',{id:role,query:f.query,limit:200});items=res.memories||[];total=items.length;
      if(sel&&!sel.new)sel=items.find(m=>m.id===sel.id)||null;}
    catch(e){items=[];total=0;note.append(h('div',{class:'mg-banner'},h('span',{text:e.message})));}
    drawList();drawPanel();
  }
  function roleList(){
    const rows=items.map(m=>{
      const r=h('div',{class:'mg-row pick'+(sel&&sel.id===m.id?' sel':''),tabindex:'0',role:'button','aria-label':m.text},
        h('span',{class:'mg-two'},h('b',{text:m.text,style:'white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical'}),
          h('small',{class:'mem-meta',text:[roleKind(m.kind),(m.created||'').slice(0,10)].join(' · ')}),m.by?h('small',{text:'by '+who(m.by)}):null),
        h('span',{class:'mg-dimtext mem-c',text:roleKind(m.kind)}),
        h('span',{class:'mg-sub mem-c',text:(m.created||'').slice(0,10)}));
      const open=()=>{sel=m;drawList();drawPanel();};r.onclick=open;r.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};
      return r;});
    const empty=f.query?'Nothing matches "'+f.query+'".':'Nothing remembered yet.';
    list.replaceChildren(h('div',{class:'mg-box mem-tbl'},table('minmax(0,1fr) 120px 100px',['What it remembers','Kind','Noted'],rows.length?rows:[h('div',{class:'mg-empty',text:empty})],420)));
    more.replaceChildren(h('span',{class:'mg-sub',text:total?total+' shown':''}));
  }
  function rolePanel(){
    if(!sel){panel.replaceChildren(h('div',{class:'mg-pad'},h('div',{class:'mg-actions'},h('span',{class:'mg-sub',text:'Pick a memory.'}),info('No passwords, keys or code: the same rules as the home agent\'s memory.','About')),
      h('a',{href:'#roles',text:'Roles'})));return;}
    const err=h('small',{class:'mg-error',role:'alert'}),close=btn('×','icon',()=>{sel=null;drawList();drawPanel();},{'aria-label':'Close'});
    const act=(b,body,done)=>run(b,async()=>{err.textContent='';try{await hapi('role_memory_edit',{id:role,...body});status.textContent=done;sel=null;await roleLoad();}catch(e){err.textContent=e.message;throw e;}});
    if(sel.new){
      const text=h('textarea',{rows:'4','aria-label':'Memory text'}),kind=h('select',null,ROLE_KINDS.map(([v,l])=>h('option',{value:v,text:l})));
      const save=btn('Remember','primary',()=>act(save,{op:'remember',text:text.value,kind:kind.value},'Remembered.'));
      panel.replaceChildren(h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Add a memory'}),h('small',{text:'For @'+role})),close),
        h('div',{class:'mg-pad'},h('label',{class:'mg-field'},'What it should remember',text),h('label',{class:'mg-field'},'Kind',kind),h('div',{class:'mg-actions end'},err,save)));
      return;}
    const forget=btn('Forget','danger',()=>act(forget,{op:'forget',memory_id:sel.id},'Forgotten.'));
    panel.replaceChildren(h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:roleKind(sel.kind)}),h('small',{text:sel.id})),close),
      h('div',{class:'mg-pad'},h('p',{style:'margin:0;white-space:pre-wrap;overflow-wrap:anywhere',text:sel.text}),
        kv('Noted',(sel.created||'').replace('T',' ').replace('Z',' UTC')),sel.by?kv('By',sel.by):null,h('div',{class:'mg-actions end'},err,forget)));
  }
  async function load(offset=0){
    if(role)return roleLoad();
    const r=await post({action:'memory_list',kind:f.kind,state:f.state,query:f.query,limit:50,offset});
    items=offset?items.concat(r.items):r.items;total=r.total;drawList();
  }

  function drawTop(){
    const s=d.status||{active:{}},counts=s.active||{},n=Object.values(counts).reduce((a,b)=>a+b,0);
    summary.replaceChildren(info('It reads the relevant parts before it answers and adds to them afterwards. Correct anything here, or make it forget. Passwords, codes and keys are never kept: those belong in Secrets.','About memory'));
    note.replaceChildren();
    if(!d.enabled){note.append(h('div',{class:'mg-banner'},h('span',{text:'Memory is off: turn on the memory plugin, then restart.'}),h('a',{href:'#plugins',text:'Plugins'})));}
    else if(!d.home.enabled){note.append(h('div',{class:'mg-banner'},h('span',{text:'Home agent off: nothing new is remembered.'}),h('a',{href:'#home',text:'Set up'})));}
    else if(d.settings.auto_remember===false){note.append(h('div',{class:'mg-banner'},h('span',{text:'Auto-remember is off.',title:'Only memories added here, by agents or by its own tools are kept.'})));}
    kinds.replaceChildren(seg([['','All · '+n],...d.kinds.map(k=>[k.id,k.label+' · '+(counts[k.id]||0)])],f.kind,id=>{f.kind=id;drawTop();load();},'Kind'));
    const e=s.embeddings||{},last=s.last_consolidation;
    foot.textContent=(e.model?'Search: meaning ('+e.model+')':e.sources&&e.sources.length?(e.last_error?'Search: keyword ('+e.sources[0]+' not answering)':'Search: meaning via '+e.sources[0]):'Search: keyword')
      +' · tidied '+(last?ago(last.ts):'never')+(s.waiting_turns?' · '+s.waiting_turns+' to summarise':'');
    foot.title=e.model||e.sources&&e.sources.length?'':'Set an embedding model in Settings > Memory for meaning-based search.';
  }
  function drawList(){
    if(role)return roleList();
    const rows=items.map(m=>{
      const r=h('div',{class:'mg-row pick'+(sel&&sel.id===m.id?' sel':''),tabindex:'0',role:'button','aria-label':m.text},
        h('span',{class:'mg-two'},h('b',{text:m.text,style:'white-space:normal;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical'}),
          h('small',{class:'mem-meta',text:[KIND_ONE[m.kind]||m.kind,'about '+who(m.about),ago(m.updated)].join(' · ')}),
          h('small',{text:[SOURCES[m.source]||m.source,m.state&&m.state!=='active'?STATES.find(x=>x[0]===m.state)?.[1].toLowerCase():null,m.pinned?'kept':null,m.kind==='procedure'&&!m.approved&&!m.pinned?'needs your approval':null].filter(Boolean).join(' · ')})),
        h('span',{class:'mg-dimtext mem-c',text:KIND_ONE[m.kind]||m.kind}),
        h('span',{class:'mg-dimtext mem-c',text:who(m.about)}),
        h('span',{class:'mg-sub mem-c',text:ago(m.updated)}));
      const open=()=>show(m.id);r.onclick=open;r.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};
      return r;});
    const empty=f.query?'Nothing matches "'+f.query+'".':f.state!=='active'?'Nothing here.':'Nothing remembered yet.';
    list.replaceChildren(h('div',{class:'mg-box mem-tbl'},table('minmax(0,1fr) 130px 110px 90px',['What it remembers','Kind','About','Updated'],rows.length?rows:[h('div',{class:'mg-empty',text:empty})],520)));
    more.replaceChildren(items.length<total?btn('Show more ('+(total-items.length)+')','',()=>load(items.length)):h('span',{class:'mg-sub',text:total?total+' shown':''}));
  }
  async function show(id){
    try{const r=await post({action:'memory_show',id});sel=r.memory;drawList();drawPanel();
      if(innerWidth<900)panel.scrollIntoView({behavior:'smooth',block:'start'});}
    catch(e){status.textContent=e.message;status.classList.add('bad');}
  }
  function drawPanel(){
    panel.classList.toggle('mem-panel-idle',!sel);
    if(role)return rolePanel();
    if(!sel){panel.replaceChildren(h('div',{class:'mg-pad'},h('div',{class:'mg-actions'},h('span',{class:'mg-sub',text:'Pick a memory.'}),
      info('About people: names and preferences people tell it. Facts: things about your home and setup. Conversations: short summaries, rolled up by week. Skills: ways of doing things that worked. Memories it does not use fade over time; keep one to stop that.','How it remembers'))));return;}
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
    const erase=btn('Forget and erase','danger',()=>{if(!confirm('Erase this memory and every earlier version of it for good? Its history will only say that something was forgotten.'))return;act(erase,{action:'memory_forget',id:sel.id,purge:true},()=>'Forgotten and erased.');});
    // Skills steer what the agent does with tools, so only approved ones reach a prompt.
    const skill=!isNew&&sel.kind==='procedure'&&live;
    const approve=skill?btn(sel.approved?'Withdraw approval':'Approve','soft',()=>act(approve,{action:'memory_approve',id:sel.id,approved:!sel.approved},
      ()=>sel.approved?'Approval withdrawn: the agent no longer uses this skill.':'Approved: the agent may use this skill.')):null;
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
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:isNew?'Add a memory':KIND_ONE[sel.kind]||sel.kind}),h('small',{text:isNew?'One plain statement':'About '+who(sel.scope)})),close),
      h('div',{class:'mg-pad'},
        h('label',{class:'mg-field'},'What it remembers',text,live?null:h('small',{class:'mg-sub',text:'Only memories in use can be changed.'})),
        h('div',{class:'mg-actions',style:'flex-wrap:wrap;gap:12px'},h('label',{class:'mg-field',style:'flex:1 1 150px'},'Kind',kind),h('label',{class:'mg-field',style:'flex:1 1 180px'},'About (a person, or "shared")',about)),
        h('label',{class:'mg-actions',title:'Never let it fade'},pinned,'Keep'),
        skill?h('p',{class:sel.approved||sel.pinned?'mg-sub':'mg-sub mg-warn',style:'white-space:normal',text:sel.approved||sel.pinned?'Approved: the agent reads this skill when it is relevant.':'Learned, not yet approved: the agent will not use this skill until you approve it.'}):null,
        live?h('div',{class:'mg-actions end'},err,isNew?null:erase,isNew?null:forget,approve,save):h('div',{class:'mg-actions end'},err,sel.state!=='forgotten'||sel.text!=='[forgotten]'?erase:null)),
      facts);
  }
  function kv(k,v){return h('div',{class:'mg-log',style:'grid-template-columns:96px minmax(0,1fr)'},h('span',{class:'mg-sub',text:k}),h('span',{text:v,style:'white-space:normal;overflow-wrap:anywhere'}));}
  return {
    activate(){refresh().catch(e=>{list.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));panel.replaceChildren();});},
    deactivate(){clearTimeout(timer);}};
}
