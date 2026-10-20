// Help: the Rook user manual, and the "the manual changed" prompt after an
// update. Pages come from the knowledge base's copy when the hub imported one
// (with the operator's edits), else from the manual bundled with this release.
// All page text is rendered as text nodes, never as HTML.
const {h, btn, dialog, useCss, info} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);
const API='/account/manual/api';

function loadCss(){if(document.querySelector('link[data-manual]'))return;const css=document.createElement('link');css.rel='stylesheet';css.href='/account/manual/assets/manual.css'+new URL(import.meta.url).search;css.dataset.manual='1';document.head.append(css);}

async function get(params){
  const r=await fetch(API+'?'+new URLSearchParams(params),{headers:{Accept:'application/json'}});
  const ct=r.headers.get('content-type')||'';
  if(!ct.includes('application/json'))throw Error('Sign in to read the manual.');
  const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;
}
async function post(csrf,body){
  const r=await fetch(API,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});
  const d=await r.json().catch(()=>({error:'Request failed'}));if(!r.ok||d.error)throw Error(d.error||'Request failed');return d.result;
}

// Markdown the manual uses: #..### headings, - and 1. lists, ``` code, `code`,
// **bold**, [[manual-<id>]] page links and http(s) links.
export function renderMarkdown(text,onPage,title=id=>id.replace(/-/g,' ')){
  const out=h('div',{class:'mn-md'});let list=null,sub=null,para=[],code=null;
  const inline=(s,into)=>{for(const part of s.split(/(\[\[[a-z0-9][a-z0-9-]{0,79}\]\]|`[^`]+`|\*\*[^*]+\*\*|https?:\/\/[^\s)<>"]+)/)){if(!part)continue;let m;
    if((m=part.match(/^\[\[(.+)\]\]$/))){const slug=m[1];const id=slug==='rook-manual'?'':slug.replace(/^manual-/,'');
      if(slug.startsWith('manual-')||slug==='rook-manual'){const a=h('a',{href:'#help'+(id?'?page='+encodeURIComponent(id):''),class:'mn-ref',text:slug==='rook-manual'?'Rook manual':title(id)});a.onclick=e=>{e.preventDefault();onPage(id);};into.append(a);}
      else into.append(h('a',{href:'#knowledge?p='+encodeURIComponent(slug),class:'mn-ref',text:slug}));}
    else if(part.startsWith('`')&&part.endsWith('`')&&part.length>1)into.append(h('code',{text:part.slice(1,-1)}));
    else if(/^\*\*[^*]+\*\*$/.test(part))into.append(h('strong',{text:part.slice(2,-2)}));
    else if(/^https?:\/\//.test(part))into.append(h('a',{href:part,target:'_blank',rel:'noopener noreferrer',text:part}));
    else into.append(document.createTextNode(part));}};
  const flush=()=>{if(para.length){const p=h('p');inline(para.join(' '),p);out.append(p);para=[];}};
  for(const line of (text||'').split('\n')){
    if(code!==null){if(line.startsWith('```')){out.append(h('pre',{text:code.join('\n')}));code=null;}else code.push(line);continue;}
    if(line.startsWith('```')){flush();list=null;code=[];continue;}
    const hd=line.match(/^(#{1,3})\s+(.*)$/);if(hd){flush();list=null;const e=h('h'+(hd[1].length+1));inline(hd[2],e);out.append(e);continue;}
    const ul=line.match(/^[-*]\s+(.*)$/),ol=line.match(/^\d+[.)]\s+(.*)$/),nest=line.match(/^\s{2,}[-*]\s+(.*)$/);
    if(ul||ol){flush();sub=null;const tag=ul?'UL':'OL';if(!list||list.tagName!==tag){list=h(tag.toLowerCase());out.append(list);}const li=h('li');inline((ul||ol)[1],li);list.append(li);continue;}
    if(nest&&list&&list.lastChild){if(!sub){sub=h('ul');list.lastChild.append(sub);}const li=h('li');inline(nest[1],li);sub.append(li);continue;}
    if(/^\s{2,}\S/.test(line)&&list&&list.lastChild){inline(' '+line.trim(),(sub&&sub.lastChild)||list.lastChild);continue;}
    if(!line.trim()){flush();list=null;sub=null;continue;}
    list=null;sub=null;para.push(line.trim());}
  if(code!==null)out.append(h('pre',{text:code.join('\n')}));flush();return out;
}

// -- the update prompt ----------------------------------------------------------
function diffList(title,items,note,boxes){
  if(!items.length)return null;
  return h('section',{class:'mn-diff-group'},h('h3',{text:`${title} (${items.length})`}),
    h('ul',{class:'mn-diff'},items.map(i=>{
      const box=i.local_edit&&boxes?h('input',{type:'checkbox',value:i.id}):null;if(box)boxes.push(box);
      return h('li',{},h('span',{class:'mn-diff-title',text:i.title}),
        i.deprecated?h('span',{class:'mn-badge',text:'deprecated'}):null,
        i.preview?h('span',{class:'mn-badge',text:'preview'}):null,
        i.local_edit?h('span',{class:'mn-badge warn',text:note}):null,
        box?h('label',{class:'mn-keep'},box,' Replace my edits'):null);})));
}

export function reviewDialog(status,csrf,done){
  const d=status.diff,boxes=[];
  const msg=h('p',{class:'mn-msg',role:'status'});
  const busy=on=>{for(const b of dlg.querySelectorAll('button'))b.disabled=on;};
  const act=async(action,extra={})=>{busy(true);msg.textContent=action==='update'?'Updating the knowledge base copy…':'';
    try{const res=await post(csrf,{action,...extra});dlg.close();done&&done(action,res);}
    catch(e){msg.textContent=e.message;busy(false);}};
  const edited=[...d.changed,...d.removed,...d.added].some(i=>i.local_edit);
  const dlg=dialog('The Rook manual changed',
    h('p',{class:'mn-dim',text:`Rook ${status.version} · your copy: `+(status.imported?`Rook ${status.imported.version}`:'none')+'. Update it?'}),
    h('div',{class:'mn-diff-wrap'},
      diffList('Added',d.added,'name already used',boxes),
      diffList('Changed',d.changed,'you edited this',boxes),
      diffList('Removed',d.removed,'you edited this',boxes)),
    h('div',{class:'mn-dim mn-line'},h('span',{text:edited?'Your edits are kept unless ticked.':'Removed pages are archived.'}),
      info((edited?'Pages you edited in the knowledge base are kept as they are unless you tick Replace my edits. ':'')+'Removed pages are archived, not deleted. Not now asks again tomorrow; Skip version waits for the next manual change.','About updating the manual')),
    msg,
    h('div',{class:'mg-bar mn-actions'},
      btn('Update','primary',()=>act('update',{overwrite:boxes.filter(b=>b.checked).map(b=>b.value)})),
      btn('Not now','',()=>act('snooze')),
      btn('Skip version','',()=>act('decline'),{title:'Never for this version'})));
  dlg.classList.add('mn-dialog');
  return dlg;
}

function summary(d){
  const parts=[];if(d.added.length)parts.push(d.added.length+' added');if(d.changed.length)parts.push(d.changed.length+' changed');if(d.removed.length)parts.push(d.removed.length+' removed');
  return parts.join(', ');
}

// The banner at the top of the dashboard. Called once at start; quiet for
// people who are not the operator, and when there is nothing to ask.
export async function checkManualUpdate(host){
  useCss();loadCss();
  let s;try{s=await get({view:'status'});}catch{return null;}
  if(!s.prompt||!host)return null;
  const old=document.getElementById('manual-banner');if(old)old.remove();
  const close=()=>bar.remove();
  const bar=h('div',{class:'mg mn-banner',id:'manual-banner',role:'region','aria-label':'Manual update'},
    h('div',{class:'mn-banner-text',title:s.imported?'The Rook manual changed in this update: update your knowledge base copy?'
                                                    :'Rook now comes with a user manual: copy it into your knowledge base?'},
      h('strong',{text:s.imported?'Manual updated':'Manual available'}),h('span',{text:` · ${summary(s.diff)}`})),
    h('div',{class:'mn-banner-actions'},
      btn('Review','primary',()=>reviewDialog(s,s.csrf,()=>close())),
      btn('×','icon',async()=>{try{await post(s.csrf,{action:'snooze'});}catch(e){console.warn(e.message);}close();},{'aria-label':'Not now',title:'Not now'})));
  host.prepend(bar);return bar;
}

// -- the Help view ----------------------------------------------------------------
export async function mountManual(root){
  useCss();loadCss();
  const side=h('nav',{class:'mn-side','aria-label':'Manual pages'});
  const page=h('article',{class:'mn-page'});
  const search=h('input',{type:'search',placeholder:'Search the manual',class:'mn-search','aria-label':'Search the manual'});
  const toc=h('div',{class:'mn-toc'});
  const note=h('div',{class:'mn-note'});
  // Phones: the page list folds into a picker that names the open page (desktop keeps it open).
  const wide=matchMedia('(min-width:761px)');
  const pickLabel=h('span',{text:'Pages'});
  const picker=h('details',{class:'mn-picker',open:wide.matches},h('summary',null,pickLabel,h('small',{text:'Pages'})),search,toc,note);
  wide.addEventListener('change',()=>{picker.open=wide.matches;});
  side.append(picker);
  root.replaceChildren(h('div',{class:'mn-layout'},side,page));
  let pages=[],full=null,current='',status=null,csrf='';

  const open=async id=>{
    current=id||(pages[0]&&pages[0].id)||'';
    for(const a of toc.querySelectorAll('a'))a.classList.toggle('on',a.dataset.id===current);
    pickLabel.textContent=(pages.find(x=>x.id===current)||{}).title||'Pages';if(!wide.matches)picker.open=false;
    if(!current){page.replaceChildren(h('p',{class:'mn-dim',text:'The manual has no pages.'}));return;}
    page.replaceChildren(h('p',{class:'mn-dim',text:'Loading…'}));
    try{
      const p=await get({view:'page',id:current});
      const meta=h('div',{class:'mn-meta'},
        p.source==='knowledge'?h('span',{class:'mn-badge ok',text:p.edited?'Your copy (edited)':'Your copy',title:p.edited?'Your knowledge base copy (edited)':'Your knowledge base copy'})
                              :h('span',{class:'mn-badge',text:'Rook '+(status?.version||'')}),
        p.deprecated?h('span',{class:'mn-badge warn',text:'deprecated'}):null,
        p.preview?h('span',{class:'mn-badge warn',text:'preview'}):null,
        p.since?h('span',{class:'mn-dim',text:'since '+p.since}):null,
        p.source==='knowledge'?h('a',{href:'#knowledge?p='+encodeURIComponent(p.slug),text:'In Knowledge',title:'Open in Knowledge'}):null);
      const body=renderMarkdown(p.body,id=>go(id),id=>(pages.find(x=>x.id===id)||{}).title||id);
      // The page's own first heading is its title: show it once.
      const first=body.firstElementChild;if(first&&first.tagName==='H2'&&first.textContent.trim()===p.title.trim())first.remove();
      page.replaceChildren(h('h1',{text:p.title}),meta,body);
      page.scrollTop=0;window.scrollTo({top:0});
    }catch(e){page.replaceChildren(h('p',{class:'mn-error',text:e.message}));}
  };
  const go=id=>{const target='#help'+(id?'?page='+encodeURIComponent(id):'');if(location.hash!==target)history.pushState(null,'',target);open(id);};

  const drawToc=list=>{
    toc.replaceChildren(...list.map(p=>{const a=h('a',{href:'#help?page='+encodeURIComponent(p.id),text:p.title+(p.preview?' (preview)':''),class:(p.id===current?'on':'')+(p.deprecated?' old':'')});a.dataset.id=p.id;
      a.onclick=e=>{e.preventDefault();go(p.id);};return a;}));
    if(!list.length)toc.append(h('p',{class:'mn-dim',text:'No page matches.'}));
  };
  search.oninput=async()=>{
    const q=search.value.trim().toLowerCase();
    if(!q){drawToc(pages);return;}
    if(!full){try{full=(await get({view:'pages',full:1})).pages;}catch{full=pages;}}
    const words=q.split(/\s+/);
    drawToc(full.filter(p=>words.every(w=>(p.title+' '+(p.body||'')).toLowerCase().includes(w))));
  };

  const drawNote=()=>{
    note.replaceChildren();if(!status)return;
    const src=status.knowledge==='on'&&status.imported?'your copy ('+status.imported.version+')':status.knowledge==='on'?'not copied yet':'bundled (knowledge off)';
    note.append(h('p',{text:`Rook ${status.version} · ${status.pages} pages · `+src}));
    if(status.admin&&status.pending){
      note.append(h('div',{class:'mn-callout'},h('span',{text:status.imported?summary(status.diff):'Not in your knowledge base'}),
        btn(status.imported?'Review':'Copy','soft',()=>reviewDialog(status,csrf,async()=>{await refresh();open(current);}),{title:status.imported?'Review changes':'Review and copy'})));
    }
  };
  const refresh=async()=>{
    const [st,list]=await Promise.all([get({view:'status'}).catch(()=>null),get({view:'pages'})]);
    status=st&&{...st,admin:st.admin};csrf=list.csrf;pages=list.pages;full=null;drawToc(pages);drawNote();
  };
  const fromHash=()=>{const [v,q]=location.hash.slice(1).split('?');return v==='help'?new URLSearchParams(q||'').get('page')||'':null;};
  return {
    async activate(){try{await refresh();}catch(e){page.replaceChildren(h('p',{class:'mn-error',text:e.message}));return;}const id=fromHash();await open(id===null?current:id);},
    deactivate(){},
    open:go,
  };
}
