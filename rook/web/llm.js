// Manage > Model providers: the language models this hub can use, by name
// (the "llm" hub plugin, rook/hub/plugins/llm). Providers added here are any
// OpenAI-compatible server; plugins contribute their own (llmanifold). The home
// agent picks one on its page. API keys are vault references only; this page
// never sees a key.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, ago, useCss, btn, table, two, info} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API='/account/settings/api';

export async function mountModels(root){
  useCss();
  let csrf='',d=null,sel=null,busy=false,listed={};
  const status=h('p',{class:'mg-status',role:'status'});
  const list=h('div',{class:'mg-main'}),panel=h('section',{class:'mg-box strong mg-side'});
  root.replaceChildren(h('div',{class:'mg'},status,h('div',{class:'mg-split'},list,panel)));

  async function get(){const r=await fetch(API+'?view=llm');const j=await r.json().catch(()=>({error:'Bad response'}));if(j.csrf)csrf=j.csrf;if(!r.ok||j.error)throw Error(j.error||'Sign in with your operator account to manage model providers.');return j;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const j=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||j.error)throw Error(j.error||'Request failed');return j;}
  async function refresh(){d=await get();if(sel&&sel!=='new'&&!d.providers.some(p=>p.id===sel))sel=null;if(!sel&&d.providers.length)sel=d.providers[0].id;draw();}
  const run=async(b,work,err)=>{if(busy)return;busy=true;b.disabled=true;err.textContent='';try{await work();}catch(e){err.textContent=e.message;}finally{busy=false;b.disabled=false;}};
  const users=id=>d.users.filter(u=>u.provider===id).map(u=>u.who);
  function stateOf(p){
    const c=p.last_check;
    if(!c)return ['Not checked','mg-dimtext'];
    return c.ok?['Answering · '+c.latency_ms+' ms','mg-good']:['Not answering','mg-bad'];
  }
  function draw(){drawList();drawPanel();}
  function drawList(){
    const rows=d.providers.map(p=>{const [label,cls]=stateOf(p),u=users(p.id);
      return h('div',{class:'mg-row pick'+(p.id===sel?' sel':''),onclick:()=>{sel=p.id;draw();}},
        two(p.label||p.id,p.id+(u.length?' · '+u.join(', '):''),false),
        two(p.host,p.models.length?p.models.length+' model'+(p.models.length>1?'s':''):'all models',true),
        h('span',{class:'mg-dimtext',text:p.source==='settings'?'Added here':p.source.replace('plugin:','')}),
        h('span',{class:cls,text:label}));});
    const extra=d.plugins.map(x=>h('div',{class:'mg-row'},two('llmanifold','plugin not loaded',false),
      h('span',{class:'mg-sub',style:'grid-column:span 2;white-space:normal',text:x.reason}),
      h('a',{href:'#settings?view=plugin&target='+x.module,text:'Set it up'})));
    const all=[...rows,...extra];
    list.replaceChildren(
      table('minmax(0,1.1fr) minmax(0,1.2fr) 120px 150px',['Provider','Address · models','Source','State'],
        all.length?all:[h('div',{class:'mg-empty',text:'No providers yet.'})],'640px'),
      h('div',{class:'mg-actions'},btn('+ Add','primary',()=>{sel='new';drawList();drawPanel();if(innerWidth<900)panel.scrollIntoView({behavior:'smooth',block:'start'});},{'aria-label':'Add a provider',title:'Add a provider'}),
        h('span',{class:'mg-sub',text:d.locked?'Set by '+d.env+' in the hub\'s environment.':''}),
        info('Language models this hub can talk to: a model server on your network, a front door like llmanifold, or a paid API. Add one, check it answers, then pick it for the home agent. Keys stay in Secrets.','About model providers')));
  }
  function field(label,ctl,note){return h('label',{class:'mg-field'},note?h('span',{class:'mg-flabel'},label,info(note,'About '+label)):label,ctl);}
  function checkView(p){
    const c=p.last_check;if(!c)return null;
    const out=[h('p',{class:'mg-sub '+(c.ok?'mg-good':'mg-bad'),style:'white-space:normal',text:c.ok?'Answered in '+c.latency_ms+' ms with '+c.models+' model'+(c.models===1?'':'s')+' ('+ago(c.checked_at)+').':'Not answering: '+c.error})];
    const det=c.detail;
    if(det&&det.flows&&det.flows.length)out.push(h('p',{class:'mg-sub',style:'white-space:normal'},'Flows: ',h('span',{class:'mg-mono',text:det.flows.join(', ')})));
    if(det&&det.admin){
      const a=det.admin;
      if(!a.ok)out.push(h('p',{class:'mg-sub mg-bad',text:'Admin API: '+a.error}));
      else out.push(h('div',null,h('div',{class:'mg-label',style:'margin:6px 0',text:'Lanes'+(a.version?' · llmanifold '+a.version:'')+' · '+a.inflight+' busy · '+(a.queued||0)+' queued'}),
        ...a.endpoints.map(e=>h('div',{class:'mg-log',style:'grid-template-columns:minmax(0,1fr) auto auto'},h('span',{class:'mg-mono',text:e.name+(e.model?' ('+e.model+')':'')}),
          h('span',{class:e.healthy===false||e.state==='down'?'mg-bad':'mg-good',text:e.state||(e.healthy===false?'down':'healthy')}),
          h('span',{class:'mg-sub',text:(e.inflight||0)+(e.lanes!=null?'/'+e.lanes:'')+' lanes busy'})))));
    }
    return h('div',{style:'display:flex;flex-direction:column;gap:6px'},...out);
  }
  function drawPanel(){
    const p=sel==='new'?null:d.providers.find(x=>x.id===sel);
    if(sel!=='new'&&!p){panel.replaceChildren(h('p',{class:'mg-empty',text:'Pick a provider, or add one.'}));return;}
    const err=h('small',{class:'mg-error',role:'alert'});
    const check=p?btn('Check','soft',()=>run(check,async()=>{const r=await post({action:'llm_check',provider:p.id});p.last_check=r.check;drawList();drawPanel();status.textContent=r.check.ok?(p.label||p.id)+' answered.':(p.label||p.id)+' did not answer.';},err)):null;
    if(p&&p.source!=='settings'){
      const ns=p.source.replace('plugin:','');
      panel.replaceChildren(
        h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:p.label||p.id}),h('small',{text:ns+' plugin · '+p.id}))),
        h('div',{class:'mg-pad'},
          ns==='llmanifold'?info('llmanifold is an LLM front door: one address in front of your model servers and paid APIs, with queueing and fallbacks. Ask it for a flow name; it picks the engine.','About llmanifold'):null,
          two(p.host,p.key_set?'with a token from Secrets':'no token (open flows)',true),
          h('p',{class:'mg-sub',style:'white-space:normal',text:p.models.length?'Offers: '+p.models.join(', ')+(p.default_model?' · default '+p.default_model:''):'Offers every flow it lists'+(p.default_model?' · default '+p.default_model:'')+'.'}),
          checkView(p),
          h('div',{class:'mg-actions end'},err,h('a',{class:'mg-btn',href:'#settings?view=plugin&target='+ns,text:'Its settings'}),check)));
      return;
    }
    const v=p||{id:'',label:'',base_url:'',api_key:'',models:[],default_model:''};
    const id=h('input',{value:v.id,placeholder:'lab-gpu',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)',disabled:!!p});
    const label=h('input',{value:v.label&&v.label!==v.id?v.label:'',placeholder:'Lab GPU',autocomplete:'off'});
    const url=h('input',{value:v.base_url||'',placeholder:'http://llm.example.com:8080/v1',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const refName=(String(v.api_key||'').match(/^\{\{secret:(.+)\}\}$/)||[])[1]||'';
    const key=h('select',null,h('option',{value:'',text:'No key'}),d.secrets.map(n=>h('option',{value:n,text:n,selected:n===refName})),refName&&!d.secrets.includes(refName)?h('option',{value:refName,text:refName+' (missing from Secrets)',selected:true}):null);
    const models=h('textarea',{rows:'2',placeholder:'Blank: every model it lists',style:'font:13px/1.6 var(--mono)'});models.value=(v.models||[]).join(', ');
    const dl=h('datalist',{id:'llm-models'},(listed[v.id]||[]).map(m=>h('option',{value:m})));
    const def=h('input',{value:v.default_model||'',list:'llm-models',placeholder:'model id',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const found=h('div',{class:'mg-actions'});
    const form=()=>({id:id.value.trim().toLowerCase(),label:label.value.trim(),base_url:url.value.trim(),api_key:key.value?'{{secret:'+key.value+'}}':'',
      models:models.value.split(',').map(x=>x.trim()).filter(Boolean),default_model:def.value.trim()});
    const showModels=ms=>{dl.replaceChildren(...ms.map(m=>h('option',{value:m})));
      found.replaceChildren(...ms.slice(0,24).map(m=>h('button',{type:'button',class:'mg-chip',text:m,title:'Add to the offered models',onclick:()=>{const cur=form().models;if(!cur.includes(m)){cur.push(m);models.value=cur.join(', ');}}})));};
    if(listed[v.id])showModels(listed[v.id]);
    const listBtn=p?btn('List models','',()=>run(listBtn,async()=>{const r=await post({action:'llm_models',provider:p.id});if(!r.ok)throw Error(r.error);listed[p.id]=r.models;showModels(r.models);status.textContent=r.models.length+' model'+(r.models.length===1?'':'s')+' at '+p.host+'. Click one to offer it.';},err)):null;
    const save=btn(p?'Save':'Add','primary',()=>run(save,async()=>{const f=form();const r=await post({action:'llm_save',provider:f});d={...d,...r};sel=f.id;draw();status.textContent='Saved '+f.id+'.';},err));
    const del=p?btn('Remove','danger',()=>run(del,async()=>{
      const u=users(p.id);if(!confirm('Remove '+p.id+'?'+(u.length?' '+u.join(', ')+' uses it and will stop answering until you pick another.':'')))return;
      const r=await post({action:'llm_delete',provider:p.id});d={...d,...r};sel=null;draw();status.textContent='Removed '+p.id+'.';},err)):null;
    if(d.locked)for(const c of [label,url,key,models,def])c.disabled=true;
    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:p?(p.label||p.id):'Add a provider'}),h('small',{text:'OpenAI-compatible · llm.providers'}))),
      h('div',{class:'mg-pad'},
        field('Name (id)',id,p?'Other settings refer to it by this name.':'Short and lowercase, e.g. lab-gpu. Other settings refer to it by this name.'),
        field('Label',label),
        field('Address',url,'Its /v1 base: llama.cpp, vLLM, LM Studio, Ollama and most paid APIs work.'),
        field('API key',key,'A secret from Secrets, used as {{secret:name}}. Add the key there first.'),
        field('Models to offer',models,'Comma-separated. Blank offers every model it lists.'),
        p?h('div',{class:'mg-actions'},listBtn,found):null,dl,
        field('Default model',def,'Used when a caller does not name one.'),
        p?checkView(p):null,
        h('div',{class:'mg-actions end rl-save'},err,del,check,save)));
  }
  return {activate(){refresh().catch(e=>{list.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));panel.replaceChildren();});},deactivate(){}};
}
