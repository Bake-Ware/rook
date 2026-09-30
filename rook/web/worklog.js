// Worklog: Work sessions as rooms (one per project directory and host) with
// live sessions as real terminals and finished ones collapsed into a log.
// Terminal bytes arrive 1:1 from the worker PTY: /account/work/term/<id>
// sends binary frames (8-byte big-endian stream offset + raw bytes) and JSON
// state; the browser sends JSON input/resize/control. See docs/web/worklog.md.
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const base = path => { const p = String(path || '').replace(/\/+$/, ''); return p.slice(p.lastIndexOf('/') + 1) || p || '(no directory)'; };
const ago = t => { if (!t) return ''; const s = Math.max(0, Date.now() / 1000 - t); return s < 90 ? 'just now' : s < 5400 ? Math.round(s / 60) + ' min ago' : s < 129600 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago'; };
const LIVE = 'live';
let xtermLoading = null;
function loadXterm() {
  if (!xtermLoading) {
    if (!document.querySelector('link[data-xterm-style]')) {
      const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = '/account/work/assets/vendor/xterm.css'; link.dataset.xtermStyle = '1'; document.head.append(link);
    }
    xtermLoading = Promise.all([import('/account/work/assets/vendor/xterm.mjs'), import('/account/work/assets/vendor/addon-fit.mjs')])
      .then(([x, f]) => ({Terminal: x.Terminal, FitAddon: f.FitAddon}));
  }
  return xtermLoading;
}
function themeColors() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
  return {background: v('--term-bg', '#0d0f12'), foreground: v('--term-fg', '#e6e1cf'), cursor: v('--accent', '#e6b450')};
}

// One live terminal view bound to a Work session.
class TermView {
  constructor(session, host, csrf, onChange) {
    this.session = session; this.host = host; this.csrf = csrf; this.onChange = onChange;
    this.cursor = null; this.me = null; this.state = {holder: null, viewers: [], running: true};
    this.retry = 0; this.disposed = false; this.decoder = null;
  }
  async mount() {
    const {Terminal, FitAddon} = await loadXterm();
    if (this.disposed) return;
    this.term = new Terminal({convertEol: false, cursorBlink: true, scrollback: 5000, fontSize: 13,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', theme: themeColors(), allowProposedApi: false});
    this.fit = new FitAddon(); this.term.loadAddon(this.fit);
    this.term.open(this.host);
    this.term.onData(data => this.send({op: 'input', data, enc: 't'}));
    this.term.onBinary(data => this.send({op: 'input', data: btoa(data), enc: 'b'}));
    this.resizeObserver = new ResizeObserver(() => this.layout()); this.resizeObserver.observe(this.host);
    this.connect();
  }
  holder() { return this.state.holder && this.state.holder === this.me; }
  layout() {
    // The holder sizes the PTY to its own window; everyone else renders the
    // PTY's grid as-is so output wraps exactly as it does for the holder.
    if (!this.term || this.disposed) return;
    const {cols, rows} = this.state;
    if (this.holder()) {
      try { this.fit.fit(); } catch {}
      if (this.term.cols !== cols || this.term.rows !== rows) this.send({op: 'resize', cols: this.term.cols, rows: this.term.rows});
    } else if (cols && rows) {
      if (this.term.cols !== cols || this.term.rows !== rows) this.term.resize(cols, rows);
    } else {
      try { this.fit.fit(); } catch {}
    }
  }
  connect() {
    if (this.disposed) return;
    const q = this.cursor != null ? '?since=' + this.cursor : '';
    const ws = this.ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/account/work/term/' + encodeURIComponent(this.session) + q);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { this.retry = 0; this.note(''); };
    ws.onmessage = e => {
      if (ws !== this.ws) return;
      if (typeof e.data !== 'string') return this.output(e.data);
      const m = JSON.parse(e.data);
      if (m.type === 'hello') { this.me = m.viewer; }
      if (m.type === 'hello' || m.type === 'state') { this.state = m; this.term.options.disableStdin = !!(m.holder && m.holder !== this.me) || !m.running; this.layout(); this.onChange?.(this); }
      else if (m.type === 'reset') { this.term.reset(); this.cursor = null; }
      else if (m.type === 'error') this.note(m.error);
    };
    ws.onclose = e => {
      if (ws !== this.ws || this.disposed) return;
      if (e.code === 1008) return this.note('Session expired. Sign in again.');
      if (!this.state.running) return;
      this.note('Reconnecting…');
      this.retry = Math.min(this.retry + 1, 6);
      this.timer = setTimeout(() => this.connect(), 500 * 2 ** (this.retry - 1));
    };
  }
  output(buffer) {
    const view = new DataView(buffer);
    let start = Number(view.getBigUint64(0)), bytes = new Uint8Array(buffer, 8);
    if (this.cursor != null && start < this.cursor) { // already have part of it
      const skip = this.cursor - start; if (skip >= bytes.length) return;
      bytes = bytes.subarray(skip); start = this.cursor;
    }
    this.term.write(bytes);
    this.cursor = start + bytes.length;
  }
  send(data) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({...data, csrf: this.csrf}));
  }
  note(text) { this.lastNote = text; this.onChange?.(this); }
  focus() { this.term?.focus(); }
  dispose() {
    this.disposed = true; clearTimeout(this.timer); this.resizeObserver?.disconnect();
    try { this.ws?.close(); } catch {}
    this.term?.dispose();
  }
}

export async function mountWorklog(root, boot, toClassic) {
  if (!document.querySelector('link[data-worklog-style]')) {
    const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = '/account/work/assets/worklog.css'; link.dataset.worklogStyle = '1'; document.head.append(link);
  }
  const {csrf} = boot;
  root.innerHTML = `
    <div class="wl">
      <aside class="wl-side">
        <div class="wl-side-head"><strong>Worklog</strong><button type="button" id="wl-new">+ Launch</button></div>
        <input id="wl-search" type="search" placeholder="Filter rooms and sessions…" aria-label="Filter rooms and sessions">
        <nav id="wl-rooms" aria-label="Rooms"><p class="wl-muted">Connecting…</p></nav>
        <div class="wl-side-foot"><span id="wl-connection" class="wl-muted" role="status">Connecting…</span>${toClassic ? '<button type="button" id="wl-classic">Classic view</button>' : ''}</div>
      </aside>
      <section class="wl-main">
        <header class="wl-room-head"><div><h2 id="wl-room-title">Worklog</h2><div id="wl-room-meta" class="wl-muted"></div></div></header>
        <form id="wl-launch" class="wl-launch" hidden>
          <h3>Launch an agent</h3>
          <div class="wl-grid">
            <label>Harness<select name="harness" required></select></label>
            <label>Host<select name="worker" required><option value="">Choose a host</option></select></label>
            <label class="wl-wide">Working directory<input name="cwd" placeholder="/home/you/project" required maxlength="2000"></label>
            <label>Model<input name="model" placeholder="Host default" maxlength="100"></label>
            <label>Title<input name="title" placeholder="What is this session for?" maxlength="160"></label>
            <label>Persona<input name="persona" placeholder="(persona plugin — optional)" maxlength="100"></label>
            <label class="wl-check"><input type="checkbox" name="mcp" checked> Give it a Rook MCP token scoped to this session</label>
          </div>
          <p class="wl-muted">The agent runs in a real terminal on the host, using that host's own login for the harness. The token expires after a day and is revoked when the session ends.</p>
          <div class="wl-row"><button type="submit">Launch</button><button type="button" id="wl-launch-cancel">Cancel</button></div>
        </form>
        <p id="wl-notice" role="alert"></p>
        <div id="wl-live"></div>
        <h3 id="wl-done-head" class="wl-section" hidden>Earlier</h3>
        <div id="wl-done"></div>
        <p id="wl-empty" class="wl-muted" hidden>No sessions in this room yet.</p>
      </section>
    </div>`;
  const $ = s => root.querySelector(s);
  let ws, active = true, reconnect, generation = 0, sessions = [], hosts = [], room = null, follow = null;
  const views = new Map();          // session id -> {card, view}
  const transcripts = new Map();    // session id -> loaded transcript state
  const pending = new Map();

  function notify(text) { $('#wl-notice').textContent = text || ''; }
  function send(data) {
    if (ws?.readyState !== WebSocket.OPEN) { notify('Connection is unavailable.'); return false; }
    ws.send(JSON.stringify({...data, csrf})); return true;
  }
  function command(op, session, extra = {}) {
    const id = crypto.randomUUID(); const data = {op, id, session, ...extra};
    if (send(data)) pending.set(id, data);
  }
  const isLive = s => !!s.term_running;
  const roomKey = s => JSON.stringify([s.worker_name || '?', s.cwd || '']);
  function rooms() {
    const map = new Map();
    for (const s of sessions) {
      const k = roomKey(s); let r = map.get(k);
      if (!r) map.set(k, r = {key: k, host: s.worker_name || '?', cwd: s.cwd || '', items: [], live: 0, updated: 0});
      r.items.push(s); r.updated = Math.max(r.updated, s.updated || 0); if (isLive(s)) r.live++;
    }
    return [...map.values()].sort((a, b) => (b.live > 0) - (a.live > 0) || b.updated - a.updated);
  }
  function filtered(list) {
    const q = $('#wl-search').value.trim().toLowerCase();
    return q ? list.filter(r => [r.host, r.cwd, ...r.items.map(s => s.title + ' ' + (s.agent || ''))].join(' ').toLowerCase().includes(q)) : list;
  }
  function renderRooms() {
    const all = rooms(); const live = sessions.filter(isLive).length;
    if (!room) room = live ? LIVE : all[0]?.key || LIVE;
    const list = filtered(all);
    $('#wl-rooms').innerHTML = `<button type="button" class="wl-room ${room === LIVE ? 'selected' : ''}" data-room="${esc(LIVE)}"><strong>Live now</strong><span>${live ? live + ' running' : 'nothing running'}</span>${live ? '<i class="wl-dot"></i>' : ''}</button>` +
      (list.length ? list.map(r => `<button type="button" class="wl-room ${r.key === room ? 'selected' : ''}" data-room="${esc(r.key)}"><strong>${esc(base(r.cwd))}</strong><span>${esc(r.host)} · ${r.items.length} session${r.items.length === 1 ? '' : 's'}${r.updated ? ' · ' + esc(ago(r.updated)) : ''}</span>${r.live ? '<i class="wl-dot"></i>' : ''}</button>`).join('') : '<p class="wl-muted">No matching rooms.</p>');
  }
  function roomSessions() {
    if (room === LIVE) return sessions.filter(isLive);
    return sessions.filter(s => roomKey(s) === room);
  }
  function hostFor(s) { return hosts.find(h => h.name === s.worker_name); }
  function statusText(s) {
    if (isLive(s)) return 'running';
    if (s.term && s.term_exit != null) return s.term_exit < 0 ? 'ended' : 'exited ' + s.term_exit;
    if (s.external) return 'running on host · follow it in Classic view';
    if (s.active) return 'active on host';
    return s.status || 'done';
  }
  function renderRoom() {
    const r = rooms().find(x => x.key === room);
    $('#wl-room-title').textContent = room === LIVE ? 'Live now' : base(r?.cwd);
    $('#wl-room-meta').textContent = room === LIVE ? 'Every running terminal, across hosts and projects.' : r ? `${r.host} · ${r.cwd}` : '';
    const items = roomSessions().slice().sort((a, b) => (b.updated || 0) - (a.updated || 0));
    const live = items.filter(isLive), done = items.filter(s => !isLive(s));
    // Live terminals: keep existing views, add new, drop ended/out-of-room.
    const want = new Set(live.map(s => s.id));
    for (const [id, v] of views) if (!want.has(id)) { v.view.dispose(); v.card.remove(); views.delete(id); }
    for (const s of live) {
      let v = views.get(s.id);
      if (!v) {
        const card = document.createElement('article'); card.className = 'wl-term'; card.dataset.session = s.id;
        card.innerHTML = `<header><div class="wl-term-title"><i class="wl-dot"></i><strong></strong><span class="wl-muted"></span></div><div class="wl-term-actions"><span class="wl-holder"></span><button type="button" data-act="take">Take control</button><button type="button" data-act="release">Release</button><select data-act="handoff" aria-label="Hand off control"></select><button type="button" data-act="int" title="Send Ctrl-C">Ctrl-C</button><button type="button" data-act="collapse" aria-expanded="true">Collapse</button><button type="button" data-act="close">End session</button></div></header><div class="wl-xterm"></div><p class="wl-term-note wl-muted" role="status"></p>`;
        $('#wl-live').append(card);
        const view = new TermView(s.id, card.querySelector('.wl-xterm'), csrf, renderTermCard);
        v = {card, view}; views.set(s.id, v); view.mount().catch(e => { card.querySelector('.wl-term-note').textContent = 'Terminal failed to load: ' + e.message; });
      }
      v.card.querySelector('.wl-term-title strong').textContent = s.title;
      v.card.querySelector('.wl-term-title span').textContent = [s.harness || s.agent, s.worker_name, room === LIVE ? s.cwd : '', s.model].filter(Boolean).join(' · ');
      renderTermCard(v.view);
    }
    $('#wl-done-head').hidden = !done.length || !live.length && room === LIVE;
    const open = new Set([...root.querySelectorAll('#wl-done details[open]')].map(d => d.dataset.session));
    $('#wl-done').innerHTML = done.map(s => {
      const h = hostFor(s), canPty = !!h?.term, canResume = s.imported && !s.active && !s.term_running && !s.external;
      const launched = s.harness && !s.imported;
      return `<details class="wl-entry" data-session="${esc(s.id)}" ${open.has(s.id) ? 'open' : ''}><summary><i class="wl-state wl-${esc(statusText(s).split(' ')[0].replace(/[^a-z]/g, ''))}"></i><strong>${esc(s.title)}</strong><span class="wl-muted">${esc([s.agent || 'codex', s.worker_name, s.message_count ? s.message_count + ' messages' : '', ago(s.updated)].filter(Boolean).join(' · '))}</span><em>${esc(statusText(s))}</em></summary>
        <div class="wl-entry-body"><div class="wl-muted">${esc(s.cwd || '')}${s.error ? ' · <span class="wl-bad">' + esc(s.error) + '</span>' : ''}${s.term_note ? ' · ' + esc(s.term_note) : ''}</div>
        <div class="wl-row">${canResume ? `<button type="button" data-resume="${esc(s.id)}" ${h ? '' : 'disabled title="Host is offline"'}>Resume${canPty ? ' in terminal' : ' on host'}</button>` : ''}${launched && h?.term ? `<button type="button" data-relaunch="${esc(s.id)}">Launch again</button>` : ''}
        <label class="wl-muted">Status <select data-status="${esc(s.id)}">${['auto', 'pending', 'blocked', 'closed'].map(v => `<option value="${v}" ${(s.review_status || 'auto') === v ? 'selected' : ''}>${v[0].toUpperCase() + v.slice(1)}</option>`).join('')}</select></label>
        ${s.imported ? `<button type="button" data-transcript="${esc(s.id)}">Show conversation</button>` : ''}</div>
        <div class="wl-transcript" data-transcript-body="${esc(s.id)}"></div></div></details>`;
    }).join('');
    for (const [id, t] of transcripts) { const body = root.querySelector(`[data-transcript-body="${CSS.escape(id)}"]`); if (body) renderTranscript(id, body); }
    $('#wl-empty').hidden = items.length > 0;
    if (room === LIVE && !items.length) $('#wl-empty').textContent = 'Nothing is running. Launch an agent, or resume a session from a room.';
    else $('#wl-empty').textContent = 'No sessions in this room yet.';
  }
  function renderTermCard(view) {
    const v = views.get(view.session); if (!v) return;
    const st = view.state, me = view.me, holder = st.holder;
    const others = (st.viewers || []).filter(x => x.id !== me);
    const who = holder ? (holder === me ? 'You have control' : (st.viewers || []).find(x => x.id === holder)?.label + ' has control') : 'Nobody has control — type to take it';
    v.card.querySelector('.wl-holder').textContent = who + (others.length ? ` · ${others.length + 1} watching` : '');
    v.card.querySelector('[data-act=take]').hidden = holder === me;
    v.card.querySelector('[data-act=release]').hidden = holder !== me;
    const hand = v.card.querySelector('[data-act=handoff]');
    hand.hidden = holder !== me || !others.length;
    hand.innerHTML = '<option value="">Hand off to…</option>' + others.map(x => `<option value="${esc(x.id)}">${esc(x.label)}</option>`).join('');
    v.card.querySelector('.wl-term-note').textContent = view.lastNote || (st.running === false ? (st.lost || 'Process exited' + (st.exit_code != null ? ' with code ' + st.exit_code : '') + '.') : '');
    v.card.classList.toggle('ended', st.running === false);
  }
  async function loadTranscript(id) {
    let t = transcripts.get(id);
    if (!t) transcripts.set(id, t = {messages: [], offset: 0, contentOffset: 0, snapshot: '', more: true, loading: false, error: ''});
    if (t.loading || !t.more) return;
    t.loading = true; t.error = '';
    try {
      for (let pages = 0; t.more && pages < 5; pages++) {
        const q = new URLSearchParams({offset: t.offset, content_offset: t.contentOffset, snapshot: t.snapshot});
        const r = await fetch(`/account/work/history/${encodeURIComponent(id)}?${q}`, {cache: 'no-store'});
        const page = await r.json(); if (!r.ok) throw Error(page.error || 'Unable to read host history.');
        t.snapshot = page.snapshot || t.snapshot;
        for (const m of page.messages || []) {
          const last = t.messages[t.messages.length - 1];
          if (last && last.index === m.index) last.text += m.content; else t.messages.push({index: m.index, role: m.role, text: m.content});
        }
        t.more = !!page.truncated; if (t.more) { t.offset = page.next_offset; t.contentOffset = page.next_content_offset; }
      }
    } catch (e) { t.error = e.message; t.more = false; }
    t.loading = false;
    const body = root.querySelector(`[data-transcript-body="${CSS.escape(id)}"]`); if (body) renderTranscript(id, body);
  }
  function renderTranscript(id, body) {
    const t = transcripts.get(id); if (!t) return;
    body.innerHTML = t.messages.map(m => `<div class="wl-msg wl-${esc(m.role)}"><b>${esc(m.role)}</b><pre>${esc(m.text)}</pre></div>`).join('') +
      (t.loading ? '<p class="wl-muted">Reading from host…</p>' : '') + (t.error ? `<p class="wl-bad">${esc(t.error)}</p>` : '') +
      (t.more && !t.loading ? `<button type="button" data-transcript="${esc(id)}">Load more</button>` : '');
  }
  function renderHosts() {
    const form = $('#wl-launch'), pick = form.querySelector('[name=worker]'), value = pick.value;
    const term = hosts.filter(h => h.term);
    pick.innerHTML = '<option value="">Choose a host</option>' + term.map(h => `<option value="${esc(h.id)}">${esc(h.name)}</option>`).join('');
    if (term.some(h => h.id === value)) pick.value = value;
    renderHarnesses();
  }
  function renderHarnesses() {
    const form = $('#wl-launch'), h = hosts.find(x => x.id === form.querySelector('[name=worker]').value);
    const pick = form.querySelector('[name=harness]'), value = pick.value;
    const list = h ? h.harnesses : boot.harnesses || ['claude', 'codex', 'hermes', 'shell'];
    pick.innerHTML = list.map(x => `<option value="${esc(x)}">${esc(x)}</option>`).join('');
    if (list.includes(value)) pick.value = value;
  }
  function render() { renderRooms(); renderRoom(); }
  function connect() {
    if (!active) return;
    const gen = ++generation;
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/account/work/ws');
    ws.onopen = () => { if (gen === generation) $('#wl-connection').textContent = 'Connected'; };
    ws.onmessage = e => {
      if (gen !== generation) return;
      const m = JSON.parse(e.data);
      if (m.type === 'index') {
        sessions = m.sessions || [];
        // After a launch, open the new session's room once it is listed.
        const launched = follow && sessions.find(x => x.id === follow);
        if (launched) { room = roomKey(launched); follow = null; }
        if (JSON.stringify(hosts) !== JSON.stringify(m.hosts || [])) { hosts = m.hosts || []; renderHosts(); }
        render();
      } else if (m.type === 'launched') {
        $('#wl-launch').hidden = true;
        const s = sessions.find(x => x.id === m.session);
        if (s) { room = roomKey(s); render(); } else follow = m.session;
      } else if (m.type === 'error') notify(m.error);
      else if (m.type === 'ack' && m.result?.status !== 'accepted') {
        pending.delete(m.id);
        if (m.result?.status === 'error') notify(m.result.error || 'The request failed.');
      }
    };
    ws.onclose = e => {
      if (gen !== generation) return;
      $('#wl-connection').textContent = e.code === 1008 ? 'Session expired. Sign in again.' : 'Disconnected · sessions keep running';
      if (active && e.code !== 1008) reconnect = setTimeout(connect, 1500);
    };
  }
  $('#wl-new').onclick = () => {
    const form = $('#wl-launch'); form.hidden = !form.hidden; if (form.hidden) return;
    const r = rooms().find(x => x.key === room);
    if (r) { form.querySelector('[name=cwd]').value = r.cwd; const h = hosts.find(x => x.name === r.host && x.term); if (h) { form.querySelector('[name=worker]').value = h.id; renderHarnesses(); } }
    form.querySelector('[name=cwd]').focus();
  };
  $('#wl-launch-cancel').onclick = () => { $('#wl-launch').hidden = true; };
  $('#wl-launch').querySelector('[name=worker]').onchange = renderHarnesses;
  $('#wl-launch').onsubmit = e => {
    e.preventDefault(); notify('');
    const f = new FormData(e.currentTarget), values = Object.fromEntries(f);
    command('launch', undefined, {...values, mcp: f.get('mcp') === 'on', cols: 120, rows: 32});
  };
  $('#wl-search').oninput = renderRooms;
  $('#wl-rooms').onclick = e => { const b = e.target.closest('[data-room]'); if (b) { room = b.dataset.room; render(); } };
  $('#wl-classic')?.addEventListener('click', () => toClassic());
  root.querySelector('.wl-main').addEventListener('click', e => {
    const t = e.target.closest('button'); if (!t) return;
    if (t.dataset.resume) { command('resume', t.dataset.resume, {pty: true, mcp: false, cols: 120, rows: 32}); return; }
    if (t.dataset.transcript) { loadTranscript(t.dataset.transcript); return; }
    if (t.dataset.relaunch) {
      const s = sessions.find(x => x.id === t.dataset.relaunch), h = hostFor(s); if (!s || !h) return;
      const form = $('#wl-launch'); form.hidden = false;
      form.querySelector('[name=worker]').value = h.id; renderHarnesses();
      for (const k of ['cwd', 'model', 'title', 'persona']) form.querySelector(`[name=${k}]`).value = s[k] || '';
      form.querySelector('[name=harness]').value = s.harness; form.scrollIntoView({block: 'nearest'}); return;
    }
    const card = t.closest('.wl-term'); if (!card) return;
    const v = views.get(card.dataset.session); if (!v) return;
    const act = t.dataset.act;
    if (act === 'take' || act === 'release') { v.view.send({op: act}); if (act === 'take') v.view.focus(); }
    else if (act === 'int') v.view.send({op: 'signal', sig: 'INT'});
    else if (act === 'collapse') { const hidden = card.classList.toggle('collapsed'); t.textContent = hidden ? 'Expand' : 'Collapse'; t.setAttribute('aria-expanded', String(!hidden)); if (!hidden) v.view.layout(); }
    else if (act === 'close') { if (confirm('End this session? The process on the host is stopped.')) command('term_close', card.dataset.session); }
  });
  root.querySelector('.wl-main').addEventListener('change', e => {
    const t = e.target;
    if (t.dataset.status) command('status', t.dataset.status, {status: t.value});
    else if (t.dataset.act === 'handoff' && t.value) { const card = t.closest('.wl-term'); views.get(card.dataset.session)?.view.send({op: 'handoff', to: t.value}); }
  });
  connect();
  return {
    activate() { if (!active) { active = true; connect(); render(); } },
    deactivate() {
      active = false; clearTimeout(reconnect); generation++; try { ws?.close(); } catch {}
      for (const v of views.values()) { v.view.dispose(); v.card.remove(); }
      views.clear();
    }
  };
}
