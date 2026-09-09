export async function mountBands(root, {csrf, onChange = () => {}}) {
const $=id=>root.querySelector("#"+id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state={bands:[],migrations:[]},current=null,loading=false;
const running=new Set();
async function api(data){
 const r=await fetch('/account/bands/api',data?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...data,csrf})}:{headers:{Accept:'application/json'}});
 if(r.redirected)throw Error('Your session expired. Sign in again and reload this page.');
 const d=await r.json();if(!r.ok)throw Error(d.error||`Request failed (${r.status})`);return d;
}
function message(text,error=false){$('message').textContent=text;$('message').className=error?'error':'';}
function render(){
 $('bands-list').innerHTML=state.bands.map(b=>{
 const owner=b.role==='owner',open=state.migrations.some(m=>['prepared','active'].includes(m.phase)&&(m.band_id===b.id||m.target_band_id===b.id));
 return `<section><div class="band-head"><h2>${esc(b.name)}</h2><span class="badge">${esc(b.role)}${b.primary?' · primary':''} · ${b.active?'active':'revoked'}</span></div>
 <p class="muted">${b.workers.length} visible workers · ${b.enrolled_devices} enrolled devices · key version ${b.epoch}</p>
 ${owner?`<div class="band-tools"><button class="secondary" data-op="rename" data-band="${esc(b.id)}">Rename</button><button data-op="move_all" data-band="${esc(b.id)}" ${!b.active||open||!b.workers.length?'disabled':''}>Migrate all…</button><button class="secondary" data-op="psk" data-band="${esc(b.id)}" ${!b.active||open?'disabled':''}>Migrate PSK…</button><button class="danger" data-op="delete" data-band="${esc(b.id)}" ${b.primary||open?'disabled':''} title="${b.primary?'The configured primary band cannot be deleted':open?'Finish the open migration first':'Delete band and revoke its enrollment'}">Delete…</button></div>`:''}
 ${b.workers.map(w=>`<div class="worker-line"><span>${esc(w.name)}<small>${w.online?'Online':'Offline'}${w.can_move?'':' · update required to move'}</small></span>${owner?`<button class="secondary" data-op="move" data-band="${esc(b.id)}" data-worker="${esc(w.id)}" ${!w.online||!w.can_move||open||!b.active?'disabled':''}>Move to band…</button>`:''}</div>`).join('')}
 ${!b.workers.length?'<p class="muted">No workers currently visible.</p>':''}</section>`;
 }).join('')||'<section class="empty">You have no bands yet. Create one above or <a href="/account">accept an invitation</a>.</section>';
 $('migration-list').innerHTML=state.migrations.length?'<h2>Migrations</h2>'+state.migrations.map(m=>{
 const ws=m.workers||[],staged=ws.filter(w=>w.staged).length,confirmed=ws.filter(w=>w.confirmed).length;
 const title=m.target_band_id?'Worker move':'PSK migration',active=['prepared','active'].includes(m.phase);
 const source=state.bands.find(b=>b.id===m.band_id),target=state.bands.find(b=>b.id===m.target_band_id);
 return `<section><h3>${title}${source?' · '+esc(source.name):''}${target?' → '+esc(target.name):''}</h3><p>${esc(m.phase)}${m.overdue&&active?' · migration window expired; review before continuing':''}</p>${m.error?`<p class="error">${esc(m.error)}</p>`:''}
 ${ws.length?`<progress value="${staged+confirmed}" max="${ws.length*2}" aria-label="Migration progress"></progress><p>${staged}/${ws.length} saved destination config · ${confirmed}/${ws.length} verified on destination</p><details><summary>Workers</summary><div class="migration-workers">${ws.map(w=>`<p>${esc(w.worker_id)} · ${w.confirmed?'verified':w.staged?'saved config':'waiting for config'}</p>`).join('')}</div></details>`:''}
 ${active?`<p class="muted">${running.has(m.id)?'Running. Keep this page open; workers refresh within about 30 seconds.':'Resume here to continue verification. Progress is saved when you leave this page.'}</p><button data-resume="${esc(m.id)}" ${running.has(m.id)?'disabled':''}>Resume</button>${m.phase==='prepared'?` <button class="secondary" data-abort="${esc(m.id)}">Cancel migration</button>`:''}`:''}</section>`;
 }).join(''):'';
}
async function reload(){state=await api();render();onChange(state);}
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
 const names={create:'Create band',rename:'Rename band',delete:'Delete band',move:'Move worker to band',move_all:'Migrate all workers',psk:'Migrate PSK'};
 $('dialog-title').textContent=names[op];$('submit-band').textContent={delete:'Delete band',move:'Move worker',move_all:'Migrate all',psk:'Start PSK migration'}[op]||'Save';
 $('dialog-description').textContent={create:'A new five-word key is generated and stored for this band.',rename:'Change the display name. Workers keep their current connection.',delete:`Delete “${band?.name}”? Its key, pairing codes, and device enrollment will be revoked. Move its workers first if you want to keep managing them.`,move:'The worker saves its destination configuration, reconnects, and verifies the move.',move_all:'Move every visible worker. All enrolled devices must be online; missing devices will block the move.',psk:'Generate a replacement key and migrate this band’s workers to it. The old key is retired only after every expected worker is verified. This is for routine key changes; use account recovery controls for a compromised key.'}[op];
 const preview=['move','move_all','psk'].includes(op);
 $('worker-preview').innerHTML=preview?'<p>Selected workers:</p><ul>'+current.workers.map(id=>`<li>${esc(band.workers.find(w=>w.id===id)?.name||id)}</li>`).join('')+'</ul>':'';
 if(op==='psk'&&!current.workers.length)$('worker-preview').textContent=band.enrolled_devices?'Enrolled devices are offline. Bring them online before migrating the key.':'This band is empty. Its key will be replaced immediately.';
 $('submit-band').disabled=preview&&!current.workers.length&&!(op==='psk'&&!band.enrolled_devices);
 $('band-dialog').showModal();
}
$('target-band').onchange=()=>{const isNew=$('target-band').value==='new';$('new-name-field').hidden=!isNew;$('new-band-name').required=isNew&&!$('target-field').hidden;};
$('create-band').onclick=()=>openDialog('create');
$('cancel-dialog').onclick=()=>$('band-dialog').close();
$('bands-list').onclick=e=>{const b=e.target.closest('button[data-op]');if(b&&!b.disabled)openDialog(b.dataset.op,b.dataset.band,b.dataset.worker);};
$('migration-list').onclick=async e=>{
 const b=e.target.closest('button');if(!b||b.disabled)return;
 try{if(b.dataset.resume){running.add(b.dataset.resume);await tick();}else if(b.dataset.abort){if(!confirm('Cancel this migration before workers switch bands?'))return;await api({op:'abort',migration_id:b.dataset.abort});running.delete(b.dataset.abort);await reload();}}catch(err){message(err.message,true);}
};
$('band-form').onsubmit=async e=>{
 e.preventDefault();const btn=$('submit-band');btn.disabled=true;$('dialog-error').textContent='';
 try{
  const {op,band,workers}=current;
  let data={op,band_id:band?.id,name:$('band-name').value,confirm:true};
  if(['move','move_all','psk'].includes(op))data={op:'prepare',mode:op,band_id:band.id,workers,confirm:true,...(op==='psk'?{}:$('target-band').value==='new'?{new_band_name:$('new-band-name').value}:{target_band_id:$('target-band').value})};
  const result=await api(data);if(data.op==='prepare'&&result.phase==='prepared')running.add(result.id);
  $('band-dialog').close();message(data.op==='prepare'&&result.phase==='prepared'?'Migration started. Progress is shown below.':'Band updated.');await reload();
 }catch(err){$('dialog-error').textContent=err.message;}finally{btn.disabled=false;}
};
async function tick(){
 if(loading)return;loading=true;
 try{for(const id of running){try{const m=await api({op:'advance',migration_id:id});if(!['prepared','active'].includes(m.phase))running.delete(id);}catch(err){running.delete(id);message(err.message,true);}}await reload();}catch(err){message(err.message,true);}finally{loading=false;}
}
async function openWorker(wid){
 await reload();
 const band=state.bands.find(b=>b.workers.some(w=>w.id===wid));
 if(band&&band.role==='owner')openDialog('move',band.id,wid);
 else message('Worker is no longer visible or you do not own its current band.',true);
}
await reload();
const timer=setInterval(()=>{if(!root.hidden||running.size)tick();},5000);
return {refresh:reload,openWorker,destroy(){clearInterval(timer);}};
}
