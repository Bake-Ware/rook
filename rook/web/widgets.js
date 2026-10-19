// Start page widgets: a small registry and the built-in kinds.
//
//   register(type, {title, description, defaults, render(body, widget, ctx), editor?(form, widget)})
//
// `render` fills `body` (a <div>) for one widget; `ctx` has `band()` (the
// shared band data below), `post(body)` (the ui account API) and
// `refreshEvery(ms, fn)`. Everything a widget shows is set as text, never as
// HTML. Plugins add a kind by declaring WIDGETS on their hub plugin with a
// module URL; the start page imports that module, which calls register()
// (docs/web/start-page.md). Built-in types have no dot; plugin types do.
const search = new URL(import.meta.url).search;
const {h, el, ago} = await import('/account/bands/assets/manage.js' + search);

const registry = new Map();
export function register(type, spec) {
  if (typeof type !== 'string' || !type || typeof spec?.render !== 'function') throw Error('register(type, {render})');
  registry.set(type, {title: type, description: '', defaults: {}, ...spec});
}
export const widgetTypes = () => [...registry.entries()].map(([type, spec]) => ({type, ...spec}));
export const widgetType = type => registry.get(type);
window.RookWidgets = {register};   // for plugin scripts that are not modules

// -- shared band data -----------------------------------------------------------
// One fetch of each source per 15 s, however many widgets (and the Overview)
// read it. Each source fails on its own: a widget shows what it has.
const TTL = 15000;
const cache = new Map();
async function json(url) {
  const r = await fetch(url, {headers: {Accept: 'application/json'}, cache: 'no-store'});
  const body = await r.json().catch(() => null);
  if (!r.ok) throw Error(body?.error || 'HTTP ' + r.status);
  return body;
}
function source(key, load) {
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < TTL) return hit.p;
  const p = load(); cache.set(key, {at: Date.now(), p});
  p.catch(() => cache.delete(key));
  return p;
}
export const ONLINE_SECS = 90;
export function band(fresh = false) {
  if (fresh) cache.clear();
  const settle = p => p.then(value => ({value}), error => ({error: error.message}));
  // The ui API first: after a password sign-in its answer is what turns the
  // dashboard session into the account session the sessions list needs.
  const knowledge = settle(source('knowledge', () => json('/account/ui/api?view=overview')));
  return knowledge.then(k => Promise.all([
    settle(source('workers', () => json('/api/band/workers'))),
    settle(source('rooms', () => json('/api/chat/rooms').then(d => d.rooms || []))),
    settle(source('sessions', () => json('/account/work/sessions?cached=1&limit=200'))),
  ]).then(([workers, rooms, sessions]) => ({workers, rooms, sessions, knowledge: k})));
}

// -- links into the dashboard --------------------------------------------------------
export const link = {
  task: t => '#work?' + new URLSearchParams({t: t.slug || t.id, ...(t.band ? {b: t.band} : {})}),
  project: p => '#work?' + new URLSearchParams({p: p.slug || p.id, ...(p.band ? {b: p.band} : {})}),
  page: p => '#knowledge?' + new URLSearchParams({p: p.slug || p.id, ...(p.band ? {b: p.band} : {})}),
  room: id => '#huddles?' + new URLSearchParams({room: id}),
  session: s => '#sessions?' + new URLSearchParams({key: s.key}),
};
export const shortName = id => {
  id = String(id || '?');
  // Knowledge actors are "<token>.<client>.<host>": "claude on laptop".
  const compound = /^([^.:]+)\.[^.]+\.(.+)$/.exec(id);
  if (compound) return compound[1] + ' on ' + compound[2];
  const n = id.includes(':') ? id.split(':', 2)[1] : id; return n.replace(/^(hermes|claude)_/, '');
};
export function a(href, text, cls) { return h('a', {href, text, class: cls}); }
export function empty(text) { return el('p', text, 'w-empty'); }
export function failed(text) { return el('p', text, 'w-empty w-bad'); }
const SAFE_URL = /^(https?:\/\/|#[a-z])/i;
export const safeUrl = u => (typeof u === 'string' && SAFE_URL.test(u.trim())) ? u.trim() : null;

// -- built-in kinds ----------------------------------------------------------------------
register('bookmarks', {
  title: 'Bookmarks', description: 'Groups of links to the apps and sites you use.',
  defaults: {groups: [{name: '', links: []}]},
  render(body, w) {
    const groups = (w.options?.groups || []).filter(g => (g.links || []).length);
    if (!groups.length) return body.append(empty('No links yet. Choose Edit page, then the pencil on this widget.'));
    for (const g of groups) {
      if (g.name) body.append(el('h4', g.name, 'w-group'));
      body.append(h('ul', {class: 'w-links'}, g.links.map(l => {
        const url = safeUrl(l.url); if (!url) return null;
        const external = !url.startsWith('#');
        let host = '';
        if (external) { try { host = new URL(url).host; } catch {} }
        return h('li', null, h('a', {href: url, ...(external ? {target: '_blank', rel: 'noopener noreferrer'} : {})},
          h('b', {text: l.label}), l.note ? h('small', {text: l.note}) : (host ? h('small', {text: host}) : null)));
      })));
    }
  },
});

register('feed', {
  title: 'News feed', description: 'Headlines from an RSS or Atom feed, fetched by your hub.',
  defaults: {url: '', limit: 8},
  async render(body, w, ctx) {
    const o = w.options || {};
    if (!o.url) return body.append(empty('No feed address yet.'));
    body.append(empty('Loading…'));
    let out;
    try { out = await ctx.post({action: 'feed', url: o.url, limit: o.limit || 8}); }
    catch (e) { body.replaceChildren(failed(e.message)); return; }
    if (!out.ok) { body.replaceChildren(failed(out.error)); return; }
    const f = out.feed;
    body.replaceChildren();
    if (!f.items.length) return body.append(empty('The feed has no items.'));
    body.append(h('ul', {class: 'w-feed'}, f.items.map(i => h('li', null,
      safeUrl(i.link) && !i.link.startsWith('#') ? h('a', {href: i.link, target: '_blank', rel: 'noopener noreferrer', text: i.title}) : h('span', {text: i.title}),
      i.published ? h('time', {text: ago(i.published, '')}) : null,
      o.summaries && i.summary ? h('p', {text: i.summary}) : null))));
    if (f.title && !w.title) ctx.setTitle(f.title);
  },
});

register('note', {
  title: 'Note', description: 'A few lines of your own text.',
  defaults: {text: ''},
  render(body, w) {
    const text = w.options?.text || '';
    if (!text) return body.append(empty('Empty note.'));
    for (const para of text.split(/\n{2,}/)) body.append(el('p', para, 'w-note'));
  },
});

function count(n, one, many) { return n + ' ' + (n === 1 ? one : many); }
const limitOf = w => Math.max(1, Math.min(30, +(w.options?.limit) || 6));

register('workers', {
  title: 'Workers', description: 'How many of your machines are online, and which are not.',
  async render(body, w, ctx) {
    const {workers} = await ctx.band();
    if (workers.error) return body.append(failed('Workers: ' + workers.error));
    const all = workers.value || [], up = all.filter(x => (x.last_seen_age_secs ?? 1e9) < ONLINE_SECS);
    body.append(h('div', {class: 'w-stat'}, h('b', {text: up.length + '/' + all.length}), h('span', {text: 'online'})));
    const down = all.filter(x => !up.includes(x)).slice(0, limitOf(w));
    if (down.length) body.append(el('h4', 'Not answering', 'w-group'),
      h('ul', {class: 'w-rows'}, down.map(x => h('li', null, h('span', {class: 'w-dot off'}), h('b', {text: x.name || x.worker_id}),
        h('small', {text: 'seen ' + ago(Date.now() / 1000 - (x.last_seen_age_secs || 0))})))));
    else if (all.length) body.append(empty('Every worker is answering.'));
    body.append(a('#workers', 'All workers', 'w-more'));
  },
});

register('tasks', {
  title: 'Open tasks', description: 'Tasks in progress or blocked, across your projects.',
  async render(body, w, ctx) {
    const {knowledge} = await ctx.band();
    if (knowledge.error) return body.append(failed('Tasks: ' + knowledge.error));
    const k = knowledge.value?.knowledge;
    if (!k) return body.append(empty(knowledge.value?.reason === 'off' ? 'Knowledge and tasks are off on this hub.' : 'Tasks are for the operator account.'));
    const rows = [];
    for (const p of k.projects) for (const key of ['blocked', 'in_progress']) for (const t of p[key]) rows.push({t, p, key});
    if (!rows.length) return body.append(empty('Nothing in progress.'));
    body.append(h('ul', {class: 'w-rows'}, rows.slice(0, limitOf(w)).map(({t, p, key}) => h('li', null,
      h('span', {class: 'w-state ' + key, text: key === 'blocked' ? 'blocked' : 'doing'}),
      a(link.task(t), t.title), h('small', {text: p.project.title})))));
    body.append(a('#work', 'All work', 'w-more'));
  },
});

register('rooms', {
  title: 'Chat rooms', description: 'The busiest rooms, with your unread messages.',
  async render(body, w, ctx) {
    const {rooms} = await ctx.band();
    if (rooms.error) return body.append(failed('Rooms: ' + rooms.error));
    const list = (rooms.value || []).slice().sort((x, y) => (y.unread || 0) - (x.unread || 0) || x.last_activity_age_secs - y.last_activity_age_secs);
    if (!list.length) return body.append(empty('No rooms yet.'));
    body.append(h('ul', {class: 'w-rows'}, list.slice(0, limitOf(w)).map(r => h('li', null,
      a(link.room(r.room), r.title || r.room), r.unread ? h('span', {class: 'w-badge', text: String(r.unread)}) : null,
      h('small', {text: (r.last_sender ? shortName(r.last_sender) + ' · ' : '') + ago(Date.now() / 1000 - r.last_activity_age_secs)})))));
    body.append(a('#huddles', 'Open huddles', 'w-more'));
  },
});

register('knowledge', {
  title: 'Recently changed pages', description: 'Knowledge pages that changed lately, and who changed them.',
  async render(body, w, ctx) {
    const {knowledge} = await ctx.band();
    if (knowledge.error) return body.append(failed('Knowledge: ' + knowledge.error));
    const k = knowledge.value?.knowledge;
    if (!k) return body.append(empty('Knowledge is for the operator account.'));
    if (!k.recent.length) return body.append(empty('No pages yet.'));
    body.append(h('ul', {class: 'w-rows'}, k.recent.slice(0, limitOf(w)).map(p => h('li', null,
      a(link.page(p), p.title), h('small', {text: shortName(p.actor) + ' ' + (p.action || 'changed') + ' · ' + ago(p.ts)})))));
    body.append(a('#knowledge', 'All knowledge', 'w-more'));
  },
});

register('sessions', {
  title: 'Agent sessions', description: 'Agent sessions running now, and those waiting for you.',
  async render(body, w, ctx) {
    const {sessions} = await ctx.band();
    if (sessions.error) return body.append(failed('Sessions: ' + sessions.error));
    const list = (sessions.value?.sessions || []).filter(s => s.state === 'live' || s.state === 'idle');
    if (!list.length) return body.append(empty('No session is running.'));
    body.append(h('ul', {class: 'w-rows'}, list.slice(0, limitOf(w)).map(s => h('li', null,
      h('span', {class: 'w-state ' + s.state, text: s.state === 'idle' ? 'waiting' : 'working'}),
      a(link.session(s), s.title || s.agent), h('small', {text: s.agent + ' on ' + (s.worker || '?')})))));
    body.append(a('#sessions', 'All sessions', 'w-more'));
  },
});

export {count};
