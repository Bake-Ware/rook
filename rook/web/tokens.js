// API tokens: one row per token, with the picture its identity shows in chat.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old manage.js.
const {h, ago, day, useCss, btn, chips, table, two, dialog, menu, more, info, copy} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const SOON=14*86400, IDLE=30*86400;

export async function mountTokens(root){
  useCss();
  let csrf='',data={tokens:[],avatars:{}},filter='all',query='',identity='',busy=false;
  const status=h('p',{class:'mg-status settings-status',role:'status','aria-live':'polite'});
  const search=h('input',{type:'search',class:'mg-grow','aria-label':'Filter tokens',placeholder:'Filter',oninput:()=>{query=search.value.trim().toLowerCase();draw();}});
  const chipHost=h('span',{class:'mg-actions'}),list=h('div',{class:'mg-box mg-cards-sm'}),summary=h('span');
  const file=h('input',{type:'file',class:'avatar-file',accept:'image/png,image/jpeg,image/webp',hidden:true});
  const label=h('input',{name:'name',maxlength:'64',placeholder:'claude, codex, ci-runner',required:true,style:'font-family:var(--mono)'});
  const person=h('select',{name:'user','aria-label':'Belongs to'});
  const ttl=h('select',{name:'ttl'},[['2592000','30 days'],['86400','1 day'],['604800','7 days'],['7776000','90 days'],['31536000','1 year'],['','Never']].map(([v,t])=>h('option',{value:v,text:t})));
  const formError=h('small',{class:'mg-error dialog-error',role:'alert'});
  const create=h('form',{class:'mg-box callout mg-pad token-create',hidden:true,onsubmit:e=>{e.preventDefault();perform(async()=>{
      const made=await api({op:'create',name:label.value,ttl:ttl.value?Number(ttl.value):null,user:person.value||null});
      label.value='';create.hidden=true;showSecret(made.token);await refresh();},formError);}},
    h('div',{class:'mg-bar',style:'align-items:flex-end'},
      h('label',{class:'mg-field',style:'flex:2 1 220px'},'Label',label),h('label',{class:'mg-field',style:'flex:1 1 160px'},'Belongs to',person),h('label',{class:'mg-field',style:'flex:1 1 140px'},'Expires',ttl),
      h('button',{class:'mg-btn soft',text:'Create',title:'Create and show once'}),info('The label is the identity this token’s work is recorded under. A token reaches exactly what the person it belongs to reaches, from any client. The secret is shown once.','About tokens')),
    formError);
  const otherPicture=()=>{const id=h('input',{placeholder:'agent:claude_laptop',maxlength:'200',required:true});const d=dialog('Picture for another identity',h('form',{onsubmit:e=>{e.preventDefault();identity=id.value.trim();d.close();file.click();}},h('label',{class:'mg-field'},'Identity',id),h('div',{class:'mg-actions end'},h('button',{class:'mg-btn primary',text:'Choose picture…'}))));id.focus();};
  root.replaceChildren(h('div',{class:'mg'},
    h('div',{class:'mg-bar'},search,chipHost,btn('New token','primary',()=>{create.hidden=!create.hidden;if(!create.hidden)label.focus();},{'data-create':''}),more([{label:'Picture for another identity…',run:otherPicture}],'More token actions')),
    create,status,list,
    h('div',{class:'mg-bar mg-sub',style:'gap:8px'},summary,info('The square is the picture shown in chat for that identity; tap it to change it.','About pictures')),
    file));

  async function api(body){
    const r=await fetch('/account/tokens/api',body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,csrf})}:{});
    if(!r.headers.get('content-type')?.includes('application/json'))throw Error('Sign in to an owner account to manage tokens.');
    const d=await r.json();if(!r.ok)throw Error(d.error||'Token request failed.');return d;
  }
  async function perform(work,target){
    if(busy)return;busy=true;(target||status).textContent='';status.classList.remove('bad');
    try{await work();}catch(e){(target||status).textContent=e.message;if(!target)status.classList.add('bad');}finally{busy=false;}
  }
  async function refresh(){
    const session=await fetch('/account/session');if(!session.ok)throw Error('Sign in to an owner account.');
    const me=(await session.json()).user;csrf=me.csrf;data=await api();
    const keep=person.value||me.id;person.replaceChildren(...(data.people||[]).map(p=>h('option',{value:p.id,text:p.name})));person.value=keep;person.disabled=!(data.people||[]).length;
    draw();
  }
  const nameOf=uid=>((data.people||[]).find(p=>p.id===uid)||{}).name||uid||'—';
  function showSecret(token){
    const c=btn('Copy token','soft',()=>copy(token,c),{'data-copy-token':''});
    dialog('Copy your new token',h('p',{text:'This secret is shown once. Copy it before closing.'}),h('pre',{class:'token-secret',tabindex:'0',text:token,'data-secret':''}),h('div',{class:'mg-actions end'},c));
  }
  function picture(id){
    const v=data.avatars[id],attrs={class:'mg-avatar','aria-label':'Change picture for '+id,title:'Change picture','data-picture':id,onclick:()=>{identity=id;file.click();}};
    return v?h('input',{...attrs,type:'image',src:'/api/avatar?id='+encodeURIComponent(id)+'&v='+v,alt:''}):h('button',{...attrs,type:'button',text:id.split(':').pop().slice(0,2).toUpperCase()});
  }
  function draw(){
    const now=Date.now()/1000;
    const rows=data.tokens.map(t=>({token:t,id:'agent:'+(t.name||t.id),label:t.name||t.id,soon:!!t.expires_at&&t.expires_at-now<SOON,idle:!t.last_used_at||now-t.last_used_at>IDLE}));
    const house=data.relay&&data.relay.house;
    const phones=((data.relay&&data.relay.devices)||[]).map(d=>({id:d.identity,label:d.label,relayer:d,relayId:d.identity,phone:!String(d.identity).startsWith('voice:'),voice:String(d.identity).startsWith('voice:'),soon:false,idle:false}));
    rows.unshift(...(house?[{id:house.identity,label:house.label,relayer:house,relayId:'house',soon:false,idle:false}]:[]),...phones);
    const taken=new Set(rows.map(r=>r.id));
    for(const id of new Set(['user:operator','agent:static',...Object.keys(data.avatars)]))if(!taken.has(id))rows.push({id,label:id.split(':').pop(),soon:false,idle:false});
    const tokens=rows.filter(r=>r.token);
    chipHost.replaceChildren(...chips([['all','All '+tokens.length],['soon','Expiring '+tokens.filter(r=>r.soon).length],['idle','Unused '+tokens.filter(r=>r.idle).length]],filter,id=>{filter=id;draw();}));
    const shown=rows.filter(r=>(filter==='all'||(r.token&&r[filter]))&&(!query||(r.label+' '+r.id).toLowerCase().includes(query)));
    const expires=t=>{if(!t.expires_at)return 'never';const d=Math.ceil((t.expires_at-now)/86400);return d<=0?'expired':d===1?'in 1 day':'in '+d+' days';};
    list.replaceChildren(table('44px minmax(0,1.5fr) minmax(0,1fr) 100px 110px 110px 96px 44px',['','Label · identity','Belongs to','Created','Expires','Last used',h('span',null,'Relays',info('May relay my approvals: this client may approve or deny waiting items for you when you tell it your decision (your own words are recorded). Secrets, tokens, policy, settings, job locks and the dashboard-only list always need you here. Off for everyone until you turn it on.','About relaying')),''],shown.length?shown.map(r=>{
      const t=r.token,more=btn('⋯','icon',()=>menu(more,[
        {label:(data.avatars[r.id]?'Change':'Set')+' picture',run:()=>{identity=r.id;file.click();}},
        data.avatars[r.id]&&{label:'Clear picture',run:()=>perform(async()=>{await api({op:'avatar_clear',identity:r.id});await refresh();})},
        t&&(data.people||[]).length&&{label:'Belongs to…',run:()=>belongs(t)},
        t&&{label:'Revoke…',danger:true,run:()=>revoke(t)}]),{'aria-label':'More actions for '+r.label,'data-more':r.id});
      return h('div',{class:'mg-row'+(t?' token-row':'')},picture(r.id),
        two(r.label,t?r.id+' · '+t.preview:r.id+' · no token'),
        h('span',{class:'mg-dimtext','data-person':t?t.id:''},t?h('span',{class:'sm-only',text:'belongs to '}):null,t?nameOf(t.user):'—'),
        h('span',{class:'mg-dimtext'},t?h('span',{class:'sm-only',text:'created '}):null,t?day(t.created_at):'—'),
        h('span',{class:'mg-mono '+(t&&r.soon?'mg-warn':'mg-dimtext')},t?h('span',{class:'sm-only',text:'expires '}):null,t?expires(t):'—'),
        h('span',{class:t&&!t.last_used_at?'mg-warn':'mg-dimtext'},t?h('span',{class:'sm-only',text:'used '}):null,t?ago(t.last_used_at):'—'),
        relaySwitch(r),more);
    }):[h('div',{class:'mg-empty',text:tokens.length?'Nothing matches.':'No tokens yet.'})],760));
    summary.textContent=shown.filter(r=>r.token).length+' of '+tokens.length+' tokens';
  }
  function relaySwitch(r){
    const who=r.token||r.relayer,rid=r.token?r.token.id:r.relayId;
    if(!who||!(data.relay&&data.relay.available))return h('span',{class:'mg-dimtext',text:'—'});
    const box=h('input',{type:'checkbox','data-relay':rid,'aria-label':'May relay my approvals: '+r.label,title:who.relay?'Relays your approvals'+(who.relay_by?' (turned on by '+who.relay_by+')':''):'May relay my approvals'});
    box.checked=!!who.relay;
    const set=on=>perform(async()=>{await api({op:'relay',id:rid,enabled:on});await refresh();status.textContent=on?r.label+' may relay your approvals.':r.label+' no longer relays your approvals.';});
    box.onchange=()=>{const on=box.checked;box.checked=!on;if(!on){set(false);return;}
      const err=h('small',{class:'mg-error dialog-error',role:'alert'});
      const d=dialog('May relay my approvals',h('form',{onsubmit:e=>{e.preventDefault();perform(async()=>{await api({op:'relay',id:rid,enabled:true});d.close();await refresh();status.textContent=r.label+' may relay your approvals.';},err);}},
        r.phone?h('p',null,'Tapping Yes or No on a Rook decision card on ',h('strong',{text:r.label}),' approves or denies that waiting item (held calls, huddle cards, questions). Each tap is recorded with the phone and the time and shown beside the item.')
        :r.voice?h('p',null,'Answering a card out loud ("clear my cards") approves or denies it, only when the voice is sure it is you by your voiceprint. Loosening the trust map is read back first and needs your spoken yes and a check word. Each answer is recorded with your words and the voiceprint score.')
          :h('p',null,h('strong',{text:r.label}),' may approve or deny waiting items (held calls, huddle cards, questions) when you tell it your decision. Each one records your words and shows them beside the item.'),
        h('p',{class:'mg-dimtext',text:'Secrets, tokens, policy, settings, job locks and the dashboard-only list (Settings > Approvals) still need you here.'}),
        err,h('div',{class:'mg-actions end'},h('button',{class:'mg-btn primary',text:'Allow relaying'}))));};
    return h('label',{class:'mg-dimtext token-relay'},box,h('span',{class:'sm-only',text:' relays approvals'}));
  }
  function belongs(t){
    const err=h('small',{class:'mg-error dialog-error',role:'alert'});
    const pick=h('select',{'aria-label':'Belongs to'},(data.people||[]).map(p=>h('option',{value:p.id,text:p.name})));pick.value=t.user||'';
    const d=dialog('Whose token is this?',h('form',{onsubmit:e=>{e.preventDefault();perform(async()=>{await api({op:'owner',id:t.id,user:pick.value});d.close();await refresh();},err);}},
      h('p',null,h('strong',{text:t.name}),' reaches exactly what this person reaches.'),h('label',{class:'mg-field'},'Belongs to',pick),err,h('div',{class:'mg-actions end'},h('button',{class:'mg-btn primary',text:'Save'}))));
  }
  function revoke(t){
    const err=h('small',{class:'mg-error dialog-error',role:'alert'});
    const d=dialog('Revoke token',h('form',{class:'token-revoke',onsubmit:e=>{e.preventDefault();perform(async()=>{await api({op:'revoke',id:t.id,confirm:true});d.close();await refresh();status.textContent='Token revoked.';},err);}},
      h('p',null,'Revoke ',h('strong',{text:t.name}),'? Clients using it will lose access.'),err,h('div',{class:'mg-actions end'},h('button',{class:'mg-btn danger',text:'Revoke token'}))));
  }
  file.onchange=()=>perform(async()=>{
    const f=file.files[0];if(!f)return;
    const bitmap=await createImageBitmap(f),canvas=document.createElement('canvas');canvas.width=canvas.height=96;
    const side=Math.min(bitmap.width,bitmap.height);
    canvas.getContext('2d').drawImage(bitmap,(bitmap.width-side)/2,(bitmap.height-side)/2,side,side,0,0,96,96);bitmap.close();
    await api({op:'avatar',identity,data:canvas.toDataURL('image/png')});file.value='';await refresh();
  });
  await refresh();
  return {refresh,deactivate(){document.querySelectorAll('dialog.mg-dialog[open]').forEach(d=>d.close());}};
}
