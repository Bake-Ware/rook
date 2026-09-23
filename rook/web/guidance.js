// Agent instructions editor. All stored text is rendered as text, never HTML.
const CSS=`#view-guidance{max-width:960px}#view-guidance .gd-help{color:#9eb0b5;line-height:1.55;margin:0 0 1rem}#view-guidance h2{font-size:1rem;margin:1.8rem 0 .3rem}#view-guidance .gd-where{color:#9eb0b5;font-size:.85rem;margin:0 0 .8rem}
#view-guidance .gd-slot{background:#101b21;border:1px solid #34464e;border-radius:10px;padding:.9rem;margin-bottom:.7rem}#view-guidance .gd-slot header{display:flex;justify-content:space-between;gap:.6rem;flex-wrap:wrap;align-items:baseline}
#view-guidance code{font-size:.9rem;color:#cfe7df}#view-guidance small{color:#9eb0b5}#view-guidance .gd-edited{color:#e8c77a}
#view-guidance textarea,#view-guidance input,#view-guidance select,#view-guidance button{font:inherit;border:1px solid #48575b;border-radius:6px;padding:.55rem;background:#19242a;color:#e1e9e6}
#view-guidance textarea{width:100%;box-sizing:border-box;min-height:4.2rem;margin:.55rem 0;line-height:1.5;resize:vertical}#view-guidance .gd-server textarea{min-height:15rem}
#view-guidance button{cursor:pointer}#view-guidance button:hover{border-color:#73baa2}#view-guidance button:focus-visible,#view-guidance textarea:focus-visible{outline:2px solid #73baa2}
#view-guidance .gd-row{display:flex;gap:.5rem;flex-wrap:wrap;align-items:center}#view-guidance .gd-count{margin-left:auto}#view-guidance .gd-status{color:#9cc9bd;min-height:1.4rem}#view-guidance .gd-error{color:#ffada6}
#view-guidance details{margin-top:.5rem}#view-guidance details pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit;font-size:.85rem;color:#c7d3d0;border-left:2px solid #34464e;padding-left:.6rem}
@media(max-width:640px){#view-guidance .gd-count{margin-left:0}}`;
const SECTIONS=[
 ['server','Connection instructions','Sent once when an agent connects (MCP initialize). Keep it to what every agent needs.'],
 ['tool','Tool tips','Appended to a tool’s description as “Tip: …”. Agents see it when they list tools.'],
 ['cap','Capability tips','Attached as _tips to a rook_call reply whose cap starts with this prefix (e.g. proc. or shell.exec). Shown once per agent session.'],
 ['worker','Worker tips','Attached as _tips to a rook_call reply from this worker. Shown once per agent session.'],
];
export async function mountGuidance(root){
 const style=document.createElement('style');style.textContent=CSS;document.head.append(style);
 const el=(tag,txt,cls)=>{const e=document.createElement(tag);if(txt!==undefined)e.textContent=txt;if(cls)e.className=cls;return e;};
 const date=t=>new Date(t*1000).toLocaleString();
 let csrf='',tools=[],editable=true;
 const status=el('p','','gd-status');status.setAttribute('role','status');
 async function api(body){const r=await fetch('/account/guidance/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}
 function slot(s){
  const box=el('section',undefined,'gd-slot'+(s.kind==='server'?' gd-server':''));
  const head=el('header');head.append(el('code',s.key));
  head.append(el('small',s.edited?(s.text?'Edited':'Disabled')+' by '+s.actor+' · '+date(s.updated):(s.default===null?'':'Default'),s.edited?'gd-edited':''));
  const ta=el('textarea');ta.value=s.text;ta.setAttribute('aria-label',s.key);ta.disabled=!editable;
  const limit=s.kind==='server'?6000:1000;const count=el('small','','gd-count');const upd=()=>{count.textContent=ta.value.length+' / '+limit;};upd();ta.oninput=upd;
  const row=el('div',undefined,'gd-row');const err=el('small','','gd-error');
  const save=el('button','Save');save.onclick=()=>act(save,err,{action:'set',key:s.key,text:ta.value});
  row.append(save);
  if(s.edited&&s.default!==null){const reset=el('button','Reset to default');reset.onclick=()=>act(reset,err,{action:'reset',key:s.key});row.append(reset);}
  if(s.edited&&s.default===null){const del=el('button','Remove');del.onclick=()=>act(del,err,{action:'reset',key:s.key});row.append(del);}
  row.append(err,count);
  box.append(head,ta,row);
  if(s.default!==null&&s.edited){const d=el('details');d.append(el('summary','Show default'),el('pre',s.default));box.append(d);}
  const h=el('details');h.append(el('summary','History'));h.ontoggle=async()=>{if(!h.open||h.dataset.loaded)return;h.dataset.loaded=1;try{const r=await fetch('/account/guidance/api?history='+encodeURIComponent(s.key));const d=await r.json();if(!d.history.length)h.append(el('small','No edits yet.'));for(const e of d.history)h.append(el('small',date(e.ts)+' · '+e.actor+(e.text===null?' · reset to default':'')),el('pre',e.text??''));}catch(e){h.append(el('small',e.message,'gd-error'));}};
  box.append(h);
  return box;
 }
 async function act(button,err,body){button.disabled=true;err.textContent='';try{const d=await api(body);render(d.slots);status.textContent='Saved. New agent connections and tool listings use it now; tips apply on the next matching call.';}catch(e){err.textContent=e.message;}finally{button.disabled=false;}}
 function adder(){
  const box=el('section',undefined,'gd-slot');box.append(el('strong','Add a tip'));
  const row=el('div',undefined,'gd-row');row.style.marginTop='.6rem';
  const kind=el('select');for(const [k,label] of [['cap','Capability prefix'],['worker','Worker name'],['tool','Tool']]){const o=el('option',label);o.value=k;kind.append(o);}
  const name=el('input');name.placeholder='e.g. hermes. or kaiju';name.setAttribute('aria-label','Name');
  const tool=el('select');tool.setAttribute('aria-label','Tool');for(const t of tools){const o=el('option',t);o.value=t;tool.append(o);}tool.hidden=true;
  kind.onchange=()=>{tool.hidden=kind.value!=='tool';name.hidden=kind.value==='tool';name.placeholder=kind.value==='cap'?'e.g. hermes. or deluge.add':'exact worker name';};
  row.append(kind,name,tool);
  const ta=el('textarea');ta.placeholder='Terse advice an agent needs at that moment.';ta.setAttribute('aria-label','Tip text');
  const err=el('small','','gd-error');const add=el('button','Add');
  add.onclick=()=>{const key=kind.value+':'+(kind.value==='tool'?tool.value:name.value.trim());act(add,err,{action:'set',key,text:ta.value});};
  const r2=el('div',undefined,'gd-row');r2.append(add,err);
  box.append(row,ta,r2);return box;
 }
 function render(slots){
  root.replaceChildren(el('p','Advice Rook gives to agents at the moment it is useful. Keep entries short: they are sent with every connection or matching call. Tips are guidance only and never allow or block anything.','gd-help'),status);
  if(!editable)root.append(el('p','The instructions store is unavailable, so defaults are in effect and editing is disabled.','gd-error'));
  for(const [k,title,where] of SECTIONS){root.append(el('h2',title),el('p',where,'gd-where'));for(const s of slots.filter(s=>s.kind===k))root.append(slot(s));}
  if(editable)root.append(el('h2','Add'),adder());
 }
 async function load(){const r=await fetch('/account/guidance/api');const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Sign in with your operator account to edit agent instructions.');csrf=d.csrf;tools=d.tools;editable=d.editable;render(d.slots);}
 return {activate(){load().catch(e=>{root.replaceChildren(el('p',e.message,'gd-error'));});},deactivate(){}};
}
