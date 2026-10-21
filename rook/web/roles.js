// Manage > Roles: the team members the house agent calls on in huddles
// (docs/design/huddles.md 4, 8). A role is a charter, what it watches, where it
// looks, the tools it may use and its own memory, kept in its home folder on
// its agent host. The list on the left, the selected role's form on the right
// (with its history); a preset (picked from one dropdown, grouped by category)
// fills the form with a starting role. Uses the
// role.* caps through /account/huddle/api (operator account), so every change
// is a new revision with you as its author.
const search=new URL(import.meta.url).search;
const {h,ago,useCss,btn,table,two,info,more}=await import('/account/bands/assets/manage.js'+search);
const {hapi}=await import('/account/huddle/assets/huddles.js'+search);

const BACKENDS=[['','The house agent\'s'],['claude-code','Claude Code'],['codex','Codex'],['opencode','OpenCode']];
const EFFORTS=['','low','medium','high','xhigh','max'];
const AUTONOMY={propose:'Proposes (you approve)',routine:'Does routine work (workspace, coding sessions on a branch)',act:'Acts (as routine, until H6 tiers)'};
const MEMORY={write:'Reads and writes',read:'Read only',off:'Off'};
const MODELS=['haiku','sonnet','opus'];
const lines=t=>t.split('\n').map(x=>x.trim()).filter(Boolean);
// Phone width: the table keeps the name and state only.
const CSS='@media(max-width:700px){#view-roles .rl-tbl .mg-table>div{min-width:0!important}'
  +'#view-roles .rl-tbl .mg-head,#view-roles .rl-tbl .mg-row{grid-template-columns:minmax(0,1fr) 70px!important}'
  +'#view-roles .rl-tbl .rl-c,#view-roles .rl-tbl .mg-head div:nth-child(2),#view-roles .rl-tbl .mg-head div:nth-child(3){display:none}}'
  +'#view-roles .rl-src{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr) minmax(0,1.4fr) 40px;gap:6px}'
  +'@media(max-width:700px){#view-roles .rl-src{grid-template-columns:minmax(0,1fr) minmax(0,1fr) 40px}#view-roles .rl-src .rl-args{grid-column:1/3}}'
  +'#view-roles .rl-pick{min-width:0;flex:1 1 220px;max-width:340px}#view-roles .rl-pick select{width:100%}'
  +'#view-roles .mg-bar{flex-wrap:nowrap}#view-roles .rl-idle{display:flex;align-items:center;gap:8px}'
  +'@media(max-width:900px){#view-roles .rl-panel-idle{display:none}}'
  +'@media(max-width:700px){#view-roles .rl-pick{max-width:none}}'
  +'#view-roles .rl-ws{display:flex;flex-direction:column;gap:2px;margin:6px 0}'
  +'#view-roles .rl-ws button{all:unset;cursor:pointer;display:flex;justify-content:space-between;gap:8px;padding:6px 4px;border-bottom:1px solid var(--line,#8883);font-family:var(--mono);font-size:13px;min-width:0}'
  +'#view-roles .rl-ws button span:first-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}'
  +'#view-roles .rl-ws button:focus-visible{outline:2px solid var(--accent,#48f)}'
  +'#view-roles .rl-file{white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:12.5px;max-height:60vh;overflow:auto;padding:8px;border:1px solid var(--line,#8883);border-radius:6px;margin:6px 0 0}';

// The preset dropdown: one <select>, its options grouped under their category's
// name (optgroup headings are not selectable), in the categories' and presets'
// sort order. Presets already saved stay listed, greyed, so a category reads whole.
export function presetSelect(groups,picked){
  return h('select',{'aria-label':'Add from presets'},
    h('option',{value:'',text:'From preset…',selected:!picked}),
    (groups||[]).map(c=>h('optgroup',{label:c.name},(c.presets||[]).map(p=>
      h('option',{value:p.id,text:p.name+(p.saved?' (added)':''),disabled:!!p.saved,selected:p.id===picked,title:p.description||''})))));
}
export function presetDescription(groups,id){
  for(const c of groups||[])for(const p of c.presets||[])if(p.id===id)return p.description||'';
  return '';
}

export async function mountRoles(root){
  useCss();
  if(!document.getElementById('roles-css'))document.head.append(h('style',{id:'roles-css',text:CSS}));
  let d=null,sel=null,hist=[],busy=false,picked='',voicesP=null;
  const status=h('p',{class:'mg-status',role:'status'});
  const bar=h('div',{class:'mg-bar'}),list=h('div'),panel=h('div',{class:'mg-box strong mg-side'});
  root.replaceChildren(h('div',{class:'mg'},status,h('div',{class:'mg-split'},h('div',{class:'mg-main'},bar,list),panel)));

  async function refresh(keep){
    d=await hapi('role_list');
    if(keep&&sel&&!sel.new)await show(sel.id);else{drawList();drawPanel();}
  }
  async function run(b,work){if(busy)return;busy=true;if(b)b.disabled=true;status.classList.remove('bad');
    try{await work();}catch(e){status.textContent=e.message;status.classList.add('bad');}finally{busy=false;if(b)b.disabled=false;}}

  function drawList(){
    const add=btn('+ New','primary',()=>{picked='';sel={new:true,doc:{id:'',name:'',description:'',category:'',charter:'',concerns:[],checklist:[],examples:[],seed_memory:[],sources:[],tools:{allow:['knowledge.read'],deny:[]},builtin:[],autonomy:'propose',memory:'write',backend:'',model:'',effort:'',persona:'',enabled:true}};hist=[];drawList();drawPanel();},{'aria-label':'New role',title:'New blank role'});
    const groups=d.preset_categories||[];
    const pick=presetSelect(groups,picked);
    pick.onchange=()=>{const id=pick.value;if(!id)return;
      run(pick,async()=>{const r=await hapi('role_presets',{id});picked=id;sel={new:true,doc:r.preset};hist=[];drawList();drawPanel();
        if(innerWidth<900)panel.scrollIntoView({behavior:'smooth',block:'start'});});};
    bar.replaceChildren(groups.length?h('div',{class:'rl-pick'},pick):h('span',{class:'mg-grow'}),add);
    const rows=(d.roles||[]).map(r=>{
      const row=h('div',{class:'mg-row pick'+(sel&&!sel.new&&sel.id===r.id?' sel':''),tabindex:'0',role:'button'},
        two(r.name||r.id,'@'+r.id),
        h('span',{class:'mg-dimtext rl-c',text:r.host||'not yet'}),
        h('span',{class:'mg-dimtext rl-c',text:r.autonomy==='act'?'acts':'proposes'}),
        h('span',{class:r.enabled?'mg-good':'mg-dimtext',text:r.enabled?'On':'Off'}));
      const open=()=>run(null,()=>show(r.id));row.onclick=open;row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};
      return row;});
    list.replaceChildren(h('div',{class:'mg-box rl-tbl'},table('minmax(0,1fr) 130px 100px 70px',['Role','Host','Autonomy','State'],
      rows.length?rows:[h('div',{class:'mg-empty',text:'No roles yet.'})],480)));
  }
  async function show(id){
    const [r,hh]=await Promise.all([hapi('role_get',{id}),hapi('role_history',{id})]);
    picked='';sel={id,doc:r.role};hist=Array.isArray(hh)?hh:(hh.items||[]);drawList();drawPanel();
    if(innerWidth<900)panel.scrollIntoView({behavior:'smooth',block:'start'});
  }

  function field(label,ctl,note){return h('label',{class:'mg-field'},note?h('span',{class:'mg-flabel'},label,info(note,'About '+label)):label,ctl);}
  function select(options,value){return h('select',null,options.map(([v,t])=>h('option',{value:v,text:t,selected:v===(value||'')})));}
  function area(value,rows,ph){const t=h('textarea',{rows:String(rows),placeholder:ph||''});t.value=value;return t;}

  // Its workspace (drafts, notes, working files), read-only: the list, then
  // one file. Loaded when opened, so the form stays fast.
  function workspaceBox(doc){
    const body=h('div'),box=h('details',null,h('summary',{text:'Workspace'}),body);
    const size=n=>n==null?'':n<1024?n+' B':(n/1024).toFixed(n<10240?1:0)+' kB';
    async function list(){
      body.replaceChildren(h('p',{class:'mg-sub',text:'Loading…'}));
      try{const r=await hapi('role_workspace',{id:doc.id});const files=r.files||[];
        body.replaceChildren(files.length?h('div',{class:'rl-ws'},files.map(f=>h('button',{type:'button',title:f.path,onclick:()=>open(f.path)},
          h('span',{text:f.path}),h('span',{class:'mg-sub',text:size(f.size)+(f.modified?' · '+ago(f.modified):'')})))):
          h('p',{class:'mg-sub',text:'Empty. The role keeps its drafts and notes here.'}));}
      catch(e){body.replaceChildren(h('p',{class:'mg-bad',text:e.message}));}
    }
    async function open(path){
      body.replaceChildren(h('p',{class:'mg-sub',text:'Loading…'}));
      try{const r=await hapi('role_workspace',{id:doc.id,path});
        body.replaceChildren(h('div',{class:'mg-actions',style:'justify-content:space-between'},
          btn('‹ Files','',list,{'aria-label':'Back to the file list'}),h('span',{class:'mg-mono',text:path,style:'overflow-wrap:anywhere'})),
          h('pre',{class:'rl-file',text:(r.content||'')+(r.truncated?'\n'+r.truncated:'')}));}
      catch(e){body.replaceChildren(btn('‹ Files','',list),h('p',{class:'mg-bad',text:e.message}));}
    }
    box.addEventListener('toggle',()=>{if(box.open&&!body.childNodes.length)list();});
    return box;
  }

  function drawPanel(){
    panel.classList.toggle('rl-panel-idle',!sel);
    if(!sel){panel.replaceChildren(h('div',{class:'mg-pad'},h('div',{class:'rl-idle'},h('span',{class:'mg-sub',text:'Pick a role.'}),
      info('A role that proposes reads with its tools and suggests actions; you approve each one in the huddle.','About roles'))));return;}
    const doc=sel.doc,isNew=!!sel.new,err=h('small',{class:'mg-error',role:'alert'});
    const mono={autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'};
    const id=h('input',{...mono,value:doc.id||'',placeholder:'qa',disabled:!isNew});
    const name=h('input',{value:doc.name||'',placeholder:'QA engineer',autocomplete:'off'});
    const description=h('input',{value:doc.description||'',placeholder:'One line: what it is for (the chair reads this).',autocomplete:'off',maxlength:'160'});
    const cats=d.preset_categories||[];
    const category=select([['','No category'],...cats.map(c=>[c.id,c.name])],doc.category);
    if(doc.category&&!cats.some(c=>c.id===doc.category))category.append(h('option',{value:doc.category,text:doc.category,selected:true}));
    const charter=area(doc.charter||'',doc.charter&&doc.charter.length>400?10:3,'What it owns, what it never does, how it decides, when it asks a person.');
    const concerns=area((doc.concerns||[]).join('\n'),4,'One per line: what it watches and raises without being asked.');
    const checklist=area((doc.checklist||[]).join('\n'),4,'One per line: what it works through on every turn.');
    const examples=area((doc.examples||[]).join('\n'),3,'One per line: questions it answers well.');
    const seed=area((doc.seed_memory||[]).join('\n'),3,'One per line: what it knows to start with.');
    // Where it looks before answering: a cap, optionally on one worker, with arguments.
    const srcRows=h('div',{style:'display:flex;flex-direction:column;gap:6px'});
    const addSrc=s=>{
      const cap=h('input',{...mono,value:s.cap||'',placeholder:'task.read','aria-label':'Cap'});
      const worker=h('input',{...mono,value:s.worker||'',placeholder:'any worker','aria-label':'Worker'});
      const args=h('input',{...mono,class:'rl-args',value:s.args&&Object.keys(s.args).length?JSON.stringify(s.args):'',placeholder:'{"action": "deck"}','aria-label':'Arguments (JSON)'});
      const row=h('div',{class:'rl-src'},cap,worker,args,btn('×','icon',()=>row.remove(),{'aria-label':'Remove this source'}));
      srcRows.append(row);};
    (doc.sources||[]).forEach(addSrc);
    const sources=h('div',{class:'mg-field'},h('span',{class:'mg-flabel'},'Sources',info('Where it looks before answering, as caps it calls (arguments as JSON). They must be within its tools.','About sources')),
      srcRows,h('div',null,btn('+ Source','',()=>addSrc({}),{title:'Add a source'})));
    const allow=area(((doc.tools||{}).allow||[]).join('\n'),3,'knowledge.read');
    const deny=area(((doc.tools||{}).deny||[]).join('\n'),2,'');
    const builtin=['read','search'].map(b=>h('input',{type:'checkbox',value:b,checked:(doc.builtin||[]).includes(b)}));
    const autonomy=select(Object.entries(AUTONOMY),doc.autonomy||'propose');
    const memory=select(Object.entries(MEMORY),doc.memory||'write');
    const backend=select(BACKENDS,doc.backend);
    const model=h('input',{...mono,value:doc.model||'',placeholder:'blank: the house agent\'s',list:'role-models'});
    const effort=select(EFFORTS.map(e=>[e,e||'Default']),doc.effort);
    const calls=h('input',{type:'number',min:1,max:100,value:doc.max_tool_calls||'',placeholder:'blank: its category\'s, else the hub\'s'});
    const persona=select([['','The house agent\'s persona'],...(d.personas||[]).map(p=>[p.id,p.name+' ('+p.id+')'])],doc.persona);
    if(doc.persona&&!(d.personas||[]).some(p=>p.id===doc.persona))persona.append(h('option',{value:doc.persona,text:doc.persona+' (missing)',selected:true}));
    const enabled=h('input',{type:'checkbox',checked:doc.enabled!==false});
    // The voice the Speak button reads its huddle messages in (the hub's voice engine's voices).
    const voice=h('select',null,h('option',{value:'',text:'Band default'}));
    if(doc.voice)voice.append(h('option',{value:doc.voice,text:doc.voice,selected:true}));
    voicesP=voicesP||hapi('voices').catch(()=>({available:false,voices:[]}));
    voicesP.then(v=>{
      const cat=(v.catalog&&v.catalog.length)?v.catalog:(v.voices||[]).map(x=>({id:x,label:x}));
      voice.replaceChildren(h('option',{value:'',text:'Band default'+(v.default?' ('+v.default+')':'')}),
        ...cat.map(c=>h('option',{value:c.id,text:c.label&&c.label!==c.id?c.label+' ('+c.id+')':c.id,selected:c.id===doc.voice})));
      if(doc.voice&&!cat.some(c=>c.id===doc.voice))voice.append(h('option',{value:doc.voice,text:doc.voice+' (not on the voice engine)',selected:true}));
      if(!v.available)voice.title='No voice engine on this hub now ('+(v.reason||'voice is off')+'): the browser reads messages aloud.';
    });

    function value(){
      const srcs=[...srcRows.children].map(r=>{const [cap,worker,args]=r.querySelectorAll('input');
        if(!cap.value.trim())return null;let a={};
        if(args.value.trim()){try{a=JSON.parse(args.value);}catch{throw Error('Arguments for '+cap.value.trim()+' are not valid JSON.');}}
        const s={cap:cap.value.trim(),args:a};if(worker.value.trim())s.worker=worker.value.trim();return s;}).filter(Boolean);
      return {id:id.value.trim().toLowerCase(),name:name.value.trim(),description:description.value.trim(),category:category.value,
        charter:charter.value.trim(),concerns:lines(concerns.value),checklist:lines(checklist.value),examples:lines(examples.value),seed_memory:lines(seed.value),sources:srcs,
        tools:{allow:lines(allow.value),deny:lines(deny.value)},builtin:builtin.filter(c=>c.checked).map(c=>c.value),
        autonomy:autonomy.value,memory:memory.value,backend:backend.value,model:model.value.trim(),effort:effort.value,
        persona:persona.value,enabled:enabled.checked,max_tool_calls:calls.value?Number(calls.value):0,voice:voice.value};
    }
    const save=btn(isNew?'Create':'Save','primary',()=>run(save,async()=>{err.textContent='';
      try{const role=value();const r=await hapi('role_set',{role,expect_rev:isNew?0:doc.rev});
        status.textContent=r.unchanged?'No changes.':(isNew?'Created @'+r.id+'.':'Saved (revision '+r.rev+').');
        sel={id:r.id};await refresh(true);}
      catch(e){err.textContent=e.message;}}));
    const del=()=>{if(!confirm('Delete role @'+doc.id+'? Its history stays, and its home folder on '+(doc.host||'its host')+' is left in place.'))return;
      run(null,async()=>{await hapi('role_delete',{id:doc.id});status.textContent='Deleted @'+doc.id+'.';sel=null;await refresh();});};
    const acts=isNew?null:more([{label:'Memory',href:'#memory?role='+encodeURIComponent(doc.id)},{label:'Delete…',danger:true,run:del}],'Role actions');
    const close=btn('×','icon',()=>{sel=null;picked='';drawList();drawPanel();},{'aria-label':'Close'});

    // Host: where its home folder is. Changed only by moving the folder.
    const hosts=d.hosts||[];
    const label=x=>x.name+' · '+(x.online?'online':'offline')+(Object.keys(x.harnesses||{}).length?' · '+Object.entries(x.harnesses).map(([n,i])=>n+(i&&i.logged_in===false?' (signed out)':'')).join(', '):'');
    const target=h('select',{'aria-label':'Move to'},hosts.filter(x=>x.name!==doc.host).map(x=>h('option',{value:x.name,text:label(x)})));
    const move=btn('Move','',()=>{if(!target.value)return;if(!confirm('Move @'+doc.id+' to '+target.value+'? Its home folder (memory and settings) is copied there and the old copy is set aside.'))return;
      run(move,async()=>{const r=await hapi('role_move',{id:doc.id,host:target.value});status.textContent=r.moved===false?'Already there.':'Moved to '+r.host+'.'+(r.warning?' '+r.warning:'');await refresh(true);});});
    const hostBox=isNew?null:h('div',{class:'mg-field'},'Host',
      h('div',{class:'mg-actions'},h('span',{class:'mg-mono',text:doc.host||'none yet',title:doc.host?'':'Its first answer creates its home on the house agent\'s host'}),
        doc.host&&!hosts.some(x=>x.name===doc.host)?h('small',{class:'mg-bad',text:'not on the band now'}):null),
      hosts.filter(x=>x.name!==doc.host).length?h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},target,move):null);

    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:isNew?'New role':(doc.name||doc.id)}),
        h('small',{text:isNew?'New':'@'+doc.id+' · rev '+doc.rev})),h('div',{class:'mg-actions',style:'flex-wrap:nowrap;gap:6px'},acts,close)),
      h('div',{class:'mg-pad'},
        h('div',{class:'mg-actions',style:'flex-wrap:wrap;gap:12px'},h('label',{class:'mg-field',style:'flex:1 1 120px'},'Id (@id)',id),h('label',{class:'mg-field',style:'flex:2 1 180px'},'Name',name)),
        field('Description',description),field('Category',category),
        h('label',{class:'mg-actions',title:'The chair may call on it'},enabled,'Enabled'),
        field('Charter',charter),field('Concerns',concerns),field('Checklist',checklist),sources,
        field('Tools it may call',allow,'One cap or pattern per line (task.read, info.*). It never gets shell, file editing or other roles.'),
        field('Never',deny,'Patterns refused even when allowed above.'),
        h('div',{class:'mg-field'},'Built-in file tools',h('div',{class:'mg-actions'},
          h('label',{class:'mg-actions'},builtin[0],'Read files'),h('label',{class:'mg-actions'},builtin[1],'Search files'))),
        field('Autonomy',autonomy),field('Memory',memory),
        h('details',null,h('summary',{text:'Examples & memory'}),h('div',{class:'mg-pad',style:'padding-left:0;padding-right:0'},
          field('Example questions',examples,'Shown to the chair, so it knows when to call on this role.'),
          field('Starting memory',seed,'Saved into its memory when its home is created (its first answer). Never passwords, keys or code.'))),
        h('details',null,h('summary',{text:'Model'}),h('div',{class:'mg-pad',style:'padding-left:0;padding-right:0'},
          field('Runs on',backend),field('Model',model),h('datalist',{id:'role-models'},MODELS.map(m=>h('option',{value:m}))),field('Effort',effort),field('Tool calls per turn',calls,'Blank: its category\'s (engineering 30, operations 24), else Tool calls per role turn in Settings.'),field('Persona (how it talks)',persona),field('Speaking voice',voice,'The voice its huddle messages are read aloud in (the Speak button). Band default: the voice engine\'s own.'))),
        hostBox,
        isNew?null:workspaceBox(doc),
        h('div',{class:'mg-actions end rl-save'},err,save),
        isNew?null:h('div',null,h('div',{class:'mg-label',style:'margin-bottom:8px',text:'History'}),
          hist.length?hist.slice(0,15).map(e=>h('div',{class:'mg-log'},h('span',{class:'mg-mono',text:e.doc?'rev '+e.rev:'deleted'}),
            h('span',{text:(e.note?e.note+' · ':'')+(e.actor||''),title:e.note||''}),h('span',{class:'mg-sub',style:'font-size:12px',text:ago(e.ts)}))):h('p',{class:'mg-sub',text:'No changes yet.'}))));
  }
  return {
    activate(){refresh(true).catch(e=>{list.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));panel.replaceChildren();});},
    deactivate(){}};
}
