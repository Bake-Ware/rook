// Sessions: every agent session on every worker in one list, grouped by host
// then project folder, with one place to open, steer, resume and stop them.
// The list is GET /account/work/sessions; actions are
// POST /account/work/session/<op> (docs/design/sessions.md §3.6). Opening a
// session shows the best view it has: its Rook terminal (tier 1), else the
// live events the Claude Code mod mirrors (tier 2), else the transcript tail
// (tier 3). See docs/web/sessions.md.
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const base = path => { const p = String(path || '').replace(/[\\/]+$/, ''); return p.slice(Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\')) + 1) || p || '(no folder)'; };
const ago = t => { if (!t) return ''; const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 129600 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
const STATES = {live: 0, idle: 1, closed: 2};
const rank = r => STATES[r.state] ?? 2;
const AGENTS = ['claude', 'codex', 'hermes', 'shell'];
const POLL_MS = 8000, FOLLOW_MS = 3000, TAIL = 40, MAX_NODES = 400;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

export async function mountSessions(root, boot, toClassic) {
  const search = new URL(import.meta.url).search;
  if (!document.querySelector('link[data-sessions-style]')) {
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = '/account/work/assets/sessions.css' + search; link.dataset.sessionsStyle = '1'; document.head.append(link);
  }
  const {TermView, loadXterm, themeColors} = await import('/account/work/assets/worklog.js' + search);
  const {csrf} = boot;
  root.innerHTML = `
    <div class="sx">
      <header class="sx-bar">
        <div class="sx-title"><h2>Sessions</h2><span id="sx-counts" class="sx-muted"></span></div>
        <div class="sx-bar-actions"><button type="button" id="sx-new" class="sx-primary">+ New session</button>${toClassic ? '<button type="button" id="sx-classic" class="sx-link" title="The Codex app-server view">Classic view</button>' : ''}</div>
      </header>
      <div class="sx-filters">
        <input id="sx-search" type="search" placeholder="Search title, folder or id…" aria-label="Search sessions" maxlength="200">
        <select id="sx-agent" aria-label="Agent"><option value="">All agents</option>${AGENTS.map(a => `<option>${a}</option>`).join('')}</select>
        <select id="sx-host" aria-label="Host"><option value="">All hosts</option></select>
        <label class="sx-check"><input type="checkbox" id="sx-live"> Live only</label>
        <span id="sx-status" class="sx-muted" role="status">Loading…</span>
      </div>
      <div id="sx-notices"></div>
      <form id="sx-form" class="sx-form" hidden>
        <h3>New session</h3>
        <div class="sx-grid">
          <label>Host<select name="worker" required></select></label>
          <label>Harness<select name="harness" required></select></label>
          <label class="sx-wide">Folder<input name="cwd" list="sx-folders" placeholder="/path/to/project" required maxlength="2000" autocomplete="off"></label>
          <label>Model<input name="model" placeholder="Host default" maxlength="100"></label>
          <label>Persona<input name="persona" placeholder="Harness default" maxlength="100"></label>
          <label>Title<input name="title" placeholder="What is this session for?" maxlength="160"></label>
          <label>Task<input name="task" placeholder="Optional: t_… or slug" maxlength="120"></label>
          <label class="sx-check sx-full"><input type="checkbox" name="mcp" checked> Give it a Rook MCP token scoped to this session</label>
        </div>
        <datalist id="sx-folders"></datalist>
        <p class="sx-muted">It runs in a real terminal on the host, with that host's own login for the harness. The token expires after a day and is revoked when the session ends.</p>
        <p class="sx-error" id="sx-form-error" role="alert"></p>
        <div class="sx-row"><button type="submit" class="sx-primary">Start</button><button type="button" id="sx-form-cancel">Cancel</button></div>
      </form>
      <div class="sx-body" id="sx-body">
        <div id="sx-list" role="navigation" aria-label="Sessions"><p class="sx-muted">Loading…</p></div>
        <section id="sx-detail" aria-live="polite" hidden></section>
      </div>
    </div>`;
  const $ = s => root.querySelector(s);
  let active = true, data = {sessions: [], workers: [], errors: []}, timer = null, loading = null, stale = false, typing = null;
  let open = null;   // {rec, mode, view, el}

  async function post(op, body, signal) {
    const r = await fetch('/account/work/session/' + op, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...body, csrf}), cache: 'no-store', signal});
    let j; try { j = await r.json(); } catch { j = {ok: false, error: r.statusText || 'Request failed.'}; }
    if (!r.ok || !j.ok) throw Object.assign(Error(j.error || 'Request failed (' + r.status + ').'), {status: r.status});
    return j;
  }
  const who = rec => ({worker: rec.worker_id, agent: rec.agent, native_id: rec.native_id});
  const workerOf = rec => data.workers.find(w => w.worker_id === rec.worker_id);

  // -- the list -----------------------------------------------------------------

  async function refresh() {
    // A change while a request is out is fetched right after it.
    if (loading) { stale = true; return loading; }
    const q = new URLSearchParams({limit: '50'});
    const query = $('#sx-search').value.trim(); if (query) q.set('query', query);
    if ($('#sx-live').checked) q.set('live_only', '1');
    loading = (async () => {
      try {
        const r = await fetch('/account/work/sessions?' + q, {cache: 'no-store'});
        if (r.status === 401 || r.status === 403) throw Error('Sign in with your operator account to see sessions.');
        if (!r.ok) throw Error('Could not load sessions (' + r.status + ').');
        data = await r.json();
        $('#sx-status').textContent = 'Updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
        render();
      } catch (e) { $('#sx-status').textContent = e.message; }
      finally { loading = null; if (stale) { stale = false; refresh(); } }
    })();
    return loading;
  }
  function schedule() { clearTimeout(timer); if (active) timer = setTimeout(async () => { if (!document.hidden) await refresh(); schedule(); }, POLL_MS); }
  function visible() {
    const agent = $('#sx-agent').value, host = $('#sx-host').value, live = $('#sx-live').checked;
    const q = $('#sx-search').value.trim().toLowerCase();
    return data.sessions.filter(r => (!agent || r.agent === agent) && (!host || r.worker_id === host)
      && (!live || r.state !== 'closed')
      && (!q || `${r.title} ${r.cwd} ${r.native_id}`.toLowerCase().includes(q)));
  }
  function groups(list) {
    const hosts = new Map();
    for (const r of list) {
      let h = hosts.get(r.worker_id); if (!h) hosts.set(r.worker_id, h = {id: r.worker_id, name: r.worker || r.worker_id, projects: new Map(), best: 2, updated: 0});
      const key = r.cwd || ''; let p = h.projects.get(key); if (!p) h.projects.set(key, p = {cwd: key, items: [], best: 2, updated: 0});
      p.items.push(r); p.best = Math.min(p.best, rank(r)); p.updated = Math.max(p.updated, r.updated || 0);
      h.best = Math.min(h.best, rank(r)); h.updated = Math.max(h.updated, r.updated || 0);
    }
    const order = (a, b) => a.best - b.best || b.updated - a.updated;
    return [...hosts.values()].sort((a, b) => order(a, b) || a.name.localeCompare(b.name)).map(h => ({...h,
      projects: [...h.projects.values()].sort(order).map(p => ({...p, items: p.items.sort((a, b) => rank(a) - rank(b) || (b.updated || 0) - (a.updated || 0))}))}));
  }
  function render() {
    const counts = {live: 0, idle: 0, closed: 0}; for (const r of data.sessions) counts[r.state in counts ? r.state : 'closed']++;
    $('#sx-counts').textContent = `${counts.live} live · ${counts.idle} idle · ${counts.closed} closed`;
    const pick = $('#sx-host'), chosen = pick.value;
    pick.innerHTML = '<option value="">All hosts</option>' + data.workers.map(w => `<option value="${esc(w.worker_id)}">${esc(w.name || w.worker_id)}</option>`).join('');
    if (data.workers.some(w => w.worker_id === chosen)) pick.value = chosen;
    renderNotices(); renderList(); renderForm(); syncOpen();
  }
  function renderNotices() {
    const notes = [];
    for (const e of data.errors || []) {
      const w = data.workers.find(x => x.worker_id === e.worker_id);
      notes.push(w?.stale && w.fetched ? `${esc(e.worker || e.worker_id)} did not answer (${esc(e.error)}); showing its list from ${esc(ago(w.fetched))}.`
        : `${esc(e.worker || e.worker_id)}: ${esc(e.error)}`);
    }
    $('#sx-notices').innerHTML = notes.map(n => `<p class="sx-notice">${n}</p>`).join('');
  }
  function badge(r) {
    const out = [];
    if (r.view?.terminal) out.push('terminal'); else if (r.view?.mirror) out.push('mirror');
    if (r.origin === 'rook') out.push('rook');
    if (r.links?.task) out.push('task ' + r.links.task);
    return out.map(b => `<span class="sx-chip">${esc(b)}</span>`).join('');
  }
  function stateWord(r) {
    if (r.state === 'live') return r.activity === 'ready' ? 'live' : 'working';
    return r.state || 'closed';
  }
  function renderList() {
    const list = visible(), selected = open?.rec.key;
    if (!list.length) {
      $('#sx-list').innerHTML = `<p class="sx-muted sx-empty">${data.sessions.length ? 'No sessions match these filters.' : data.workers.length ? 'No sessions on your hosts yet. Start one with New session.' : 'No connected hosts report sessions.'}</p>`;
      return;
    }
    $('#sx-list').innerHTML = groups(list).map(h => {
      const w = data.workers.find(x => x.worker_id === h.id);
      const c = w?.counts ? ` · ${w.counts.live || 0} live, ${w.counts.idle || 0} idle` : '';
      return `<section class="sx-host"><h3 class="sx-host-head">${esc(h.name)}<span class="sx-muted">${esc(c)}</span>${w?.stale ? '<span class="sx-chip sx-warn">stale</span>' : ''}</h3>` +
        h.projects.map(p => `<div class="sx-project"><div class="sx-project-head" title="${esc(p.cwd)}"><strong>${esc(base(p.cwd))}</strong><span class="sx-muted">${esc(p.cwd)}</span></div>` +
          p.items.map(r => `<button type="button" class="sx-item${r.key === selected ? ' selected' : ''}" data-key="${esc(r.key)}"><i class="sx-dot sx-${esc(r.state)}" aria-hidden="true"></i><span class="sx-item-main"><strong>${esc(r.title || r.native_id)}</strong><span class="sx-muted">${esc([r.agent, stateWord(r), ago(r.updated), r.messages ? r.messages + ' messages' : ''].filter(Boolean).join(' · '))}</span>${badge(r) ? `<span class="sx-badges">${badge(r)}</span>` : ''}</span></button>`).join('') + '</div>').join('') +
        '</section>';
    }).join('');
  }

  // -- new session -----------------------------------------------------------------

  function renderForm() {
    const form = $('#sx-form'), pick = form.elements.worker, value = pick.value;
    const hosts = data.workers.filter(w => (w.harnesses || []).length);
    pick.innerHTML = hosts.length ? hosts.map(w => `<option value="${esc(w.worker_id)}">${esc(w.name || w.worker_id)}</option>`).join('') : '<option value="">No host can start sessions</option>';
    if (hosts.some(w => w.worker_id === value)) pick.value = value;
    renderHarnesses();
  }
  function renderHarnesses() {
    const form = $('#sx-form'), w = data.workers.find(x => x.worker_id === form.elements.worker.value);
    const pick = form.elements.harness, value = pick.value, list = w?.harnesses || [];
    pick.innerHTML = list.map(h => `<option>${esc(h)}</option>`).join('');
    if (list.includes(value)) pick.value = value; else if (list.includes('claude')) pick.value = 'claude';
    const folders = [...new Set(data.sessions.filter(r => r.worker_id === w?.worker_id && r.cwd).map(r => r.cwd))].slice(0, 50);
    $('#sx-folders').innerHTML = folders.map(f => `<option value="${esc(f)}"></option>`).join('');
  }
  $('#sx-new').onclick = () => {
    const form = $('#sx-form'); form.hidden = !form.hidden; if (form.hidden) return;
    $('#sx-form-error').textContent = '';
    if (open?.rec && data.workers.some(w => w.worker_id === open.rec.worker_id && (w.harnesses || []).length)) {
      form.elements.worker.value = open.rec.worker_id; renderHarnesses();
      if (open.rec.cwd) form.elements.cwd.value = open.rec.cwd;
    }
    form.elements.cwd.focus();
  };
  $('#sx-form-cancel').onclick = () => { $('#sx-form').hidden = true; };
  $('#sx-form').elements.worker.onchange = renderHarnesses;
  $('#sx-form').onsubmit = async e => {
    e.preventDefault();
    const form = e.currentTarget, f = new FormData(form), values = Object.fromEntries(f), button = form.querySelector('[type=submit]');
    button.disabled = true; button.textContent = 'Starting…'; $('#sx-form-error').textContent = '';
    try {
      const res = await post('new', {...values, mcp: f.get('mcp') === 'on', id: crypto.randomUUID(), cols: 120, rows: 32});
      form.hidden = true; form.elements.title.value = ''; form.elements.task.value = '';
      const w = data.workers.find(x => x.worker_id === values.worker);
      select({key: 'new:' + res.session, worker_id: values.worker, worker: w?.name || values.worker, agent: values.harness,
        native_id: res.terminal, title: res.title || values.harness, cwd: values.cwd, state: 'live', origin: 'rook',
        view: {terminal: res.terminal, mirror: false, transcript: false}, input: 'pty', links: {work_session: res.session, ...(values.task ? {task: values.task} : {})}});
      refresh();
    } catch (err) { $('#sx-form-error').textContent = err.message; }
    finally { button.disabled = false; button.textContent = 'Start'; }
  };

  // -- an open session -------------------------------------------------------------

  function modeOf(rec) {
    // A closed session's finished terminal says less than its transcript.
    if (rec.view?.terminal && (rec.state !== 'closed' || !rec.view.transcript)) return 'terminal:' + rec.view.terminal;
    if (rec.view?.mirror) return 'mirror';
    if (rec.view?.transcript) return 'transcript';
    return 'none';
  }
  function same(a, b) {
    return a.key === b.key || (a.links?.work_session && a.links.work_session === b.links?.work_session)
      || (a.view?.terminal && a.worker_id === b.worker_id && a.view.terminal === b.view?.terminal);
  }
  function syncOpen() {
    if (!open) return;
    const found = data.sessions.find(r => same(r, open.rec));
    if (found) { open.rec = found; renderDetail(); }
  }
  function select(rec) {
    if (open && same(open.rec, rec)) { open.rec = rec; renderDetail(); return; }
    closeDetail();
    const el = $('#sx-detail'); el.hidden = false; $('#sx-body').classList.add('has-detail');
    el.innerHTML = `
      <header class="sx-d-head">
        <button type="button" class="sx-back" data-act="back">← All sessions</button>
        <div class="sx-d-title"><h3></h3><div class="sx-muted sx-d-meta"></div></div>
        <div class="sx-d-actions">
          <button type="button" data-act="resume" hidden>Resume in a Rook terminal</button>
          <button type="button" data-act="stop" hidden>Stop</button>
          <button type="button" data-act="close" class="sx-link" aria-label="Close this session's view">Close</button>
        </div>
      </header>
      <p class="sx-hint" hidden></p>
      <form class="sx-d-link"><label class="sx-muted">Task <input name="task" placeholder="t_… or slug" maxlength="120"></label><button type="submit">Link</button><span class="sx-muted sx-link-note" role="status"></span></form>
      <div class="sx-view"></div>
      <form class="sx-send" hidden>
        <label class="sx-muted" for="sx-send-text">Send to this session</label>
        <textarea id="sx-send-text" rows="2" maxlength="24000" placeholder="A message, as if typed by you…"></textarea>
        <div class="sx-row"><span class="sx-muted sx-send-note" role="status"></span><button type="submit" class="sx-primary">Send</button></div>
      </form>`;
    open = {rec, mode: null, view: null, el};
    el.querySelector('.sx-d-link [name=task]').value = rec.links?.task || '';
    renderDetail(); renderList();
    if (matchMedia('(max-width: 850px)').matches) el.scrollIntoView({block: 'start'});
  }
  function closeDetail() {
    open?.view?.dispose(); open = null;
    const el = $('#sx-detail'); el.hidden = true; el.replaceChildren(); $('#sx-body').classList.remove('has-detail');
  }
  function renderDetail() {
    const {rec, el} = open, w = workerOf(rec);
    el.querySelector('.sx-d-title h3').textContent = rec.title || rec.native_id;
    el.querySelector('.sx-d-meta').textContent = [rec.agent, rec.worker, rec.cwd, stateWord(rec),
      rec.origin === 'external' ? 'started outside Rook' : 'started by Rook', rec.model,
      rec.inbox_policy && rec.inbox_policy !== 'unknown' ? 'inbox: ' + rec.inbox_policy : ''].filter(Boolean).join(' · ');
    const live = rec.state !== 'closed';
    const canTerm = (w?.harnesses || []).includes(rec.agent);
    const resume = el.querySelector('[data-act=resume]');
    resume.hidden = live || !rec.resumable || !['claude', 'codex'].includes(rec.agent);
    resume.disabled = !canTerm; resume.title = canTerm ? '' : 'This host cannot run Rook terminals for ' + rec.agent;
    el.querySelector('[data-act=stop]').hidden = !(live && rec.origin === 'rook');
    const hint = el.querySelector('.sx-hint');
    hint.hidden = !(live && rec.origin === 'external' && !rec.view?.terminal);
    hint.textContent = rec.agent === 'claude' ? 'Started outside Rook. To take it over here, run /rook-move in that Claude Code; Rook cannot stop it from this page.'
      : 'Started outside Rook. End it on its host; once closed, resume it here in a Rook terminal.';
    el.querySelector('.sx-send').hidden = !(live && rec.input && rec.input !== 'none');
    const mode = modeOf(rec);
    if (mode !== open.mode) {
      open.view?.dispose(); open.view = null; open.mode = mode;
      const host = el.querySelector('.sx-view'); host.replaceChildren();
      if (mode.startsWith('terminal:')) open.view = new TermCard(host, rec);
      else if (mode === 'mirror') open.view = new LiveLog(host, rec);
      else if (mode === 'transcript') open.view = new Transcript(host, rec);
      else host.innerHTML = '<p class="sx-muted">This session has nothing to show: no Rook terminal, mirror or transcript.</p>';
    } else open.view?.update?.(rec);
  }
  $('#sx-detail').addEventListener('click', async e => {
    const b = e.target.closest('button[data-act]'); if (!b || !open) return;
    const rec = open.rec, act = b.dataset.act;
    if (act === 'back' || act === 'close') { closeDetail(); renderList(); return; }
    if (act === 'stop') {
      if (!confirm('Stop this session? Its process on ' + (rec.worker || 'the host') + ' is ended.')) return;
      b.disabled = true;
      try { const res = await post('stop', who(rec)); note(res.stopped ? 'Stopped.' : res.note || 'Already closed.'); await refresh(); }
      catch (err) { note(err.message, true); } finally { b.disabled = false; }
    } else if (act === 'resume') {
      b.disabled = true; b.textContent = 'Resuming…';
      try {
        const res = await post('resume', {...who(rec), cwd: rec.cwd || '', title: rec.title || '', id: crypto.randomUUID(), cols: 120, rows: 32});
        select({...rec, key: 'resume:' + res.session, state: 'live', origin: 'rook', input: 'pty',
          view: {...rec.view, terminal: res.terminal}, links: {...rec.links, work_session: res.session}});
        refresh();
      } catch (err) { note(err.message, true); b.disabled = false; b.textContent = 'Resume in a Rook terminal'; }
    }
  });
  function note(text, bad) {
    const n = open?.el.querySelector('.sx-send-note'); if (!n) return;
    n.textContent = text; n.classList.toggle('sx-error', !!bad);
    if (open.el.querySelector('.sx-send').hidden) { const h = open.el.querySelector('.sx-hint'); h.hidden = false; h.textContent = text; }
  }
  $('#sx-detail').addEventListener('submit', async e => {
    e.preventDefault(); if (!open) return;
    const form = e.target, rec = open.rec;
    if (form.matches('.sx-d-link')) {
      const task = form.elements.task.value.trim(), out = form.querySelector('.sx-link-note');
      try {
        await post('link', {...who(rec), task, work_session: rec.links?.work_session || ''});
        out.textContent = task ? 'Linked.' : 'Unlinked.'; open.rec = {...rec, links: {...rec.links, task: task || undefined}}; refresh();
      } catch (err) { out.textContent = err.message; }
      return;
    }
    if (form.matches('.sx-send')) {
      const box = form.querySelector('textarea'), text = box.value, button = form.querySelector('button');
      if (!text.trim()) return;
      button.disabled = true; note('Sending…');
      try {
        const res = await post('send', {...who(rec), text, command_id: crypto.randomUUID()});
        box.value = '';
        note(res.delivery === 'held' ? `Waiting for approval on ${rec.worker || 'its host'}.`
          : res.delivery === 'keys' ? 'Typed into the Rook terminal.' : 'Delivered as a new turn.');
      } catch (err) { note(err.message, true); }
      finally { button.disabled = false; }
    }
  });
  $('#sx-detail').addEventListener('keydown', e => {
    if (e.target.id === 'sx-send-text' && e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); e.target.form.requestSubmit(); }
  });

  // Tier 1: the session's Rook terminal (take/release control; the holder sizes it).
  class TermCard {
    constructor(host, rec) {
      this.rec = rec; this.disposed = false;
      host.innerHTML = `<article class="sx-term"><header><span class="sx-holder sx-muted"></span><span class="sx-term-actions"><button type="button" data-t="take">Take control</button><button type="button" data-t="release">Release</button><select data-t="handoff" aria-label="Hand off control"></select><button type="button" data-t="int" title="Send Ctrl-C">Ctrl-C</button></span></header><div class="sx-xterm"></div><p class="sx-term-note sx-muted" role="status"></p></article>`;
      this.card = host.firstElementChild;
      this.card.addEventListener('click', e => {
        const b = e.target.closest('[data-t]'); if (!b || !this.term) return;
        if (b.dataset.t === 'take' || b.dataset.t === 'release') { this.term.send({op: b.dataset.t}); if (b.dataset.t === 'take') this.term.focus(); }
        else if (b.dataset.t === 'int') this.term.send({op: 'signal', sig: 'INT'});
      });
      this.card.addEventListener('change', e => { if (e.target.dataset.t === 'handoff' && e.target.value) this.term?.send({op: 'handoff', to: e.target.value}); });
      this.start(rec);
    }
    async start(rec) {
      let sid = rec.links?.work_session;
      try {
        if (!sid) sid = (await post('attach', {...who(rec), terminal: rec.view.terminal})).session;
      } catch (err) { this.card.querySelector('.sx-term-note').textContent = err.message; return; }
      if (this.disposed) return;
      this.sid = sid;
      this.term = new TermView(sid, this.card.querySelector('.sx-xterm'), csrf, view => this.paint(view));
      this.term.mount().catch(err => { this.card.querySelector('.sx-term-note').textContent = 'Terminal failed to load: ' + err.message; });
    }
    paint(view) {
      const st = view.state, me = view.me, holder = st.holder, others = (st.viewers || []).filter(x => x.id !== me);
      const text = holder ? (holder === me ? 'You have control' : ((st.viewers || []).find(x => x.id === holder)?.label || 'Someone') + ' has control') : 'Nobody has control: type to take it';
      this.card.querySelector('.sx-holder').textContent = text + (others.length ? ` · ${others.length + 1} watching` : '');
      this.card.querySelector('[data-t=take]').hidden = holder === me;
      this.card.querySelector('[data-t=release]').hidden = holder !== me;
      const hand = this.card.querySelector('[data-t=handoff]');
      hand.hidden = holder !== me || !others.length;
      hand.innerHTML = '<option value="">Hand off to…</option>' + others.map(x => `<option value="${esc(x.id)}">${esc(x.label)}</option>`).join('');
      this.card.querySelector('.sx-term-note').textContent = view.lastNote || (st.running === false ? (st.lost || 'Process exited' + (st.exit_code != null ? ' with code ' + st.exit_code : '') + '.') : '');
      this.card.classList.toggle('ended', st.running === false);
    }
    dispose() { this.disposed = true; this.term?.dispose(); }
  }

  // Read-only emulators for untrusted tool output: no input, no clipboard
  // (OSC 52) and no links (OSC 8); mounted only near the viewport.
  class Panes {
    constructor(scroller) {
      this.mounted = new Map();
      this.io = new IntersectionObserver(entries => {
        for (const e of entries) e.isIntersecting ? this.mount(e.target) : this.unmount(e.target);
      }, {root: scroller, rootMargin: '600px 0px'});
    }
    add(el, text) {
      const lines = String(text).split('\n').length;
      el._text = String(text); el.classList.add('sx-pane');
      el.style.height = Math.min(18, Math.max(2, lines)) * 17 + 10 + 'px';
      this.io.observe(el);
    }
    async mount(el) {
      if (this.mounted.has(el) || this.disposed) return;
      this.mounted.set(el, null);
      const {Terminal, FitAddon} = await loadXterm();
      if (this.disposed || !this.mounted.has(el)) return;
      const t = new Terminal({disableStdin: true, convertEol: true, cursorBlink: false, scrollback: 2000, fontSize: 12,
        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', theme: themeColors(), allowProposedApi: false});
      t.parser.registerOscHandler(52, () => true);
      t.parser.registerOscHandler(8, () => true);
      const fit = new FitAddon(); t.loadAddon(fit); t.open(el);
      try { fit.fit(); } catch {}
      t.write('\x1b[?25l' + el._text);
      this.mounted.set(el, t);
    }
    unmount(el) { const t = this.mounted.get(el); this.mounted.delete(el); t?.dispose(); }
    forget(el) { this.io.unobserve(el); this.unmount(el); }
    dispose() { this.disposed = true; this.io.disconnect(); for (const t of this.mounted.values()) t?.dispose(); this.mounted.clear(); }
  }
  function trim(log, panes) {
    while (log.childElementCount > MAX_NODES) { const n = log.firstElementChild; n.querySelectorAll('.sx-pane').forEach(p => panes.forget(p)); n.remove(); }
  }
  function stick(log) { return log.scrollHeight - log.scrollTop - log.clientHeight < 80; }

  // Tier 2: live events from the Claude Code mod's mirror (sessions.mirror).
  class LiveLog {
    constructor(host, rec) {
      this.rec = rec; this.cursor = 0; this.tools = new Map(); this.streaming = null; this.ctrl = new AbortController();
      host.innerHTML = `<div class="sx-live-head"><span class="sx-chip sx-state">connecting</span><span class="sx-muted">Live view from the Rook mod on ${esc(rec.worker || 'its host')}</span></div><div class="sx-log" tabindex="0" aria-label="Session events"></div><p class="sx-muted sx-log-note" role="status"></p>`;
      this.log = host.querySelector('.sx-log'); this.chip = host.querySelector('.sx-state'); this.noteEl = host.querySelector('.sx-log-note');
      this.panes = new Panes(this.log);
      this.loop();
    }
    async loop() {
      let wait = 0, backoff = 1000;
      while (!this.ctrl.signal.aborted) {
        if (!active || document.hidden) { await sleep(1000); continue; }
        try {
          const res = await post('mirror', {...who(this.rec), cursor: this.cursor, wait}, this.ctrl.signal);
          if (this.ctrl.signal.aborted) return;
          this.noteEl.textContent = res.exists === false && !res.events.length ? 'No mirror yet for this session.' : '';
          const end = stick(this.log);
          for (const ev of res.events) this.apply(ev);
          trim(this.log, this.panes);
          if (end) this.log.scrollTop = this.log.scrollHeight;
          this.cursor = res.cursor; backoff = 1000; wait = 20;
          if (res.done) { this.setState('ended'); return; }
        } catch (err) {
          if (this.ctrl.signal.aborted) return;
          this.noteEl.textContent = err.message + ' Retrying…';
          await sleep(backoff); backoff = Math.min(backoff * 2, 15000);
        }
      }
    }
    setState(state) {
      this.chip.textContent = state === 'waiting' ? `waiting for approval on ${this.rec.worker || 'its host'}` : state;
      this.chip.dataset.state = state;
    }
    node(cls, html) { const n = document.createElement('div'); n.className = cls; n.innerHTML = html; this.log.append(n); return n; }
    apply(ev) {
      const t = ev.type;
      if (t !== 'assistant.delta' && t !== 'assistant.done') this.streaming = null;
      if (t === 'session.start') this.node('sx-sys', esc(['Session started', ev.model, ev.version ? 'Claude Code ' + ev.version : '', ev.inbound ? 'inbox: ' + ev.inbound : ''].filter(Boolean).join(' · ')));
      else if (t === 'prompt') this.node('sx-msg sx-user', `<b>${ev.from === 'peer' ? 'Message from another session' : 'Prompt'}</b><div class="sx-text"></div>`).querySelector('.sx-text').textContent = ev.text || '';
      else if (t === 'assistant.delta') {
        if (!this.streaming) { this.streaming = this.unfinished = this.node('sx-msg sx-assistant streaming', '<b>Assistant</b><div class="sx-text"></div>').querySelector('.sx-text'); }
        this.streaming.textContent += ev.text || '';
      } else if (t === 'assistant.done') {
        // The full message: it replaces the streamed pieces, even when other events came in between.
        const prior = this.unfinished?.isConnected && (ev.text || '').startsWith(this.unfinished.textContent) ? this.unfinished : null;
        const text = this.streaming || prior || this.node('sx-msg sx-assistant', '<b>Assistant</b><div class="sx-text"></div>').querySelector('.sx-text');
        text.textContent = ev.text || text.textContent; text.parentElement.classList.remove('streaming'); this.streaming = this.unfinished = null;
      } else if (t === 'tool.call') {
        const n = this.node('sx-tool', `<details open><summary><span class="sx-tool-name"></span><span class="sx-muted sx-tool-arg"></span><span class="sx-tool-ok"></span></summary><pre class="sx-tool-input"></pre><div class="sx-tool-out"></div></details>`);
        n.querySelector('.sx-tool-name').textContent = ev.name || 'tool';
        const input = typeof ev.input === 'string' ? ev.input : JSON.stringify(ev.input ?? '', null, 2);
        n.querySelector('.sx-tool-arg').textContent = input.replace(/\s+/g, ' ').slice(0, 120);
        n.querySelector('.sx-tool-input').textContent = input;
        if (ev.id) { this.tools.set(ev.id, n); if (this.tools.size > 200) this.tools.delete(this.tools.keys().next().value); }
      } else if (t === 'tool.result') {
        const n = this.tools.get(ev.id) || this.node('sx-tool', `<details open><summary><span class="sx-tool-name">result</span><span class="sx-tool-ok"></span></summary><div class="sx-tool-out"></div></details>`);
        this.tools.delete(ev.id);
        const ok = n.querySelector('.sx-tool-ok'); ok.textContent = ev.ok === false ? 'error' : 'ok'; ok.classList.toggle('sx-error', ev.ok === false);
        const out = n.querySelector('.sx-tool-out'); out.replaceChildren();
        if (ev.text) { const pane = document.createElement('div'); out.append(pane); this.panes.add(pane, ev.text); }
        n.querySelector('details').open = ev.ok === false;
      } else if (t === 'turn.end') this.node('sx-turn', esc('Turn ended' + (ev.stop_reason && ev.stop_reason !== 'answer' ? ' (' + ev.stop_reason + ')' : '')));
      else if (t === 'state') this.setState(ev.state || 'idle');
      else if (t === 'session.end') { this.node('sx-sys', esc('Session ended' + (ev.reason ? ' (' + ev.reason + ')' : ''))); this.setState('ended'); }
      if (this.chip.dataset.state === undefined && t !== 'state') this.setState(this.rec.state === 'idle' ? 'idle' : 'live');
    }
    update(rec) { this.rec = rec; }
    dispose() { this.ctrl.abort(); this.panes.dispose(); }
  }

  // Tier 3: the transcript tail (sessions.follow), polled.
  class Transcript {
    constructor(host, rec) {
      this.rec = rec; this.msgs = new Map(); this.nodes = new Map(); this.offset = 0; this.version = ''; this.ctrl = new AbortController();
      host.innerHTML = `<div class="sx-live-head"><span class="sx-chip">transcript</span><span class="sx-muted">From the session's log on ${esc(rec.worker || 'its host')}${rec.state === 'closed' ? '' : ', a few seconds behind'}</span></div><div class="sx-log" tabindex="0" aria-label="Transcript"></div><p class="sx-muted sx-log-note" role="status">Reading…</p>`;
      this.log = host.querySelector('.sx-log'); this.noteEl = host.querySelector('.sx-log-note');
      this.panes = new Panes(this.log);
      this.loop();
    }
    async loop() {
      let backoff = 2000;
      while (!this.ctrl.signal.aborted) {
        if (!active || document.hidden) { await sleep(1000); continue; }
        let again = false;
        try {
          const page = await post('follow', {...who(this.rec), offset: this.offset, version: this.version}, this.ctrl.signal);
          if (this.ctrl.signal.aborted) return;
          again = this.apply(page); backoff = 2000;
          this.noteEl.textContent = this.msgs.size ? '' : 'No messages yet.';
        } catch (err) {
          if (this.ctrl.signal.aborted) return;
          this.noteEl.textContent = err.message;
          await sleep(backoff); backoff = Math.min(backoff * 2, 30000); continue;
        }
        if (!again) {
          if (this.rec.state === 'closed' && this.version) { await this.idle(); continue; }
          await sleep(FOLLOW_MS);
        }
      }
    }
    async idle() { while (!this.ctrl.signal.aborted && this.rec.state === 'closed') await sleep(1000); }
    apply(page) {
      if (page.unchanged) return false;
      const total = page.total_messages || 0, from = Number.isInteger(page.replace_from) ? page.replace_from : this.offset;
      if (from === 0 && total > TAIL && (page.messages || []).length && page.messages[0].index === 0 && page.truncated) {
        // Start (or restart) at the tail instead of paging the whole history.
        this.clear(); this.offset = total - TAIL; this.version = ''; this.skipped = total - TAIL; return true;
      }
      for (const [i, n] of this.nodes) if (i >= from) { n.querySelectorAll('.sx-pane').forEach(p => this.panes.forget(p)); n.remove(); this.nodes.delete(i); this.msgs.delete(i); }
      for (const m of page.messages || []) {
        const prev = this.msgs.get(m.index);
        this.msgs.set(m.index, {role: m.role, text: prev && m.content_offset > 0 ? prev.text + m.content : m.content});
      }
      if (page.truncated) {
        if (page.next_offset > from) this.offset = page.next_offset;
        else { const m = this.msgs.get(from); if (m) m.clipped = true; this.offset = from + 1; }
        this.version = '';
      } else {
        this.version = page.version || '';
        this.offset = Math.max(0, ...this.msgs.keys());
      }
      this.paint();
      return !!page.truncated && this.offset < total;
    }
    clear() { for (const n of this.nodes.values()) { n.querySelectorAll('.sx-pane').forEach(p => this.panes.forget(p)); n.remove(); } this.nodes.clear(); this.msgs.clear(); }
    paint() {
      const end = stick(this.log);
      if (this.skipped && !this.log.querySelector('.sx-skipped')) { const s = document.createElement('div'); s.className = 'sx-sys sx-skipped'; s.textContent = `${this.skipped} earlier messages not shown`; this.log.prepend(s); }
      for (const i of [...this.msgs.keys()].sort((a, b) => a - b)) {
        const m = this.msgs.get(i); let n = this.nodes.get(i);
        if (n && n._text === m.text) continue;
        const fresh = !n;
        if (fresh) { n = document.createElement('div'); this.nodes.set(i, n); }
        else n.querySelectorAll('.sx-pane').forEach(p => this.panes.forget(p));
        n._text = m.text;
        const tool = m.role === 'tool';
        n.className = tool ? 'sx-tool' : 'sx-msg sx-' + (m.role === 'user' ? 'user' : 'assistant');
        n.innerHTML = `<b>${esc(tool ? 'Tool' : m.role === 'user' ? 'User' : 'Assistant')}${m.clipped ? ' (clipped)' : ''}</b><div class="sx-text"></div>`;
        if (tool) { const pane = n.querySelector('.sx-text'); pane.classList.remove('sx-text'); this.panes.add(pane, m.text); }
        else n.querySelector('.sx-text').textContent = m.text;
        if (fresh) {
          const after = [...this.nodes.keys()].filter(k => k > i).sort((a, b) => a - b)[0];
          if (after !== undefined && this.nodes.get(after).isConnected) this.log.insertBefore(n, this.nodes.get(after)); else this.log.append(n);
        }
      }
      trim(this.log, this.panes);
      if (end) this.log.scrollTop = this.log.scrollHeight;
    }
    update(rec) { this.rec = rec; }
    dispose() { this.ctrl.abort(); this.panes.dispose(); }
  }

  // -- wiring ----------------------------------------------------------------------

  $('#sx-list').addEventListener('click', e => {
    const b = e.target.closest('[data-key]'); if (!b) return;
    const rec = data.sessions.find(r => r.key === b.dataset.key); if (rec) select(rec);
  });
  $('#sx-search').addEventListener('input', () => { renderList(); clearTimeout(typing); typing = setTimeout(refresh, 350); });
  $('#sx-live').addEventListener('change', () => { renderList(); refresh(); });
  $('#sx-agent').addEventListener('change', renderList);
  $('#sx-host').addEventListener('change', renderList);
  $('#sx-classic')?.addEventListener('click', () => toClassic());
  await refresh(); schedule();
  return {
    activate() { if (!active) { active = true; refresh(); schedule(); } },
    deactivate() { active = false; clearTimeout(timer); closeDetail(); }
  };
}
