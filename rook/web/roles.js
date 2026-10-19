// Manage > Roles: the team members the house agent calls on in huddles
// (docs/design/huddles.md 4, 8). A role is a charter, what it watches, where it
// looks, the tools it may use and its own memory, kept in its home folder on
// its agent host. The list on the left, the selected role's form on the right
// (with its history); presets fill the form with a starting role. Uses the
// role.* caps through /account/huddle/api (operator account), so every change
// is a new revision with you as its author.
const search=new URL(import.meta.url).search;
const {h,ago,useCss,btn,table,two,menu}=await import('/account/bands/assets/manage.js'+search);
const {hapi}=await import('/account/huddle/assets/huddles.js'+search);

const BACKENDS=[['','The house agent\'s'],['claude-code','Claude Code'],['codex','Codex'],['opencode','OpenCode']];
const EFFORTS=['','low','medium','high','xhigh','max'];
const AUTONOMY={propose:'Proposes: you approve each action',act:'Acts within its tools'};
const MEMORY={write:'Remembers (reads and writes)',read:'Reads what it remembers, adds nothing',off:'No memory'};
const MODELS=['haiku','sonnet','opus'];
const lines=t=>t.split('\n').map(x=>x.trim()).filter(Boolean);
// Phone width: the table keeps the name and state only.
const CSS='@media(max-width:700px){#view-roles .rl-tbl .mg-table>div{min-width:0!important}'
  +'#view-roles .rl-tbl .mg-head,#view-roles .rl-tbl .mg-row{grid-template-columns:minmax(0,1fr) 70px!important}'
  +'#view-roles .rl-tbl .rl-c,#view-roles .rl-tbl .mg-head div:nth-child(2),#view-roles .rl-tbl .mg-head div:nth-child(3){display:none}}'
  +'#view-roles .rl-src{display:grid;grid-template-columns:minmax(0,1.2fr) minmax(0,.8fr) minmax(0,1.4fr) 40px;gap:6px}'
  +'@media(max-width:700px){#view-roles .rl-src{grid-template-columns:minmax(0,1fr) minmax(0,1fr) 40px}#view-roles .rl-src .rl-args{grid-column:1/3}}';

export async function mountRoles(root){
  useCss();
  if(!document.getElementById('roles-css'))document.head.append(h('style',{id:'roles-css',text:CSS}));
  let d=null,sel=null,hist=[],busy=false;
  const status=h('p',{class:'mg-status',role:'status'}),summary=h('p',{class:'mg-sub'});
  const bar=h('div',{class:'mg-bar'}),list=h('div'),panel=h('div',{class:'mg-box strong mg-side'});
  root.replaceChildren(h('div',{class:'mg'},summary,status,h('div',{class:'mg-split'},h('div',{class:'mg-main'},bar,list),panel)));

  async function refresh(keep){
    d=await hapi('role_list');
    if(keep&&sel&&!sel.new)await show(sel.id);else{drawList();drawPanel();}
  }
  async function run(b,work){if(busy)return;busy=true;if(b)b.disabled=true;status.classList.remove('bad');
    try{await work();}catch(e){status.textContent=e.message;status.classList.add('bad');}finally{busy=false;if(b)b.disabled=false;}}

  function drawList(){
    summary.textContent='Roles are the team members the house agent calls on in huddles. Each has its own concerns, tools and memory, kept in its own folder on its agent host.';
    const add=btn('Add a role','primary',()=>{sel={new:true,doc:{id:'',name:'',charter:'',concerns:[],sources:[],tools:{allow:['knowledge.read'],deny:[]},builtin:[],autonomy:'propose',memory:'write',backend:'',model:'',effort:'',persona:'',enabled:true}};hist=[];drawList();drawPanel();});
    const presets=(d.presets||[]).filter(p=>!p.saved);
    const fromPreset=btn('Add from presets','',()=>menu(fromPreset,presets.map(p=>({label:p.name+' (@'+p.id+')',run:()=>{const {saved,...doc}=p;sel={new:true,doc};hist=[];drawList();drawPanel();}}))),{disabled:!presets.length,title:presets.length?'':'Every preset is saved already'});
    bar.replaceChildren(h('span',{class:'mg-grow'}),fromPreset,add);
    const rows=(d.roles||[]).map(r=>{
      const row=h('div',{class:'mg-row pick'+(sel&&!sel.new&&sel.id===r.id?' sel':''),tabindex:'0',role:'button'},
        two(r.name||r.id,'@'+r.id),
        h('span',{class:'mg-dimtext rl-c',text:r.host||'not yet'}),
        h('span',{class:'mg-dimtext rl-c',text:r.autonomy==='act'?'acts':'proposes'}),
        h('span',{class:r.enabled?'mg-good':'mg-dimtext',text:r.enabled?'On':'Off'}));
      const open=()=>run(null,()=>show(r.id));row.onclick=open;row.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}};
      return row;});
    list.replaceChildren(h('div',{class:'mg-box rl-tbl'},table('minmax(0,1fr) 130px 100px 70px',['Role','Host','Autonomy','State'],
      rows.length?rows:[h('div',{class:'mg-empty',text:'No roles yet. Add one, or start from a preset: a project manager, a QA engineer and a lead developer.'})],480)));
  }
  async function show(id){
    const [r,hh]=await Promise.all([hapi('role_get',{id}),hapi('role_history',{id})]);
    sel={id,doc:r.role};hist=Array.isArray(hh)?hh:(hh.items||[]);drawList();drawPanel();
    if(innerWidth<900)panel.scrollIntoView({behavior:'smooth',block:'start'});
  }

  function field(label,ctl,note){return h('label',{class:'mg-field'},label,ctl,note?h('small',{class:'mg-sub',style:'white-space:normal',text:note}):null);}
  function select(options,value){return h('select',null,options.map(([v,t])=>h('option',{value:v,text:t,selected:v===(value||'')})));}
  function area(value,rows,ph){const t=h('textarea',{rows:String(rows),placeholder:ph||''});t.value=value;return t;}

  function drawPanel(){
    if(!sel){panel.replaceChildren(h('div',{class:'mg-pad'},h('p',{class:'mg-sub',style:'white-space:normal',text:'Pick a role to change it, or add one. A role that only proposes reads with its tools and suggests actions; you approve each one in the huddle.'})));return;}
    const doc=sel.doc,isNew=!!sel.new,err=h('small',{class:'mg-error',role:'alert'});
    const mono={autocomplete:'off',spellcheck:'false',style:'font-family:var(--mono)'};
    const id=h('input',{...mono,value:doc.id||'',placeholder:'qa',disabled:!isNew});
    const name=h('input',{value:doc.name||'',placeholder:'QA engineer',autocomplete:'off'});
    const charter=area(doc.charter||'',3,'One paragraph: what this role is for.');
    const concerns=area((doc.concerns||[]).join('\n'),4,'One per line: what it watches and raises without being asked.');
    // Where it looks before answering: a cap, optionally on one worker, with arguments.
    const srcRows=h('div',{style:'display:flex;flex-direction:column;gap:6px'});
    const addSrc=s=>{
      const cap=h('input',{...mono,value:s.cap||'',placeholder:'task.read','aria-label':'Cap'});
      const worker=h('input',{...mono,value:s.worker||'',placeholder:'any worker','aria-label':'Worker'});
      const args=h('input',{...mono,class:'rl-args',value:s.args&&Object.keys(s.args).length?JSON.stringify(s.args):'',placeholder:'{"action": "deck"}','aria-label':'Arguments (JSON)'});
      const row=h('div',{class:'rl-src'},cap,worker,args,btn('×','icon',()=>row.remove(),{'aria-label':'Remove this source'}));
      srcRows.append(row);};
    (doc.sources||[]).forEach(addSrc);
    const sources=h('div',{class:'mg-field'},'Sources',srcRows,h('div',null,btn('Add a source','',()=>addSrc({}))),
      h('small',{class:'mg-sub',style:'white-space:normal',text:'Where it looks before answering, as caps it calls (arguments as JSON). They must be within its tools.'}));
    const allow=area(((doc.tools||{}).allow||[]).join('\n'),3,'knowledge.read');
    const deny=area(((doc.tools||{}).deny||[]).join('\n'),2,'');
    const builtin=['read','search'].map(b=>h('input',{type:'checkbox',value:b,checked:(doc.builtin||[]).includes(b)}));
    const autonomy=select(Object.entries(AUTONOMY),doc.autonomy||'propose');
    const memory=select(Object.entries(MEMORY),doc.memory||'write');
    const backend=select(BACKENDS,doc.backend);
    const model=h('input',{...mono,value:doc.model||'',placeholder:'blank: the house agent\'s',list:'role-models'});
    const effort=select(EFFORTS.map(e=>[e,e||'Default']),doc.effort);
    const persona=select([['','The house agent\'s persona'],...(d.personas||[]).map(p=>[p.id,p.name+' ('+p.id+')'])],doc.persona);
    if(doc.persona&&!(d.personas||[]).some(p=>p.id===doc.persona))persona.append(h('option',{value:doc.persona,text:doc.persona+' (missing)',selected:true}));
    const enabled=h('input',{type:'checkbox',checked:doc.enabled!==false});

    function value(){
      const srcs=[...srcRows.children].map(r=>{const [cap,worker,args]=r.querySelectorAll('input');
        if(!cap.value.trim())return null;let a={};
        if(args.value.trim()){try{a=JSON.parse(args.value);}catch{throw Error('Arguments for '+cap.value.trim()+' are not valid JSON.');}}
        const s={cap:cap.value.trim(),args:a};if(worker.value.trim())s.worker=worker.value.trim();return s;}).filter(Boolean);
      return {id:id.value.trim().toLowerCase(),name:name.value.trim(),charter:charter.value.trim(),concerns:lines(concerns.value),sources:srcs,
        tools:{allow:lines(allow.value),deny:lines(deny.value)},builtin:builtin.filter(c=>c.checked).map(c=>c.value),
        autonomy:autonomy.value,memory:memory.value,backend:backend.value,model:model.value.trim(),effort:effort.value,
        persona:persona.value,enabled:enabled.checked};
    }
    const save=btn(isNew?'Create':'Save','primary',()=>run(save,async()=>{err.textContent='';
      try{const role=value();const r=await hapi('role_set',{role,expect_rev:isNew?0:doc.rev});
        status.textContent=r.unchanged?'No changes.':(isNew?'Created @'+r.id+'.':'Saved (revision '+r.rev+').');
        sel={id:r.id};await refresh(true);}
      catch(e){err.textContent=e.message;}}));
    const del=isNew?null:btn('Delete','danger',()=>{if(!confirm('Delete role @'+doc.id+'? Its history stays, and its home folder on '+(doc.host||'its host')+' is left in place.'))return;
      run(del,async()=>{await hapi('role_delete',{id:doc.id});status.textContent='Deleted @'+doc.id+'.';sel=null;await refresh();});});
    const close=btn('×','icon',()=>{sel=null;drawList();drawPanel();},{'aria-label':'Close'});

    // Host: where its home folder is. Changed only by moving the folder.
    const hosts=d.hosts||[];
    const label=x=>x.name+' · '+(x.online?'online':'offline')+(Object.keys(x.harnesses||{}).length?' · '+Object.entries(x.harnesses).map(([n,i])=>n+(i&&i.logged_in===false?' (signed out)':'')).join(', '):'');
    const target=h('select',{'aria-label':'Move to'},hosts.filter(x=>x.name!==doc.host).map(x=>h('option',{value:x.name,text:label(x)})));
    const move=btn('Move','',()=>{if(!target.value)return;if(!confirm('Move @'+doc.id+' to '+target.value+'? Its home folder (memory and settings) is copied there and the old copy is set aside.'))return;
      run(move,async()=>{const r=await hapi('role_move',{id:doc.id,host:target.value});status.textContent=r.moved===false?'Already there.':'Moved to '+r.host+'.'+(r.warning?' '+r.warning:'');await refresh(true);});});
    const hostBox=isNew?null:h('div',{class:'mg-field'},'Host',
      h('div',{class:'mg-actions'},h('span',{class:'mg-mono',text:doc.host||'none yet: its first answer creates its home on the house agent\'s host'}),
        doc.host&&!hosts.some(x=>x.name===doc.host)?h('small',{class:'mg-bad',text:'not on the band now'}):null),
      hosts.filter(x=>x.name!==doc.host).length?h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},target,move):null);

    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:isNew?'New role':(doc.name||doc.id)}),
        h('small',{text:isNew?'Saved as a role on the hub':'@'+doc.id+' · revision '+doc.rev})),close),
      h('div',{class:'mg-pad'},
        h('div',{class:'mg-actions',style:'flex-wrap:wrap;gap:12px'},h('label',{class:'mg-field',style:'flex:1 1 120px'},'Id (people write @id)',id),h('label',{class:'mg-field',style:'flex:2 1 180px'},'Name',name)),
        h('label',{class:'mg-actions'},enabled,'Enabled: the chair may call on it'),
        field('Charter',charter),field('Concerns',concerns),sources,
        field('Tools it may call',allow,'One cap or pattern per line (task.read, info.*). It never gets shell, file editing or other roles.'),
        field('Never',deny,'Patterns refused even when allowed above.'),
        h('div',{class:'mg-field'},'Built-in file tools',h('div',{class:'mg-actions'},
          h('label',{class:'mg-actions'},builtin[0],'Read files'),h('label',{class:'mg-actions'},builtin[1],'Search files'))),
        field('Autonomy',autonomy),field('Memory',memory),
        h('details',null,h('summary',{text:'Model'}),h('div',{class:'mg-pad',style:'padding-left:0;padding-right:0'},
          field('Runs on',backend),field('Model',model),h('datalist',{id:'role-models'},MODELS.map(m=>h('option',{value:m}))),field('Effort',effort),field('Persona (how it talks)',persona))),
        hostBox,
        isNew?null:h('div',{class:'mg-actions'},h('a',{href:'#memory?role='+encodeURIComponent(doc.id),text:'What it remembers'})),
        h('div',{class:'mg-actions end'},err,del,save),
        isNew?null:h('div',null,h('div',{class:'mg-label',style:'margin-bottom:8px',text:'History'}),
          hist.length?hist.slice(0,15).map(e=>h('div',{class:'mg-log'},h('span',{class:'mg-mono',text:e.doc?'rev '+e.rev:'deleted'}),
            h('span',{text:(e.note?e.note+' · ':'')+(e.actor||''),title:e.note||''}),h('span',{class:'mg-sub',style:'font-size:12px',text:ago(e.ts)}))):h('p',{class:'mg-sub',text:'No changes yet.'}))));
  }
  return {
    activate(){refresh(true).catch(e=>{list.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));panel.replaceChildren();});},
    deactivate(){}};
}
