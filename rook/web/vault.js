// Secrets: the hub's vault. Values are write-only here — the page can set,
// replace and delete them but never displays one.
import {h,ago,day,useCss,btn,chips,table,two,dialog,copy} from '/account/bands/assets/manage.js';

export async function mountVault(root){
  useCss();
  let csrf='',data={secrets:[],access:[]},picked='',filter='all',query='';
  const status=h('p',{class:'mg-status',role:'status'});
  const search=h('input',{type:'search',class:'mg-grow','aria-label':'Filter secrets',placeholder:'Filter by name or what it is for',oninput:()=>{query=search.value.trim().toLowerCase();draw();}});
  const chipHost=h('span',{class:'mg-actions'}),list=h('div',{class:'mg-box mg-main'}),panel=h('div',{class:'mg-box strong mg-side'});
  root.replaceChildren(h('div',{class:'mg'},
    h('div',{class:'mg-bar'},search,chipHost,btn('Add a secret','primary',addDialog)),status,
    h('div',{class:'mg-split'},list,panel)));

  async function api(body){const r=await fetch('/account/vault/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}
  async function refresh(){
    const r=await fetch('/account/vault/api');const d=await r.json();
    if(!r.ok||d.error)throw Error(d.error||'Sign in with your operator account to manage secrets.');
    csrf=d.csrf;data=d;if(!data.secrets.some(s=>s.name===picked))picked=data.secrets[0]?.name||'';draw();
  }
  const placeholder=name=>'{{secret:'+name+'}}';
  const masked=d=>d.journal_rows_masked?' · masked in '+d.journal_rows_masked+' journal rows':'';

  function draw(){
    const unused=data.secrets.filter(s=>!s.last_used).length;
    chipHost.replaceChildren(...chips([['all','All '+data.secrets.length],['never','Never used '+unused]],filter,id=>{filter=id;draw();}));
    const shown=data.secrets.filter(s=>(filter==='all'||!s.last_used)&&(!query||(s.name+' '+(s.description||'')).toLowerCase().includes(query)));
    const rows=shown.map(s=>{
      const c=btn('{ }','icon',e=>{e.stopPropagation();copy(placeholder(s.name),c);},{'aria-label':'Copy the placeholder for '+s.name,title:'Copy the placeholder for this secret',style:'font:500 11px var(--mono);color:var(--accent)'});
      return h('div',{class:'mg-row pick'+(s.name===picked?' sel':''),onclick:()=>{picked=s.name;draw();}},
        h('button',{type:'button',style:'all:unset;cursor:pointer;min-width:0','aria-current':s.name===picked?'true':null},two(s.name,s.description||'',true)),
        h('span',{text:ago(s.last_used),class:s.last_used?'mg-dimtext':'mg-warn'}),h('span',{text:day(s.updated),class:'mg-dimtext'}),c);
    });
    list.replaceChildren(table('minmax(0,1.5fr) 110px 100px 44px',['Name · what it is for','Last used','Updated',''],rows.length?rows:[h('div',{class:'mg-empty',text:data.secrets.length?'Nothing matches.':'No secrets yet. Add one to let agents use it by name.'})]));
    drawPanel(data.secrets.find(s=>s.name===picked));
  }

  function drawPanel(s){
    if(!s){panel.replaceChildren(h('div',{class:'mg-empty',text:'Agents use a secret by name. The value is filled in on the way to the worker and masked in replies.'}));return;}
    const err=h('small',{class:'mg-error',role:'alert'});
    const what=h('textarea',{rows:'2','aria-label':'What '+s.name+' is for'});what.value=s.description||'';
    const value=h('input',{type:'password',autocomplete:'new-password',placeholder:'New value',class:'mg-grow','aria-label':'New value for '+s.name});
    const run=async(button,work)=>{button.disabled=true;err.textContent='';try{await work();}catch(e){err.textContent=e.message;}finally{button.disabled=false;}};
    const cp=btn('Copy','',()=>copy(placeholder(s.name),cp));
    const replace=btn('Replace value','soft',()=>run(replace,async()=>{if(!value.value)throw Error('Enter the new value first.');const d=await api({action:'set',name:s.name,value:value.value});status.textContent='Replaced '+s.name+masked(d)+'.';await refresh();}));
    const save=btn('Save description','',()=>run(save,async()=>{await api({action:'describe',name:s.name,description:what.value});status.textContent='Updated '+s.name+'.';await refresh();}));
    let armed=false;
    const del=btn('Delete','danger',()=>{if(!armed){armed=true;del.textContent='Click again to delete';setTimeout(()=>{armed=false;del.textContent='Delete';},4000);return;}run(del,async()=>{await api({action:'delete',name:s.name});status.textContent='Deleted '+s.name+'.';await refresh();});});
    const log=data.access.filter(a=>a.name===s.name).slice(0,12);
    panel.replaceChildren(
      h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:s.name,class:'mg-mono',style:'font-size:16px'}),h('small',{text:'set by '+s.set_by+' · updated '+ago(s.updated)}))),
      h('div',{class:'mg-pad'},
        h('label',{class:'mg-field'},'What it is for',what),
        h('div',{class:'mg-field'},'Use it in a call',h('div',{class:'mg-actions',style:'flex-wrap:nowrap'},h('code',{class:'mg-code',text:placeholder(s.name)}),cp)),
        h('div',{class:'mg-actions'},value,replace),
        h('div',{class:'mg-actions'},save,del),err,
        h('div',null,h('div',{class:'mg-label',style:'margin-bottom:8px',text:'Access log'}),
          log.length?log.map(a=>h('div',{class:'mg-log',title:new Date(a.ts*1000).toLocaleString()+(a.task?' · task '+a.task:'')},
            h('span',{class:'mg-label '+(a.action==='get'||a.action==='read'?'mg-warn':a.action==='set'?'mg-good':''),text:a.action}),
            h('span',{text:a.actor+(a.via?' · '+a.via:'')}),h('span',{class:'mg-sub',style:'font-size:12px',text:ago(a.ts)}))):h('p',{class:'mg-sub',text:'No access recorded.'}))));
  }

  function addDialog(){
    const name=h('input',{placeholder:'nas-admin-password',autocomplete:'off',required:true}),what=h('input',{placeholder:'What it is for, where it works'}),value=h('input',{type:'password',autocomplete:'new-password',required:true});
    const err=h('small',{class:'mg-error',role:'alert'}),save=h('button',{class:'mg-btn primary',text:'Save'});
    const form=h('form',{onsubmit:async e=>{e.preventDefault();save.disabled=true;err.textContent='';
      try{const n=name.value.trim();const d=await api({action:'set',name:n,value:value.value,description:what.value});picked=n;status.textContent='Saved '+n+masked(d)+'.';dlg.close();await refresh();}
      catch(x){err.textContent=x.message;}finally{save.disabled=false;}}},
      h('label',{class:'mg-field'},'Name',name),h('label',{class:'mg-field'},'What it is for',what),h('label',{class:'mg-field'},'Value',value),err,h('div',{class:'mg-actions end'},save));
    const dlg=dialog('Add a secret',h('p',{text:'The value is encrypted on the hub and never shown again.'}),form);name.focus();
  }

  return {activate(){refresh().catch(e=>{list.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));panel.replaceChildren();});},deactivate(){}};
}
