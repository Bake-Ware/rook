const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export async function mountWork(root) {
  if (!document.querySelector('link[data-work-style]')) {
    const link=document.createElement('link');link.rel='stylesheet';link.href='/account/work/assets/work.css'+new URL(import.meta.url).search;link.dataset.workStyle='1';document.head.append(link);
  }
  const response=await fetch('/account/work/bootstrap');
  if(!response.ok)throw Error('Sign in with your operator account to use Work.');
  const {csrf}=await response.json();
  root.innerHTML=`
    <div class="work-layout">
      <aside class="work-sidebar">
        <div class="work-list-heading"><strong>Work sessions</strong><button id="work-new" type="button">+ New</button></div>
        <input id="work-search" type="search" placeholder="Search sessions…" aria-label="Search by title, host, agent, or status">
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
          <div class="work-actions"><span id="work-status"></span><button id="work-interrupt" type="button">Stop turn</button><button id="work-close" type="button">Close session</button><button id="work-resume" type="button">Reopen</button><button id="work-history-refresh" type="button" title="Reread the full conversation from host">Refresh</button></div></header>
          <p id="work-error" role="alert" hidden></p>
          <div class="work-tabs"><button type="button" data-pane="conversation" aria-pressed="true">Conversation</button><button type="button" data-pane="changes" aria-pressed="false">Changes</button></div>
          <div id="work-conversation" class="work-output" aria-label="Conversation"></div>
          <div id="work-history-controls" hidden><span id="work-history-note" role="status"></span></div>
          <div id="work-changes" class="work-output" hidden><pre id="work-diff"></pre></div>
          <p id="work-resume-note" class="work-muted" role="status"></p><details id="work-terminal" hidden><summary>Host terminal</summary><pre id="work-terminal-output"></pre></details><div id="work-pending"></div>
          <form id="work-compose"><label class="work-muted" for="work-input">Message your agent</label><textarea id="work-input" rows="3" maxlength="24000" placeholder="Describe the task, or steer work already in progress…" required></textarea><div><span class="work-muted">Enter to send · Shift+Enter for a new line</span><button type="submit">Send</button></div></form>
        </div>
        <p id="work-notice" role="alert"></p>
      </section>
    </div>`;
  const $=s=>root.querySelector(s);
  let ws, active=true, reconnect, selected=null, state=null, workers=[], sessions=[], pendingKey='', generation=0;
  let history=null, historyRequest=null, hostView=null, viewRequest=null;
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
    const query=$('#work-search').value.trim().toLowerCase();
    const visible=sessions.filter(s=>[s.title,s.worker_name,s.agent,s.status,s.cwd].join(' ').toLowerCase().includes(query))
      .sort((a,b)=>(b.updated||0)-(a.updated||0)||a.id.localeCompare(b.id));
    $('#work-list').innerHTML=visible.length?visible.map(s=>`<div class="work-session-card ${s.id===selected?'selected':''}"><button type="button" class="work-session-link" data-session="${esc(s.id)}"><strong>${esc(s.title)}</strong><span>${esc(s.agent||'codex')} · ${esc(s.worker_name)} · ${esc(s.status)}</span></button><label class="work-card-status">Status <select data-status-session="${esc(s.id)}" aria-label="Status for ${esc(s.title)}">${['auto','pending','blocked','closed'].map(v=>`<option value="${v}" ${(s.review_status||'auto')===v?'selected':''}>${v[0].toUpperCase()+v.slice(1)}</option>`).join('')}</select></label><button type="button" data-close-session="${esc(s.id)}" ${s.status==='closed'?'hidden':''}>Close</button></div>`).join(''):'<p class="work-muted">No matching sessions.</p>';
  }
  function select(id){
    if(selected)drafts.set(selected,$('#work-input').value);
    historyRequest?.abort();historyRequest=null;history=null;
    viewRequest?.abort();viewRequest=null;hostView=null;
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
    if(kind==='agentMessage'||kind==='userMessage')return `<article class="work-message ${kind}"><div class="work-role">${kind==='userMessage'?'You':esc(state?.agent==='claude'?'Claude':'Codex')}</div><div class="work-text">${esc(itemText(item))}</div></article>`;
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
  async function loadHistory(reset=false,follow=false){
    if(!state?.imported)return;
    if(reset){historyRequest?.abort();history={id:selected,items:{},order:[],offset:0,contentOffset:0,more:true};}
    if(!history||history.loading||(!history.more&&!follow))return;
    const current=history, controller=new AbortController();historyRequest=controller;current.loading=true;
    let firstFollow=follow, failed=false;
    if(!follow)$('#work-history-note').textContent='Reading the conversation from host…';
    try{
      while(current.more||firstFollow){
      const query=firstFollow?new URLSearchParams({follow:'1',offset:Math.max(0,current.order.length-1),version:current.version||''}):new URLSearchParams({offset:current.offset,content_offset:current.contentOffset,snapshot:current.snapshot||''});
      const response=await fetch(`/account/work/history/${encodeURIComponent(current.id)}?${query}`,{signal:controller.signal,cache:'no-store'});
      const page=await response.json();
      if(!response.ok)throw Error(page.error||'Unable to read host history.');
      if(history!==current||selected!==current.id||!active)return;
      if(firstFollow){
        firstFollow=false;
        if(page.live===false){current.noLive=true;return;}
        if(page.unchanged){$('#work-history-note').textContent='';return;}
        current.pendingVersion=page.version;
        const from=page.replace_from;
        if(!Number.isInteger(from)||from<0||from>current.order.length)throw Error('Host returned an invalid update cursor.');
        for(const id of current.order.slice(from))delete current.items[id];
        current.order.length=from;
      }
      current.snapshot=page.snapshot||current.snapshot;
      current.pendingVersion=page.version||current.pendingVersion;
      if(typeof page.active==='boolean')state={...state,active:page.active};
      const fragments=page.messages||[];
      for(const m of fragments){
        const id=String(m.index), item=current.items[id];
        if((item?.characters||0)!==m.content_offset)throw Error('History changed. Refresh from host to read it again.');
        if(!item){current.items[id]={id,type:m.role==='user'?'userMessage':'agentMessage',text:'',characters:0};current.order.push(id);}
        const next=current.items[id];next.text+=m.content;next.characters+=Array.from(m.content).length;
        if(next.type==='userMessage')next.content=[{text:next.text}];
      }
      current.more=!!page.truncated;
      if(!current.more){current.version=current.pendingVersion||current.version;current.pendingVersion=null;}
      if(current.more){
        if(page.next_offset<current.offset||(page.next_offset===current.offset&&page.next_content_offset<=current.contentOffset))throw Error('Host returned an invalid history cursor.');
        current.offset=page.next_offset;current.contentOffset=page.next_content_offset;
      }
      $('#work-history-note').textContent=current.more?`Reading conversation… ${current.order.length}${page.total_messages?' / '+page.total_messages:''} messages`:'';
      renderSession(state);
      if(current.more){
        await new Promise(resolve=>setTimeout(resolve,25));
        if(controller.signal.aborted||history!==current||selected!==current.id||!active)return;
      }
      }
    }catch(error){failed=true;current.more=false;if(error.name!=='AbortError'&&history===current)$('#work-history-note').textContent=error.message;}
    finally{
      current.loading=false;
      const tick=()=>{
        if(!active||history!==current||current.noLive)return;
        if(document.hidden){setTimeout(tick,2000);return;}
        loadHistory(false,!current.more);
      };
      if(!controller.signal.aborted)setTimeout(tick,failed?5000:2000);
    }
  }
  async function loadHostView(reset=false){
    if(!state?.remote_runtime)return;
    if(reset){viewRequest?.abort();hostView=null;}
    if(!hostView)hostView={id:selected,revision:0,items:{},order:[],pending:{},diff:''};
    const current=hostView;
    if(current.loading||current.revision>=state.worker_revision&&current.revision)return;
    current.loading=true;
    const controller=new AbortController();viewRequest=controller;
    let token='',offset=0,text='',revision=current.revision,completed=false;
    $('#work-history-note').textContent='Reading from host…';
    try{
      while(true){
        const query=new URLSearchParams({since:current.revision,token,offset});
        const response=await fetch(`/account/work/view/${encodeURIComponent(current.id)}?${query}`,{signal:controller.signal,cache:'no-store'});
        const page=await response.json();
        if(!response.ok)throw Error(page.error||'Unable to read host session.');
        if(hostView!==current||selected!==current.id||!active)return;
        text+=page.data;revision=page.revision;
        if(!page.truncated)break;
        if(page.next_offset<=offset)throw Error('Host returned an invalid view cursor.');
        token=page.token;offset=page.next_offset;
        await new Promise(resolve=>setTimeout(resolve,250));
        if(controller.signal.aborted)return;
      }
      const delta=JSON.parse(text);
      Object.assign(current,{...delta,items:{...current.items,...delta.items},revision});completed=true;
      $('#work-history-note').textContent='';
      renderSession(state);
    }catch(error){if(error.name!=='AbortError'&&hostView===current)$('#work-history-note').textContent=error.message;}
    finally{
      current.loading=false;
      if(completed&&hostView===current&&state?.worker_revision>current.revision)setTimeout(()=>{if(active&&hostView===current)loadHostView();},250);
    }
  }
  function renderSession(s){
    if(s.id!==selected)return;
    state=s;
    $('#work-history-controls').hidden=!(s.imported||s.remote_runtime);
    $('#work-history-refresh').hidden=!(s.imported||s.remote_runtime);
    if(s.remote_runtime){
      if(!hostView||hostView.revision<s.worker_revision)loadHostView();
      s={...s,items:hostView?.items||{},order:hostView?.order||[],pending:hostView?.pending||{},diff:hostView?.diff||'',error:hostView?.error||s.error};
    }
    if(s.imported){
      if(!history){loadHistory(true);}
      s={...s,items:history?.items||{},order:history?.order||[]};
    }
    $('#work-title').textContent=s.title;
    $('#work-meta').textContent=[s.agent||'codex',s.worker_name,s.cwd,s.model,s.imported||s.remote_runtime?(s.active?'Active on host':'History stored on host'):''].filter(Boolean).join(' · ');
    $('#work-status').textContent=s.disconnected?'Host disconnected':s.status;
    $('#work-error').hidden=!s.error;$('#work-error').textContent=s.error||'';
    $('#work-interrupt').disabled=!(s.turn_id||s.external_running);
    $('#work-interrupt').hidden=!!s.imported&&!s.external_running;
    $('#work-compose').hidden=!!s.imported&&!(s.external_running||s.messageable);
    $('#work-compose label').textContent=s.imported&&s.external_running?'Send input to the host terminal':'Message your agent';
    $('#work-terminal').hidden=!s.imported||!s.terminal;
    $('#work-terminal-output').textContent=(s.terminal||'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'');
    $('#work-resume-note').textContent=s.message_note||s.resume_note||(s.imported&&s.active&&!s.messageable&&!s.external_running?'This host does not expose a messaging connection for this session.':'');
    $('#work-input').placeholder=s.imported?'Send a prompt or answer the terminal’s request.':'Describe the task, or steer work already in progress…';
    $('#work-close').hidden=s.status==='closed';
    $('#work-resume').hidden=s.imported?!!(s.external_running||s.active):(s.activity||s.status)!=='closed'||!s.thread_id;
    $('#work-resume').textContent=s.imported?'Resume on host':'Reopen';
    $('#work-compose button').disabled=(s.imported?!(s.external_running||s.messageable):!['ready','working'].includes(s.activity||s.status)||(s.needs_input||Object.keys(s.pending||{}).length>0))||!!s.disconnected||[...pendingCommands.values()].some(c=>c.session===s.id&&c.op==='message');
    const out=$('#work-conversation');
    const bottom=out.scrollHeight-out.scrollTop-out.clientHeight<80;
    const scroll=out.scrollTop;
    if(s.order?.length){
      if(!renderedItems.size)out.replaceChildren();
      const currentIds=new Set(s.order);
      for(const [id,previous] of renderedItems){if(!currentIds.has(id)){previous.node.remove();renderedItems.delete(id);}}
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
    } else {renderedItems.clear();out.innerHTML='<p class="work-muted">'+(s.imported?'Read conversation messages from the host.':'Your agent is ready for a task.')+'</p>';}

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
      $('#work-connection').textContent='Connected';
      if(selected)send({op:'select',session:selected});
      for(const c of pendingCommands.values())if(c.op!=='create')send({op:'receipt',session:c.session,id:c.id});
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
          else {let last=null;try{last=localStorage.getItem('rook-session-host');}catch{}const first=last&&workers.find(w=>w.name===last);if(first)pick.value=first.id;}
          if(!pick.dataset.remember){pick.dataset.remember='1';pick.addEventListener('change',()=>{const w=workers.find(x=>x.id===pick.value);if(w)try{localStorage.setItem('rook-session-host',w.name);}catch{}});}
        }
      } else if(m.type==='session')renderSession(m.session);
      else if(m.type==='selected')select(m.session);
      else if(m.type==='error')notify(m.error);
      else if(m.type==='ack'&&m.result?.status!=='accepted'){
        const sent=pendingCommands.get(m.id);pendingCommands.delete(m.id);
        if(sent?.op==='message'&&m.result?.status==='error'){
          const existing=sent.session===selected?$('#work-input').value:drafts.get(sent.session);
          drafts.set(sent.session,existing?existing+'\n\n'+sent.text:sent.text);
          if(sent.session===selected)$('#work-input').value=drafts.get(sent.session);
          notify(m.result.error||'Message could not be delivered.');
        }
        if(state&&sent?.session===selected)renderSession(state);
      }
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
    historyRequest?.abort();history=null;viewRequest?.abort();hostView=null;
    selected=null;$('#work-create').hidden=false;$('#work-session').hidden=true;renderList();
  };
  $('#work-history-refresh').onclick=()=>state?.remote_runtime?loadHostView(true):loadHistory(true);
  $('#work-search').oninput=renderList;
  $('#work-list').onclick=e=>{
    const close=e.target.closest('[data-close-session]');
    if(close){command('close',{session:close.dataset.closeSession});return;}
    const b=e.target.closest('[data-session]');if(b)select(b.dataset.session);
  };
  $('#work-list').onchange=e=>{
    const pick=e.target.closest('[data-status-session]');
    if(pick)command('status',{session:pick.dataset.statusSession,status:pick.value});
  };
  $('#work-create').onsubmit=e=>{
    e.preventDefault();const values=Object.fromEntries(new FormData(e.currentTarget));
    command('create',values);
  };
  $('#work-compose').onsubmit=e=>{
    e.preventDefault();const input=$('#work-input');
    if(command('message',{text:input.value})){drafts.set(selected,'');input.value='';$('#work-compose button').disabled=true;}
  };
  $('#work-input').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.altKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();if(!$('#work-compose button').disabled)$('#work-compose').requestSubmit();}};
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
    deactivate(){historyRequest?.abort();history=null;viewRequest?.abort();hostView=null;active=false;clearTimeout(reconnect);generation++;ws?.close();}
  };
}
