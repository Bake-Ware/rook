// Home agent: the hub's own LLM. Configured here, stored through the settings
// service (keys home.*), answered by the "home" hub plugin. The API key is only
// ever a vault reference ({{secret:name}}); this page never sees a key.
import {h,ago,useCss,btn,table,two} from '/account/bands/assets/manage.js';

const API='/account/settings/api';
const VIA={chat:'Chat',ask:'home.ask',test:'Test'};

export async function mountHome(root){
  useCss();
  let csrf='',d=null,models=[],busy=false;
  const status=h('p',{class:'mg-status',role:'status'}),summary=h('p',{class:'mg-sub'});
  const list=h('div',{class:'mg-main'}),panel=h('div',{class:'mg-box strong mg-side'});
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
  function draw(){
    const s=d.status,[label,cls]=state(s);
    summary.textContent='The home agent is an LLM that lives at the hub. People reach it in Chat as @'+s.name+'; agents ask it with home.ask on worker rook. It acts as '+s.identity+' and everything it does is journaled under that name.';
    const row=h('div',{class:'mg-row sel'},two(s.name,s.identity,true),two(s.model||'no model',s.endpoint_host||'no endpoint',true),h('span',{text:label,class:cls}),h('span',{class:'mg-dimtext',text:s.tools?'knowledge search':'no tools'}));
    const acts=d.activity.map(a=>h('div',{class:'mg-row',title:a.error||''},
      h('span',{class:'mg-dimtext',text:ago(a.ts)}),h('span',{text:VIA[a.via]||a.via}),
      two(a.sender||'',a.room||'',false),
      h('span',{class:a.ok?'mg-good':'mg-bad',text:a.ok?(a.latency_ms!=null?(a.latency_ms/1000).toFixed(1)+' s':'ok')+(a.tools&&a.tools.length?' · '+a.tools.length+' tool call'+(a.tools.length>1?'s':''):''):(a.error||'failed').slice(0,80)})));
    list.replaceChildren(
      table('minmax(0,1fr) minmax(0,1.2fr) 150px 120px',['Agent','Model · endpoint','State','Tools'],[row]),
      h('div',{class:'mg-label',style:'margin-top:6px',text:'Recent activity (since the MCP server started)'}),
      table('90px 80px minmax(0,1fr) minmax(0,1fr)',['When','Via','From · room','Result'],acts.length?acts:[h('div',{class:'mg-empty',text:'Nothing yet. Use Test, or mention @'+s.name+' in a chat room.'})]));
    drawPanel();
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
  return {activate(){refresh().catch(e=>{list.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));panel.replaceChildren();});},deactivate(){}};
}
