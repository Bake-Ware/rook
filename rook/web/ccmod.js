// Manage > Claude Code: rules for Claude Code on your workers (when something
// happens in a session, do something), pushed to each worker's Claude Code
// settings. Everything goes through /account/ccmod/api (operator only;
// rook/hub/plugins/ccmod/web.py). Stored text is rendered as text.
// Versioned like this module (?v=...), so a stale cache cannot pair it with
// an old manage.js.
const {h, useCss, btn, chips, dialog, ago} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API='/account/ccmod/api';
const RISK_NOTE={read:'reads only',write:'changes data',exec:'runs code or controls a machine',admin:'administers the band'};
const SCOPE_LABEL={all:'Everywhere',band:'One band',worker:'One worker',project:'One project',agent:'One agent'};
const GROUP_LABEL={claude:'In Claude Code',worker:'Noticed by the worker',rook:'In Rook'};
// Ready-made rules: a calm place to start.
const EXAMPLES=[
  {name:'Tell me when a session is waiting',trigger:{event:'Idle',minutes:10},scope:{kind:'all'},
   actions:[{type:'post',room_title:'Claude Code',text:'{worker}: a session in {project} has been waiting on you for {minutes} min.'}]},
  {name:'Never force-delete folders',trigger:{event:'PreToolUse',matcher:'Bash',contains:'rm -rf'},scope:{kind:'all'},
   actions:[{type:'block',message:'Deleting folders with rm -rf is blocked on this band. Ask the person first.'},
            {type:'post',room_title:'Claude Code',text:'{worker}: blocked an rm -rf in {project}.'}]},
  {name:'Update the Rook task before stopping',trigger:{event:'Stop'},scope:{kind:'all'},
   actions:[{type:'inject',text:'Before you stop: if you claimed a Rook task, update it (rook_task) with what you did and what is left.'}]},
];
const CSS=`
.cc h1{margin:0 8px 0 0;font:500 20px var(--sans);color:var(--accent)}
.cc-list{display:flex;flex-direction:column;border:1px solid var(--line);background:var(--panel)}
.cc-rule{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:6px 14px;align-items:center;padding:12px 16px;border-bottom:1px solid #20261d}
.cc-rule:last-child{border-bottom:0}
.cc-rule.off{opacity:.62}
.cc-rule h3{margin:0;font:500 14px var(--sans);display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center}
.cc-rule p{margin:2px 0 0;color:var(--dim);font-size:13px;line-height:1.45}
.cc-tags{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.cc-tag{font:500 9px var(--mono);letter-spacing:1px;padding:2px 6px;border:1px solid var(--line2);color:var(--dim);text-transform:uppercase}
.cc-tag.write{border-color:#514b35;color:var(--accent)}
.cc-tag.exec,.cc-tag.admin{border-color:#6b4336;color:var(--bad);background:#241614}
.cc-tag.hub{border-color:#3d5a33;color:var(--green)}
.cc-switch{width:18px;height:18px}
.cc-ex{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px}
.cc-ex button{display:flex;flex-direction:column;gap:4px;align-items:flex-start;text-align:left;padding:12px 14px;border:1px solid var(--line);background:var(--panel);color:var(--fg);cursor:pointer;font:13px var(--sans);line-height:1.45}
.cc-ex button:hover{border-color:var(--accent)}
.cc-ex small{color:var(--dim);font-size:12px}
.cc-form{display:flex;flex-direction:column;gap:14px}
.cc-row{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
.cc-row>.mg-field{flex:1 1 180px;min-width:0}
.cc-act{display:flex;flex-direction:column;gap:10px;padding:12px;border:1px solid var(--line);background:var(--panel2)}
.cc-act-head{display:flex;gap:8px;align-items:center}
.cc-act-head select{flex:1 1 auto}
.cc-sentence{padding:10px 12px;border-left:2px solid var(--accent);background:#1f1c12;font-size:13px;line-height:1.5}
.cc-help{color:var(--dim);font-size:12px;line-height:1.45;margin:0}
.cc-help code{font:11px var(--mono);color:#c9cdb8}
dialog.cc-wide{width:min(760px,calc(100vw - 32px))}
.cc-workers{border:1px solid var(--line);background:var(--panel)}
.cc-w{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1.2fr) minmax(0,1fr) auto;gap:8px 14px;align-items:center;padding:10px 16px;border-bottom:1px solid #20261d;font-size:13px}
.cc-w:last-child{border-bottom:0}
.cc-w.head{font:500 10px var(--mono);letter-spacing:1px;color:var(--dim);text-transform:uppercase;min-height:0}
.cc-w small{display:block;color:var(--dim);font-size:11px;overflow-wrap:anywhere}
.cc-w .acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.cc-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:var(--dim)}
.cc-dot.ok{background:var(--green)}.cc-dot.stale{background:var(--accent)}.cc-dot.bad{background:var(--bad)}
.cc-diff{margin:0;padding:10px 0;background:#10140f;border:1px solid var(--line);font:12px/1.55 var(--mono);max-height:420px;overflow:auto;white-space:pre}
.cc-diff span{display:block;padding:0 12px;color:#c9cdb8}
.cc-diff .add{background:#16200f;color:var(--green)}
.cc-diff .del{background:#241614;color:var(--bad)}
.cc-diff .hunk{color:var(--dim)}
.cc-file{display:flex;flex-direction:column;gap:8px}
.cc-file h3{margin:0;font:500 13px var(--mono);overflow-wrap:anywhere}
.cc-log{border:1px solid var(--line);background:var(--panel)}
.cc-log>div{display:grid;grid-template-columns:90px minmax(0,1fr) minmax(0,1.4fr);gap:4px 14px;padding:9px 16px;border-bottom:1px solid #20261d;font-size:13px;align-items:baseline}
.cc-log>div:last-child{border-bottom:0}
.cc-log small{color:var(--dim);font-size:12px;overflow-wrap:anywhere}
.cc-log .bad{color:var(--bad)}
@media(max-width:760px){
 .cc-rule{grid-template-columns:auto minmax(0,1fr)}.cc-rule>.mg-actions{grid-column:1/-1}
 .cc-w{grid-template-columns:minmax(0,1fr) auto}.cc-w .hide{display:none}.cc-w .acts{grid-column:1/-1;justify-content:flex-start}
 .cc-w.head{display:none}
 .cc-log>div{grid-template-columns:minmax(0,1fr)}
}
`;
function css(){
  useCss();
  if(!document.querySelector('style[data-cc]')){const s=document.createElement('style');s.dataset.cc='1';s.textContent=CSS;document.head.append(s);}
}

let csrf='';
async function get(params){
  const r=await fetch(API+'?'+new URLSearchParams(params||{}));
  const d=await r.json().catch(()=>({error:'Bad response'}));
  if(d.csrf)csrf=d.csrf;
  if(!r.ok||d.error)throw Error(d.error||'Request failed');
  return d;
}
async function post(body){
  if(!csrf)await get({view:'page'});
  const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});
  const d=await r.json().catch(()=>({error:'Bad response'}));
  if(!r.ok)throw Error(d.error||'Request failed');
  return d;
}

function diffView(text){
  const pre=h('pre',{class:'cc-diff'});
  for(const line of String(text||'').split('\n')){
    if(!line)continue;
    const cls=line.startsWith('+++')||line.startsWith('---')?'hunk':line.startsWith('@@')?'hunk':line[0]==='+'?'add':line[0]==='-'?'del':'';
    pre.append(h('span',{class:cls,text:line}));
  }
  return pre;
}

export async function mountCcmod(root){
  css();
  const page=h('div',{class:'mg cc'});root.replaceChildren(page);
  const status=h('p',{class:'mg-status',role:'status'});
  let tab='rules', d=null, timer=null;
  function say(text,bad){status.className='mg-status'+(bad?' bad':'');status.textContent=text||'';}
  async function load(){d=await get({view:'page'});return d;}
  const ev=id=>(d.choices.events.find(e=>e.id===id)||{});

  // -- rules -------------------------------------------------------------------------
  function ruleRow(r){
    const sw=h('input',{type:'checkbox',class:'cc-switch',checked:r.enabled,'aria-label':(r.enabled?'Switch off ':'Switch on ')+r.name,title:r.enabled?'On':'Off'});
    sw.onchange=async()=>{sw.disabled=true;try{await post({action:'toggle',id:r.id,enabled:sw.checked});await load();render();say(r.name+(sw.checked?' is on.':' is off.')+' Push to workers to put the change into effect.');}catch(e){sw.checked=!sw.checked;sw.disabled=false;say(e.message,true);}};
    const tags=h('div',{class:'cc-tags'},
      h('span',{class:'cc-tag '+r.risk,text:r.risk,title:RISK_NOTE[r.risk]||''}),
      h('span',{class:'cc-tag',text:SCOPE_LABEL[r.scope.kind]+(r.scope.target?': '+r.scope.target:'')}),
      r.actions.map(a=>h('span',{class:'cc-tag'+(['post','notify','cap'].includes(a.type)?' hub':''),text:a.type==='cap'?'runs '+a.cap:(d.choices.actions.find(x=>x.id===a.type)||{}).label||a.type})));
    return h('div',{class:'cc-rule'+(r.enabled?'':' off')},sw,
      h('div',null,h('h3',null,h('span',{text:r.name})),h('p',{text:r.summary}),tags),
      h('div',{class:'mg-actions',style:'display:flex;gap:6px'},btn('Edit','',()=>editor(r)),
        btn('Delete','danger',async()=>{if(!confirm('Delete the rule "'+r.name+'"? Push afterwards to take it off the workers.'))return;try{await post({action:'delete',id:r.id});await load();render();say('Deleted. Push to workers to take it off them.');}catch(e){say(e.message,true);}})));
  }
  function rulesTab(body){
    body.append(h('p',{class:'mg-sub',text:'A rule says: when something happens in a Claude Code session, do something. Rook adds its own hooks to Claude Code\'s settings on each worker you push to, next to your own (they are never replaced), and keeps a backup. Posts, notifications and capabilities run on the hub; blocking, extra instructions and the status line happen inside Claude Code.'}));
    const bar=h('div',{class:'mg-bar'},btn('New rule','primary',()=>editor(null)),h('span',{class:'mg-grow'}),
      btn('Push to all workers','soft',()=>pushAll()));
    body.append(bar);
    if(!d.rules.length){
      body.append(h('div',{class:'mg-box mg-pad'},h('b',{text:'No rules yet.'}),h('p',{class:'mg-sub',text:'Start from one of these, or write your own.'}),
        h('div',{class:'cc-ex'},EXAMPLES.map(x=>h('button',{type:'button',onclick:()=>editor(null,x)},h('b',{text:x.name}),h('small',{text:exampleNote(x)}))))));
      return;
    }
    body.append(h('div',{class:'cc-list'},d.rules.map(ruleRow)));
    body.append(h('details',null,h('summary',{class:'mg-label',text:'Examples'}),
      h('div',{class:'cc-ex',style:'margin-top:10px'},EXAMPLES.map(x=>h('button',{type:'button',onclick:()=>editor(null,x)},h('b',{text:x.name}),h('small',{text:exampleNote(x)}))))));
  }
  function exampleNote(x){
    return {Idle:'Posts in a "Claude Code" room after 10 minutes of waiting.',PreToolUse:'Blocks the command and posts in a room.',Stop:'Reminds Claude before it finishes its turn.'}[x.trigger.event]||'';
  }

  // -- the editor ----------------------------------------------------------------------
  function editor(rule,template){
    const r=JSON.parse(JSON.stringify(rule||template||{name:'',trigger:{event:'PreToolUse'},scope:{kind:'all'},actions:[{type:'post',room_title:'Claude Code',text:''}]}));
    r.trigger=r.trigger||{event:'PreToolUse'};r.scope=r.scope||{kind:'all'};r.actions=r.actions||[];
    const err=h('p',{class:'mg-error',role:'alert'});
    const sentence=h('div',{class:'cc-sentence',text:'…'});
    const name=h('input',{value:r.name||'',placeholder:'e.g. Never force-delete folders',maxlength:'80','aria-label':'Name'});
    const event=h('select',{'aria-label':'When'});
    for(const g of ['claude','worker','rook']){
      const og=h('optgroup',{label:GROUP_LABEL[g]});
      for(const e of d.choices.events.filter(e=>e.group===g))og.append(h('option',{value:e.id,text:e.label,selected:e.id===r.trigger.event}));
      event.append(og);
    }
    const matcher=h('input',{value:r.trigger.matcher||'',placeholder:'any tool, or Bash, or Edit|Write','aria-label':'Tool'});
    const contains=h('input',{value:r.trigger.contains||'',placeholder:'optional, e.g. rm -rf','aria-label':'Only if it mentions'});
    const minutes=h('input',{type:'number',min:'1',max:'1440',value:String(r.trigger.minutes||10),'aria-label':'Minutes'});
    const fMatcher=h('label',{class:'mg-field'},'Tool',matcher);
    const fContains=h('label',{class:'mg-field'},'Only if it mentions',contains);
    const fMinutes=h('label',{class:'mg-field'},'After this many minutes',minutes);
    const scope=h('select',{'aria-label':'Where'},Object.entries(SCOPE_LABEL).map(([k,v])=>h('option',{value:k,text:v,selected:k===r.scope.kind})));
    const targetBox=h('label',{class:'mg-field'});
    let target=null;
    function drawTarget(){
      const k=scope.value,cur=target?target.value:(r.scope.target||'');
      if(k==='all'){targetBox.hidden=true;target=null;return;}
      targetBox.hidden=false;
      if(k==='band'||k==='worker'){
        const opts=k==='band'?d.bands.map(b=>[b,b]):d.workers.map(w=>[w.name,w.name+(w.band?' · '+w.band:'')]);
        target=h('select',{'aria-label':'Which'},h('option',{value:'',text:'Choose…'}),opts.map(([v,t])=>h('option',{value:v,text:t,selected:v===cur})));
        if(cur&&!opts.some(([v])=>v===cur))target.append(h('option',{value:cur,text:cur+' (not online)',selected:true}));
      }else target=h('input',{value:cur,placeholder:k==='project'?'a folder name (myapp) or a path (/srv/myapp)':'the agent name in ROOK_AGENT','aria-label':'Which'});
      target.oninput=target.onchange=refreshSentence;
      targetBox.replaceChildren(k==='project'?'Project':k==='agent'?'Agent':k==='band'?'Band':'Worker',target);
    }
    const actionsBox=h('div',{class:'cc-form'});
    function actionFields(a,i){
      const allowed=ev(event.value).actions||[];
      const type=h('select',{'aria-label':'Action '+(i+1)},d.choices.actions.filter(x=>allowed.includes(x.id)||x.id===a.type).map(x=>h('option',{value:x.id,text:x.label+(x.where==='hub'?'':' (in Claude Code)'),selected:x.id===a.type})));
      const remove=btn('×','icon',()=>{r.actions.splice(i,1);drawActions();},{'aria-label':'Remove action '+(i+1)});
      const box=h('div',{class:'cc-act'},h('div',{class:'cc-act-head'},type,remove));
      type.onchange=()=>{r.actions[i]={type:type.value};drawActions();};
      const text=(key,label,ph,area)=>{const inp=area?h('textarea',{rows:'3',placeholder:ph||''}):h('input',{placeholder:ph||''});inp.value=a[key]||'';inp.oninput=()=>{a[key]=inp.value;refreshSentence();};return h('label',{class:'mg-field'},label,inp);};
      if(a.type==='post'){
        const where=h('select',{'aria-label':'Where to post'});
        where.append(h('option',{value:'new',text:'A new room…',selected:!!a.room_title||(!a.room&&!a.huddle)}));
        for(const rm of d.rooms)where.append(h('option',{value:'room:'+rm.id,text:'Room: '+rm.title,selected:a.room===rm.id}));
        if(d.huddles)where.append(h('option',{value:'huddle',text:'A project huddle…',selected:!!a.huddle}));
        const extra=h('div');
        const drawWhere=()=>{
          const v=where.value;
          if(v==='new'){delete a.room;delete a.huddle;a.room_title=a.room_title||'Claude Code';extra.replaceChildren(text('room_title','Room name','Claude Code'));}
          else if(v==='huddle'){delete a.room;delete a.room_title;a.huddle=a.huddle||'';extra.replaceChildren(text('huddle','Project','myapp'));}
          else{delete a.room_title;delete a.huddle;a.room=v.slice(5);extra.replaceChildren();}
          refreshSentence();
        };
        where.onchange=drawWhere;drawWhere();
        box.append(h('label',{class:'mg-field'},'Post in',where),extra,text('text','Message','{worker}: Claude Code {event} {tool} in {project}',true));
      }else if(a.type==='notify'){
        box.append(text('text','Message','{worker}: a session in {project} needs you',true));
      }else if(a.type==='cap'){
        const cap=h('input',{value:a.cap||'',placeholder:d.policy.allowlist[0]||'notify.send','aria-label':'Capability',list:'cc-caps'});
        const caps=h('datalist',{id:'cc-caps'},d.policy.allowlist.filter(x=>!x.endsWith('*')).map(x=>h('option',{value:x})));
        cap.oninput=()=>{a.cap=cap.value.trim();refreshSentence();};
        const wk=h('select',{'aria-label':'On which worker'},h('option',{value:'same',text:'The worker where it happened',selected:!a.worker||a.worker==='same'}),
          d.workers.map(w=>h('option',{value:w.name,text:w.name,selected:a.worker===w.name})));
        wk.onchange=()=>{a.worker=wk.value;};
        const args=h('textarea',{rows:'3',placeholder:'{"text": "…"}','aria-label':'Arguments (JSON)'});args.value=a.args?JSON.stringify(a.args,null,1):'';
        args.oninput=()=>{try{a.args=args.value.trim()?JSON.parse(args.value):{};args.setCustomValidity('');}catch{args.setCustomValidity('Not valid JSON');a.args='bad';}};
        box.append(caps,h('div',{class:'cc-row'},h('label',{class:'mg-field'},'Capability',cap),h('label',{class:'mg-field'},'On',wk)),
          h('label',{class:'mg-field'},'Arguments (fixed JSON: nothing from the session is put in them)',args),
          h('p',{class:'cc-help',text:d.policy.allowlist.length?'Allowed: '+d.policy.allowlist.join(', ')+(d.policy.allow_exec?' (code-running capabilities allowed)':' (capabilities that run code are not allowed)')+'. Only an administrator can save this.':'No capability is allowed yet. An administrator lists them under Settings > Claude Code first.'}));
      }else if(a.type==='inject'){
        box.append(text('text','Instructions for Claude','Before you stop, update the Rook task you claimed.',true),
          h('p',{class:'cc-help',text:event.value==='Stop'||event.value==='SubagentStop'?'On "finishes its turn" Claude is asked to carry on once with these instructions.':'Added to what Claude sees, marked as coming from your band\'s rules.'}));
      }else if(a.type==='block'){
        box.append(text('message','Message Claude sees','Blocked by a Rook rule.'));
      }else if(a.type==='statusline'){
        box.append(text('text','Status line','rook · {project}'),h('p',{class:'cc-help',text:'Shown under the prompt from this event on, unless you already have your own status line.'}));
      }
      return box;
    }
    function drawActions(){
      const allowed=ev(event.value).actions||[];
      actionsBox.replaceChildren(...r.actions.map(actionFields),
        h('div',{class:'mg-bar'},btn('Add an action','soft',()=>{r.actions.push({type:allowed.includes('post')?'post':allowed[0]});drawActions();}),
          h('p',{class:'cc-help',text:'You can use {worker}, {project}, {tool}, {event}, {prompt}, {message} and {minutes} in messages.'})));
      refreshSentence();
    }
    function drawTrigger(){
      const e=ev(event.value);
      fMatcher.hidden=!e.tool;fContains.hidden=!e.text;fMinutes.hidden=!e.minutes;
      for(const o of scope.options)o.disabled=e.group==='rook'&&(o.value==='project'||o.value==='agent');
      if(scope.selectedOptions[0]?.disabled){scope.value='all';drawTarget();}
    }
    function collect(){
      const t={event:event.value};
      const e=ev(event.value);
      if(e.tool&&matcher.value.trim())t.matcher=matcher.value.trim();
      if(e.text&&contains.value.trim())t.contains=contains.value.trim();
      if(e.minutes)t.minutes=Number(minutes.value)||0;
      const out={name:name.value.trim(),enabled:rule?rule.enabled:true,trigger:t,scope:{kind:scope.value,target:target?target.value.trim():''},actions:r.actions};
      if(rule)out.id=rule.id;
      return out;
    }
    let seq=0;
    async function refreshSentence(){
      const n=++seq;
      try{const x=await post({action:'save',rule:collect(),dry_run:true});if(n!==seq)return;
        sentence.textContent=x.summary;err.textContent='';
        risk.replaceChildren(h('span',{class:'cc-tag '+x.rule.risk,text:x.rule.risk}),' ',RISK_NOTE[x.rule.risk]||'');}
      catch(e){if(n!==seq)return;sentence.textContent='…';err.textContent=e.message;}
    }
    const risk=h('span',{class:'cc-help'});
    event.onchange=()=>{drawTrigger();drawActions();};
    scope.onchange=()=>{drawTarget();refreshSentence();};
    for(const x of [name,matcher,contains,minutes])x.oninput=refreshSentence;
    drawTarget();drawTrigger();
    const save=btn(rule?'Save':'Create rule','primary',async()=>{
      save.disabled=true;
      try{await post({action:'save',rule:collect(),rev:rule?rule.rev:undefined});dlg.close();await load();render();
        say((rule?'Saved. ':'Created. ')+'Push to workers to put it into effect.');}
      catch(e){err.textContent=e.message;}finally{save.disabled=false;}
    });
    const dlg=dialog(rule?'Edit rule':'New rule',h('div',{class:'cc-form'},
      h('label',{class:'mg-field'},'Name',name),
      h('h3',{class:'mg-label',text:'When'}),h('div',{class:'cc-row'},h('label',{class:'mg-field'},'This happens',event),fMatcher,fMinutes),fContains,
      h('h3',{class:'mg-label',text:'Where'}),h('div',{class:'cc-row'},h('label',{class:'mg-field'},'On',scope),targetBox),
      h('h3',{class:'mg-label',text:'Do'}),actionsBox,
      sentence,risk,err,h('div',{class:'mg-actions',style:'display:flex;gap:8px;justify-content:flex-end'},btn('Cancel','',()=>dlg.close()),save)));
    dlg.classList.add('cc-wide');
    drawActions();
  }

  // -- workers ---------------------------------------------------------------------------
  function state(w){
    if(!w.online)return ['','offline'];
    if(w.supported===false)return ['bad','cannot take rules (older worker, or no Claude Code)'];
    if(!w.push)return ['', 'not pushed yet'];
    if(w.push.skipped)return ['bad',w.push.error||'skipped'];
    if(!w.push.ok)return ['bad','last push failed: '+(w.push.error||'?')];
    return w.current?['ok','up to date · rev '+w.push.rev]:['stale','out of date: push again'];
  }
  function workersTab(body){
    body.append(h('p',{class:'mg-sub',text:'Each worker gets the rules that apply to it. Preview shows exactly what would change in each Claude Code settings file there before anything is written. Workers that cannot take rules (older builds, or no Claude Code) are left alone.'}));
    body.append(h('div',{class:'mg-bar'},btn('Push to all workers','primary',()=>pushAll()),h('span',{class:'mg-grow'})));
    const box=h('div',{class:'cc-workers'},h('div',{class:'cc-w head'},h('div',{text:'Worker'}),h('div',{class:'hide',text:'Status'}),h('div',{class:'hide',text:'Last push'}),h('div')));
    for(const w of d.workers){
      const [cls,label]=state(w);
      const can=w.online&&w.supported;
      box.append(h('div',{class:'cc-w'},
        h('div',null,h('b',{text:w.name}),h('small',{text:[w.band&&'band '+w.band,w.online?(w.rules??0)+' rule'+(w.rules===1?'':'s')+' apply':null].filter(Boolean).join(' · ')})),
        h('div',{class:'hide'},h('span',{class:'cc-dot '+cls}),h('span',{text:label})),
        h('div',{class:'hide'},h('span',{text:w.push&&w.push.at?ago(w.push.at):'never'}),w.push&&(w.push.installations||[]).length?h('small',{text:w.push.installations.map(i=>i.action).join(', ')}):null),
        h('div',{class:'acts'},
          btn('Preview','',()=>preview(w),{disabled:!can}),
          btn('Push','soft',()=>pushOne(w),{disabled:!can}),
          btn('Remove','danger',()=>removeFrom(w),{disabled:!can||!w.push||!w.push.ok,title:'Take Rook\'s hooks off this worker\'s Claude Code'}))));
    }
    if(!d.workers.length)box.append(h('div',{class:'mg-empty',text:'No workers online.'}));
    body.append(box);
  }
  async function preview(w,remove){
    const out=h('div',{class:'cc-form'},h('p',{class:'mg-sub',text:'Asking '+w.name+'…'}));
    const dlg=dialog((remove?'Remove from ':'What a push writes on ')+w.name,out);dlg.classList.add('cc-wide');
    try{
      const p=await get({view:'preview',worker:w.id,...(remove?{remove:'1'}:{})});
      if(!p.ok){out.replaceChildren(h('p',{class:'mg-error',text:p.error||'The worker could not preview.'}));return;}
      const kids=[h('p',{class:'mg-sub',text:(p.rules?p.rules+' rule'+(p.rules===1?'':'s')+' for this worker':'No rules for this worker')+(p.events&&p.events.length?'; hooks on '+p.events.join(', '):'')+(p.statusline?'; a status line':'')+'. Only Rook\'s own entries change; a backup of each file is kept on the worker.'})];
      if(p.note)kids.push(h('p',{class:'mg-warn',text:p.note}));
      for(const f of p.installations||[]){
        const verb={create:'will be created',update:'will change',remove:'Rook\'s entries will be taken out',unchanged:'already up to date',absent:'nothing to do',error:'cannot be changed'}[f.action]||f.action;
        kids.push(h('div',{class:'cc-file'},h('h3',{text:f.path}),h('span',{class:f.action==='error'?'mg-error':'mg-sub',text:verb+(f.error?': '+f.error:'')}),
          (f.notes||[]).map(n=>h('p',{class:'mg-warn',text:n})),f.diff?diffView(f.diff):null));
      }
      if(p.rules_file)kids.push(h('div',{class:'cc-file'},h('h3',{text:p.rules_file.path}),h('span',{class:'mg-sub',text:'Rook\'s rules file: '+p.rules_file.action}),p.rules_file.diff?h('details',null,h('summary',{class:'mg-label',text:'Show'}),diffView(p.rules_file.diff)):null));
      const go=btn(remove?'Remove now':'Push now',remove?'danger':'primary',async()=>{dlg.close();remove?await doRemove(w):await pushOne(w);});
      kids.push(h('div',{class:'mg-actions',style:'display:flex;gap:8px;justify-content:flex-end'},btn('Close','',()=>dlg.close()),go));
      out.replaceChildren(...kids);
    }catch(e){out.replaceChildren(h('p',{class:'mg-error',text:e.message}));}
  }
  function report(r){
    const bad=(r.results||[]).filter(x=>!x.ok&&!x.skipped),skipped=(r.results||[]).filter(x=>x.skipped);
    const good=(r.results||[]).length-bad.length-skipped.length;
    say(good+' worker'+(good===1?'':'s')+' updated'+(skipped.length?'; skipped '+skipped.map(x=>x.worker).join(', ')+' (cannot take rules)':'')+(bad.length?'; failed: '+bad.map(x=>x.worker+' ('+x.error+')').join('; '):'.'),!!bad.length);
  }
  async function pushAll(){
    if(!confirm('Push the rules to every online worker? Rook adds its hooks to each Claude Code installation there, next to the person\'s own settings.'))return;
    say('Pushing…');try{const r=await post({action:'push'});await load();render();report(r);}catch(e){say(e.message,true);}
  }
  async function pushOne(w){say('Pushing to '+w.name+'…');try{const r=await post({action:'push',worker:w.id});await load();render();report(r);}catch(e){say(e.message,true);}}
  function removeFrom(w){preview(w,true);}
  async function doRemove(w){say('Removing from '+w.name+'…');try{const r=await post({action:'remove',worker:w.id});await load();render();report(r);}catch(e){say(e.message,true);}}

  // -- activity ----------------------------------------------------------------------------
  function activityTab(body){
    body.append(h('p',{class:'mg-sub',text:'What the rules did on the hub: posts, notifications and capabilities (blocking and instructions happen inside Claude Code and are not listed here).'}));
    const box=h('div',{class:'cc-log'});
    for(const a of d.activity)box.append(h('div',null,h('small',{text:ago(a.ts)}),
      h('span',null,h('b',{text:a.rule_name||a.rule}),h('small',{text:' · '+a.worker+' · '+a.event})),
      h('span',{class:a.ok?'':'bad',text:a.action+': '+a.detail})));
    if(!d.activity.length)box.append(h('div',{class:'mg-empty',text:'Nothing yet.'}));
    body.append(box);
  }

  async function render(){
    const bar=h('div',{class:'mg-bar'},h('h1',{text:'Claude Code'}));
    const tabs=h('div',{class:'mg-tabs',role:'tablist'});
    for(const [id,label] of [['rules','Rules'],['workers','Workers'],['activity','Activity']])
      tabs.append(h('button',{type:'button',class:id===tab?'on':'',text:label,role:'tab','aria-selected':String(id===tab),onclick:()=>{tab=id;say('');render();}}));
    const refresh=btn('Refresh','',async()=>{refresh.disabled=true;try{await load();render();}catch(e){say(e.message,true);}finally{refresh.disabled=false;}});
    bar.append(h('span',{class:'mg-grow'}),refresh);
    const body=h('div',{class:'mg',style:'gap:12px'});
    page.replaceChildren(bar,tabs,status,body);
    if(tab==='rules')rulesTab(body);else if(tab==='workers')workersTab(body);else activityTab(body);
  }
  function tick(){
    if(!root.isConnected||document.hidden||document.querySelector('dialog[open]')||tab==='rules')return;
    load().then(()=>{if(!document.querySelector('dialog[open]'))render();}).catch(()=>{});
  }
  return {
    async activate(){
      try{await load();await render();}
      catch(e){page.replaceChildren(h('h1',{text:'Claude Code'}),h('p',{class:'mg-error',text:e.message}));}
      clearInterval(timer);timer=setInterval(tick,15000);
    },
    deactivate(){clearInterval(timer);timer=null;},
  };
}
