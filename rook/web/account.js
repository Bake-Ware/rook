// Account & access: who you are and how you sign in on the left; your bands
// and the devices enrolled in them on the right.
import {h,ago,day,useCss,btn,table,two,dialog,menu,accountAction,reveal,pairing} from '/account/bands/assets/manage.js';

export async function mountAccount(root, {onChange = () => {}} = {}) {
  useCss();
  let csrf='',user=null,bands=[],tab='bands',asked='';
  const status=h('p',{class:'mg-status settings-status',role:'status','aria-live':'polite'});
  const left=h('div',{class:'mg-side',style:'display:flex;flex-direction:column;gap:18px;flex:1 1 340px'}),right=h('div',{class:'mg-box mg-main',style:'flex:2 1 520px;gap:0'});
  root.replaceChildren(h('div',{class:'mg'},status,h('div',{class:'mg-split'},left,right)));

  async function act(fields,done){
    status.className='mg-status settings-status';status.textContent='';
    try{const d=await accountAction(csrf,fields);if(d.pairing){pairing(d);return d;}await refresh();onChange();(done||(()=>{status.textContent='Changes saved.';}))(d);return d;}
    catch(e){status.className+=' bad';status.textContent=e.message;}
  }
  function form(fields,submit,...kids){
    return h('form',{onsubmit:e=>{e.preventDefault();const b=e.submitter;if(b)b.disabled=true;const data=Object.fromEntries(new FormData(e.target));
      Promise.resolve(submit({...fields,...data})).finally(()=>{if(b)b.disabled=false;});}},kids);
  }
  async function refresh(){
    const query=location.hash.split('?')[1]||'';
    const [r,b]=await Promise.all([fetch('/account/component'+(query?'?'+query:'')),fetch('/account/bands/api',{headers:{Accept:'application/json'}})]);
    if(!r.ok||!r.headers.get('content-type')?.includes('application/json'))throw Error('Sign in to your account and reload this view.');
    const d=await r.json();csrf=d.csrf;user=d.user;bands=b.ok?(await b.json()).bands:[];
    draw();
    const params=new URLSearchParams(query);
    if(params.get('invite')&&asked!==query){asked=query;acceptDialog(params.get('invite'));}
    if(params.get('device')&&asked!==query){asked=query;const code=left.querySelector('[name=user_code]');code.value=params.get('device');code.focus();}
    return d;
  }

  function draw(){
    const avatar=user.avatar?h('img',{class:'mg-avatar big',src:'/account/avatar/'+encodeURIComponent(user.id)+'?v='+user.avatar_updated,alt:''}):h('span',{class:'mg-avatar big',text:(user.name||'?').slice(0,2).toUpperCase()});
    const upload=h('input',{type:'file',name:'image',accept:'image/png,image/jpeg,image/webp',hidden:true,onchange:()=>{if(!upload.files[0])return;const fd={op:'avatar_upload',image:upload.files[0]};act(fd);}});
    const profile=h('div',{class:'mg-box mg-pad'},h('div',{class:'mg-label',text:'Profile'}),
      h('div',{class:'mg-actions',style:'gap:14px;flex-wrap:nowrap'},avatar,h('div',{class:'mg-two'},h('b',{class:'profile-name',style:'font-size:18px',text:user.name}),h('small',{text:user.email||user.username||''}))),
      form({op:'profile'},f=>act(f,()=>{status.textContent='Name saved.';}),h('div',{class:'mg-actions',style:'align-items:flex-end'},h('label',{class:'mg-field mg-grow'},'Display name',h('input',{name:'name',value:user.name,maxlength:'100',required:true})),h('button',{class:'mg-btn soft',text:'Save name'}))),
      h('div',{class:'mg-actions'},h('span',{class:'mg-sub mg-grow',style:'font-size:12px',text:'Picture: '+(!user.avatar?'initials':user.avatar_source==='custom'?'uploaded':'Google photo')}),
        btn('Upload…','',()=>upload.click()),
        user.google_enabled&&user.avatar_source!=='google'?btn('Use Google photo','',()=>act({op:'avatar',source:'google'})):null,
        user.avatar?btn('Use initials','',()=>act({op:'avatar',source:'initials'})):null,upload),
      h('div',{class:'mg-actions'},btn('Sign out','',()=>act({op:'logout'}))));
    const login=(name,detail,action)=>h('div',{class:'mg-row',style:'grid-template-columns:minmax(0,1fr) auto;min-height:52px'},two(name,detail),action||h('span'));
    const signin=h('div',{class:'mg-box'},h('div',{class:'mg-label mg-navlabel',text:'Sign-in'}),
      user.google_enabled?login('Google',user.google_connected?'Connected':'Not connected',user.google_connected?btn('Disconnect','',()=>{if(confirm('Disconnect Google from this account?'))act({op:'unlink'});}):h('a',{class:'mg-btn',href:'/auth/google?link=1',text:'Connect'})):null,
      user.managed_login?login('Password','Managed in the server configuration'):user.has_password?login('Password','Set for '+user.username,btn('Change','',passwordDialog)):login('Password','No local login',btn('Add','',localDialog)));
    const installer=h('div',{class:'mg-box callout mg-pad'},h('div',{class:'mg-label mg-warn',text:'Approve an installer'}),
      h('p',{class:'mg-sub',style:'font-size:13px;color:var(--fg)',text:'An installer you started shows a code. Approving it lets that installer fetch your band configurations once.'}),
      form({op:'device_approve'},f=>act(f,()=>{status.textContent='Installer approved.';}),h('div',{class:'mg-actions'},h('input',{name:'user_code',maxlength:'8',required:true,placeholder:'ABCD1234','aria-label':'Installer code',class:'mg-grow',style:'font:500 16px var(--mono);letter-spacing:2px'}),h('button',{class:'mg-btn primary',text:'Approve'}))));
    left.replaceChildren(profile,signin,installer);
    drawRight();
  }

  function drawRight(){
    const owned=bands.filter(b=>b.role==='owner');
    const devices=owned.flatMap(b=>(b.devices||[]).map(d=>({...d,band:b})));
    const tabs=[['bands','Your bands · '+bands.length],['devices','Devices · '+devices.filter(d=>d.active).length]];
    const add=tab==='bands'?btn('Accept an invitation','soft',()=>acceptDialog('')):btn('Pair a device','soft',pairDialog,{disabled:!owned.some(b=>b.active)});
    let body,foot;
    if(tab==='bands'){
      body=table('minmax(0,1.4fr) minmax(0,1fr) 90px 44px',['Band','Workers','Your role',''],bands.length?bands.map(b=>{
        const on=b.workers.filter(w=>w.online).length,more=btn('⋯','icon',()=>menu(more,[
          {label:'Open on the Bands page',href:'#bands'},
          b.active&&{label:'Download configuration',href:'/account/configurations?band='+encodeURIComponent(b.id)},
          b.role==='owner'&&b.active&&{label:'Pairing code for a device',run:()=>act({op:'pair',band_id:b.id})},
          b.role==='owner'&&b.active&&{label:'Invite a person',run:()=>act({op:'invite',band_id:b.id},d=>reveal('Invite a person','Share this single-use link. It expires in seven days.',d.text))}]),{'aria-label':'More actions for '+b.name});
        return h('div',{class:'mg-row'},two(b.name,(b.primary?'Default band · ':'')+(b.active?'key v'+b.epoch:'revoked')),h('span',{class:'mg-mono mg-dimtext',text:on+' of '+b.workers.length+' online'}),h('span',{class:'mg-mono '+(b.role==='owner'?'mg-good':'mg-dimtext'),text:b.role}),more);
      }):[h('div',{class:'mg-empty',text:'You are not in any band yet. Accept an invitation to join one.'})],520);
      foot=h('span',null,'Members, keys and worker moves are on the ',h('a',{href:'#bands',text:'Bands page'}),'. ',h('a',{href:'/account/configurations',text:'Download all configurations'}),'.');
    }else{
      body=table('minmax(0,1.4fr) minmax(0,1fr) 110px 44px',['Device','Band','Last seen',''],devices.length?devices.map(d=>{
        const more=d.active?btn('⋯','icon',()=>menu(more,[{label:'Revoke certificate…',danger:true,run:()=>{if(confirm('Revoke the certificate for '+d.name+'? It can no longer fetch its configuration.'))act({op:'device_revoke',band_id:d.band.id,device_id:d.id},()=>{status.textContent='Device certificate revoked.';});}}]),{'aria-label':'More actions for '+d.name}):h('span');
        return h('div',{class:'mg-row'},two(d.name,'enrolled '+day(d.created)),h('span',{class:'mg-mono mg-dimtext',text:d.band.name}),h('span',{class:'mg-mono '+(d.active?'mg-dimtext':'mg-bad'),text:d.active?ago(d.last_seen):'revoked'}),more);
      }):[h('div',{class:'mg-empty',text:owned.length?'No enrolled devices.':'Devices show here for bands you own.'})],520);
      foot='Each device holds its own certificate. Revoking one stops only that device from fetching its configuration.';
    }
    right.replaceChildren(h('div',{class:'mg-bar',style:'justify-content:space-between;padding-right:18px;border-bottom:1px solid var(--line)'},
      h('div',{class:'mg-tabs',role:'tablist',style:'border:0'},tabs.map(([id,text])=>h('button',{type:'button',role:'tab',class:id===tab?'on':'','aria-selected':String(id===tab),'data-section-tab':id,text,onclick:()=>{tab=id;drawRight();}}))),add),
      body,h('div',{class:'mg-sub',style:'padding:12px 18px;font-size:12px'},foot));
  }

  function acceptDialog(code){
    const d=dialog('Accept an invitation',form({op:'accept'},async f=>{if(await act(f,()=>{status.textContent='Invitation accepted.';}))d.close();},
      h('label',{class:'mg-field'},'Invitation code',h('input',{name:'invite',value:code,required:true})),h('div',{class:'mg-actions end'},h('button',{class:'mg-btn primary',text:'Accept'}))));
  }
  function pairDialog(){
    const owned=bands.filter(b=>b.role==='owner'&&b.active);
    const d=dialog('Pair a device',form({op:'pair'},f=>{d.close();return act(f);},
      h('label',{class:'mg-field'},'Band',h('select',{name:'band_id'},owned.map(b=>h('option',{value:b.id,text:b.name})))),
      h('div',{class:'mg-actions end'},h('button',{class:'mg-btn primary',text:'Show pairing code'}))));
  }
  function passwordDialog(){
    const d=dialog('Change password',form({op:'password'},async f=>{if(await act(f,()=>{status.textContent='Password changed.';}))d.close();},
      h('label',{class:'mg-field'},'Current password',h('input',{type:'password',name:'current_password',autocomplete:'current-password',required:true})),
      h('label',{class:'mg-field'},'New password',h('input',{type:'password',name:'password',minlength:'12',autocomplete:'new-password',required:true})),
      h('div',{class:'mg-actions end'},h('button',{class:'mg-btn primary',text:'Change password'}))));
  }
  function localDialog(){
    const d=dialog('Add a local login',form({op:'local'},async f=>{if(await act(f,()=>{status.textContent='Local login added.';}))d.close();},
      h('label',{class:'mg-field'},'Username',h('input',{name:'username',autocomplete:'username',required:true})),
      h('label',{class:'mg-field'},'Password',h('input',{type:'password',name:'password',minlength:'12',autocomplete:'new-password',required:true})),
      h('div',{class:'mg-actions end'},h('button',{class:'mg-btn primary',text:'Add login'}))));
  }

  await refresh();
  return {refresh,deactivate(){document.querySelectorAll('dialog.mg-dialog[open]').forEach(d=>d.close());}};
}
