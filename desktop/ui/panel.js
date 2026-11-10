'use strict';
// The Rook panel: the hub's pages, and what waits on you. The hub has no
// Work, Knowledge or Sessions lists here: holds, the hub's pages and the house
// agent only (castling-attention: the agent opens a page). Everything opens
// in the dashboard window; nothing is decided here.
const listEl = $('#list'), msg = $('#msg');
let loading = 0;

const KIND = { hold: 'Held call', job: 'Job call', card: 'Huddle card', ask: 'Question', house: 'House agent', trust: 'Trust change' };
const EMPTY = 'Nothing is waiting for you.';

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text) e.textContent = text;
  return e;
}

function row(item) {
  const li = el('li', 'pn-item');
  li.tabIndex = 0;
  li.setAttribute('role', 'link');
  const title = el('div', 'pn-title', item.title);
  const meta = el('div', 'pn-meta');
  const body = el('div', 'pn-body');
  meta.textContent = [KIND[item.kind] || item.kind, item.who, item.age && item.age + ' ago', item.name].filter(Boolean).join(' · ');
  body.textContent = item.text;
  li.append(title, meta);
  if (body.textContent) li.append(body);
  const open = () => invoke('open_dashboard_cmd', { path: item.link }).catch(e => setMsg(msg, String(e), 'err'));
  li.addEventListener('click', open);
  li.addEventListener('keydown', e => { if (e.key === 'Enter') open(); });
  return li;
}

async function load(quiet) {
  const mine = ++loading;
  if (!quiet) setMsg(msg, 'Loading…');
  try {
    const items = await invoke('panel_data', { kind: 'waiting' });
    $('#n-waiting').textContent = items.length ? String(items.length) : '';
    if (mine !== loading) return;
    listEl.replaceChildren(...items.map(row));
    setMsg(msg, items.length ? '' : EMPTY);
  } catch (e) {
    if (mine === loading) { listEl.replaceChildren(); setMsg(msg, String(e), 'err'); }
  }
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

async function refresh() {
  const st = await invoke('get_state');
  $('#hub-line').textContent = st.signed_in ? st.hub + (st.principal ? ' · ' + st.principal : '') : 'Not signed in. Open Settings to connect to your hub.';
  sections(st.sections);
  $('#ask-page').hidden = !st.signed_in;
  if (!st.signed_in) { listEl.replaceChildren(); setMsg(msg, 'Sign in to your hub first (Settings).', 'err'); return; }
  load();
}

$('#ask').addEventListener('click', () => invoke('show_window', { name: 'overlay' }));
$('#ask-page').addEventListener('click', () => invoke('ask_on_page_cmd').catch(e => setMsg(msg, String(e), 'err')));
$('#open-dashboard').addEventListener('click', () => invoke('open_dashboard_cmd', {}).catch(e => setMsg(msg, String(e), 'err')));
$('#refresh').addEventListener('click', refresh);
$('#settings').addEventListener('click', () => invoke('open_settings', {}));

listen('panel-refresh', refresh);
listen('state-changed', () => invoke('get_state').then(st => sections(st.sections)));
// Keep the lists fresh while the panel is in front.
setInterval(() => { if (!document.hidden) load(true); }, 60000);
refresh().catch(e => setMsg(msg, String(e), 'err'));
