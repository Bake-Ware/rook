// Bands: a table of bands and a panel for the selected one (workers, members, key).
import {h,ago,day,useCss,btn,table,two,dialog,accountAction,reveal,pairing} from '/account/bands/assets/manage.js';

const ACTIVE=['prepared','active'];

export async function mountBands(root, {csrf, onChange = () => {}}) {
useCss();
const $=id=>root.querySelector("#"+id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state={bands:[],migrations:[]},current=null,loading=false,picked='',tab='workers';
const running=new Set();
async function api(data){
 const r=await fetch('/account/bands/api',data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,csrf})}:{headers:{Accept:'application/json'}});
 if(r.redirected)throw Error('Your session expired. Sign in again and reload this page.');
 const d=await r.json();if(!r.ok)throw Error(d.error||`Request failed (${r.status})`);return d;
}
function message(text,error=false){$('message').textContent=text;$('message').className='mg-status'+(error?' bad':'');}
const busy=b=>state.migrations.some(m=>ACTIVE.includes(m.phase)&&(m.band_id===b.id||m.target_band_id===b.id));
const plural=(n,word)=>n+' '+word+(n===1?'':'s');
// Account-side actions (members, pairing, keys) share the account endpoint.
async function account(fields,done){
 try{const d=await accountAction(csrf,fields);await reload();if(done)done(d);return d;}catch(err){message(err.message,true);}
}

function render(){
 const bands=state.bands,live=state.migrations.filter(m=>ACTIVE.includes(m.phase));
 $('bands-summary').textContent=plural(bands.length,'band')+' · '+plural(bands.reduce((n,b)=>n+b.workers.length,0),'worker')+(live.length?' · '+plural(live.length,'migration')+' running':'');
 if(!bands.some(b=>b.id===picked))picked=bands[0]?.id||'';
 $('bands-list').replaceChildren(table('minmax(0,1.4fr) 120px 110px 80px 80px',['Band','Key','Workers','Devices','Role'],bands.length?bands.map(b=>{
  const on=b.workers.filter(w=>w.online).length;
  return h('div',{class:'mg-row pick'+(b.id===picked?' sel':''),onclick:()=>{picked=b.id;render();}},
   h('button',{type:'button',style:'all:unset;cursor:pointer;min-width:0','aria-current':b.id===picked?'true':null},h('span',{class:'mg-two'},h('b',null,b.name,b.primary?h('span',{class:'mg-tag',text:'DEFAULT'}):null,b.active?null:h('span',{class:'mg-tag line',text:'REVOKED'})))),
   h('span',{class:'mg-mono mg-dimtext',text:b.label+' · v'+b.epoch}),
   h('span',null,h('span',{class:on?'mg-good':'mg-dimtext',text:String(on)}),h('span',{class:'mg-sub',text:' of '+b.workers.length+' online'})),
   h('span',{class:'mg-dimtext',text:String(b.enrolled_devices)}),h('span',{class:'mg-mono '+(b.role==='owner'?'mg-good':'mg-dimtext'),text:b.role}));
 }):[h('div',{class:'mg-empty'},'You have no bands yet. Create one, or ',h('a',{href:'/#account',text:'accept an invitation'}),'.')],600));
 panel(bands.find(b=>b.id===picked));
 $('migration-list').replaceChildren(...live.map(banner));
 const past=state.migrations.filter(m=>!ACTIVE.includes(m.phase));
 $('migration-history').replaceChildren(...(past.length?[h('details',null,h('summary',{text:'Past migrations · '+past.length}),past.map(m=>h('p',{class:'mg-sub',text:label(m)+' · '+m.phase+(m.error?' · '+m.error:'')})))]:[]));
}
function label(m){
 const source=state.bands.find(b=>b.id===m.band_id),target=state.bands.find(b=>b.id===m.target_band_id);
 return (m.target_band_id?'Worker move':'Key rotation')+(source?' · '+source.name:'')+(target?' → '+target.name:'');
}
function banner(m){
 const ws=m.workers||[],staged=ws.filter(w=>w.staged).length,confirmed=ws.filter(w=>w.confirmed).length,on=running.has(m.id);
 const resume=btn(on?'Running…':'Resume','soft',async()=>{try{running.add(m.id);await tick();}catch(err){message(err.message,true);}},{disabled:on});
 const cancel=m.phase==='prepared'?btn('Cancel','',async()=>{if(!confirm('Cancel this migration before workers switch bands?'))return;try{await api({op:'abort',migration_id:m.id});running.delete(m.id);await reload();}catch(err){message(err.message,true);}}):null;
 return h('div',{class:'mg-banner'},h('span',{class:'mg-label mg-warn',text:m.target_band_id?'Worker move':'Key rotation'}),
  h('span',{class:'mg-grow',title:ws.map(w=>w.worker_id+': '+(w.confirmed?'verified':w.staged?'saved config':'waiting for config')).join('\n')},label(m).split(' · ').slice(1).join(' · ')+': '+staged+' of '+ws.length+' saved the new config, '+confirmed+' verified.'+(m.overdue?' The migration window expired; review before continuing.':on?' Keep this page open; workers refresh within about 30 seconds.':' Resume to continue verification.')+(m.error?' '+m.error:'')),
  ws.length?h('progress',{value:String(staged+confirmed),max:String(ws.length*2),'aria-label':'Migration progress'}):null,resume,cancel);
}
function panel(b){
 const host=$('band-panel');
 if(!b){host.replaceChildren(h('div',{class:'mg-empty',text:'A band is a group of workers that share one key.'}));return;}
 const owner=b.role==='owner',open=busy(b);
 if(!owner)tab='workers';
 const tabs=[['workers','Workers · '+b.workers.length]].concat(owner?[['members','Members · '+b.members.length],['key','Key']]:[]);
 const body=h('div');
 if(tab==='workers'){
  body.append(...[...b.workers].sort((x,y)=>x.name.localeCompare(y.name)).map(w=>h('div',{class:'mg-row',style:'grid-template-columns:minmax(0,1fr) auto auto;min-height:52px'},two(w.name,w.description||''),
   h('span',{class:'mg-mono '+(w.online?'mg-good':'mg-dimtext'),title:w.can_move?'':'This worker needs an update before it can move.',text:(w.online?'online':'offline')+(w.can_move?'':' · update')}),
   owner?btn('Move…','',()=>openDialog('move',b.id,w.id),{disabled:!w.online||!w.can_move||open||!b.active}):null)));
  if(!b.workers.length)body.append(h('div',{class:'mg-empty',text:'No workers on this band right now.'}));
  if(owner)body.append(h('div',{class:'mg-pad'},h('div',{class:'mg-actions'},btn('Move all…','soft',()=>openDialog('move_all',b.id),{disabled:!b.active||open||!b.workers.length}),h('span',{class:'mg-sub',style:'font-size:12px',text:open?'Finish the running migration first.':'A move needs the worker online and up to date.'}))));
 }else if(tab==='members'){
  body.append(...b.members.map(p=>{
   const role=h('select',{'aria-label':'Role for '+p.name},[['member','Member'],['owner','Owner'],['remove','Remove…']].map(([v,t])=>h('option',{value:v,text:t,selected:v===p.role})));
   role.onchange=()=>{if(role.value==='remove'&&!confirm('Remove '+p.name+' from '+b.name+'?')){role.value=p.role;return;}account({op:'member',band_id:b.id,user_id:p.id,role:role.value},()=>message('Members updated.')).then(d=>{if(!d)role.value=p.role;});};
   return h('div',{class:'mg-row',style:'grid-template-columns:minmax(0,1fr) auto;min-height:52px'},two(p.name,p.username||''),role);}));
  body.append(h('div',{class:'mg-pad'},h('div',{class:'mg-actions'},btn('Invite a person','soft',()=>account({op:'invite',band_id:b.id},d=>reveal('Invite a person','Share this single-use link. It expires in seven days.',d.text)),{disabled:!b.active}))));
 }else{
  const fact=(k,v)=>[h('span',{class:'mg-sub',text:k}),h('span',{class:'mg-mono',text:v})];
  const devices=b.devices||[];
  body.append(h('div',{class:'mg-pad'},
   h('div',{style:'display:grid;grid-template-columns:auto minmax(0,1fr);gap:8px 16px;align-items:baseline'},fact('Fingerprint',b.label),fact('Key version',String(b.epoch)),fact('Hub',b.hub||'—'),fact('Status',b.active?'active':'revoked')),
   h('div',{class:'mg-actions'},
    btn('Pairing code','soft',()=>account({op:'pair',band_id:b.id},d=>{if(d.pairing)pairing(d);}),{disabled:!b.active}),
    btn('Revoke pairing code','',()=>account({op:'pair_revoke',band_id:b.id},()=>message('Pairing code revoked.'))),
    btn('Rotate key…','',()=>openDialog('psk',b.id),{disabled:!b.active||open}),
    b.active?h('a',{class:'mg-btn',href:'/account/configurations?band='+encodeURIComponent(b.id),text:'Download configuration'}):null),
   h('p',{class:'mg-sub',style:'font-size:12px',text:'Rotating hands every enrolled worker the new key, then retires the old one once all are verified.'}),
   h('div',null,h('div',{class:'mg-label',style:'margin-bottom:6px',text:'Enrolled devices · '+devices.filter(d=>d.active).length}),
    devices.length?devices.map(d=>h('div',{class:'mg-log',style:'grid-template-columns:minmax(0,1fr) auto auto;align-items:center'},h('span',{text:d.name}),
     h('span',{class:'mg-sub',style:'font-size:12px',text:d.active?'seen '+ago(d.last_seen)+' · enrolled '+day(d.created):'revoked'}),
     d.active?btn('Revoke','danger',()=>{if(confirm('Revoke the certificate for '+d.name+'? It can no longer fetch its configuration.'))account({op:'device_revoke',band_id:b.id,device_id:d.id},()=>message('Device certificate revoked.'));},{style:'min-height:32px'}):h('span'))):h('p',{class:'mg-sub',text:'No enrolled devices.'})),
   h('details',null,h('summary',{text:'Compromised key'}),h('div',{class:'mg-pad',style:'padding:12px 0 0'},
    h('p',{class:'mg-sub',style:'font-size:12px',text:'Replacing at once cuts off every worker on the old key until it is enrolled again. Never send a replacement key over a compromised band.'}),
    h('div',{class:'mg-actions'},
     btn('Replace key now…','danger',()=>{if(confirm('Replace the key for '+b.name+' now? Existing workers must be enrolled again.'))account({op:'rotate',band_id:b.id,confirm:'yes'},d=>reveal('New permanent key','Shown once. Re-enroll devices with this key or a new pairing code.',d.text));}),
     btn('Revoke key…','danger',()=>{if(confirm('Revoke the current key for '+b.name+'?'))account({op:'revoke',band_id:b.id,confirm:'yes'},()=>message('Band key revoked.'));},{disabled:!b.active}))))));
 }
 host.replaceChildren(
  h('div',{class:'mg-panel-head'},h('div',{class:'mg-two'},h('h2',{text:b.name}),h('small',{text:b.role+' · key '+b.label+' v'+b.epoch+' · '+plural(b.enrolled_devices,'enrolled device')})),
   owner?h('div',{class:'mg-actions'},btn('Rename','',()=>openDialog('rename',b.id)),btn('Delete…','danger',()=>openDialog('delete',b.id),{disabled:b.primary||open,title:b.primary?'The default band cannot be deleted.':open?'Finish the running migration first.':'Delete the band and revoke its enrollment.'})):null),
  h('div',{class:'mg-tabs',role:'tablist'},tabs.map(([id,text])=>h('button',{type:'button',role:'tab',class:id===tab?'on':'','aria-selected':String(id===tab),text,onclick:()=>{tab=id;render();}}))),body);
}
async function reload(){const next=await api(),same=JSON.stringify(next)===JSON.stringify(state);state=next;if(!same||!root.querySelector('#bands-list .mg-head')){render();onChange(state);}}
function openDialog(op,bid,wid){
 const band=state.bands.find(b=>b.id===bid);
 current={op,band,workers:op==='move'?[wid]:(band?.workers.map(w=>w.id)||[])};
 $('dialog-error').textContent='';$('name-field').hidden=!['create','rename'].includes(op);
 $('target-field').hidden=!['move','move_all'].includes(op);$('new-name-field').hidden=true;
 $('band-name').value=op==='rename'?band.name:'';$('new-band-name').value='';
 $('band-name').required=['create','rename'].includes(op);$('new-band-name').required=false;
 const dest=state.bands.filter(b=>b.active&&b.id!==bid);
 $('target-band').innerHTML=dest.map(b=>`<option value="${esc(b.id)}">${esc(b.name)} (${esc(b.role)})</option>`).join('')+'<option value="new">＋ Create new band…</option>';
 $('target-band').dispatchEvent(new Event('change'));
 const names={create:'New band',rename:'Rename band',delete:'Delete band',move:'Move worker to band',move_all:'Move all workers',psk:'Rotate key'};
 $('dialog-title').textContent=names[op];$('submit-band').textContent={create:'Create band',delete:'Delete band',move:'Move worker',move_all:'Move all',psk:'Start rotation'}[op]||'Save';
 $('submit-band').className='mg-btn '+(op==='delete'?'danger':'primary');
 $('dialog-description').textContent={create:'A new five-word key is generated and stored for this band.',rename:'Change the display name. Workers keep their current connection.',delete:`Delete “${band?.name}”? Its key, pairing codes, and device enrollment will be revoked. Move its workers first if you want to keep managing them.`,move:'The worker saves its destination configuration, reconnects, and verifies the move.',move_all:'Move every visible worker. All enrolled devices must be online; missing devices will block the move.',psk:'Generate a replacement key and migrate this band’s workers to it. The old key is retired only after every expected worker is verified. This is for routine key changes; for a compromised key use “Compromised key” on the Key tab.'}[op];
 const preview=['move','move_all','psk'].includes(op);
 $('worker-preview').textContent=preview&&current.workers.length?'Workers: '+current.workers.map(id=>band.workers.find(w=>w.id===id)?.name||id).join(', '):'';
 if(op==='psk'&&!current.workers.length)$('worker-preview').textContent=band.enrolled_devices?'Enrolled devices are offline. Bring them online before rotating the key.':'This band is empty. Its key will be replaced immediately.';
 $('submit-band').disabled=preview&&!current.workers.length&&!(op==='psk'&&!band.enrolled_devices);
 $('band-dialog').showModal();
}
$('target-band').onchange=()=>{const isNew=$('target-band').value==='new';$('new-name-field').hidden=!isNew;$('new-band-name').required=isNew&&!$('target-field').hidden;};
$('create-band').onclick=()=>openDialog('create');
$('cancel-dialog').onclick=()=>$('band-dialog').close();
$('band-form').onsubmit=async e=>{
 e.preventDefault();const button=$('submit-band');button.disabled=true;$('dialog-error').textContent='';
 try{
  const {op,band,workers}=current;
  let data={op,band_id:band?.id,name:$('band-name').value,confirm:true};
  if(['move','move_all','psk'].includes(op))data={op:'prepare',mode:op,band_id:band.id,workers,confirm:true,...(op==='psk'?{}:$('target-band').value==='new'?{new_band_name:$('new-band-name').value}:{target_band_id:$('target-band').value})};
  const result=await api(data);if(data.op==='prepare'&&result.phase==='prepared')running.add(result.id);
  $('band-dialog').close();message(data.op==='prepare'&&result.phase==='prepared'?'Migration started. Progress is shown above.':'Band updated.');await reload();
 }catch(err){$('dialog-error').textContent=err.message;}finally{button.disabled=false;}
};
async function tick(){
 if(loading)return;loading=true;
 try{for(const id of running){try{const m=await api({op:'advance',migration_id:id});if(!ACTIVE.includes(m.phase))running.delete(id);}catch(err){running.delete(id);message(err.message,true);}}await reload();}catch(err){message(err.message,true);}finally{loading=false;}
}
async function openWorker(wid){
 await reload();
 const band=state.bands.find(b=>b.workers.some(w=>w.id===wid));
 if(band){picked=band.id;tab='workers';render();}
 if(band&&band.role==='owner')openDialog('move',band.id,wid);
 else message('Worker is no longer visible or you do not own its current band.',true);
}
await reload();
// Leave an open menu or a half-typed select alone: only poll when nothing in the panel has focus.
const timer=setInterval(()=>{if((!root.hidden&&!root.querySelector('#band-panel select:focus'))||running.size)tick();},5000);
return {refresh:reload,openWorker,destroy(){clearInterval(timer);}};
}
