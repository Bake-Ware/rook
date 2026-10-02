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
export const ago=t=>{if(!t)return 'never';const m=Math.round((Date.now()/1000-t)/60);return m<1?'just now':m<60?m+' min ago':m<1440?Math.round(m/60)+'h ago':m<43200?Math.round(m/1440)+'d ago':day(t);};
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
// Pairing code: refreshes itself until the dialog closes.
export function pairing(data){
  const code=h('strong',{class:'mg-big pairing-code'}),left=h('p',{class:'mg-sub pairing-expiry'}),unix=h('pre',{class:'pairing-command'}),win=h('pre',{class:'pairing-windows'});
  let grant=data.pairing,pending=false,stopped=false;
  const show=()=>{const url=data.origin+'/worker?band='+grant.code;code.textContent=grant.code;unix.textContent=`curl -fsSL '${url}' | bash`;win.textContent=`iex (irm "${url}&os=windows")`;};
  show();
  const d=dialog('Pair a worker',h('p',{text:'Use this code on the machine you want to connect.'}),code,left,h('span',{class:'mg-label',text:'Linux / macOS'}),unix,h('span',{class:'mg-label',text:'Windows PowerShell'}),win);
  const timer=setInterval(async()=>{
    if(stopped)return;
    const s=Math.ceil(grant.expires-Date.now()/1000);left.textContent=Math.max(0,s)+' seconds remaining';
    if(s>0||pending)return;pending=true;
    try{
      const r=await fetch('/account/pairing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf:data.csrf,band_id:data.band_id,session:grant.session})});
      const next=await r.json();if(!r.ok)throw Error(next.error||'Pairing stopped. Generate a new code.');
      grant=next;show();
    }catch(e){left.textContent=e.message;stopped=true;}finally{pending=false;}
  },1000);
  d.addEventListener('close',()=>clearInterval(timer));
  return d;
}
