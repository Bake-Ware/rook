// Jobs: scheduled, branching work the hub runs (docs/design/jobs.md).
// Everything goes through /account/jobs/api, which runs job.read / job.write
// as the signed-in account. Stored text is rendered as text, never as HTML.
import {h,ago,useCss,btn,chips,table,two,dialog,menu} from '/account/bands/assets/manage.js';

const API='/account/jobs/api';
const FINAL=['success','failure','hang','interrupted','dropped','blocked','cancelled'];
const BAD=['failure','hang','interrupted','blocked'];
const STATES=['queued','due','running',...FINAL];

// ---- api -------------------------------------------------------------------
// A signed-out fetch answers JSON 401; anything that is not JSON (a proxy
// page, an old hub's login redirect) is never parsed as JSON.
async function readJson(r){
  if(!(r.headers.get('content-type')||'').includes('application/json')){
    throw Object.assign(Error(r.status===401||r.redirected?'Sign in to use Jobs.':'Jobs are unavailable (HTTP '+r.status+').'),{status:r.status});
  }
  const d=await r.json();
  if(!r.ok||d.error||d.ok===false)throw Object.assign(Error(d.error||'Request failed.'),{status:r.status,code:d.code,errors:d.errors});
  return d;
}
export function client(fetcher=fetch){
  const s={csrf:'',tz:'UTC',admin:false,principal:''};
  s.boot=async()=>{const d=await readJson(await fetcher(API,{headers:{Accept:'application/json'}}));s.csrf=d.csrf;s.tz=d.timezone||'UTC';s.admin=!!d.admin;s.principal=d.principal||'';return s;};
  s.call=async(action,{id=null,query='',data=null}={})=>{
    if(!s.csrf)await s.boot();
    const r=await fetcher(API,{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},body:JSON.stringify({csrf:s.csrf,action,id,query,data})});
    return (await readJson(r)).result;
  };
  return s;
}

// ---- time ------------------------------------------------------------------
function safeZone(tz){try{new Intl.DateTimeFormat(undefined,{timeZone:tz});return tz;}catch{return 'UTC';}}
export function fmt(iso,tz,withZone=true){
  if(!iso)return '—';const t=Date.parse(iso);if(isNaN(t))return String(iso);
  return new Intl.DateTimeFormat(undefined,{timeZone:safeZone(tz),month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',hourCycle:'h23',...(withZone?{timeZoneName:'short'}:{})}).format(t);
}
const secs=iso=>iso?Date.parse(iso)/1000:0;
export function dur(a,b){
  if(!a)return '—';const s=Math.max(0,Math.round(((b?Date.parse(b):Date.now())-Date.parse(a))/1000));
  return s<60?s+'s':s<3600?Math.floor(s/60)+'m '+(s%60)+'s':Math.floor(s/3600)+'h '+Math.floor(s%3600/60)+'m';
}

// ---- cron in plain English -----------------------------------------------------
const DOW=['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const MON=['','January','February','March','April','May','June','July','August','September','October','November','December'];
const MACRO={'@hourly':'0 * * * *','@daily':'0 0 * * *','@midnight':'0 0 * * *','@weekly':'0 0 * * 0','@monthly':'0 0 1 * *','@yearly':'0 0 1 1 *','@annually':'0 0 1 1 *'};
const NAMES={dow:['sun','mon','tue','wed','thu','fri','sat'],mon:['jan','feb','mar','apr','may','jun','jul','aug','sep','oct','nov','dec']};
const pad=n=>String(n).padStart(2,'0');
function num(tok,kind){const t=tok.toLowerCase();if(kind==='dow'&&NAMES.dow.includes(t))return NAMES.dow.indexOf(t);if(kind==='mon'&&NAMES.mon.includes(t))return NAMES.mon.indexOf(t)+1;return /^\d+$/.test(t)?+t:NaN;}
function name(n,kind){return kind==='dow'?DOW[n]:kind==='mon'?MON[n]:String(n);}
function joinWords(xs){return xs.length<2?xs.join(''):xs.slice(0,-1).join(', ')+' and '+xs[xs.length-1];}
function listText(f,kind){
  return joinWords(f.split(',').map(part=>{
    const [range,step]=part.split('/');
    const [a,b]=range.split('-');
    const base=range==='*'?'':b!==undefined?name(num(a,kind),kind)+' through '+name(num(b,kind),kind):name(num(a,kind),kind);
    return step?'every '+step+(kind==='dow'?' days':kind==='mon'?' months':'')+(base?' from '+base:''):base;
  }));
}
export function describeCron(expr){
  let e=String(expr||'').trim();e=MACRO[e.toLowerCase()]||e;
  const f=e.split(/\s+/);if(f.length!==5)return 'Not a 5-field cron expression.';
  const [mi,hr,dom,mon,dow]=f;
  const single=x=>/^\d+$/.test(x),every=x=>(x.match(/^\*\/(\d+)$/)||[])[1];
  let when;
  if(mi==='*'&&hr==='*')when='every minute';
  else if(every(mi)&&hr==='*')when='every '+every(mi)+' minutes';
  else if(single(mi)&&hr==='*')when=+mi===0?'every hour, on the hour':'every hour at minute '+mi;
  else if(single(mi)&&every(hr))when='every '+every(hr)+' hours'+(+mi===0?', on the hour':' at minute '+mi);
  else if(single(mi)&&/^\d+(,\d+)*$/.test(hr))when='at '+joinWords(hr.split(',').map(x=>pad(x)+':'+pad(mi)));
  else if(every(mi))when='every '+every(mi)+' minutes during hour '+listText(hr,'hr');
  else when='at minute '+listText(mi,'mi')+(hr==='*'?' of every hour':' past hour '+listText(hr,'hr'));
  const days=[];
  if(dom!=='*'&&dow!=='*')days.push('on day '+listText(dom,'dom')+' of the month or on '+listText(dow,'dow'));
  else if(dom!=='*')days.push('on day '+listText(dom,'dom')+' of the month');
  else if(dow!=='*')days.push('on '+listText(dow,'dow'));
  else if(/^at /.test(when))days.push('every day');
  if(mon!=='*')days.push('in '+listText(mon,'mon'));
  const s=[when,...days].join(' ');
  return s.charAt(0).toUpperCase()+s.slice(1)+'.';
}
// Builder: the plain choices a schedule usually needs, as a cron string.
export function buildCron(o){
  const [H,M]=String(o.time||'00:00').split(':').map(x=>parseInt(x,10)||0);
  const n=Math.max(1,parseInt(o.n,10)||1);
  if(o.mode==='minutes')return '*/'+n+' * * * *';
  if(o.mode==='hours')return (parseInt(o.minute,10)||0)+' */'+n+' * * *';
  if(o.mode==='daily')return M+' '+H+' * * *';
  if(o.mode==='weekly')return M+' '+H+' * * '+((o.days&&o.days.length?o.days:[1]).join(','));
  if(o.mode==='monthly')return M+' '+H+' '+Math.min(31,Math.max(1,parseInt(o.dom,10)||1))+' * *';
  return '';
}

// ---- page ------------------------------------------------------------------
const TEMPLATE={name:'new-job',description:'',enabled:true,
  triggers:[{kind:'cron',expr:'0 3 * * *'}],entry:'main',
  steps:{main:{kind:'cap',worker:'rook',cap:'hub.info',timeout:'5m'}}};
const TABS=[['overview','Overview'],['jobs','Jobs'],['editor','Editor'],['runs','Runs'],['guardrails','Guardrails'],['settings','Settings']];

export async function mountJobs(root){
  useCss();
  if(!document.querySelector('link[data-jobs]'))document.head.append(h('link',{rel:'stylesheet',href:'/account/jobs/assets/jobs.css'+new URL(import.meta.url).search,'data-jobs':'1'}));
  const api=client();
  let tab='overview',timer=null,active=false;
  const tabs=h('div',{class:'mg-tabs jb-tabs',role:'tablist'});
  const notice=h('p',{class:'mg-status bad',role:'alert'});
  const body=h('div',{class:'jb-body'});
  root.replaceChildren(h('div',{class:'mg jb'},tabs,notice,body));
  const fail=e=>{notice.textContent=e.message;};
  const clear=()=>{notice.textContent='';};
  // Shared between tabs.
  const ed={id:null,revision:null,text:JSON.stringify(TEMPLATE,null,2)};
  const runFilter={job:'',state:'',from:'',to:'',open:null};

  function drawTabs(){
    tabs.replaceChildren(...TABS.map(([id,label])=>h('button',{type:'button',role:'tab',class:id===tab?'on':'','aria-selected':String(id===tab),text:label,onclick:()=>show(id)})));
  }
  async function show(id){
    tab=id;clear();drawTabs();clearInterval(timer);timer=null;
    const view={overview,jobs:jobsTab,editor,runs,guardrails,settings}[tab];
    try{await view();}catch(e){fail(e);}
    if(tab==='overview'&&active)timer=setInterval(()=>{if(tab==='overview'&&!document.hidden)overview().catch(fail);},15000);
  }
  const openJob=async ref=>{const j=await api.call('get',{id:ref});ed.id=j.id;ed.revision=j.revision;ed.text=JSON.stringify(j.definition,null,2);show('editor');};
  const openRuns=(job,open)=>{runFilter.job=job||'';runFilter.open=open||null;show('runs');};

  // ---- overview ----
  async function overview(){
    const weekAgo=new Date(Date.now()-7*86400e3).toISOString();
    const [list,running,queued,next,bad,week]=await Promise.all([
      api.call('list',{data:{limit:500}}),
      api.call('runs',{data:{states:['running'],limit:50}}),
      api.call('runs',{data:{states:['queued','due'],limit:500}}),
      api.call('next',{data:{count:8}}),
      api.call('runs',{data:{states:BAD,limit:8}}),
      api.call('runs',{data:{since:weekAgo,limit:500}})]);
    const tz=api.tz;
    const done=week.runs.filter(r=>FINAL.includes(r.state)&&r.state!=='cancelled'&&r.state!=='dropped');
    const ok=done.filter(r=>r.state==='success').length;
    const blocked=list.jobs.filter(j=>j.paused_reason||(j.last_run&&j.last_run.state==='blocked'));
    const upcoming=next.upcoming.slice().sort((a,b)=>secs(a.at)-secs(b.at)).slice(0,6);
    const item=(main,sub,cls,onclick)=>h(onclick?'button':'div',{type:onclick?'button':null,class:'jb-item'+(onclick?' link':''),onclick},h('span',{text:main,class:cls||''}),h('small',{text:sub}));
    const card=(label,big,cls,items,empty)=>h('section',{class:'mg-box jb-card'},
      h('div',{class:'jb-card-head'},h('span',{class:'mg-label',text:label}),h('b',{class:'jb-big '+(cls||''),text:String(big)})),
      h('div',{class:'jb-items'},items.length?items:h('small',{class:'mg-sub',text:empty})));
    body.replaceChildren(h('div',{class:'jb-cards'},
      card('Running now',running.runs.length,running.runs.length?'mg-warn':'',
        running.runs.slice(0,6).map(r=>item(r.job,'started '+fmt(r.started,tz)+' · '+dur(r.started),'',()=>openRuns(r.job_id,r.id))),'Nothing running.'),
      card('Next runs',upcoming.length,'',upcoming.map(u=>item(u.job,fmt(u.at,tz)+' · '+u.kind,'',()=>openJob(u.job_id))),'Nothing scheduled.'),
      card('Recent failures',bad.runs.length,bad.runs.length?'mg-bad':'',
        bad.runs.slice(0,6).map(r=>item(r.job,r.state+' · '+fmt(r.finished||r.created,tz),'mg-bad',()=>openRuns(r.job_id,r.id))),'No failures.'),
      card('Blocked',blocked.length,blocked.length?'mg-bad':'',
        blocked.map(j=>item(j.name,j.paused_reason?'paused: '+j.paused_reason:'last run blocked by a guardrail','mg-bad',()=>openJob(j.id))),'No blocked jobs.'),
      card('Success, 7 days',done.length?Math.round(100*ok/done.length)+'%':'—',done.length&&ok<done.length?'mg-warn':'mg-good',
        [item(ok+' of '+done.length+' finished runs','since '+fmt(weekAgo,tz))],''),
      card('Queue',queued.runs.length,'',
        Object.entries(queued.runs.reduce((m,r)=>(m[r.job]=(m[r.job]||0)+1,m),{})).slice(0,6).map(([n,c])=>item(n,c+' waiting')),'Nothing waiting.')));
  }

  // ---- jobs list ----
  let jobQuery='',jobChip='all';
  async function jobsTab(){
    const list=await api.call('list',{data:{limit:500}});
    const search=h('input',{type:'search',class:'mg-grow',placeholder:'Filter jobs','aria-label':'Filter jobs',value:jobQuery,oninput:()=>{jobQuery=search.value.trim().toLowerCase();draw();}});
    const chipHost=h('span',{class:'mg-actions'});
    const host=h('div',{class:'mg-box'});
    body.replaceChildren(h('div',{class:'mg-bar'},search,chipHost,btn('New job','primary',()=>{ed.id=null;ed.revision=null;ed.text=JSON.stringify(TEMPLATE,null,2);show('editor');})),host);
    const act=async(work)=>{clear();try{await work();await jobsTab();}catch(e){fail(e);}};
    function draw(){
      const off=list.jobs.filter(j=>!j.enabled).length;
      chipHost.replaceChildren(...chips([['all','All '+list.jobs.length],['on','Enabled '+(list.jobs.length-off)],['off','Disabled '+off]],jobChip,id=>{jobChip=id;draw();}));
      const shown=list.jobs.filter(j=>(jobChip==='all'||(jobChip==='on')===j.enabled)&&(!jobQuery||(j.name+' '+(j.description||'')).toLowerCase().includes(jobQuery)));
      const rows=shown.map(j=>{
        const toggle=h('input',{type:'checkbox',checked:j.enabled,'aria-label':(j.enabled?'Disable ':'Enable ')+j.name,onchange:()=>act(()=>api.call(j.enabled?'disable':'enable',{id:j.id}))});
        const more=btn('⋯','icon',()=>menu(more,[
          {label:'Edit',run:()=>openJob(j.id).catch(fail)},
          {label:'Runs',run:()=>openRuns(j.id)},
          {label:'Delete…',danger:true,run:()=>confirmDelete(j)}]),{'aria-label':'More for '+j.name});
        const last=j.last_run;
        return h('div',{class:'mg-row'},
          h('button',{type:'button',class:'jb-plain',onclick:()=>openJob(j.id).catch(fail)},two(j.name,j.paused_reason?'paused: '+j.paused_reason:(j.description||''),true)),
          toggle,
          h('span',{class:'mg-mono jb-ellipsis',title:j.triggers.join('\n'),text:j.triggers.join(' · ')||'manual'}),
          h('span',{class:'mg-dimtext',text:j.next?fmt(j.next,api.tz):'—'}),
          last?h('button',{type:'button',class:'jb-plain jb-state '+last.state,text:last.state,title:fmt(last.finished,api.tz),onclick:()=>openRuns(j.id,last.id)}):h('span',{class:'mg-sub',text:'never run'}),
          h('span',{class:'mg-actions'},btn('Run','',()=>act(()=>api.call('run',{id:j.id})),{disabled:!j.enabled,title:j.enabled?'Run now':'Enable the job to run it'}),more));
      });
      host.replaceChildren(table('minmax(0,1.6fr) 44px minmax(0,1.4fr) 150px 110px 112px',['Job','On','Triggers','Next run','Last result',''],
        rows.length?rows:[h('div',{class:'mg-empty',text:list.jobs.length?'Nothing matches.':'No jobs yet.'})],820));
    }
    function confirmDelete(j){
      const err=h('small',{class:'mg-error',role:'alert'});
      const go=btn('Delete '+j.name,'danger',async()=>{go.disabled=true;try{await api.call('delete',{id:j.id});d.close();if(ed.id===j.id){ed.id=null;ed.revision=null;}await jobsTab();}catch(e){err.textContent=e.message;go.disabled=false;}});
      const d=dialog('Delete job',h('p',{text:'Delete '+j.name+' and its run history? Running runs are cancelled.'}),err,h('div',{class:'mg-actions end'},go));
    }
    draw();
  }

  // ---- editor ----
  async function editor(){
    const text=h('textarea',{class:'jb-json',spellcheck:'false','aria-label':'Job JSON',autocomplete:'off'});text.value=ed.text;
    const problems=h('div',{class:'jb-problems',role:'alert'});
    const title=h('span',{class:'mg-mono',text:ed.id?ed.id+' · revision '+ed.revision:'new job (unsaved)'});
    text.addEventListener('input',()=>{ed.text=text.value;syncTriggers();});
    text.addEventListener('keydown',e=>{if(e.key==='Tab'&&!e.shiftKey){e.preventDefault();text.setRangeText('  ',text.selectionStart,text.selectionEnd,'end');ed.text=text.value;}
      if((e.ctrlKey||e.metaKey)&&e.key==='s'){e.preventDefault();save();}});
    const parse=()=>{try{const v=JSON.parse(text.value);if(!v||typeof v!=='object'||Array.isArray(v))throw Error('The job must be a JSON object.');return v;}catch(e){showProblems([e.message],[]);return null;}};
    function showProblems(errors,warnings,ok){
      problems.replaceChildren(...errors.map(x=>h('div',{class:'mg-error',text:x})),...warnings.map(x=>h('div',{class:'mg-warn',text:'warning: '+x})),ok?h('div',{class:'mg-good',text:ok}):null);
    }
    async function validate(){const doc=parse();if(!doc)return false;
      try{const v=await api.call('validate',{data:doc});showProblems(v.errors||[],v.warnings||[],v.valid?'Valid.':'');return v.valid;}catch(e){showProblems(e.errors||[e.message],[]);return false;}}
    async function save(){const doc=parse();if(!doc)return;
      try{
        const j=ed.id?await api.call('update',{id:ed.id,data:{...doc,revision:ed.revision}}):await api.call('create',{data:doc});
        ed.id=j.id;ed.revision=j.revision;ed.text=text.value=JSON.stringify(j.definition,null,2);
        title.textContent=j.id+' · revision '+j.revision;runNow.disabled=false;showProblems([],j.warnings||[],'Saved.');syncTriggers();
      }catch(e){showProblems(e.errors||[e.message],[]);}}
    const format=()=>{const doc=parse();if(doc){ed.text=text.value=JSON.stringify(doc,null,2);showProblems([],[]);}};
    const schema=async()=>{try{const s=await api.call('describe_schema');dialog('Job schema',h('pre',{class:'jb-schema',text:JSON.stringify(s,null,2),tabindex:'0'}));}catch(e){fail(e);}};
    const runNow=btn('Run now','',async()=>{try{await api.call('run',{id:ed.id});openRuns(ed.id);}catch(e){showProblems([e.message],[]);}},{disabled:!ed.id});
    // Cron helper.
    const trigSel=h('select',{'aria-label':'Trigger',onchange:()=>pickTrigger()});
    const expr=h('input',{class:'mg-mono mg-grow',placeholder:'0 3 * * *','aria-label':'Cron expression',oninput:()=>preview()});
    const tz=h('input',{class:'mg-mono',placeholder:api.tz,'aria-label':'Time zone (blank: hub zone)',list:'jb-zones',oninput:()=>preview()});
    const zones=h('datalist',{id:'jb-zones'},(Intl.supportedValuesOf?Intl.supportedValuesOf('timeZone'):[]).map(z=>h('option',{value:z})));
    const reads=h('p',{class:'jb-reads'}),fires=h('div',{class:'jb-fires'});
    let triggers=[],pending=0;
    function syncTriggers(){
      let doc;try{doc=JSON.parse(text.value);}catch{return;}
      triggers=Array.isArray(doc&&doc.triggers)?doc.triggers:[];
      const keep=trigSel.value;
      trigSel.replaceChildren(...triggers.map((t,i)=>h('option',{value:String(i),text:'#'+i+' '+(t.kind||'?')+(t.expr?' '+t.expr:t.when?' '+t.when:t.every?' '+t.every:'')})),h('option',{value:'new',text:'+ new cron trigger'}));
      trigSel.value=[...trigSel.options].some(o=>o.value===keep)?keep:(triggers.findIndex(t=>t.kind==='cron')>=0?String(triggers.findIndex(t=>t.kind==='cron')):'new');
      if(document.activeElement!==expr&&document.activeElement!==tz)pickTrigger();
    }
    function pickTrigger(){const t=triggers[+trigSel.value];expr.value=t&&t.kind==='cron'?t.expr||'':t&&t.kind==='at'?'':expr.value;tz.value=t&&t.tz||'';preview();}
    async function preview(){
      const t=triggers[+trigSel.value];
      const spec=t&&t.kind==='at'?{kind:'at',when:t.when,tz:tz.value||undefined}:{kind:'cron',expr:expr.value.trim(),tz:tz.value.trim()||undefined};
      reads.textContent=spec.kind==='cron'?describeCron(spec.expr):'Runs once.';
      const mine=++pending;
      if(spec.kind==='cron'&&!spec.expr){fires.replaceChildren();return;}
      try{
        const r=await api.call('next',{data:{trigger:spec,count:5}});if(mine!==pending)return;
        fires.replaceChildren(table('minmax(0,1fr) minmax(0,1fr)',['In '+r.zone,'Hub ('+r.timezone+')'],
          r.next.length?r.next.map(n=>h('div',{class:'mg-row jb-tight'},h('span',{class:'mg-mono',text:fmt(n.local,r.zone)}),h('span',{class:'mg-mono mg-dimtext',text:fmt(n.hub,r.timezone)}))):[h('div',{class:'mg-empty',text:'Never fires.'})],260));
      }catch(e){if(mine===pending)fires.replaceChildren(h('div',{class:'mg-error',text:e.message}));}
    }
    function writeExpr(cron){
      const doc=parse();if(!doc)return;
      if(cron!==undefined)expr.value=cron;
      if(!Array.isArray(doc.triggers))doc.triggers=[];
      const spec={kind:'cron',expr:expr.value.trim()};if(tz.value.trim())spec.tz=tz.value.trim();
      if(trigSel.value==='new'||!doc.triggers[+trigSel.value]){doc.triggers.push(spec);trigSel.value=String(doc.triggers.length-1);}
      else doc.triggers[+trigSel.value]=spec;
      ed.text=text.value=JSON.stringify(doc,null,2);syncTriggers();preview();
    }
    // Builder.
    const mode=h('select',{'aria-label':'Repeat',onchange:()=>drawBuilder()},
      [['minutes','Every N minutes'],['hours','Every N hours'],['daily','Daily at'],['weekly','Weekly on'],['monthly','Monthly on day']].map(([v,l])=>h('option',{value:v,text:l})));
    mode.value='daily';
    const n=h('input',{type:'number',min:'1',max:'59',value:'15','aria-label':'N',class:'jb-num'});
    const minute=h('input',{type:'number',min:'0',max:'59',value:'0','aria-label':'At minute',class:'jb-num'});
    const at=h('input',{type:'time',value:'03:00','aria-label':'At time'});
    const dom=h('input',{type:'number',min:'1',max:'31',value:'1','aria-label':'Day of the month',class:'jb-num'});
    const days=DOW.slice(0,7).map((d,i)=>h('label',{class:'jb-day'},h('input',{type:'checkbox',checked:i===1,value:String(i)}),d.slice(0,3)));
    const builder=h('div',{class:'mg-actions'});
    function drawBuilder(){const m=mode.value;builder.replaceChildren(mode,
      ...(m==='minutes'?[n]:m==='hours'?[n,h('span',{class:'mg-sub',text:'at minute'}),minute]:m==='daily'?[at]:m==='weekly'?[...days,at]:[dom,at]),
      btn('Use','soft',()=>writeExpr(buildCron({mode:m,n:n.value,minute:minute.value,time:at.value,dom:dom.value,days:days.map(l=>l.firstChild).filter(c=>c.checked).map(c=>+c.value)}))));}
    drawBuilder();
    body.replaceChildren(h('div',{class:'mg-split jb-editor'},
      h('div',{class:'mg-main'},
        h('div',{class:'mg-bar'},title,h('span',{class:'mg-grow'}),
          h('a',{href:'#',class:'jb-link',text:'schema',onclick:e=>{e.preventDefault();schema();}}),
          btn('Format','',format),btn('Validate','',validate),runNow,btn('Save','primary',save)),
        text,problems),
      h('aside',{class:'mg-box mg-side jb-helper'},
        h('div',{class:'mg-pad'},
          h('span',{class:'mg-label',text:'Schedule'}),
          h('div',{class:'mg-actions'},trigSel),
          builder,
          h('div',{class:'mg-actions'},expr,tz,zones,btn('Apply','',()=>writeExpr())),
          reads,fires))));
    syncTriggers();
  }

  // ---- runs ----
  async function runs(){
    const list=await api.call('list',{data:{limit:500}});
    const jobSel=h('select',{'aria-label':'Job'},h('option',{value:'',text:'All jobs'}),list.jobs.map(j=>h('option',{value:j.id,text:j.name})));jobSel.value=runFilter.job;
    const stateSel=h('select',{'aria-label':'State'},h('option',{value:'',text:'Any state'}),h('option',{value:BAD.join(','),text:'Any failure'}),STATES.map(s=>h('option',{value:s,text:s})));stateSel.value=runFilter.state;
    const from=h('input',{type:'date','aria-label':'From',value:runFilter.from}),to=h('input',{type:'date','aria-label':'To',value:runFilter.to});
    const host=h('div',{class:'mg-box'});
    const apply=()=>{Object.assign(runFilter,{job:jobSel.value,state:stateSel.value,from:from.value,to:to.value});load().catch(fail);};
    for(const c of [jobSel,stateSel,from,to])c.onchange=apply;
    body.replaceChildren(h('div',{class:'mg-bar'},jobSel,stateSel,h('span',{class:'mg-sub',text:'from'}),from,h('span',{class:'mg-sub',text:'to'}),to,h('span',{class:'mg-grow'}),btn('Refresh','',apply)),host);
    async function load(){
      const data={limit:200,steps:true};
      if(runFilter.state)data.states=runFilter.state.split(',');
      if(runFilter.from)data.since=new Date(runFilter.from+'T00:00:00').toISOString();
      let rs=(await api.call('runs',{id:runFilter.job||null,data})).runs;
      if(runFilter.to){const end=Date.parse(runFilter.to+'T23:59:59.999');rs=rs.filter(r=>Date.parse(r.created)<=end);}
      const rows=[];
      for(const r of rs){
        const open=runFilter.open===r.id;
        rows.push(h('div',{class:'mg-row pick'+(open?' sel':''),onclick:()=>{runFilter.open=open?null:r.id;load().catch(fail);}},
          two(r.job,r.id+(r.missed?' · missed':''),false),
          h('span',{class:'jb-state '+r.state,text:r.state}),
          h('span',{class:'mg-mono',text:r.trigger}),
          h('span',{class:'mg-dimtext',text:fmt(r.started||r.created,api.tz)}),
          h('span',{class:'mg-mono',text:r.started?dur(r.started,r.finished):'—'}),
          ['queued','due','running'].includes(r.state)?btn('Cancel','danger',async e=>{e.stopPropagation();try{await api.call('cancel',{id:r.id});await load();}catch(x){fail(x);}}):h('span')));
        if(open)rows.push(runDetail(r));
      }
      host.replaceChildren(table('minmax(0,1.4fr) 100px minmax(0,1fr) 160px 90px 90px',['Run','State','Trigger','Started','Took',''],
        rows.length?rows:[h('div',{class:'mg-empty',text:'No runs match.'})],760));
    }
    await load();
  }
  function runDetail(r){
    const steps=Object.entries(r.steps||{});
    const rows=steps.map(([id,s])=>{
      const out=[s.error?'error: '+s.error:'',s.reply?'reply: '+s.reply:'',typeof s.output==='string'?s.output:s.output!=null?JSON.stringify(s.output,null,2):''].filter(Boolean).join('\n');
      return h('div',{class:'jb-step'},
        h('div',{class:'jb-step-row'},h('b',{class:'mg-mono',text:id}),h('span',{class:'jb-state '+s.state,text:s.state||'—'}),
          h('span',{class:'mg-mono',text:dur(s.started,s.finished)}),h('span',{class:'mg-sub',text:(s.attempts||0)+' attempt'+(s.attempts===1?'':'s')+(s.exit_code!=null?' · exit '+s.exit_code:'')})),
        out?h('details',null,h('summary',{text:'output'}),h('pre',{text:out})):null);
    });
    return h('div',{class:'jb-detail',onclick:e=>e.stopPropagation()},
      r.error?h('p',{class:'mg-error',text:r.error}):null,
      h('div',{class:'mg-sub',text:[r.identity_used?'as '+r.identity_used:'',r.scheduled?'scheduled '+fmt(r.scheduled,api.tz):'',r.executions?r.executions+' step executions':''].filter(Boolean).join(' · ')}),
      rows.length?rows:h('p',{class:'mg-sub',text:'No steps ran.'}),
      r.vars&&Object.keys(r.vars).length?h('details',null,h('summary',{text:'vars'}),h('pre',{text:JSON.stringify(r.vars,null,2)})):null,
      r.alerts?h('details',null,h('summary',{text:'alerts'}),h('pre',{text:JSON.stringify(r.alerts,null,2)})):null);
  }

  // ---- guardrails ----
  async function guardrails(){await mountGuardrails(body,api,openJob);}

  // ---- settings ----
  async function settings(){
    const s=(await api.call('settings')).settings||{};
    const err=h('p',{class:'mg-error',role:'alert'});
    const fields={};
    const rows=Object.entries(s).filter(([k])=>k!=='guardrails').map(([k,v])=>{
      const meta=SETTING_LABELS[k]||[k.replace(/_/g,' '),''];
      let input;
      if(k==='timezone')input=h('input',{class:'mg-mono',value:v||'',list:'jb-zones-s'});
      else if(typeof v==='boolean')input=h('input',{type:'checkbox',checked:v});
      else if(typeof v==='number')input=h('input',{type:'number',value:String(v)});
      else if(v&&typeof v==='object'){input=h('textarea',{class:'mg-mono',rows:'3'});input.value=JSON.stringify(v,null,2);}
      else input=h('input',{value:v==null?'':String(v)});
      fields[k]={input,orig:v};
      return h('label',{class:'mg-field'},meta[0],input,meta[1]?h('small',{class:'mg-sub',text:meta[1]}):null);
    });
    const save=btn('Save','primary',async()=>{
      err.textContent='';const data={};
      try{
        for(const [k,{input,orig}] of Object.entries(fields)){
          let v=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.tagName==='TEXTAREA'?JSON.parse(input.value||'null'):input.value.trim();
          if(JSON.stringify(v)!==JSON.stringify(orig))data[k]=v;
        }
        if(!Object.keys(data).length)return;
        await api.call('settings',{data});await api.boot();await settings();
      }catch(e){err.textContent=e.message;}
    });
    body.replaceChildren(h('div',{class:'mg-box jb-settings'},h('div',{class:'mg-pad'},
      h('datalist',{id:'jb-zones-s'},(Intl.supportedValuesOf?Intl.supportedValuesOf('timeZone'):[]).map(z=>h('option',{value:z}))),
      rows,err,h('div',{class:'mg-actions end'},save))));
  }

  return {
    async activate(){
      active=true;
      try{await api.boot();}catch(e){
        root.replaceChildren(h('div',{class:'mg'},h('p',{class:'mg-empty'},e.message+' ',e.status===401?h('a',{href:'/account/login',text:'Sign in'}):null)));
        return;
      }
      if(!root.querySelector('.jb'))root.replaceChildren(h('div',{class:'mg jb'},tabs,notice,body));
      await show(tab);
    },
    deactivate(){active=false;clearInterval(timer);timer=null;},
  };
}

const SETTING_LABELS={
  timezone:['Time zone','IANA zone for schedules without their own tz, and for times shown.'],
  retention_days:['Keep run history (days)','A job\'s retention_days overrides it.'],
  max_step_executions:['Step executions per run','Stops a run whose branches loop more than this.'],
  notify_worker:['Notify worker','Where notify and voice steps go when a step names none. Empty: the first live worker with the cap.'],
  tick_seconds:['Scheduler tick (seconds)',''],
  default_agent:['Default agent','Who runs agent steps that name none.'],
  default_access:['Default access','read / edit / run patterns for new jobs.'],
  default_identity_fallback:['Default identity fallback','Used when a job\'s identity is revoked and it names no fallback.'],
  identity_fallback:['Default identity fallback','Used when a job\'s identity is revoked and it names no fallback.'],
};

// ---- guardrails (isolated: the guardrails workstream owns these shapes) ----
// settings job.guardrails = {deny:[patterns], allow:[patterns]};
// job.read guardrails_preview data {deny, allow} -> {blocked:[{job_id,name,steps,rule}]};
// job.write set_guardrails data {deny, allow}.
export async function mountGuardrails(body,api,openJob){
  const lines=t=>t.split('\n').map(x=>x.trim()).filter(Boolean);
  let current={deny:[],allow:[]};
  try{const s=(await api.call('settings')).settings||{};if(s.guardrails&&typeof s.guardrails==='object')current={deny:s.guardrails.deny||[],allow:s.guardrails.allow||[]};}catch{}
  const preview=async g=>{try{return await api.call('guardrails_preview',{data:g});}catch(e){if(e.code==='NotAvailable'||e.status===501)return null;throw e;}};
  const first=await preview(current);
  if(first===null){body.replaceChildren(h('p',{class:'mg-empty',text:'Guardrails are not available on this hub.'}));return;}
  const deny=h('textarea',{class:'mg-mono',rows:'12','aria-label':'Deny patterns'});deny.value=current.deny.join('\n');
  const allow=h('textarea',{class:'mg-mono',rows:'5','aria-label':'Allow patterns'});allow.value=current.allow.join('\n');
  const out=h('div',{class:'jb-blocked'}),err=h('p',{class:'mg-error',role:'alert'});
  let shown=null;
  const draft=()=>({deny:lines(deny.value),allow:lines(allow.value)});
  const showBlocked=p=>{const b=(p&&p.blocked)||[];out.replaceChildren(h('span',{class:'mg-label',text:b.length?'Would block '+b.length+' job'+(b.length===1?'':'s'):'Blocks no jobs'}),
    ...b.map(x=>h('button',{type:'button',class:'jb-item link',onclick:()=>openJob(x.job_id).catch(e=>{err.textContent=e.message;})},h('span',{class:'mg-bad',text:x.name}),h('small',{text:(x.steps||[]).join(', ')+(x.rule?' · '+x.rule:'')}))));};
  const run=async()=>{err.textContent='';const g=draft();const p=await preview(g);shown=JSON.stringify(g);showBlocked(p);return p;};
  const save=btn('Save','primary',async()=>{
    try{
      const g=draft();
      if(shown!==JSON.stringify(g)){const p=await run();if((p&&p.blocked||[]).length){save.textContent='Save anyway';return;}}
      await api.call('set_guardrails',{data:g});save.textContent='Save';current=g;shown=JSON.stringify(g);await run();
    }catch(e){err.textContent=e.message;}
  });
  for(const t of [deny,allow])t.addEventListener('input',()=>{save.textContent='Save';});
  body.replaceChildren(h('div',{class:'mg-split'},
    h('div',{class:'mg-box mg-main'},h('div',{class:'mg-pad'},
      h('label',{class:'mg-field'},'Deny (one pattern per line)',deny),
      h('label',{class:'mg-field'},'Allow (one pattern per line)',allow),err,
      h('div',{class:'mg-actions end'},btn('Preview','',()=>run().catch(e=>{err.textContent=e.message;})),save))),
    h('aside',{class:'mg-box mg-side'},h('div',{class:'mg-pad'},out))));
  shown=JSON.stringify(current);showBlocked(first);
}
