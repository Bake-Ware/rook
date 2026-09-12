const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export async function mountWork(root) {
  if (!document.querySelector('link[data-work-style]')) {
    const link=document.createElement('link');link.rel='stylesheet';link.href='/account/work/assets/work.css';link.dataset.workStyle='1';document.head.append(link);
  }
  const response=await fetch('/account/work/bootstrap');
  if(!response.ok)throw Error('Sign in with your operator account to use Work.');
  const {csrf}=await response.json();
  root.innerHTML=`
    <div class="work-layout">
      <aside class="work-sidebar">
        <div class="work-list-heading"><strong>Work sessions</strong><button id="work-new" type="button">+ New</button></div>
        <div id="work-list"><p class="work-muted">Connecting…</p></div>
      </aside>
      <section class="work-main">
        <div id="work-connection" role="status">Connecting…</div>
        <form id="work-create" class="work-create">
          <h2>Start work</h2><p>Choose where your agent will work. You can leave this page while it runs.</p>
          <label>Title<input name="title" placeholder="What are we working on?" maxlength="160" required></label>
          <label>Host<select name="worker" required><option value="">Choose a host</option></select></label>
          <label>Working directory<input name="cwd" placeholder="/home/bake/project" required></label>
          <label>Agent<select name="agent"><option>Codex</option></select></label>
          <label>Model<input name="model" placeholder="Use the host’s default model" maxlength="100"></label>
          <p class="work-muted">The directory must exist on the selected host. Codex uses that host’s login, with workspace writes and approval prompts for additional access.</p>
          <button type="submit">Create session</button>
        </form>
        <div id="work-session" hidden>
          <header class="work-session-heading"><div><h2 id="work-title"></h2><div id="work-meta" class="work-muted"></div></div>
          <div class="work-actions"><span id="work-status"></span><button id="work-interrupt" type="button">Stop turn</button><button id="work-close" type="button">Close agent</button><button id="work-resume" type="button">Reopen</button></div></header>
          <p id="work-error" role="alert" hidden></p>
          <div class="work-tabs"><button type="button" data-pane="conversation" aria-pressed="true">Conversation</button><button type="button" data-pane="changes" aria-pressed="false">Changes</button></div>
          <div id="work-conversation" class="work-output" aria-label="Conversation"></div>
          <div id="work-changes" class="work-output" hidden><pre id="work-diff"></pre></div>
          <div id="work-pending"></div>
          <form id="work-compose"><label class="work-muted" for="work-input">Message your agent</label><textarea id="work-input" rows="3" maxlength="24000" placeholder="Describe the task, or steer work already in progress…" required></textarea><div><span class="work-muted">Ctrl / ⌘ + Enter to send</span><button type="submit">Send</button></div></form>
        </div>
        <p id="work-notice" role="alert"></p>
      </section>
    </div>`;
  const $=s=>root.querySelector(s);
  let ws, active=true, reconnect, selected=null, state=null, workers=[], sessions=[], pendingKey='', generation=0;
  const drafts=new Map();
  const renderedItems=new Map();
  const pendingCommands=new Map();
  function notify(text){$('#work-notice').textContent=text;}
  function send(data){
    if(ws?.readyState!==WebSocket.OPEN){notify('Connection is unavailable. Your draft is still here.');return false;}
    ws.send(JSON.stringify({...data,csrf}));return true;
  }
  function command(op, extra={}){
    const id=crypto.randomUUID();
    const data={op,id,session:selected,...extra};
    if(!send(data))return false;
    pendingCommands.set(id,data);notify('');return true;
  }
  function renderList(){
    $('#work-list').innerHTML=sessions.length?sessions.map(s=>`<button type="button" class="work-session-link ${s.id===selected?'selected':''}" data-session="${esc(s.id)}"><strong>${esc(s.title)}</strong><span>${esc(s.worker_name)} · ${esc(s.status)}</span></button>`).join(''):'<p class="work-muted">No sessions yet.</p>';
  }
  function select(id){
    if(selected)drafts.set(selected,$('#work-input').value);
    selected=id;state=null;pendingKey='';renderedItems.clear();
    $('#work-input').value=drafts.get(id)||'';
    $('#work-create').hidden=true;$('#work-session').hidden=false;
    $('#work-title').textContent='Loading…';$('#work-conversation').replaceChildren();$('#work-pending').replaceChildren();
    send({op:'select',session:id});renderList();
  }
  function itemText(item) {
    if(item.type==='userMessage')return (item.content||[]).map(c=>c.text||'').join('\n');
    if(item.type==='reasoning')return item.text||(item.summary||[]).join('\n');
    return item.text||'';
  }
  function renderItem(item) {
    const kind=item.type;
    if(kind==='agentMessage'||kind==='userMessage')return `<article class="work-message ${kind}"><div class="work-role">${kind==='userMessage'?'You':'Codex'}</div><div class="work-text">${esc(itemText(item))}</div></article>`;
    if(kind==='reasoning')return `<details class="work-tool"><summary>Reasoning summary</summary><pre>${esc(itemText(item))}</pre></details>`;
    if(kind==='commandExecution')return `<details class="work-tool" ${item.status==='inProgress'?'open':''}><summary>${esc(item.command||'Command')} <span>${esc(item.status||'')} ${item.exitCode!=null?'· exit '+esc(item.exitCode):''}</span></summary><pre>${esc(item.aggregatedOutput||'Waiting for output…')}</pre></details>`;
    if(kind==='fileChange')return `<details class="work-tool"><summary>File changes · ${esc(item.status)}</summary>${(item.changes||[]).map(c=>`<strong>${esc(c.path)}</strong><pre>${esc(c.diff||JSON.stringify(c.kind))}</pre>`).join('')}</details>`;
    return `<details class="work-tool"><summary>${esc(kind||'Activity')}</summary><pre>${esc(JSON.stringify(item,null,2))}</pre></details>`;
  }
  function renderPending(s){
    const pending=Object.values(s.pending||{});
    const key=JSON.stringify(pending);
    if(key===pendingKey)return;
    pendingKey=key;
    $('#work-pending').innerHTML=pending.map(p=>{
      const method=p.method, params=p.params||{}, id=String(p.id);
      if(method==='item/tool/requestUserInput')return `<form class="work-question" data-request="${esc(id)}"><h3>Agent needs your input</h3>${(params.questions||[]).map(q=>`<label>${esc(q.header||'')} — ${esc(q.question)}${q.options?.length?`<select name="${esc(q.id)}">${q.options.map(o=>`<option value="${esc(o.label)}">${esc(o.label)} — ${esc(o.description)}</option>`).join('')}</select>`:`<input name="${esc(q.id)}" required>`}</label>`).join('')}<button type="submit">Answer</button></form>`;
      if(['item/commandExecution/requestApproval','item/fileChange/requestApproval','item/fileRead/requestApproval'].includes(method))return `<section class="work-approval"><h3>Approval requested</h3><pre>${esc(params.command||params.reason||JSON.stringify(params,null,2))}</pre><button type="button" data-approval="${esc(id)}" data-decision="accept">Approve once</button> <button type="button" data-approval="${esc(id)}" data-decision="decline">Decline</button></section>`;
      return `<section class="work-approval"><h3>Agent request</h3><pre>${esc(JSON.stringify(p,null,2))}</pre><p>This request type needs a newer adapter. Stop the turn to cancel it.</p></section>`;
    }).join('');
  }
  function renderSession(s){
    if(s.id!==selected)return;
    state=s;
    $('#work-title').textContent=s.title;
    $('#work-meta').textContent=[s.worker_name,s.cwd,s.model].filter(Boolean).join(' · ');
    $('#work-status').textContent=s.disconnected?'Host disconnected':s.status;
    $('#work-error').hidden=!s.error;$('#work-error').textContent=s.error||'';
    $('#work-interrupt').disabled=!s.turn_id;
    $('#work-close').hidden=s.status==='closed';
    $('#work-resume').hidden=s.status!=='closed'||!s.thread_id;
    $('#work-compose button').disabled=!['ready','working'].includes(s.status)||!!s.disconnected;
    const out=$('#work-conversation');
    const bottom=out.scrollHeight-out.scrollTop-out.clientHeight<80;
    const scroll=out.scrollTop;
    if(s.order?.length){
      if(!renderedItems.size)out.replaceChildren();
      for(const id of s.order){
        const item=s.items[id],hash=JSON.stringify(item),previous=renderedItems.get(id);
        if(previous?.hash===hash)continue;
        const node=previous?.node||document.createElement('div');
        const opened=previous?node.querySelector('details')?.open:undefined;
        node.innerHTML=renderItem(item);
        if(opened!==undefined&&node.querySelector('details'))node.querySelector('details').open=opened;
        if(!previous)out.append(node);
        renderedItems.set(id,{node,hash});
        if(item.type==='userMessage'&&itemText(item)===drafts.get(selected))drafts.delete(selected);
      }
    } else {out.innerHTML='<p class="work-muted">Your agent is ready for a task.</p>';}
    if(s.error&&drafts.has(selected)&&!$('#work-input').value)$('#work-input').value=drafts.get(selected);
    out.scrollTop=bottom?out.scrollHeight:scroll;
    $('#work-diff').textContent=s.diff||'No changes reported for the current turn.';
    renderPending(s);
  }
  function connect(){
    if(!active)return;
    const gen=++generation;
    ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/account/work/ws');
    ws.onopen=()=>{
      if(gen!==generation)return;
      $('#work-connection').textContent='Connected · sessions saved on the server';
      if(selected)send({op:'select',session:selected});
    };
    ws.onmessage=e=>{
      if(gen!==generation)return;
      const m=JSON.parse(e.data);
      if(m.type==='index'){
        sessions=m.sessions;renderList();
        if(JSON.stringify(workers)!==JSON.stringify(m.workers)){
          workers=m.workers;const pick=$('select[name=worker]'),value=pick.value;
          pick.innerHTML='<option value="">Choose a host</option>'+workers.map(w=>`<option value="${esc(w.id)}">${esc(w.name)}</option>`).join('');
          if(workers.some(w=>w.id===value))pick.value=value;
          else {const first=workers.find(w=>w.name==='cachyrig');if(first)pick.value=first.id;}
        }
      } else if(m.type==='session')renderSession(m.session);
      else if(m.type==='selected')select(m.session);
      else if(m.type==='error')notify(m.error);
      else if(m.type==='ack')pendingCommands.delete(m.id);
    };
    ws.onclose=e=>{
      if(gen!==generation)return;
      $('#work-connection').textContent=e.code===1008?'Session expired. Sign in again.':'Disconnected from page · work continues on the server';
      if(pendingCommands.size)notify('Connection interrupted during submission. Check the conversation before sending again.');
      if(active&&e.code!==1008)reconnect=setTimeout(connect,1500);
    };
  }
  $('#work-new').onclick=()=>{
    if(selected)drafts.set(selected,$('#work-input').value);
    selected=null;$('#work-create').hidden=false;$('#work-session').hidden=true;renderList();
  };
  $('#work-list').onclick=e=>{const b=e.target.closest('[data-session]');if(b)select(b.dataset.session);};
  $('#work-create').onsubmit=e=>{
    e.preventDefault();const values=Object.fromEntries(new FormData(e.currentTarget));
    command('create',values);
  };
  $('#work-compose').onsubmit=e=>{
    e.preventDefault();const input=$('#work-input');
    if(command('message',{text:input.value})){drafts.set(selected,input.value);input.value='';$('#work-compose button').disabled=true;}
  };
  $('#work-input').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();if(!$('#work-compose button').disabled)$('#work-compose').requestSubmit();}};
  $('#work-interrupt').onclick=()=>command('interrupt');
  $('#work-close').onclick=()=>command('close');
  $('#work-resume').onclick=()=>command('resume');
  $('#work-pending').onclick=e=>{
    const b=e.target.closest('[data-approval]');if(b){if(command('answer',{request:b.dataset.approval,decision:b.dataset.decision}))b.closest('section').querySelectorAll('button').forEach(x=>x.disabled=true);}
  };
  $('#work-pending').onsubmit=e=>{
    if(!e.target.matches('.work-question'))return;e.preventDefault();
    if(command('answer',{request:e.target.dataset.request,answers:Object.fromEntries(new FormData(e.target))}))e.target.querySelector('button').disabled=true;
  };
  root.querySelector('.work-tabs').onclick=e=>{
    const b=e.target.closest('[data-pane]');if(!b)return;
    $('#work-conversation').hidden=b.dataset.pane!=='conversation';$('#work-changes').hidden=b.dataset.pane!=='changes';
    root.querySelectorAll('[data-pane]').forEach(x=>x.setAttribute('aria-pressed',String(x===b)));
  };
  connect();
  return {
    activate(){if(!active){active=true;connect();}},
    deactivate(){active=false;clearTimeout(reconnect);generation++;ws?.close();}
  };
}
