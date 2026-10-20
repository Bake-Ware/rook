// Shared helpers for the manage pages. Everything stored is rendered as text.
export function h(tag,attrs,...kids){
  const e=document.createElement(tag);
  for(const [k,v] of Object.entries(attrs||{})){
    if(v===undefined||v===null||v===false)continue;
    if(k==='class')e.className=v;
    else if(k==='text')e.textContent=v;
    else if(k.startsWith('on'))e[k]=v;
    else if(['value','checked','disabled','hidden','selected','type','title','placeholder','href','src','required'].includes(k))e[k]=v;
    else e.setAttribute(k,v===true?'':v);
  }
  for(const kid of kids.flat()){if(kid===null||kid===undefined||kid===false)continue;e.append(kid);}
  return e;
}
// HTML-escape for the few places that build markup strings (quotes too, so
// the result is safe inside either kind of attribute quote).
export const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// A text-only element: el('td','value','cls').
export const el=(tag,txt,cls)=>{const e=document.createElement(tag);if(txt!==undefined&&txt!==null)e.textContent=txt;if(cls)e.className=cls;return e;};
// Relative time for a unix timestamp; `none` is shown when there is none.
export const ago=(t,none='never')=>{if(!t)return none;const m=Math.round((Date.now()/1000-t)/60);return m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+'h ago':m<43200?Math.round(m/1440)+'d ago':day(t);};
export const day=t=>t?new Date(t*1000).toLocaleDateString(undefined,{month:'short',day:'numeric',year:new Date(t*1000).getFullYear()===new Date().getFullYear()?undefined:'numeric'}):'—';
export function useCss(){
  if(document.querySelector('link[href*="manage.css"]'))return;
  document.head.append(h('link',{rel:'stylesheet',href:'/account/bands/assets/manage.css'+new URL(import.meta.url).search}));
}
export function btn(text,cls,onclick,attrs){return h('button',{type:'button',class:'mg-btn'+(cls?' '+cls:''),text,onclick,...attrs});}
// Filter chips: items are [id, label]; returns the row element.
export function chips(items,current,pick){
  return items.map(([id,label])=>h('button',{type:'button',class:'mg-chip'+(id===current?' on':''),text:label,'aria-pressed':String(id===current),onclick:()=>pick(id)}));
}
export function table(cols,head,rows,min){
  const inner=h('div',null,h('div',{class:'mg-head'},head.map(x=>h('div',{text:x}))),rows);
  return h('div',{class:'mg-table',style:'--cols:'+cols+(min?';--min:'+min+'px':'')},inner);
}
export function two(title,sub,mono){return h('span',{class:'mg-two'},h('b',{text:title,class:mono?'mg-mono':''}),sub?h('small',{text:sub}):null);}
let openMenu=null,closeMenu=null;
addEventListener('hashchange',()=>{if(closeMenu)closeMenu();});
export function menu(anchor,items){
  if(closeMenu)closeMenu();
  const box=h('div',{class:'mg-menu',role:'menu'},items.filter(Boolean).map(i=>i.href?h('a',{href:i.href,text:i.label,role:'menuitem'}):h('button',{type:'button',text:i.label,class:i.danger?'danger':'',role:'menuitem',onclick:()=>{close();i.run();}})));
  function close(){box.remove();if(openMenu===box){openMenu=null;closeMenu=null;}document.removeEventListener('pointerdown',away,true);document.removeEventListener('keydown',key,true);}
  function away(e){if(!box.contains(e.target))close();}
  function key(e){if(e.key==='Escape'){close();anchor.focus();}}
  document.body.append(box);openMenu=box;closeMenu=close;
  const r=anchor.getBoundingClientRect(),w=box.offsetWidth,hh=box.offsetHeight;
  box.style.left=Math.max(8,Math.min(r.right-w,innerWidth-w-8))+'px';
  box.style.top=(r.bottom+hh+8>innerHeight?Math.max(8,r.top-hh-4):r.bottom+4)+'px';
  setTimeout(()=>{document.addEventListener('pointerdown',away,true);document.addEventListener('keydown',key,true);});
  (box.querySelector('button,a')||box).focus();
}
// Compact controls shared by every page (mobile.css styles them).
// info(text): a small (i) that shows help text on tap, instead of a paragraph in the flow.
export function info(text,label='About this',cls){
  const body=typeof text==='string'?h('p',{text}):text;
  return h('details',{class:'mg-info'+(cls?' '+cls:'')},h('summary',{'aria-label':label,title:label,text:'i'}),body);
}
// more(items): a ⋯ button that opens menu(items) (secondary actions of one item).
export function more(items,label='More actions',cls){
  const b=h('button',{type:'button',class:'mg-btn icon more-btn'+(cls?' '+cls:''),text:'⋯','aria-label':label,title:label,'aria-haspopup':'menu'});
  b.onclick=e=>{e.stopPropagation();const list=typeof items==='function'?items():items;menu(b,list);};
  return b;
}
// copyChip(value, shown): a one-line id or path; tap copies the full value.
export function copyChip(value,shown,cls){
  const v=String(value??'');
  return h('span',{class:'copy-chip'+(cls?' '+cls:''),text:shown??v,title:v+' (tap to copy)','data-copy':v,role:'button',tabindex:'0','aria-label':'Copy '+v});
}
// seg(items, current, pick): a segmented control; items are [id, label, title?].
export function seg(items,current,pick,label){
  return h('div',{class:'seg',role:'group','aria-label':label||null},items.map(([id,text,title])=>h('button',{type:'button',text,title:title||null,'aria-pressed':String(id===current),onclick:()=>pick(id)})));
}
// A modal that removes its content (and any secret in it) when it closes.
export function dialog(title,...body){
  const d=h('dialog',{class:'mg mg-dialog','aria-label':title},h('div',{class:'mg-bar',style:'justify-content:space-between'},h('h2',{text:title}),btn('×','icon',()=>d.close(),{'aria-label':'Close','data-close':''})),body);
  d.addEventListener('close',()=>d.remove());
  document.body.append(d);d.showModal();return d;
}
export async function copy(text,button){
  const was=button.textContent;
  try{await navigator.clipboard.writeText(text);button.textContent='Copied';}catch{button.textContent='Copy failed';}
  setTimeout(()=>{button.textContent=was;},1500);
}
// Account actions answer with JSON, or with a small HTML page whose text we lift.
export async function accountAction(csrf,fields){
  const body=new FormData();body.set('csrf',csrf);
  for(const [k,v] of Object.entries(fields))body.set(k,v);
  const r=await fetch('/account/action',{method:'POST',headers:{'X-Rook-View':'account'},body});
  if(r.headers.get('content-type')?.includes('application/json')){
    const d=await r.json();if(!r.ok)throw Error(d.error||'Request failed.');
    if(d.redirect){location.assign(d.redirect);return new Promise(()=>{});}
    return d;
  }
  if(r.redirected)throw Error('Your session expired. Sign in again.');
  const main=new DOMParser().parseFromString(await r.text(),'text/html').querySelector('main');
  if(!main)throw Error('Unable to complete this request.');
  const title=main.querySelector('h1')?.textContent||'';
  main.querySelectorAll('script,style,link,nav,h1,a').forEach(x=>x.remove());
  if(!r.ok)throw Error(main.textContent.trim()||title);
  return {title,text:main.querySelector('pre')?.textContent||'',note:[...main.querySelectorAll('p')].map(p=>p.textContent.trim()).filter(Boolean).join(' ')};
}
// Show a one-time value (invitation link, new key) with a copy button.
export function reveal(title,note,text){
  const c=btn('Copy','soft',()=>copy(text,c));
  return dialog(title,h('p',{text:note}),h('pre',{text,tabindex:'0'}),h('div',{class:'mg-actions end'},c));
}
// Copy-paste commands that join a machine with pairing `code` through the hub
// at `origin` (the server's public base, http:// on a LAN hub):
// [label, class, text, note]. The notes describe what the installers in
// rook/remote/bootstrap.py actually do, so a first-time user knows to accept
// UAC / sudo and to wait rather than assume it hung.
export function pairCommands(origin,code){
  const url=origin+'/worker?band='+code;
  return [['Linux / macOS','pairing-command',`curl -fsSL '${url}' | bash`,
           'Runs as your user (a systemd user service on Linux) and may ask for your sudo password for system packages. Takes 1-3 minutes; the machine appears under Workers when done.'],
          ['Windows PowerShell','pairing-windows',`iex (irm "${url}&os=windows")`,
           'Opens an administrator prompt (UAC). Takes 2-5 minutes; the machine appears under Workers when done.'],
          ['Android (Termux)','pairing-android',`pkg install -y curl && curl -fsSL '${url}' | bash`,'']];
}
// A live pairing code: the code, its countdown and the three commands. `data`
// is the account pair action's JSON ({pairing, band_id, origin, csrf}). While
// the panel is on screen an expired code is replaced in the same pairing
// session; off screen it waits. stop() ends it.
export function pairingPanel(data){
  const code=h('strong',{class:'mg-big pairing-code'}),left=h('p',{class:'mg-sub pairing-expiry'}),rows=h('div',{class:'pairing-commands'});
  let grant=data.pairing,pending=false,stopped=false;
  const show=()=>{
    code.textContent=grant.code;
    rows.replaceChildren(...pairCommands(data.origin,grant.code).map(([label,cls,text,note])=>{
      const c=btn('Copy','soft',()=>copy(text,c));
      return h('div',{class:'pairing-row'},h('span',{class:'mg-label',text:label}),h('pre',{class:cls,text,tabindex:'0'}),c,
        note&&h('p',{class:'mg-sub pairing-note',text:note}));
    }));
  };
  const node=h('div',{class:'pairing-panel'},code,left,rows);
  const tick=async()=>{
    if(stopped||pending)return;
    const s=Math.ceil(grant.expires-Date.now()/1000);left.textContent=Math.max(0,s)+' seconds remaining';
    if(s>0||!node.isConnected||!node.getClientRects().length)return;
    pending=true;
    try{
      const r=await fetch('/account/pairing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf:data.csrf,band_id:data.band_id,session:grant.session})});
      const next=await r.json();if(!r.ok)throw Error(next.error||'Pairing stopped. Generate a new code.');
      grant=next;show();
    }catch(e){left.textContent=e.message;stop();}finally{pending=false;}
  };
  const timer=setInterval(tick,1000);
  function stop(){stopped=true;clearInterval(timer);}
  show();tick();
  return {node,stop};
}
// Start pairing for the operator's band: `preferLabel` (the dashboard's band
// label) if they own it, else their first active band. Uses the account
// session (the dashboard login carries over) and the same CSRF-checked
// account action as the Account page; the server allows owners only.
export async function mintPairing(preferLabel){
  const r=await fetch('/account/session',{headers:{Accept:'application/json'}});
  const d=r.headers.get('content-type')?.includes('application/json')?await r.json():{};
  if(!r.ok||!d.user)throw Error(d.error||'Sign in with your operator account first.');
  const owned=(d.bands||[]).filter(b=>b.active&&b.role==='owner');
  const band=owned.find(b=>b.label===preferLabel)||owned[0];
  if(!band)throw Error('You do not own a band to pair with. Create one on the Bands page.');
  const out=await accountAction(d.user.csrf,{op:'pair',band_id:band.id});
  if(!out.pairing)throw Error(out.note||'Could not start pairing.');
  return {...out,band_name:band.name};
}
// Pairing code dialog: refreshes itself until the dialog closes.
export function pairing(data){
  const panel=pairingPanel(data);
  const d=dialog('Pair a worker',h('p',{text:'Use this code on the machine you want to connect.'}),panel.node);
  d.addEventListener('close',()=>panel.stop());
  return d;
}
