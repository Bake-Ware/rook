'use strict';
// The Rook panel: the dashboard's sections, and four read-only lists from the
// hub (what waits on you, work in progress, knowledge search, sessions).
// Everything opens in the dashboard window; nothing is decided here.
const listEl = $('#list'), msg = $('#msg');
let tab = 'waiting', loading = 0;

const KIND = { hold: 'Held call', job: 'Job call', card: 'Huddle card', ask: 'Question', house: 'House agent', trust: 'Trust change' };
const EMPTY = {
  waiting: 'Nothing is waiting for you.',
  work: 'No task is in progress or blocked.',
  knowledge: 'Type some words and press Enter.',
  sessions: 'No live agent sessions.',
};

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

function ago(ts) {
  if (!ts) return '';
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 90) return 'just now';
  if (s < 5400) return Math.round(s / 60) + ' min ago';
  if (s < 129600) return Math.round(s / 3600) + ' h ago';
  return Math.round(s / 86400) + ' d ago';
}

function row(item) {
  const li = el('li', 'pn-item');
  li.tabIndex = 0;
  li.setAttribute('role', 'link');
  const title = el('div', 'pn-title', item.title);
  const meta = el('div', 'pn-meta');
  const body = el('div', 'pn-body');
  if (tab === 'waiting') {
    meta.textContent = [KIND[item.kind] || item.kind, item.who, item.age && item.age + ' ago', item.name].filter(Boolean).join(' · ');
    body.textContent = item.text;
  } else if (tab === 'work') {
    li.classList.add(item.state === 'blocked' ? 'blocked' : 'active');
    meta.textContent = [item.state === 'blocked' ? 'Blocked' : 'In progress', item.project].filter(Boolean).join(' · ');
    body.textContent = item.note;
  } else if (tab === 'knowledge') {
    meta.textContent = item.kind === 'knowledge' ? 'Knowledge' : item.kind === 'task' ? 'Task' : item.kind;
    body.textContent = item.excerpt;
  } else {
    if (item.state === 'live') li.classList.add('active');
    meta.textContent = [item.agent, item.worker, item.state, ago(item.updated)].filter(Boolean).join(' · ');
    body.textContent = item.cwd;
  }
  li.append(title, meta);
  if (body.textContent) li.append(body);
  const open = () => invoke('open_dashboard_cmd', { path: item.link }).catch(e => setMsg(msg, String(e), 'err'));
  li.addEventListener('click', open);
  li.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
  return li;
}

function count(which, n) {
  const c = $('#n-' + which);
  if (c) c.textContent = n ? String(n) : '';
}

async function load(which, quiet) {
  const query = which === 'knowledge' ? $('#q').value.trim() : undefined;
  if (which === 'knowledge' && !query) { listEl.replaceChildren(); setMsg(msg, EMPTY.knowledge); return; }
  const mine = ++loading;
  if (!quiet) setMsg(msg, 'Loading…');
  try {
    const items = await invoke('panel_data', { kind: which, query });
    if (which !== 'knowledge') count(which, items.length);
    if (mine !== loading || which !== tab) return;
    listEl.replaceChildren(...items.map(row));
    setMsg(msg, items.length ? '' : (which === 'knowledge' ? 'Nothing matches "' + query + '".' : EMPTY[which]));
  } catch (e) {
    if (mine === loading && which === tab) { listEl.replaceChildren(); setMsg(msg, String(e), 'err'); }
  }
}

function select(which) {
  tab = which;
  for (const b of document.querySelectorAll('[data-tab]')) b.setAttribute('aria-selected', String(b.dataset.tab === which));
  $('#search').hidden = which !== 'knowledge';
  listEl.replaceChildren();
  if (which === 'knowledge') $('#q').focus();
  load(which);
}

function sections(list) {
  const nav = $('#sections');
  nav.replaceChildren(...list.map(s => {
    const d = el('details', 'pn-sec');
    d.append(el('summary', '', s.title));
    const pages = el('div', 'pn-pages');
    for (const p of s.pages) {
      const a = el('a', '', p.title);
      a.addEventListener('click', () => { d.open = false; invoke('open_dashboard_cmd', { path: '/#' + p.hash }); });
      pages.append(a);
    }
    d.append(pages);
    // One open at a time.
    d.addEventListener('toggle', () => { if (d.open) for (const o of nav.querySelectorAll('details[open]')) if (o !== d) o.open = false; });
    return d;
  }));
}

// A 0.5 hub has no Work, Knowledge or Sessions lists: holds, the hub's pages
// and the house agent only (castling-attention: the agent opens a page).
let v05 = false;

async function refresh() {
  const st = await invoke('get_state');
  v05 = !!(st.settings && st.settings.hub_version === '0.5');
  for (const b of document.querySelectorAll('[data-tab]')) b.hidden = v05 && b.dataset.tab !== 'waiting';
  if (v05 && tab !== 'waiting') select('waiting');
  $('#hub-line').textContent = st.signed_in ? st.hub + (st.principal ? ' · ' + st.principal : '') : 'Not signed in. Open Settings to connect to your hub.';
  sections(st.sections);
  $('#ask-page').hidden = !st.signed_in;
  if (!st.signed_in) { listEl.replaceChildren(); setMsg(msg, 'Sign in to your hub first (Settings).', 'err'); return; }
  load(tab);
  // Counts on the other tabs, without switching.
  for (const w of v05 ? [] : ['waiting', 'work']) if (w !== tab) invoke('panel_data', { kind: w }).then(i => count(w, i.length)).catch(() => {});
}

for (const b of document.querySelectorAll('[data-tab]')) b.addEventListener('click', () => select(b.dataset.tab));
$('#search').addEventListener('submit', e => { e.preventDefault(); load('knowledge'); });
$('#ask').addEventListener('click', () => invoke('show_window', { name: 'overlay' }));
$('#ask-page').addEventListener('click', () => invoke('ask_on_page_cmd').catch(e => setMsg(msg, String(e), 'err')));
$('#open-dashboard').addEventListener('click', () => invoke('open_dashboard_cmd', {}).catch(e => setMsg(msg, String(e), 'err')));
$('#refresh').addEventListener('click', refresh);
$('#settings').addEventListener('click', () => invoke('open_settings', {}));

listen('panel-refresh', refresh);
listen('state-changed', () => invoke('get_state').then(st => sections(st.sections)));
// Keep the lists fresh while the panel is in front.
setInterval(() => { if (!document.hidden && tab !== 'knowledge') load(tab, true); }, 60000);
refresh().catch(e => setMsg(msg, String(e), 'err'));
