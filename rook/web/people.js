// Manage > People: who uses this hub and what each person may reach (access v2).
// Owners see everyone, what waits on them (requests in the person's own words,
// people with no access yet, new machines and bands) and change it; anyone else
// sees their own access and asks for more. Everything goes through
// /account/people/api (rook/hub/plugins/access/web.py). Stored text is rendered as text.
// Versioned like this module (?v=...), so a stale cache cannot pair it with an old manage.js.
const {h, useCss, btn, dialog, ago, more} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API='/account/people/api';
const KIND_LABEL={worker:'A machine',band:'A band',hub:'The hub\'s own tools',knowledge:'A knowledge folder',any:'Everything'};
const TIER_LABEL={read:'Read only',write:'Read and change files',exec:'Run commands (not change Rook)',admin:'Everything, including Rook'};
const CSS=`
.pp h2{margin:0;font:500 15px var(--sans)}
.pp-list{display:flex;flex-direction:column;border:1px solid var(--line);background:var(--panel)}
.pp-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 14px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line-soft)}
.pp-row:last-child{border-bottom:0}
.pp-row p{margin:2px 0 0;color:var(--dim);font-size:13px;line-height:1.45;overflow-wrap:anywhere}
.pp-row q{color:var(--fg)}
.pp-grants{grid-column:1/-1;display:flex;flex-direction:column;gap:4px;margin-top:4px}
.pp-grant{display:flex;gap:8px;align-items:center;font-size:13px}
.pp-grant span{flex:1 1 auto;min-width:0;overflow-wrap:anywhere}
.pp-tag{font:500 9px var(--mono);letter-spacing:1px;padding:2px 6px;border:1px solid var(--line2);color:var(--dim);text-transform:uppercase;margin-left:6px}
.pp-tag.owner{border-color:var(--accent-line);color:var(--accent)}
.pp-acts{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.pp-form{display:flex;flex-direction:column;gap:12px}
.pp details summary{cursor:pointer;color:var(--dim);font-size:13px}
.pp-hist{font-size:12px;color:var(--dim);display:flex;flex-direction:column;gap:6px;margin-top:8px}
.pp textarea{width:100%;min-height:72px}
@media(max-width:700px){.pp-row{grid-template-columns:minmax(0,1fr)}.pp-acts{justify-content:flex-start}}
`;

let csrf='';
async function call(body){
  const r=await fetch(API,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({csrf,...body})}:{});
  const d=await r.json().catch(()=>({error:'Bad response'}));
  if(d.csrf)csrf=d.csrf;
  if(!r.ok||d.error)throw Error(d.error||'Request failed');
  return d;
}

export async function mountPeople(root){
  useCss();
  if(!document.querySelector('style[data-pp]')){const s=document.createElement('style');s.dataset.pp='1';s.textContent=CSS;document.head.append(s);}
  const page=h('div',{class:'mg pp'});root.replaceChildren(page);
  const status=h('p',{class:'mg-status',role:'status','aria-live':'polite'});
  let d=null;
  function say(text,bad){status.className='mg-status'+(bad?' bad':'');status.textContent=text||'';}
  async function act(body,done){
    try{if(!csrf)await call();d=await call(body);render();say(done||'');}
    catch(e){say(e.message,true);throw e;}
  }
  const nameOf=uid=>(d.users||[]).find(u=>u.user===uid)?.name||uid;

  function giveDialog(pre={}){
    const c=d.choices;
    const people=(d.users||[]).filter(u=>u.role!=='owner');
    const user=h('select',{name:'user',required:true},people.map(u=>h('option',{value:u.user,text:u.name,selected:u.user===pre.user})));
    const kind=h('select',{name:'kind'},c.kinds.map(k=>h('option',{value:k,text:KIND_LABEL[k]||k,selected:k===(pre.kind||'worker')})));
    const which=h('label',{class:'mg-field'},'Which');
    const tier=h('select',{name:'tier'},c.tiers.map(t=>h('option',{value:t,text:TIER_LABEL[t],selected:t===c.default_tier})));
    const caps=h('input',{name:'caps',placeholder:'All of them, or e.g. file.*, media.*'});
    const note=h('input',{name:'note',maxlength:'500',placeholder:'Optional: why, in your words'});
    let ref=null;
    function drawWhich(){
      const k=kind.value;
      ref=k==='worker'?h('select',{name:'ref',required:true},c.machines.map(m=>h('option',{value:m.id,text:m.name,selected:m.id===pre.ref}))):
        k==='band'?h('select',{name:'ref',required:true},c.bands.map(b=>h('option',{value:b.id,text:b.name,selected:b.id===pre.ref}))):
        k==='knowledge'?h('input',{name:'ref',placeholder:'Folder slug, or * for all of it',value:pre.ref||'*',required:true}):null;
      which.replaceChildren('Which',ref||h('small',{text:'Nothing to pick.'}));
    }
    kind.onchange=drawWhich;drawWhich();
    const err=h('p',{class:'mg-error'});
    const form=h('form',{class:'pp-form',onsubmit:async e=>{e.preventDefault();err.textContent='';
      const capsList=caps.value.split(',').map(s=>s.trim()).filter(Boolean);
      try{await act({action:'grant',user:user.value,kind:kind.value,ref:ref?ref.value:'',tier:tier.value,caps:capsList,note:note.value},'Access given to '+nameOf(user.value)+'.');dlg.close();}
      catch(x){err.textContent=x.message;}}},
      people.length?null:h('p',{class:'mg-sub',text:'Everyone here is an owner: owners already reach everything.'}),
      h('label',{class:'mg-field'},'Person',user),h('label',{class:'mg-field'},'What',kind),which,
      h('label',{class:'mg-field'},'Up to',tier),h('label',{class:'mg-field'},'Tools',caps),h('label',{class:'mg-field'},'Note',note),
      err,h('div',{class:'mg-actions'},h('button',{class:'mg-btn primary',text:'Give access',disabled:!people.length})));
    const dlg=dialog('Give access',form);
  }
  function settle(what,label){
    if(!confirm(label))return;
    act({action:'settle',what},'Done.').catch(()=>{});
  }

  function waitingBox(){
    const w=d.waiting||{requests:[],no_access:[],resources:[]};
    const rows=[];
    for(const r of w.requests)rows.push(h('div',{class:'pp-row'},
      h('div',null,h('b',{text:r.name}),h('p',null,'Asked: ',h('q',{text:r.words}),' · '+ago(r.created)+(r.state==='asked'?' · the house agent asked you':''))),
      d.can_edit?h('div',{class:'pp-acts'},btn('Give access…','primary',()=>giveDialog({user:r.user})),btn('Not now','',()=>settle(r.id,'Say no to '+r.name+'\'s request?'))):null));
    for(const p of w.no_access)rows.push(h('div',{class:'pp-row'},
      h('div',null,h('b',{text:p.name}),h('p',{text:'Reaches nothing yet.'})),
      d.can_edit?h('div',{class:'pp-acts'},btn('Give access…','',()=>giveDialog({user:p.user}))):null));
    for(const r of w.resources)rows.push(h('div',{class:'pp-row'},
      h('div',null,h('b',{text:(r.kind==='worker'?'New machine ':'New band ')+r.label}),h('p',{text:'Only the owners reach it so far · '+ago(r.seen)})),
      d.can_edit?h('div',{class:'pp-acts'},btn('Share…','',()=>giveDialog({kind:r.kind,ref:r.ref})),btn('Owners only','',()=>settle(r.key,'Keep '+r.label+' with the owners?'))):null));
    return rows.length?h('section',{class:'mg',style:'gap:8px'},h('h2',{text:'Waiting on you'}),h('div',{class:'pp-list'},rows)):null;
  }

  function personRow(u){
    const owner=u.role==='owner';
    const grants=owner?null:h('div',{class:'pp-grants'},(u.grants||[]).map(g=>h('div',{class:'pp-grant'},
      h('span',{text:plainGrant(g)}),
      d.can_edit?btn('Remove','',()=>{if(!confirm('Take this away from '+u.name+'?'))return;act({action:'revoke',grant:g.id},'Removed.').catch(()=>{});}):null)));
    const actions=d.can_edit?h('div',{class:'pp-acts'},
      owner?null:btn('Give access…','',()=>giveDialog({user:u.user})),
      more([u.user!==d.me&&{label:owner?'Make a member':'Make an owner',run:()=>{
        const msg=owner?'Make '+u.name+' a member? They will reach only what grants give them.':'Make '+u.name+' an owner? Owners reach everything, including hub settings.';
        if(confirm(msg))act({action:'role',user:u.user,role:owner?'member':'owner'},'Done.').catch(()=>{});}}].filter(Boolean),'More for '+u.name)):null;
    return h('div',{class:'pp-row'},
      h('div',null,h('b',{text:u.name}),h('span',{class:'pp-tag'+(owner?' owner':''),text:u.role==='new'?'new':u.role}),h('p',{text:u.plain})),
      actions,grants);
  }
  function plainGrant(g){
    const m=(d.choices.machines.find(x=>x.id===g.ref)||{}).name, b=(d.choices.bands.find(x=>x.id===g.ref)||{}).name;
    const what={worker:'Machine '+(m||g.ref),band:'Band '+(b||g.ref),hub:'The hub\'s tools',knowledge:g.ref==='*'?'All knowledge':'Knowledge folder '+g.ref,any:'Everything'}[g.kind]||g.kind;
    return what+(g.caps&&g.caps.join(',')!=='*'?' ('+g.caps.join(', ')+')':'')+' · '+(TIER_LABEL[g.tier]||g.tier).toLowerCase();
  }

  function ownerView(){
    const hist=h('details',null,h('summary',{text:'History'}),h('div',{class:'pp-hist'},(d.history||[]).map(x=>
      h('div',null,h('b',{text:ago(x.ts)+' · '}),x.actor+' · '+x.action+(x.user_id?' '+nameOf(x.user_id):'')+(x.ref&&x.action!=='role'?' '+x.ref:'')+(x.words?' — “'+x.words+'”':'')))));
    return [waitingBox(),
      h('section',{class:'mg',style:'gap:8px'},h('div',{class:'mg-bar'},h('h2',{text:'People'}),h('span',{class:'mg-grow'}),
        d.can_edit?btn('+ Give access','primary',()=>giveDialog()):null),
        d.can_edit?null:h('p',{class:'mg-sub',text:'This sign-in also works for agents, so it can only look. Sign in with Google or your own password to change access.'}),
        h('div',{class:'pp-list'},(d.users||[]).map(personRow))),hist];
  }
  function memberView(){
    const box=h('textarea',{name:'words',maxlength:'1000',required:true,placeholder:'For example: the living room PC and the family recipes'});
    const ask=d.request?h('p',null,'You asked: ',h('q',{text:d.request.words}),' · waiting for an owner'):
      h('form',{class:'pp-form',onsubmit:async e=>{e.preventDefault();await act({action:'request',words:box.value},'Asked.').catch(()=>{});}},
        h('label',{class:'mg-field'},'What would you like to use?',box),h('div',{class:'mg-actions'},h('button',{class:'mg-btn primary',text:'Ask'})));
    return [h('section',{class:'mg-box mg-pad',style:'display:flex;flex-direction:column;gap:10px'},h('h2',{text:'Your access'}),h('p',{text:d.me.plain}),ask)];
  }
  function render(){page.replaceChildren(status,...(d.owner?ownerView():memberView()).filter(Boolean));}

  return {
    async activate(){
      try{d=await call();render();}
      catch(e){page.replaceChildren(h('p',{class:'mg-error',text:e.message}));}
    },
    deactivate(){},
  };
}
