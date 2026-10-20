// Agent instructions editor. All stored text is rendered as text, never HTML.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, ago, useCss, btn, info, menu} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const KINDS=[
 ['server','On connect','Sent once, when an agent connects. Keep it to what every agent needs.',6000],
 ['contact','Reaching the user','Appended to the connection instructions: how an agent contacts the person when they are not in the conversation (notifications, voice, a messaging bridge).',2000],
 ['hygiene','While working','Sent when a claimed task sits idle for 30 minutes with work since its last handoff. Placeholders: {slug} {title} {id} {idle} {actor}.',2000],
 ['tool','Tool tips','Added to the tool’s description as “Tip: …”. Agents see it when they list tools.',1000],
 ['cap','Capability tips','Attached as _tips to the first rook_call reply in a session whose cap starts with this prefix.',1000],
];
const TITLES={server:'Connection instructions',contact:'Contacting the user',hygiene:'Hygiene prompt'};
const SAMPLE={slug:'menu-compiler',title:'Menu compiler',id:'t_4f32…',idle:'34',actor:'agent:claude'};

export async function mountGuidance(root){
  useCss();
  let csrf='',tools=[],editable=true,slots=[],picked='',query='',adding=false;
  const status=h('p',{class:'mg-status',role:'status'}),summaryText=h('span'),summary=h('div',{class:'mg-bar mg-sub'},summaryText,info('Tips are guidance only: they never allow or block anything.','About instructions'));
  const search=h('input',{type:'search','aria-label':'Find a text',placeholder:'Find a tool, cap or phrase',style:'border-width:0 0 1px;width:100%',oninput:()=>{query=search.value.trim().toLowerCase();drawList();}});
  const items=h('div'),editor=h('div',{class:'mg-box strong mg-main',style:'gap:0'});
  const add=h('button',{type:'button',class:'mg-btn soft',style:'margin:10px 16px 14px;border-style:dashed',text:'Add a tip',onclick:()=>{adding=true;if(!wide.matches)picker.open=false;draw();}});
  // Phones: the list folds into a picker that names the open text (desktop keeps it open).
  const wide=matchMedia('(min-width:801px)');
  const pickLabel=h('span');
  const picker=h('details',{class:'gd-picker',open:wide.matches},h('summary',null,pickLabel,h('small',{text:'Texts'})),search,items,add);
  wide.addEventListener('change',()=>{picker.open=wide.matches;});
  if(!document.querySelector('style[data-gd]'))document.head.append(h('style',{'data-gd':'1',text:`.gd-picker>summary{display:none}
@media(max-width:800px){.mg .gd-picker>summary{display:flex;align-items:center;gap:10px;min-height:48px;padding:0 14px;cursor:pointer;list-style:none;color:var(--accent);font:500 15px var(--sans)}
.gd-picker>summary::-webkit-details-marker{display:none}.gd-picker>summary small{margin-left:auto;font:10px var(--mono);letter-spacing:1px;text-transform:uppercase;color:var(--dim)}
.gd-picker>summary::after{content:'▾';color:var(--dim)}.gd-picker[open]>summary::after{content:'▴'}.gd-nav{flex:1 1 100%!important}}`}));
  root.replaceChildren(h('div',{class:'mg'},summary,status,h('div',{class:'mg-split'},h('div',{class:'mg-box mg-nav gd-nav',style:'flex:0 1 280px;display:flex;flex-direction:column'},picker),editor)));

  async function api(body){const r=await fetch('/account/guidance/api',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})});const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Request failed');return d;}
  const name=s=>TITLES[s.kind]||s.key.slice(s.key.indexOf(':')+1);
  const kind=k=>KINDS.find(x=>x[0]===k)||KINDS[3];
  function seen(s,text){
    if(!text)return s.kind==='server'||s.kind==='contact'||s.kind==='hygiene'?'(nothing is sent)':'(no tip: the agent sees nothing extra)';
    if(s.kind==='hygiene')return text.replace(/\{(slug|title|id|idle|actor)\}/g,(_,k)=>SAMPLE[k]);
    if(s.kind==='tool')return 'Tip: '+text;
    if(s.kind==='cap')return '"_tips": ['+JSON.stringify(text)+']';
    return text;
  }
  function draw(){
    const edited=slots.filter(s=>s.edited).length;
    summaryText.textContent=edited+' of '+slots.length+' edited'+(editable?'':' · store unavailable: defaults in effect, editing off');
    add.hidden=!editable;
    if(!adding&&!slots.some(s=>s.key===picked))picked=slots[0]?.key||'';
    drawList();adding?drawAdder():drawEditor(slots.find(s=>s.key===picked));
    const cur=slots.find(s=>s.key===picked);pickLabel.textContent=adding?'Add a tip':cur?name(cur):'Texts';
  }
  function drawList(){
    items.replaceChildren(...KINDS.flatMap(([k,title])=>{
      const all=slots.filter(s=>s.kind===k),shown=all.filter(s=>!query||(s.key+' '+name(s)+' '+s.text).toLowerCase().includes(query));
      if(!shown.length)return [];
      return [h('div',{class:'mg-label mg-navlabel',text:title+(all.length>1?' · '+all.length:'')}),...shown.map(s=>h('button',{type:'button',class:'mg-navitem'+(!adding&&s.key===picked?' on':''),'aria-current':!adding&&s.key===picked?'true':null,onclick:()=>{picked=s.key;adding=false;if(!wide.matches)picker.open=false;draw();}},
        h('span',{text:name(s),class:TITLES[s.kind]?'':'mg-mono'}),s.edited?h('span',{class:'mg-tag',text:s.text?'EDITED':'OFF'}):null))];
    }));
    if(!items.childElementCount)items.append(h('div',{class:'mg-empty',text:'Nothing matches.'}));
  }
  async function act(button,err,body,pick){
    button.disabled=true;err.textContent='';
    try{const d=await api(body);slots=d.slots;adding=false;if(pick)picked=pick;draw();status.textContent='Saved.';}
    catch(e){err.textContent=e.message;}finally{button.disabled=false;}
  }
  function drawEditor(s){
    if(!s){editor.replaceChildren(h('div',{class:'mg-empty',text:'No instructions to show.'}));return;}
    const [, , when,limit]=kind(s.kind);
    const err=h('small',{class:'mg-error',role:'alert'}),count=h('span',{class:'mg-mono mg-sub'}),fill=h('i'),preview=h('pre',{class:'quote'});
    const ta=h('textarea',{'aria-label':'Text sent to the agent',rows:s.kind==='server'?'14':'7',style:'font:13px/1.6 var(--mono)',disabled:!editable});ta.value=s.text;
    const sync=()=>{const n=ta.value.length;count.textContent=n.toLocaleString()+' of '+limit.toLocaleString()+' characters';fill.style.width=Math.min(100,n/limit*100)+'%';fill.className=n>limit?'over':'';preview.textContent=seen(s,ta.value);};
    ta.oninput=sync;sync();
    const save=btn('Save','primary',()=>act(save,err,{action:'set',key:s.key,text:ta.value}),{disabled:!editable});
    const extra=h('div',{class:'mg-pad',hidden:true,style:'padding-top:0'});
    const showHistory=async()=>{
      if(!extra.hidden&&extra.dataset.show==='h'){extra.hidden=true;return;}
      extra.dataset.show='h';extra.hidden=false;extra.replaceChildren(h('p',{class:'mg-sub',text:'Loading…'}));
      try{const r=await fetch('/account/guidance/api?history='+encodeURIComponent(s.key));const d=await r.json();
        extra.replaceChildren(h('div',{class:'mg-label',text:'History'}),...(d.history.length?d.history.flatMap(e=>[h('small',{class:'mg-sub',text:new Date(e.ts*1000).toLocaleString()+' · '+e.actor+(e.text===null?' · reset to default':'')}),e.text===null?null:h('pre',{text:e.text})]):[h('p',{class:'mg-sub',text:'No edits yet.'})]));}
      catch(e){extra.replaceChildren(h('p',{class:'mg-error',text:e.message}));}};
    const showDefault=()=>{if(!extra.hidden&&extra.dataset.show==='d'){extra.hidden=true;return;}extra.dataset.show='d';extra.hidden=false;extra.replaceChildren(h('div',{class:'mg-label',text:'Default'}),h('pre',{text:s.default}));};
    const moreBtn=btn('⋯','icon more-btn',()=>menu(moreBtn,[
      {label:'History',run:showHistory},
      s.edited&&s.default!==null&&{label:'Show default',run:showDefault},
      s.edited&&editable&&{label:s.default===null?'Remove':'Reset to default',danger:s.default===null,run:()=>act(moreBtn,err,{action:'reset',key:s.key})}]),{'aria-label':'More actions','aria-haspopup':'menu',title:'History, default, reset'});
    editor.replaceChildren(
      h('div',{class:'mg-panel-head',style:'flex-wrap:nowrap'},h('div',{class:'mg-two',style:'flex:1 1 0;min-width:0'},h('h2',{text:name(s),class:TITLES[s.kind]?'':'mg-mono'}),h('small',{style:'white-space:normal',text:(s.edited?(s.text?'Edited':'Turned off')+' by '+s.actor+' '+ago(s.updated):'Default')})),h('div',{class:'mg-actions'},info(when,'When it is sent'),moreBtn)),
      h('div',{class:'mg-pad'},ta,h('div',{class:'mg-bar',style:'justify-content:space-between'},h('div',{class:'mg-actions mg-grow'},h('div',{class:'mg-meter'},fill),count),err,save),
        h('div',{class:'mg-label',text:'What the agent sees'}),preview),extra);
  }
  function drawAdder(){
    const err=h('small',{class:'mg-error',role:'alert'});
    const k=h('select',{'aria-label':'Kind'},h('option',{value:'cap',text:'Capability prefix'}),h('option',{value:'tool',text:'Tool'}));
    const cap=h('input',{placeholder:'hermes. or deluge.add','aria-label':'Capability prefix',class:'mg-grow',style:'font-family:var(--mono)'});
    const tool=h('select',{'aria-label':'Tool',hidden:true,class:'mg-grow'},tools.map(t=>h('option',{value:t,text:t})));
    k.onchange=()=>{tool.hidden=k.value!=='tool';cap.hidden=k.value==='tool';};
    const ta=h('textarea',{rows:'6',placeholder:'Terse advice an agent needs at that moment.','aria-label':'Tip text',style:'font:13px/1.6 var(--mono)'});
    const save=btn('Add','primary',()=>{const key=k.value+':'+(k.value==='tool'?tool.value:cap.value.trim());act(save,err,{action:'set',key,text:ta.value},key);});
    editor.replaceChildren(h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:'Add a tip'})),info('Keep it short and about the tool or capability, not one host: it goes out with every matching call.','About tips')),
      h('div',{class:'mg-pad'},h('div',{class:'mg-bar'},k,cap,tool),ta,h('div',{class:'mg-actions end'},err,btn('Cancel','',()=>{adding=false;draw();}),save)));
    cap.focus();
  }
  async function load(){const r=await fetch('/account/guidance/api');const d=await r.json();if(!r.ok||d.error)throw Error(d.error||'Sign in with your operator account to edit agent instructions.');csrf=d.csrf;tools=d.tools;editable=d.editable;slots=d.slots;draw();}
  return {activate(){load().catch(e=>{editor.replaceChildren(h('p',{class:'mg-empty mg-bad',text:e.message}));});},deactivate(){}};
}
