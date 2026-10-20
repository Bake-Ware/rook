// Overview: one page that ties sessions, chat rooms, knowledge and tasks
// together. What is running now, what waits on you, recent page changes and
// open work by project, each row linked to the things it relates to (a
// task's room and session, a session's task, a room's task).
//
// Sources (widgets.js band()): /api/band/workers, /api/chat/rooms,
// /account/work/sessions?cached=1 and /account/ui/api?view=overview (the
// knowledge half, joined on the hub). See docs/web/overview.md.
const search = new URL(import.meta.url).search;
const [{h, el, ago}, W] = await Promise.all([
  import('/account/bands/assets/manage.js' + search), import('/account/ui/assets/widgets.js' + search)]);
const {band, link, a, shortName, empty, failed, ONLINE_SECS} = W;
const REFRESH_MS = 20000;
const now = () => Date.now() / 1000;
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));

// Everything joined: which room, session and task belong together.
function join(d) {
  const k = d.knowledge.value?.knowledge || null;
  const sessions = d.sessions.value?.sessions || [];
  const rooms = d.rooms.value || [];
  const roomById = new Map(rooms.map(r => [r.room, r]));
  const tasks = new Map();   // id and slug -> {task, project}
  for (const p of k?.projects || []) for (const key of ['in_progress', 'blocked', 'todo', 'recently_done'])
    for (const t of p[key] || []) { const v = {task: t, project: p.project, key}; tasks.set(t.id, v); if (t.slug) tasks.set(t.slug, v); }
  const sessionsByTask = new Map(), tasksByRoom = new Map();
  for (const s of sessions) {
    const t = s.links?.task && tasks.get(s.links.task);
    if (t) (sessionsByTask.get(t.task.id) || sessionsByTask.set(t.task.id, []).get(t.task.id)).push(s);
  }
  const roomsByTask = new Map();
  for (const [tid, links] of Object.entries(k?.links || {})) {
    for (const l of links) if ((l.kind === 'chat' || l.kind === 'console') && roomById.has(l.ref)) {
      (roomsByTask.get(tid) || roomsByTask.set(tid, []).get(tid)).push(roomById.get(l.ref));
      const t = tasks.get(tid); if (t) (tasksByRoom.get(l.ref) || tasksByRoom.set(l.ref, []).get(l.ref)).push(t.task);
    }
  }
  for (const s of sessions) for (const id of [s.links?.chat_room, s.links?.console_room]) {
    const t = s.links?.task && tasks.get(s.links.task);
    if (id && t && roomById.has(id)) {
      const list = roomsByTask.get(t.task.id) || roomsByTask.set(t.task.id, []).get(t.task.id);
      if (!list.includes(roomById.get(id))) list.push(roomById.get(id));
      const back = tasksByRoom.get(id) || tasksByRoom.set(id, []).get(id);
      if (!back.includes(t.task)) back.push(t.task);
    }
  }
  return {k, sessions, rooms, roomById, tasks, sessionsByTask, roomsByTask, tasksByRoom};
}

// Small link chips: "room: deploy", "session: claude on build", "task: …".
const chip = (href, kind, text, title) => h('a', {class: 'ov-chip', href, title: title || text}, h('i', {text: kind}), h('span', {text}));
const roomChip = r => chip(link.room(r.room), 'room', r.title || r.name || r.room, r.name ? (r.title ? r.title + ' · ' : '') + r.name : undefined);
const sessionChip = s => chip(link.session(s), s.state === 'idle' ? 'waiting' : s.state === 'live' ? 'live' : 'session', (s.title || s.agent) + ' · ' + (s.worker || ''), s.agent + ' on ' + s.worker);
const taskChip = t => chip(link.task(t), 'task', t.title);

function card(title, sub, ...body) {
  return h('section', {class: 'ov-card'}, h('header', null, h('h2', {text: title}), sub ? h('span', {class: 'ov-sub', text: sub}) : null), ...body);
}
function tile(value, label, href, tone) {
  return h('a', {class: 'ov-tile' + (tone ? ' ' + tone : ''), href}, h('b', {text: value}), h('span', {text: label}));
}

function waiting(j) {
  const items = [];
  for (const s of j.sessions.filter(s => s.state === 'idle'))
    items.push({ts: s.updated, row: h('li', null, h('span', {class: 'ov-why', text: 'Waiting'}),
      a(link.session(s), s.title || s.agent), h('small', {text: s.agent + ' on ' + s.worker + ' · ' + ago(s.updated)}),
      h('div', {class: 'ov-chips'}, s.links?.task && j.tasks.get(s.links.task) ? taskChip(j.tasks.get(s.links.task).task) : null))});
  for (const r of j.rooms.filter(r => r.member && r.unread > 0))
    items.push({ts: now() - r.last_activity_age_secs, row: h('li', null, h('span', {class: 'ov-why', text: plural(r.unread, 'unread message')}),
      a(link.room(r.room), r.title || r.name || r.room), h('small', {text: (r.last_sender ? shortName(r.last_sender) + ': ' : '') + (r.last_text || '')}),
      h('div', {class: 'ov-chips'}, (j.tasksByRoom.get(r.room) || []).map(taskChip)))});
  for (const p of j.k?.projects || []) for (const t of p.blocked)
    items.push({ts: t.updated, row: h('li', null, h('span', {class: 'ov-why bad', text: 'Blocked'}),
      a(link.task(t), t.title), h('small', {text: t.blocked_reason || (t.waiting_on ? 'Waiting on ' + t.waiting_on.join(', ') : p.project.title)}),
      h('div', {class: 'ov-chips'}, (j.roomsByTask.get(t.id) || []).map(roomChip), (j.sessionsByTask.get(t.id) || []).map(sessionChip)))});
  for (const f of j.k?.decisions || []) {
    const t = j.tasks.get(f.record) || j.tasks.get(f.slug);
    const target = t ? link.task(t.task) : (f.slug ? '#work?' + new URLSearchParams({t: f.slug}) : '#work');
    items.push({ts: f.created, row: h('li', null, h('span', {class: 'ov-why', text: 'Your call'}),
      a(target, t ? t.task.title : (f.slug || 'task')), h('small', {text: f.what + (f.actor ? ' (' + shortName(f.actor) + ')' : '')}))});
  }
  items.sort((x, y) => (y.ts || 0) - (x.ts || 0));
  return card('Waiting on you', items.length ? plural(items.length, 'item') : '',
    items.length ? h('ul', {class: 'ov-list'}, items.slice(0, 12).map(i => i.row)) : empty('Nothing needs you.'));
}

function active(j, d) {
  const live = j.sessions.filter(s => s.state === 'live' || s.state === 'idle');
  const doing = [];
  for (const p of j.k?.projects || []) for (const t of p.in_progress) doing.push({t, p});
  const rooms = j.rooms.filter(r => r.last_activity_age_secs < 3600);
  const rows = [];
  for (const s of live) rows.push(h('li', null, h('span', {class: 'ov-state ' + s.state, text: s.state === 'idle' ? 'waiting' : 'working'}),
    a(link.session(s), s.title || s.agent), h('small', {text: s.agent + ' on ' + s.worker + ' · ' + ago(s.updated)}),
    h('div', {class: 'ov-chips'}, s.links?.task && j.tasks.get(s.links.task) ? taskChip(j.tasks.get(s.links.task).task) : null,
      [s.links?.chat_room, s.links?.console_room].filter(id => id && j.roomById.has(id)).map(id => roomChip(j.roomById.get(id))))));
  for (const {t, p} of doing.slice(0, 8)) rows.push(h('li', null, h('span', {class: 'ov-state task', text: 'task'}),
    a(link.task(t), t.title), h('small', {text: (t.claimants?.length ? t.claimants.map(shortName).join(', ') : 'unclaimed') + ' · ' + p.project.title}),
    h('div', {class: 'ov-chips'}, (j.roomsByTask.get(t.id) || []).map(roomChip), (j.sessionsByTask.get(t.id) || []).map(sessionChip))));
  for (const r of rooms.slice(0, 6)) rows.push(h('li', null, h('span', {class: 'ov-state room', text: 'chat'}),
    a(link.room(r.room), r.title || r.name || r.room), h('small', {text: (r.last_sender ? shortName(r.last_sender) + ' · ' : '') + ago(now() - r.last_activity_age_secs)}),
    h('div', {class: 'ov-chips'}, (j.tasksByRoom.get(r.room) || []).map(taskChip))));
  const notes = [d.sessions.error && 'sessions: ' + d.sessions.error, d.rooms.error && 'chat: ' + d.rooms.error].filter(Boolean);
  return card('Active now', '', rows.length ? h('ul', {class: 'ov-list'}, rows) : empty('Nothing running.'),
    notes.length ? failed('Could not load ' + notes.join('; ')) : null);
}

function recent(j, d) {
  if (!j.k) return card('Knowledge changes', '', empty(knowledgeNote(d)));
  return card('Knowledge changes', '', j.k.recent.length ? h('ul', {class: 'ov-list compact'}, j.k.recent.map(p => h('li', {class: 'ov-plain'},
    a(link.page(p), p.title), h('small', {text: shortName(p.actor) + ' ' + verb(p.action) + ' · ' + ago(p.ts)}),
    p.verification === 'disputed' ? h('span', {class: 'ov-why bad', text: 'disputed'}) : null))) : empty('No pages yet.'),
    a('#knowledge', 'All pages', 'ov-more'));
}
const verb = a => ({created: 'created', updated: 'edited', linked: 'linked', reviewed: 'reviewed', retracted: 'retracted'}[a] || a || 'changed');
function knowledgeNote(d) {
  if (d.knowledge.error) return 'Knowledge could not be loaded: ' + d.knowledge.error;
  return d.knowledge.value?.reason === 'off' ? 'Knowledge and tasks are off on this hub (Settings > Knowledge).' : 'Knowledge and tasks are for the operator account.';
}

function projects(j, d) {
  if (!j.k) return card('Open tasks', '', empty(knowledgeNote(d)));
  const open = j.k.projects.filter(p => p.counts.in_progress + p.counts.blocked + p.counts.todo > 0);
  if (!open.length) return card('Open tasks', '', empty('No open tasks.'));
  const taskRow = (t, key) => h('li', null, h('span', {class: 'ov-state ' + key, text: {in_progress: 'doing', blocked: 'blocked', todo: 'to do'}[key]}),
    a(link.task(t), t.title),
    h('div', {class: 'ov-chips'}, (j.roomsByTask.get(t.id) || []).map(roomChip), (j.sessionsByTask.get(t.id) || []).map(sessionChip),
      t.claimants?.length ? h('span', {class: 'ov-who', text: t.claimants.map(shortName).join(', ')}) : null));
  return card('Open tasks', plural(open.length, 'project'), h('div', {class: 'ov-projects'}, open.map(p => {
    const c = p.counts;
    const more = c.in_progress + c.blocked + c.todo - (p.in_progress.length + p.blocked.length + p.todo.length);
    return h('article', {class: 'ov-project'},
      h('header', null, a(link.project(p.project), p.project.title, 'ov-ptitle'),
        h('span', {class: 'ov-counts'}, [c.in_progress && plural(c.in_progress, 'doing', 'doing'), c.blocked && c.blocked + ' blocked', c.todo && c.todo + ' to do']
          .filter(Boolean).map(x => h('span', {text: x})))),
      h('ul', {class: 'ov-list compact'}, p.blocked.map(t => taskRow(t, 'blocked')), p.in_progress.map(t => taskRow(t, 'in_progress')), p.todo.slice(0, 3).map(t => taskRow(t, 'todo'))),
      more > 0 ? a(link.project(p.project), plural(more, 'more task'), 'ov-more') : null);
  })));
}

function summary(j, d) {
  const workers = d.workers.value || [];
  const up = workers.filter(w => (w.last_seen_age_secs ?? 1e9) < ONLINE_SECS).length;
  const live = j.sessions.filter(s => s.state === 'live').length, idle = j.sessions.filter(s => s.state === 'idle').length;
  let doing = 0, blocked = 0;
  for (const p of j.k?.projects || []) { doing += p.counts.in_progress; blocked += p.counts.blocked; }
  const unread = j.rooms.reduce((n, r) => n + (r.member ? r.unread || 0 : 0), 0);
  return h('div', {class: 'ov-tiles'},
    tile(d.workers.error ? '—' : up + '/' + workers.length, 'workers', '#workers', up < workers.length ? 'warn' : ''),
    tile(String(live + idle), idle ? 'sessions · ' + idle + ' wait' : 'sessions', '#sessions', idle ? 'warn' : ''),
    tile(j.k ? String(doing) : '—', blocked ? 'active · ' + blocked + ' blocked' : 'active tasks', '#work', blocked ? 'bad' : ''),
    tile(String(unread), 'unread', '#huddles', unread ? 'warn' : ''));
}

export async function mountOverview(root) {
  root.classList.add('ov');
  root.replaceChildren(el('p', 'Loading…', 'empty'));
  let active_ = false, timer = null, drawing = null;
  async function draw(fresh) {
    if (drawing) return drawing;
    drawing = (async () => {
      try {
        const d = await band(fresh), j = join(d);
        root.replaceChildren(summary(j, d), h('div', {class: 'ov-grid'},
          h('div', {class: 'ov-main'}, waiting(j), projects(j, d)),
          h('div', {class: 'ov-side'}, active(j, d), recent(j, d))),
          h('p', {class: 'ov-foot', text: 'Updated ' + new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}));
      } catch (e) { root.replaceChildren(failed(e.message)); }
      finally { drawing = null; }
    })();
    return drawing;
  }
  const tick = () => { if (active_ && !document.hidden) draw(true); };
  return {
    activate() { if (active_) return; active_ = true; draw(true); timer = setInterval(tick, REFRESH_MS); },
    deactivate() { active_ = false; clearInterval(timer); },
  };
}
