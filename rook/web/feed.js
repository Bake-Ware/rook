// Feed: what came in for you, newest first, in one stream. The phone's quiet
// feed (other apps' notifications as the phone summarized them), alerts agents
// sent, decisions asked and answered, and tasks finished or blocked. A phone
// row's Sort menu says how every phone sorts that app from now on (ring me,
// tell me, just list it, hide it); the hub pushes it to the phones.
//
// Source: /account/attention/api (rook/hub/plugins/attention/web.py). See
// docs/design/attention.md, "The Feed" and "Sorting".
const search = new URL(import.meta.url).search;
const {h, ago, btn, useCss} = await import('/account/bands/assets/manage.js' + search);
const API = '/account/attention/api';
const REFRESH_MS = 30000;
const FILTERS = [['', 'All'], ['important', 'Important'], ['phone', 'Phone'], ['alert', 'Alerts'],
  ['decision', 'Decisions'], ['work', 'Work']];
const SOURCE = {alert: 'Rook', decision: 'Decision', work: 'Work'};
// A run of rows from one app shows this many before "+N more".
const RUN_SHOWN = 2;

function css() {
  if (document.querySelector('link[href*="feed.css"]')) return;
  document.head.append(h('link', {rel: 'stylesheet', href: '/account/attention/assets/feed.css' + search}));
}

const hhmm = t => new Date(t * 1000).toLocaleTimeString(undefined, {hour: 'numeric', minute: '2-digit'});
function dayName(t) {
  const d = new Date(t * 1000), today = new Date();
  const start = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString(undefined, {weekday: 'long', month: 'short', day: 'numeric'});
}
// The phone writes "<App>: <category> — <title> · <words>": the row shows the app on its own.
function text(r) {
  if (r.source !== 'phone') return r.line;
  const head = (r.app || '') + ': ' + (r.category || '') + ' — ';
  return r.line.startsWith(head) ? r.line.slice(head.length) : r.line;
}

export async function mountFeed(root) {
  useCss(); css();
  const s = {csrf: '', filter: '', rows: [], more: false, before: null, data: null, timer: null,
    menu: null, open: new Set(), busy: false};
  root.className = 'fd';

  async function get(before) {
    const q = new URLSearchParams();
    if (s.filter === 'important') q.set('important', '1');
    else if (s.filter) q.set('source', s.filter);
    if (before) q.set('before', String(before));
    const r = await fetch(API + '?' + q, {headers: {Accept: 'application/json'}, credentials: 'same-origin'});
    const d = await r.json().catch(() => ({error: 'The Feed answered with something unreadable.'}));
    if (!r.ok) throw Error(d.error || ('HTTP ' + r.status));
    s.csrf = d.csrf || s.csrf;
    return d;
  }
  async function post(body) {
    const r = await fetch(API, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', Accept: 'application/json'},
      body: JSON.stringify({...body, csrf: s.csrf})});
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw Error(d.error || ('HTTP ' + r.status));
    return d;
  }

  async function load() {
    try {
      const d = await get();
      s.data = d; s.rows = d.rows; s.more = d.more; s.before = d.before; s.error = '';
    } catch (e) { s.error = e.message; }
    draw();
  }
  async function older() {
    if (!s.before) return;
    try {
      const d = await get(s.before);
      s.rows = s.rows.concat(d.rows.filter(r => !s.rows.some(x => x.id === r.id)));
      s.more = d.more; s.before = d.before;
    } catch (e) { s.error = e.message; }
    draw();
  }
  async function sort(r, priority) {
    s.menu = null;
    try {
      if (priority === 'default') {
        const key = (r.pkg || r.app || '').toLowerCase();
        const row = (s.data?.sorting || []).find(x => (x.pkg || x.app || '').toLowerCase() === key
          || (!x.pkg && (x.app || '').toLowerCase() === (r.app || '').toLowerCase()));
        if (row) await post({action: 'unsort', id: row.id});
      } else await post({action: 'sort', app: r.app, pkg: r.pkg || '', priority});
    } catch (e) { s.error = e.message; }
    await load();
  }

  function sortMenu(r) {
    const choices = s.data?.choices || {};
    return h('div', {class: 'fd-menu', role: 'menu'},
      h('div', {class: 'fd-menu-head', text: 'Every phone, from now on, for ' + r.app + ':'}),
      ...Object.entries(choices).map(([p, label]) => h('button', {type: 'button', role: 'menuitemradio',
        'aria-checked': r.sorted === p ? 'true' : 'false', class: 'fd-choice' + (r.sorted === p ? ' on' : ''),
        text: label, onclick: () => sort(r, p)})),
      r.sorted ? h('button', {type: 'button', role: 'menuitem', class: 'fd-choice dim',
        text: "The phone's own rules", onclick: () => sort(r, 'default')}) : null);
  }

  function row(r) {
    const tag = r.source === 'phone' ? (r.app || 'Phone') : SOURCE[r.source] || r.source;
    const tone = r.priority === 'urgent' ? ' urgent' : r.priority === 'family' ? ' family' : '';
    const sortable = r.source === 'phone' && r.app;
    const key = r.id;
    const out = h('li', {class: 'fd-row' + tone},
      h('time', {text: hhmm(r.at), title: new Date(r.at * 1000).toLocaleString()}),
      h('span', {class: 'fd-tag fd-' + r.source, text: tag}),
      h('span', {class: 'fd-text'},
        r.source === 'work' && r.slug ? h('a', {href: '#work?t=' + encodeURIComponent(r.slug), text: r.line})
          : document.createTextNode(text(r)),
        r.detail ? h('span', {class: 'fd-detail', text: ' · ' + r.detail}) : null,
        r.sorted ? h('span', {class: 'fd-sorted', text: ' · ' + (s.data?.choices?.[r.sorted] || r.sorted).toLowerCase()}) : null),
      sortable ? h('button', {type: 'button', class: 'fd-sort', text: 'Sort', 'aria-haspopup': 'menu',
        'aria-expanded': s.menu === key ? 'true' : 'false', title: 'How the phone sorts ' + r.app,
        onclick: e => { e.stopPropagation(); s.menu = s.menu === key ? null : key; draw(); }}) : null);
    if (s.menu === key) out.append(sortMenu(r));
    return out;
  }

  // Rows by day; a run of one app's rows folds after RUN_SHOWN.
  function list() {
    const ul = h('ul', {class: 'fd-list'});
    let day = '', i = 0;
    const rows = s.rows;
    while (i < rows.length) {
      const r = rows[i], d = dayName(r.at);
      if (d !== day) { day = d; ul.append(h('li', {class: 'fd-day', text: d})); }
      let j = i + 1;
      if (r.source === 'phone')
        while (j < rows.length && rows[j].source === 'phone' && rows[j].app === r.app && dayName(rows[j].at) === d) j++;
      const run = rows.slice(i, j), runKey = r.id;
      const shown = s.open.has(runKey) ? run : run.slice(0, RUN_SHOWN);
      shown.forEach(x => ul.append(row(x)));
      if (run.length > shown.length) ul.append(h('li', {class: 'fd-fold'}, h('button', {type: 'button',
        class: 'fd-more', text: 'Show ' + (run.length - shown.length) + ' more from ' + r.app,
        onclick: () => { s.open.add(runKey); draw(); }})));
      i = j;
    }
    return ul;
  }

  function sortingPanel() {
    const rows = s.data?.sorting || [], fam = s.data?.family || [];
    return h('details', {class: 'fd-sorting'},
      h('summary', {text: 'How you sort apps (' + rows.length + ')'}),
      rows.length ? h('ul', null, ...rows.map(x => h('li', null,
        h('b', {text: x.app}), h('span', {text: ' ' + x.choice.toLowerCase()}),
        h('span', {class: 'fd-detail', text: ' · ' + x.by + ', ' + ago(x.at)}),
        btn('Use the phone’s own rules', 'fd-undo', async () => {
          try { await post({action: 'unsort', id: x.id}); } catch (e) { s.error = e.message; }
          await load();
        }))))
        : h('p', {class: 'fd-detail', text: 'Use Sort on a phone row, or on the phone’s Attention tab.'}),
      h('p', {class: 'fd-detail'}, document.createTextNode('Family: ' + (fam.length ? fam.join(', ') : 'none yet') + ' ('),
        h('a', {href: '#settings?view=plugin&target=attention', text: 'Settings > Attention'}), document.createTextNode(')')));
  }

  function draw() {
    const d = s.data;
    const filters = h('div', {class: 'fd-filters', role: 'tablist'}, ...FILTERS.map(([id, label]) =>
      h('button', {type: 'button', role: 'tab', 'aria-selected': s.filter === id ? 'true' : 'false',
        class: 'fd-filter' + (s.filter === id ? ' on' : ''), text: label,
        onclick: () => { s.filter = id; s.open.clear(); s.menu = null; load(); }})));
    const top = [];
    if (d?.waiting?.count) top.push(h('a', {class: 'fd-waiting', href: '#approvals'},
      h('b', {text: d.waiting.count === 1 ? '1 decision waits on you' : d.waiting.count + ' decisions wait on you'}),
      h('span', {text: d.waiting.items.map(w => w.title).join(' · ')})));
    if (d && !d.phones.length) top.push(h('p', {class: 'fd-note'}, document.createTextNode('No phone is set up: '),
      h('a', {href: '#settings?view=plugin&target=attention', text: 'Settings > Attention > Phones'}), document.createTextNode('.')));
    if (s.error) top.push(h('p', {class: 'fd-note bad', text: s.error}));
    const body = s.rows.length ? list() : h('p', {class: 'fd-empty', text: d ? 'Nothing here yet.' : 'Loading…'});
    root.replaceChildren(filters, ...top, body,
      s.more ? h('button', {type: 'button', class: 'mg-btn fd-older', text: 'Older', onclick: older}) : '',
      d ? sortingPanel() : '');
  }

  document.addEventListener('click', e => {
    if (s.menu && !e.target.closest('.fd-menu')) { s.menu = null; draw(); }
  });
  draw();
  return {
    activate() { load(); clearInterval(s.timer); s.timer = setInterval(() => { if (!s.menu && s.rows.length <= 60) load(); }, REFRESH_MS); },
    deactivate() { clearInterval(s.timer); s.timer = null; },
  };
}
