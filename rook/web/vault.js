// Secrets: the hub's vault. Values are write-only here — the page can set,
// replace and delete them but never displays one.
const el=(tag,txt,cls)=>{const e=document.createElement(tag);if(txt!==undefined&&txt!==null)e.textContent=txt;if(cls)e.className=cls;return e;};
const date=t=>t?new Date(t*1000).toLocaleString():'—';
const ago=t=>{if(!t)return 'never';const m=Math.round((Date.now()/1000-t)/60);return m<1?'just now':m<60?m+'m ago':m<1440?Math.round(m/60)+'h ago':Math.round(m/1440)+'d ago';};

export async function mountVault(root){
  if(!document.querySelector('link[data-kn]')){const css=document.createElement('link');css.rel='stylesheet';css.href='/account/knowledge/assets/knowledge.css'+new URL(import.meta.url).search;css.dataset.kn='1';document.head.append(css);}
  let csrf='';
  const status=el('p','','kn-dim');status.setAttribute('role','status');
  async function api(body){const r=await fetch('/account/vault/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}
  function field(label,input){const w=el('label',label);w.append(input);return w;}
  function secretRow(s,refresh){
    const box=el('section',undefined,'kn-card');box.style.cursor='default';
    const top=el('div',undefined,'kn-card-top');top.append(el('strong',s.name),el('small','set by '+s.set_by+' · updated '+ago(s.updated)+' · last used '+ago(s.last_used)));box.append(top);
    if(s.description)box.append(el('span',s.description));
    box.append(el('code','{{secret:'+s.name+'}}','kn-dim'));
    const row=el('div',undefined,'kn-row');const err=el('small','','kn-error');
    const value=el('input');value.type='password';value.autocomplete='new-password';value.placeholder='New value';value.setAttribute('aria-label','New value for '+s.name);
    const replace=el('button','Replace value');replace.type='button';
    replace.onclick=async()=>{if(!value.value){err.textContent='Enter the new value first.';return;}replace.disabled=true;try{const d=await api({action:'set',name:s.name,value:value.value});value.value='';status.textContent='Replaced '+s.name+(d.journal_rows_masked?' · masked in '+d.journal_rows_masked+' journal rows':'')+'.';await refresh();}catch(e){err.textContent=e.message;}finally{replace.disabled=false;}};
    const desc=el('input');desc.value=s.description||'';desc.setAttribute('aria-label','Description for '+s.name);desc.placeholder='What it is for';
    const saveDesc=el('button','Save description');saveDesc.type='button';saveDesc.onclick=async()=>{try{await api({action:'describe',name:s.name,description:desc.value});status.textContent='Updated '+s.name+'.';await refresh();}catch(e){err.textContent=e.message;}};
    let armed=false;const del=el('button','Delete');del.type='button';
    del.onclick=async()=>{if(!armed){armed=true;del.textContent='Click again to delete '+s.name;setTimeout(()=>{armed=false;del.textContent='Delete';},4000);return;}try{await api({action:'delete',name:s.name});status.textContent='Deleted '+s.name+'.';await refresh();}catch(e){err.textContent=e.message;}};
    row.append(value,replace,desc,saveDesc,del,err);box.append(row);return box;}
  async function refresh(){
    const r=await fetch('/account/vault/api');const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Sign in with your operator account to manage secrets.');csrf=d.csrf;
    root.replaceChildren();const page=el('div',undefined,'kn-page');page.style.maxWidth='960px';root.append(page);
    page.append(el('p','Credentials your agents can use. Agents list names with rook_secret, read a value with rook_secret get (logged), or — better — put {{secret:name}} in rook_call args so the hub fills it in on the way to the worker and masks it in the reply. Values are encrypted on the hub and never shown here.','kn-dim'),status);
    const add=el('section',undefined,'kn-callout');add.append(el('strong','Add a secret'));
    const name=el('input');name.placeholder='name, e.g. starscream-root';name.autocomplete='off';
    const description=el('input');description.placeholder='What it is for, where it works';
    const value=el('input');value.type='password';value.autocomplete='new-password';value.placeholder='Value';
    const err=el('small','','kn-error');const save=el('button','Save','kn-primary');save.type='button';
    save.onclick=async()=>{save.disabled=true;err.textContent='';try{const d=await api({action:'set',name:name.value.trim(),value:value.value,description:description.value});status.textContent='Saved '+name.value.trim()+(d.journal_rows_masked?' · masked in '+d.journal_rows_masked+' journal rows':'')+'.';await refresh();}catch(e){err.textContent=e.message;}finally{save.disabled=false;}};
    const fields=el('div',undefined,'kn-fields');fields.append(field('Name',name),field('Description',description),field('Value',value));add.append(fields,el('div',undefined,'kn-row'));add.lastChild.append(save,err);page.append(add);
    page.append(el('h2','Secrets ('+d.secrets.length+')'));if(!d.secrets.length)page.append(el('p','None yet.','kn-dim'));for(const s of d.secrets)page.append(secretRow(s,refresh));
    page.append(el('h2','Access log'));const t=el('table',undefined,'kn-table');const h=el('tr');for(const c of ['When','Secret','Action','By','Via','Task'])h.append(el('th',c));t.append(h);
    for(const a of d.access){const tr=el('tr');tr.append(el('td',date(a.ts)),el('td',a.name),el('td',a.action),el('td',a.actor),el('td',a.via||''),el('td',a.task||''));t.append(tr);}
    const wrap=el('div',undefined,'kn-scroll');wrap.append(t);page.append(wrap);}
  return {activate(){refresh().catch(e=>{root.replaceChildren(el('p',e.message,'kn-error'));});},deactivate(){}};
}
