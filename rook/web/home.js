// Home agent: the hub's own LLM. Configured here, stored through the settings
// service (keys home.*), answered by the "home" hub plugin. The API key is only
// ever a vault reference ({{secret:name}}); this page never sees a key.
// The chat panel is the 1:1 huddle with the agent (a chaired huddle with no
// roles, through /account/huddle/api), so the conversation is also on the
// Huddles page.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, ago, useCss, btn, table, two, info} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);
const {hapi, voicePicker} = await import('/account/huddle/assets/huddles.js' + new URL(import.meta.url).search);

const API='/account/settings/api';
const VIA={chat:'Chat',ask:'home.ask',test:'Test','huddle-chair':'Huddle','huddle-close':'Huddle summary',job:'Job'};
const fmtN=n=>n>=1e6?(n/1e6).toFixed(1)+'M':n>=1e4?Math.round(n/1e3)+'k':n>=1e3?(n/1e3).toFixed(1)+'k':String(n);

export async function mountHome(root){
  useCss();
  let csrf='',d=null,models=[],busy=false;
  const status=h('p',{class:'mg-status',role:'status'});
  const top=h('div'),acts=h('div',{style:'display:flex;flex-direction:column;gap:6px'});
  const panel=h('div',{class:'mg-box strong mg-side'});
  const chat=homeChat({
    toForm(){panel.scrollIntoView({behavior:'smooth',block:'start'});panel.querySelector('input,select')?.focus({preventScroll:true});},
    async status(){const j=await get();d.status=j.status;d.activity=j.activity;d.usage=j.usage;d.sessions=j.sessions;drawList();return j;}});
  const list=h('div',{class:'mg-main'},top,chat.el,acts);
  root.replaceChildren(h('div',{class:'mg'},status,h('div',{class:'mg-split'},list,panel)));

  async function get(){const r=await fetch(API+'?view=home');const j=await r.json().catch(()=>({error:'Bad response'}));if(j.csrf)csrf=j.csrf;if(!r.ok||j.error)throw Error(j.error||'Sign in with your operator account to set up the home agent.');return j;}
  async function post(body){const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const j=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||j.error)throw Error(j.error||'Request failed');return j;}
  async function refresh(){d=await get();draw();}

  const val=k=>d.settings[k]?.value;
  function state(s){
    if(!s.enabled)return ['Off','mg-dimtext'];
    if(s.missing.length)return ['Needs '+s.missing.join(' and ').replace('base_url','a base URL').replace('llm_provider','its model provider back').replace('agent_host','an agent host'),'mg-warn'];
    if(s.last_error)return ['On · last error','mg-bad'];
    return ['On','mg-good'];
  }
  function draw(){drawList();drawPanel();chat.update(d.status,Number(val('timeout_s'))||90);}
  // The table and the activity list only: the chat panel and the form keep
  // what the operator is typing.
  function drawList(){
    const s=d.status,[label,cls]=state(s);
    const row=h('div',{class:'mg-row sel'},two(s.name,s.identity,true),two(s.model||'no model',s.agent_host&&s.agent_host.harness?s.agent_host.harness+' on '+(s.agent_host.host||'no host'):(s.endpoint_host||'no endpoint'),true),h('span',{text:label,class:cls}),h('span',{class:'mg-dimtext',text:s.tools?'knowledge search':'no tools'}));
    const rows=d.activity.map(a=>h('div',{class:'mg-row',title:a.error||''},
      h('span',{class:'mg-dimtext',text:ago(a.ts)}),h('span',{text:VIA[a.via]||a.via}),
      two(a.sender||'',a.room||'',false),
      h('span',{class:a.ok?'mg-good':'mg-bad',text:a.ok?(a.latency_ms!=null?(a.latency_ms/1000).toFixed(1)+' s':'ok')+(a.tools&&a.tools.length?' · '+a.tools.length+' tool call'+(a.tools.length>1?'s':''):'')+memo(a.memory):(a.error||'failed').slice(0,80)})));
    top.replaceChildren(table('minmax(0,1fr) minmax(0,1.2fr) 150px 120px',['Agent','Model · endpoint','State','Tools'],[row]));
    acts.replaceChildren(
      h('div',{class:'mg-label',style:'margin-top:6px',title:'Since the MCP server started',text:'Recent activity'}),
      table('90px 80px minmax(0,1fr) minmax(0,1fr)',['When','Via','From · room','Result'],rows.length?rows:[h('div',{class:'mg-empty',text:'Nothing yet.'})]),
      ...sessions(),...usage());
  }
  // Always-on sessions (docs/design/persistent-agents.md): one line per role,
  // "house agent: busy, 2 waiting, 1 fork", and a fresh start only when one happened.
  function sessions(){
    const s=d.sessions;if(!s||!s.on)return [];
    const k=n=>n?fmtN(n)+' tokens':'';
    const rows=Object.entries(s.roles||{}).map(([r,x])=>h('div',{class:'mg-row'},
      h('span',{text:x.line}),
      h('span',{class:'mg-dimtext',text:[k((x.main||{}).context_tokens),x.voice?'voice '+(x.voice.state||''):''].filter(Boolean).join(' · ')}),
      h('span',{class:'mg-dimtext mg-mono',text:((x.main||{}).session_id||'').slice(0,8)})));
    const reset=(s.resets||[]).filter(r=>Date.now()/1000-r.at<86400).slice(-1)[0];
    return [h('div',{class:'mg-label',style:'margin-top:6px',text:s.connected?'Sessions on '+s.host:'Sessions: no session host connected (one-off runs)'}),
      table('minmax(0,1.4fr) minmax(0,1fr) 90px',['Agent','Context','Session'],rows),
      reset?h('p',{class:'mg-sub mg-warn',style:'white-space:normal',text:reset.role+' '+reset.lane+' started a fresh session: '+reset.reason}):null].filter(Boolean);
  }
  // Turns on agent hosts in the last 24 hours, per role (what huddles cost the
  // subscription signed in there). Only when there were any.
  function usage(){
    const u=d.usage||[];if(!u.length)return [];
    const t=(r,k)=>r.tokens[k]||0;
    const rows=u.map(r=>h('div',{class:'mg-row'},
      two(r.role?'@'+r.role:'House agent',r.models.join(', '),!!r.role),
      h('span',{text:r.turns+(r.failed?' ('+r.failed+' failed)':'')}),
      h('span',{class:'mg-dimtext',text:fmtN(t(r,'input_tokens')+t(r,'cache_creation_input_tokens')+t(r,'cache_read_input_tokens'))+' in · '+fmtN(t(r,'output_tokens'))+' out'}),
      h('span',{class:'mg-dimtext',text:r.cost_usd?'$'+r.cost_usd.toFixed(2):'—'})));
    return [h('div',{class:'mg-label',style:'margin-top:6px',text:'Agent hosts · 24 h'}),
      table('minmax(0,1fr) 110px minmax(0,1fr) 80px',['Who','Turns','Tokens','Cost'],rows)];
  }
  // What memory did for one answer: "· 3 recalled · 1 remembered".
  function memo(m){if(!m)return '';return [m.recalled&&m.recalled+' recalled',m.remembered&&m.remembered+' remembered',m.forgotten&&m.forgotten+' forgotten'].filter(Boolean).map(x=>' · '+x).join('');}
  function field(label,ctl,k,note){
    const r=d.settings[k]||{};
    if(r.locked){ctl.disabled=true;}
    const help=note||r.help||'';
    return h('label',{class:'mg-field'},help?h('span',{class:'mg-flabel'},label,info(help,'About '+label)):label,ctl,
      r.locked?h('small',{class:'mg-sub mg-warn',style:'white-space:normal',text:'Set by '+(r.env||'the environment')+' on the MCP server.'}):null);
  }
  function drawPanel(){
    const err=h('small',{class:'mg-error',role:'alert'}),out=h('pre',{hidden:true,style:'white-space:pre-wrap'});
    const enabled=h('input',{type:'checkbox',checked:!!val('enabled')});
    const frontDoor=h('input',{type:'checkbox',checked:!!val('front_door')});
    const persistent=h('input',{type:'checkbox',checked:!!val('persistent')});
    const voiceLane=h('input',{type:'checkbox',checked:val('voice_lane')!==false});
    const pModel=h('input',{value:val('persistent_model')||'opus',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const vModel=h('input',{value:val('voice_model')||'claude-haiku-4-5',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const name=h('input',{value:val('name')||'home',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const provider=h('select',null,d.providers.map(p=>h('option',{value:p,text:p==='openai'?'OpenAI-compatible':p,selected:p===val('provider')})));
    // A named model provider (Intelligence sources > Model providers) replaces the endpoint fields.
    const lp=val('llm_provider')||'',lps=d.llm_providers||[];
    const llmProvider=h('select',null,h('option',{value:'',text:'None: use the endpoint below'}),lps.map(p=>h('option',{value:p.id,text:(p.label||p.id)+(p.host?' · '+p.host:''),selected:p.id===lp})),lp&&!lps.some(p=>p.id===lp)?h('option',{value:lp,text:lp+' (not available now)',selected:true}):null);
    const base=h('input',{value:val('base_url')||'',placeholder:'http://llm.example:1234/v1',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const dl=h('datalist',{id:'home-models'},models.map(m=>h('option',{value:m})));
    const model=h('input',{value:val('model')||'',list:'home-models',placeholder:'model id',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const ref=String(val('api_key')||''),refName=(ref.match(/^\{\{secret:(.+)\}\}$/)||[])[1]||'';
    const key=h('select',null,h('option',{value:'',text:'No key'}),d.secrets.map(n=>h('option',{value:n,text:n,selected:n===refName})),refName&&!d.secrets.includes(refName)?h('option',{value:refName,text:refName+' (missing from the vault)',selected:true}):null);
    const persona=h('select',null,h('option',{value:'',text:'The persona assigned to family "home" (or the default)'}),d.personas.map(p=>h('option',{value:p.id,text:p.name+' ('+p.id+')',selected:p.id===val('persona')})));
    const vp=voicePicker(val('voice'));
    const prompt=h('textarea',{rows:'4',placeholder:'Extra instructions for the home agent',style:'font:13px/1.6 var(--mono)'});prompt.value=val('system_prompt')||'';
    const tools=h('input',{type:'checkbox',checked:!!val('tools')});
    const memory=h('input',{type:'checkbox',checked:val('memory')!==false});
    const num=(k,step)=>h('input',{type:'number',step:step||'1',value:val(k)??'',style:'font-family:var(--mono)'});
    const timeout=num('timeout_s','1'),maxTok=num('max_tokens'),temp=num('temperature','0.05'),ctx=num('context_messages');
    // An agent harness (Claude Code, Codex, OpenCode) on an agent host: a
    // worker whose agenthost plugin found it installed.
    const HARN={'claude-code':['claude','Claude Code'],codex:['codex','Codex'],opencode:['opencode','OpenCode']};
    const backend=h('select',null,(d.backends||['openai']).map(b=>h('option',{value:b,text:HARN[b]?HARN[b][1]+' on an agent host':'OpenAI-compatible endpoint',selected:b===(val('backend')||'openai')})));
    const hosts=d.agent_hosts||[],cw=val('agent_host')||'';
    const hostLabel=x=>{const hs=Object.entries(x.harnesses||{}).map(([n,i])=>n+(i&&i.version?' '+i.version:'')+(i&&i.logged_in===false?' (signed out)':''));
      return x.name+' · '+(x.online?'online':'offline')+(hs.length?' · '+hs.join(', '):'');};
    const agHost=h('select',null,h('option',{value:'',text:hosts.length?'Pick a host':'No worker has Claude Code, Codex or OpenCode'}),hosts.map(x=>h('option',{value:x.name,text:hostLabel(x),selected:x.name===cw||x.worker_id===cw})),cw&&!hosts.some(x=>x.name===cw||x.worker_id===cw)?h('option',{value:cw,text:cw+' (not available now)',selected:true}):null);
    const agFallbacks=h('input',{value:(val('agent_fallback_hosts')||[]).join(', '),placeholder:'worker names, comma separated',autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'});
    const agUnavail=h('select',null,[['fallback_backend','Use the OpenAI-compatible endpoint'],['fail','Say it is unavailable']].map(([v,t])=>h('option',{value:v,text:t,selected:v===val('agent_on_unavailable')})));
    const agTimeout=num('agent_timeout_s','1');
    // One model (and effort) setting per harness; only the chosen one shows.
    const mono={autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'};
    const agLists={claude:h('datalist',{id:'home-m-claude'},(d.claude_models||[]).map(m=>h('option',{value:m}))),codex:h('datalist',{id:'home-m-codex'}),opencode:h('datalist',{id:'home-m-opencode'})};
    const agModel={claude:h('input',{...mono,value:val('claude_model')??'haiku',list:'home-m-claude'}),
      codex:h('input',{...mono,value:val('codex_model')||'',list:'home-m-codex',placeholder:'Codex default'}),
      opencode:h('input',{...mono,value:val('opencode_model')||'',list:'home-m-opencode',placeholder:'OpenCode default (provider/model)'})};
    const effortSel=k=>h('select',null,['','low','medium','high','xhigh','max'].map(v=>h('option',{value:v,text:v||'Default',selected:v===(val(k)||'')})));
    const agEffort={claude:effortSel('claude_effort'),codex:effortSel('codex_effort')};
    const ccAuth=h('select',null,[['login','The host\'s own claude login'],['token','A setup-token from Secrets']].map(([v,t])=>h('option',{value:v,text:t,selected:v===val('claude_auth')})));
    const ctRef=String(val('claude_token')||''),ctName=(ctRef.match(/^\{\{secret:(.+)\}\}$/)||[])[1]||'';
    const ccToken=h('select',null,h('option',{value:'',text:'None'}),d.secrets.map(n=>h('option',{value:n,text:n,selected:n===ctName})),ctName&&!d.secrets.includes(ctName)?h('option',{value:ctName,text:ctName+' (missing from the vault)',selected:true}):null);
    const ccTokenField=field('Token',ccToken,'claude_token','A long-lived token from `claude setup-token` on any machine, saved in Secrets.');
    const listModels=btn('List models','',()=>run(listModels,async()=>{
      const k=(HARN[backend.value]||[])[0];if(!k)return;
      const r=await post({action:'home_models',values:values()});
      if(!r.ok)throw Error(r.error);agLists[k].replaceChildren(...(r.models||[]).map(m=>h('option',{value:m})));
      status.textContent=(r.models||[]).length+' model'+((r.models||[]).length===1?'':'s')+(r.host?' from '+r.host:'')+'.'+(r.note?' '+r.note:'');}));
    const modelFields={claude:field('Model',h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},agModel.claude),'claude_model'),
      codex:field('Model',h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},agModel.codex),'codex_model'),
      opencode:field('Model',h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},agModel.opencode),'opencode_model')};
    const effortFields={claude:field('Effort',agEffort.claude,'claude_effort'),codex:field('Reasoning effort',agEffort.codex,'codex_effort')};
    const claudeOnly=h('div',{style:'display:flex;flex-direction:column;gap:14px'},field('Sign-in',ccAuth,'claude_auth'),ccTokenField);
    const cc=h('div',{style:'display:flex;flex-direction:column;gap:14px'},
      field('Agent host',agHost,'agent_host','The worker that runs the home agent\'s turns. Change it to move the agent; memory stays at the hub.'),
      field('Fallback hosts',agFallbacks,'agent_fallback_hosts'),
      field('When no host is available',agUnavail,'agent_on_unavailable'),
      ...Object.values(modelFields),h('div',{class:'mg-actions'},listModels),...Object.values(agLists),
      ...Object.values(effortFields),claudeOnly,
      field('Turn timeout (s)',agTimeout,'agent_timeout_s'));
    const values=()=>({enabled:enabled.checked,front_door:frontDoor.checked,backend:backend.value,agent_host:agHost.value,
      agent_fallback_hosts:agFallbacks.value.split(',').map(x=>x.trim()).filter(Boolean),agent_on_unavailable:agUnavail.value,
      agent_timeout_s:Number(agTimeout.value||180),persistent:persistent.checked,voice_lane:voiceLane.checked,
      persistent_model:pModel.value.trim()||'opus',voice_model:vModel.value.trim()||'claude-haiku-4-5',claude_model:agModel.claude.value.trim(),codex_model:agModel.codex.value.trim(),
      opencode_model:agModel.opencode.value.trim(),claude_effort:agEffort.claude.value,codex_effort:agEffort.codex.value,
      claude_auth:ccAuth.value,claude_token:ccToken.value?'{{secret:'+ccToken.value+'}}':'',voice:vp.select.value,name:name.value.trim().toLowerCase(),llm_provider:llmProvider.value,provider:provider.value,base_url:base.value.trim(),model:model.value.trim(),
      api_key:key.value?'{{secret:'+key.value+'}}':'',persona:persona.value,system_prompt:prompt.value,tools:tools.checked,memory:memory.checked,
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
      out.textContent=(r.ok?r.model+(r.worker?' on '+r.worker+(r.harness?' ('+r.harness+')':''):'')+' answered in '+(r.latency_ms/1000).toFixed(1)+' s:\n\n'+r.reply:'Test failed: '+r.error)+(r.note?'\n\n'+r.note:'');
      out.className=r.ok?'':'mg-bad';refresh().catch(()=>{});}));
    const save=btn('Save','primary',()=>run(save,async()=>{
      const r=await post({action:'home_save',values:values()});
      d=r;draw();status.textContent=r.saved.length?'Saved '+r.saved.map(k=>k.slice(5)).join(', ')+'.':'No changes.';}));
    const endpoint=h('div',{style:'display:flex;flex-direction:column;gap:14px'},
      field('Provider',provider,'provider'),
      field('Base URL',base,'base_url','The endpoint\'s /v1 base. Any OpenAI-compatible server works.'),
      field('API key',key,'api_key','A secret from Secrets, used as {{secret:name}}. Add the key there first; it is never shown or stored here.'));
    const syncEndpoint=()=>{endpoint.hidden=!!llmProvider.value;};
    llmProvider.addEventListener('change',syncEndpoint);syncEndpoint();
    const oai=h('div',{style:'display:flex;flex-direction:column;gap:14px'},
      field('Model provider',llmProvider,'llm_provider','From Model providers (llmanifold, or one you added there). Its address and key are used.'),
      endpoint,
      field('Model',h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},model,fetchModels),'model','List models reads /v1/models with the values on this form.'),dl);
    // An agent harness: its own group first; the endpoint becomes the fallback.
    const oaiHead=h('div',{class:'mg-label',text:'Fallback endpoint (OpenAI-compatible)'});
    const syncBackend=()=>{const k=(HARN[backend.value]||[])[0];cc.hidden=!k;oaiHead.hidden=!k;
      for(const [n,f] of Object.entries(modelFields))f.hidden=n!==k;for(const [n,f] of Object.entries(effortFields))f.hidden=n!==k;
      claudeOnly.hidden=k!=='claude';ccTokenField.hidden=ccAuth.value!=='token';};
    backend.addEventListener('change',syncBackend);ccAuth.addEventListener('change',syncBackend);syncBackend();
    const hist=d.history.slice(0,10);
    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Home agent'}),h('small',{text:'home.* · hub-wide'})),
        info('It chairs your huddles and talks with you here; agents ask it with home.ask on worker rook. It acts as '+d.status.identity+' and everything it does is journaled under that name.','About the home agent')),
      h('div',{class:'mg-pad'},
        h('label',{class:'mg-actions',title:'Answer in chat and to home.ask'},enabled,'Enabled'),
        h('div',{class:'mg-actions'},h('label',{class:'mg-actions',title:'Route every request: answer, do it (trust map permitting), start a coding session or a huddle; ask you only when it must'},frontDoor,'Front door'),h('a',{href:'#decisions',text:'Decisions'})),
        h('div',{class:'mg-actions'},h('label',{class:'mg-actions',title:'Run as always-on Claude Code sessions on the session host (Opus, with a Haiku voice); every request goes through its inbox'},persistent,'Always-on sessions'),
          info('Needs the session host (python -m rook.agents) on the machine with the Claude login. Without it, requests are one-off runs as before.','About always-on sessions')),
        h('details',null,h('summary',{text:'Always-on sessions'}),h('div',{class:'mg-pad',style:'padding-left:0;padding-right:0'},
          h('label',{class:'mg-actions'},voiceLane,'Voice session (Haiku)'),
          field('Agent model',pModel,'persistent_model'),field('Voice model',vModel,'voice_model'))),
        field('Name',name,'name','Chat identity agent:<name>; people write @'+(val('name')||'home')+'.'),
        field('Backend',backend,'backend'),
        cc,oaiHead,oai,
        field('Persona',persona,'persona'),
        field('Speaking voice',h('div',{style:'display:flex;flex-direction:column;gap:6px'},vp.el,vp.note),'voice','The voice its huddle messages are read aloud in (the Speak button). Band default: Intelligence sources > Voice.'),
        field('System prompt addition',prompt,'system_prompt'),
        h('div',{class:'mg-actions'},h('label',{class:'mg-actions',title:'Remember people, facts and past conversations'},memory,'Memory'),h('a',{href:'#memory',text:'View'})),
        h('div',{class:'mg-actions'},h('label',{class:'mg-actions'},tools,'Tools'),info('Search the knowledge wiki, and save and search its memory. Each call is journaled as '+d.status.identity+'.','About tools')),
        h('details',null,h('summary',{text:'Limits'}),h('div',{class:'mg-pad',style:'padding-left:0;padding-right:0'},
          field('Request timeout (s)',timeout,'timeout_s'),field('Longest reply (tokens)',maxTok,'max_tokens'),
          field('Temperature',temp,'temperature','Blank: the endpoint\'s default.'),field('Room messages sent as context',ctx,'context_messages'))),
        h('div',{class:'mg-actions end rl-save'},err,test,save),out,
        d.status.last_error?h('p',{class:'mg-sub mg-bad',style:'white-space:normal',text:'Last error: '+d.status.last_error}):null,
        h('div',null,h('div',{class:'mg-label',style:'margin-bottom:8px',text:'Changes'}),
          hist.length?hist.map(e=>h('div',{class:'mg-log'},h('span',{class:'mg-mono',text:e.key.slice(5)}),h('span',{text:(e.actor||e.actor_label||'')}),h('span',{class:'mg-sub',style:'font-size:12px',text:ago(e.at||e.ts)}))):h('p',{class:'mg-sub',text:'Not configured yet.'}))));
  }
  return {
    activate(){chat.el.hidden=false;refresh().then(()=>chat.start()).catch(e=>{top.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));acts.replaceChildren();chat.el.hidden=true;panel.replaceChildren();});},
    deactivate(){chat.stop();}};
}

// -- chat with the home agent ------------------------------------------------
// The conversation is the 1:1 huddle with the agent: the newest chaired huddle
// that is not archived, has no roles and is titled with the agent's name, which
// the Huddles page lists too. It is started on the first message, not by
// visiting the page. Each message starts a round in which the agent (the chair)
// answers; replies are posted whole, so the panel polls the huddle: every
// 1.5 s while a round runs, every 4 s otherwise, never while the page is hidden.
const OP='user:operator';
// What the chair posts when its turn fails (rook/hub/plugins/huddle/chair.py).
const FAILED=/^\(The chair could not finish this round: /;
const RUNNING=['chair','roles','closing'];
export function homeChat({toForm,status}){
  let ident='',name='home',on=false,timeout=90,room=null,seq=0,timer=null,live=false,waiting=null,ticks=0,loaded=false,sending=false;
  const note=h('div',{class:'hc-note',role:'status',hidden:true});
  const log=h('div',{class:'hc-log','aria-live':'polite','aria-label':'Conversation'});
  const typing=h('div',{class:'hc-typing',hidden:true});
  const box=h('textarea',{rows:'1',placeholder:'Message the home agent',autocomplete:'off','aria-label':'Message'});
  const send=btn('Send','primary',()=>submit());
  const title=h('h2'),sub=h('span',{class:'mg-sub'});
  const el=h('section',{class:'mg-box strong hc','aria-label':'Chat with the home agent'},
    h('div',{class:'mg-panel-head'},title,sub),
    note,log,typing,h('div',{class:'hc-input'},box,send));
  box.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();submit();}});
  box.addEventListener('input',()=>{box.style.height='auto';box.style.height=Math.min(box.scrollHeight,160)+'px';});

  function say(text,cls,action){
    note.hidden=!text;note.className='hc-note'+(cls?' '+cls:'');
    note.replaceChildren(...(text?[h('span',{text}),action||null].filter(Boolean):[]));
  }
  const formBtn=()=>btn('Set up','soft',toForm);
  function blocked(s){
    if(!s.enabled)return 'Off.';
    if(s.missing.length)return 'Needs '+s.missing.join(' and ').replace('base_url','a base URL')+'.';
    if(!s.chat)return 'No chat store at the hub.';
    return '';
  }
  // Called whenever the page has fresh settings (load, Save).
  function update(s,t){
    timeout=t;title.textContent='Chat with @'+s.name;
    sub.replaceChildren(h('a',{href:room?'#huddles?room='+encodeURIComponent(room):'#huddles',text:'In Huddles ↗'}));
    if(s.identity!==ident||s.name!==name){ident=s.identity;name=s.name;room=null;seq=0;loaded=false;stopWaiting();log.replaceChildren();}
    const why=blocked(s);on=!why;
    box.disabled=send.disabled=!on;
    if(why){say(why,'warn',s.enabled&&s.missing.length||!s.enabled?formBtn():null);stopWaiting();}
    else if(note.classList.contains('warn'))say('');
  }
  function empty(){log.replaceChildren(h('div',{class:'mg-empty',text:'No conversation yet.'}));}
  function render(msgs){
    if(seq===0)log.replaceChildren();
    const atBottom=log.scrollHeight-log.scrollTop-log.clientHeight<60;
    for(const m of msgs){
      if(m.seq<=seq)continue;seq=m.seq;
      const me=m.sender===OP,quiet=m.sender===ident&&FAILED.test(m.text);
      log.append(h('div',{class:'hc-msg'+(me?' me':'')+(quiet?' quiet':'')},
        h('div',{class:'hc-who'},h('b',{text:me?'You':m.sender===ident?name:m.sender.replace(/^[a-z]+:/,'')}),
          h('span',{text:new Date(m.ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})})),
        h('div',{class:'hc-text',text:m.text})));
    }
    if(!log.children.length)empty();
    if(msgs.length&&(atBottom||waiting))log.scrollTop=log.scrollHeight;
  }
  async function findRoom(){
    const all=(await hapi('list')).huddles||[];
    const mine=all.filter(x=>x.chaired&&Array.isArray(x.roles)&&!x.roles.length&&!x.archived&&x.title===name)
      .sort((a,b)=>(b.created||0)-(a.created||0));
    return mine.length?mine[0].room:null;
  }
  const read=()=>hapi('view',{room,since_seq:seq,limit:100});
  // The round a message of ours started: done ends the wait, failed says why.
  function settle(v){
    if(!waiting)return;
    const r=(v.rounds||[]).find(x=>x.trigger_seq===waiting.after);
    if(!r)return;
    if(r.state==='failed')failed(r.error||'');
    else if(!RUNNING.includes(r.state)){stopWaiting();status().catch(()=>{});}
  }
  async function load(){
    room=await findRoom();seq=0;loaded=true;
    if(!room){empty();return;}
    const v=await read();
    render(v.messages||[]);log.scrollTop=log.scrollHeight;
    // Opened while a round is still running: keep showing that it is working.
    const r=(v.rounds||[]).at(-1);
    if(on&&!waiting&&r&&RUNNING.includes(r.state))wait(r.trigger_seq,r.started*1000);
  }
  async function poll(){
    if(!live||!ident)return schedule();
    try{
      if(!loaded)await load();
      else if(room){const v=await read();render(v.messages||[]);settle(v);}
      else if(++ticks%3===0&&await findRoom())loaded=false;   // started from the Huddles page
      if(waiting)watch();
      if(note.classList.contains('lost'))say('');
    }catch(e){if(on)say(e.message,'bad lost');}
    schedule();
  }
  function schedule(){clearTimeout(timer);if(live&&!document.hidden)timer=setTimeout(poll,waiting?1500:4000);}
  document.addEventListener('visibilitychange',()=>{if(live&&!document.hidden)poll();});
  const limit=()=>timeout*2+30;
  function wait(after,since){waiting={after,since};typing.hidden=false;tick();}
  function tick(){
    if(!waiting)return;
    const s=Math.round((Date.now()-waiting.since)/1000);
    typing.textContent=name+' is thinking'+(s>=3?' · '+s+' s':'')+'…';
  }
  function stopWaiting(){waiting=null;typing.hidden=true;}
  function watch(){
    tick();
    const age=(Date.now()-waiting.since)/1000;
    if(age>limit()){stopWaiting();say('No reply after '+Math.round(age)+' s. See Recent activity.','warn');}
  }
  function failed(err){
    stopWaiting();
    say('No answer'+(err?': '+err:'')+'. Check the form, then Test.','bad',formBtn());
    status().catch(()=>{});
  }
  async function submit(){
    const text=box.value.trim();if(!text||!on||sending)return;
    sending=true;send.disabled=true;say('');
    try{
      if(!room){room=(await hapi('start',{title:name,roles:[]})).room;seq=0;loaded=true;
        sub.replaceChildren(h('a',{href:'#huddles?room='+encodeURIComponent(room),text:'In Huddles ↗'}));}
      const t0=Date.now();
      const sent=await hapi('send',{room,text});
      box.value='';box.style.height='';
      wait(sent.seq,t0);
      const v=await read();render(v.messages||[]);settle(v);log.scrollTop=log.scrollHeight;schedule();
    }catch(e){say('Not sent: '+e.message,'bad');}
    finally{sending=false;send.disabled=!on;box.focus();}
  }
  return {el,update,
    start(){if(live)return;live=true;loaded=false;poll();},
    stop(){live=false;clearTimeout(timer);}};
}

