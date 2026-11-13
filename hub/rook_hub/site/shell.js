/* Rook hub site: the shell (P1, P2, P6, P10).
   The login, the sidebar (a Pinned strip, then the segments with the pages
   installed in them), pins kept at the hub, the page header's pin control,
   a row menu, a toast line, and the background rook scene. A page is an
   ES module whose default export is a Vue component; the hub says which
   pages exist and where their modules are (GET /api/session). */
const { createApp, reactive, markRaw } = Vue;

export const store = reactive({ user: null, nav: [], pins: [], toast: '', ready: false,
  view: location.pathname + location.search });   // the page's current view, so the strip can follow it (P6)

/* ---------------- Talking to the hub ---------------- */
export async function api(method, path, body) {
  const response = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && path !== '/api/login') store.user = null;   // show the login
  if (!response.ok) {
    const error = new Error(data.message ? data.message + (data.next ? ' ' + data.next : '') : response.statusText);
    error.data = data;
    throw error;
  }
  return data;
}

let toastTimer;
export function toast(message) {
  store.toast = message;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { store.toast = ''; }, 2600);
}

/* ---------------- Views and pins (P6) ---------------- */
/* A view is a page path plus its state in the query string, so a pin restores it. */
export function viewHref(path, state) {
  const query = new URLSearchParams();
  Object.entries(state || {}).forEach(([k, v]) => { if (v !== '' && v != null && v !== false) query.set(k, v); });
  const qs = query.toString();
  return path + (qs ? '?' + qs : '');
}
export function showView(path, state) {
  history.replaceState(null, '', viewHref(path, state));
  store.view = location.pathname + location.search;
}
const sameView = (a, b) => {
  const norm = href => { const u = new URL(href, location.origin); return u.pathname + '?' + [...u.searchParams].sort().map(x => x.join('=')).join('&'); };
  return norm(a) === norm(b);
};

export const pins = {
  find(href) { return store.pins.find(p => sameView(p.href, href)); },
  async save(list) { store.pins = (await api('PUT', '/api/pins', { pins: list })).pins; },
  async add(label, href) {
    if (pins.find(href)) { toast('Already pinned: ' + label); return; }
    await pins.save([...store.pins, { label, href }]);
    toast('Pinned “' + label + '” to the sidebar');
  },
  async remove(index) {
    const [gone] = store.pins.slice(index, index + 1);
    await pins.save(store.pins.filter((_, i) => i !== index));
    toast('Unpinned “' + gone.label + '”');
  },
  async toggle(label, href) {
    const hit = store.pins.findIndex(p => sameView(p.href, href));
    if (hit >= 0) await pins.remove(hit); else await pins.add(label, href);
  },
  async move(from, to) {
    if (from === to) return;
    const list = [...store.pins];
    const [pin] = list.splice(from, 1);
    list.splice(to, 0, pin);
    await pins.save(list);
  },
};

const TACK = 'M9 3h6l-1 6 3 3v2h-4l-1 7-1-7H7v-2l3-3z';

/* The page header's pin control: pins the current view. */
const PinView = {
  props: { label: String, href: String },
  data() { return { store }; },
  computed: { on() { return !!pins.find(this.href); } },
  methods: { click() { pins.toggle(this.label, this.href).catch(e => toast(e.message)); } },
  template: `<button class="tack-btn" :class="{on}" type="button" @click="click" :aria-pressed="on"
      :title="(on ? 'Unpin ' : 'Pin this view to the sidebar: ') + label">
    <svg viewBox="0 0 24 24" :fill="on ? 'currentColor' : 'none'" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="${TACK}"/></svg>
    <span>{{ on ? 'Pinned' : 'Pin view' }}</span><small>{{ label }}</small></button>`,
};

/* A row's menu. items: [{label, pin: {label, href}} | {label, run()}] */
let openMenu = null;
document.addEventListener('click', () => { if (openMenu) { openMenu.open = false; openMenu = null; } });
const RowMenu = {
  props: { items: Array, label: { type: String, default: 'More actions' } },
  data() { return { open: false }; },
  methods: {
    toggle() { if (openMenu && openMenu !== this) openMenu.open = false; this.open = !this.open; openMenu = this.open ? this : null; },
    pick(item) {
      this.open = false; openMenu = null;
      const done = item.pin ? pins.add(item.pin.label, item.pin.href) : Promise.resolve(item.run && item.run());
      done.catch(e => toast(e.message));
    },
  },
  template: `<span class="rowmenu" @click.stop>
    <button class="kebab" type="button" :aria-label="label" :aria-expanded="open" @click="toggle">⋮</button>
    <div v-if="open" class="menu" role="menu">
      <button v-for="it in items" :key="it.label" type="button" role="menuitem" :class="{pinit: it.pin}" @click="pick(it)">
        <svg v-if="it.pin" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="${TACK}"/></svg>{{ it.label }}</button>
    </div></span>`,
};

/* ---------------- Sidebar (P2) ---------------- */
const SideBar = {
  props: ['active'],
  data() { return { store, drag: null, over: null, host: location.host }; },
  mounted() { this.$refs.art.appendChild(artToggle); },   // the scene's pause control lives in the sidebar
  methods: {
    isHere(p) { return sameView(p.href, store.view); },
    start(i, e) { this.drag = i; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', String(i)); },
    enter(i) { if (this.drag != null) this.over = i; },
    drop(i) { if (this.drag != null) pins.move(this.drag, i).catch(e => toast(e.message)); this.drag = this.over = null; },
    end() { this.drag = this.over = null; },
    nudge(i, d) { const j = i + d; if (j >= 0 && j < store.pins.length) pins.move(i, j).catch(e => toast(e.message)); },
    remove(i) { pins.remove(i).catch(e => toast(e.message)); },
    split(label) { const i = label.indexOf(': '); return i < 0 ? ['', label] : [label.slice(0, i), label.slice(i + 2)]; },
    async logout() { await api('POST', '/api/logout').catch(() => {}); store.user = null; store.pins = []; },
  },
  template: `<aside class="sidebar">
    <a class="brand" href="/" title="Rook hub">
      <img src="/site/assets/rook-logo.png" alt="" width="30" height="45">
      <div><b>♜ ROOK</b><small>{{ host }}</small></div></a>
    <div>
      <div class="nav-label"><span>Pinned</span></div>
      <ol class="pinned-strip" aria-label="Pinned views; drag to reorder">
        <li v-for="(p, i) in store.pins" :key="p.href + i" class="pin" :class="{dragging: drag===i, over: over===i && drag!==i, here: isHere(p)}"
            draggable="true" @dragstart="start(i, $event)" @dragenter.prevent="enter(i)" @dragover.prevent @drop.prevent="drop(i)" @dragend="end">
          <span class="grip" aria-hidden="true" title="Drag to reorder">⠿</span>
          <a :href="p.href" :title="p.label"><i v-if="split(p.label)[0]">{{ split(p.label)[0] }}</i>{{ split(p.label)[1] }}</a>
          <button type="button" class="pin-x" @click="remove(i)" :aria-label="'Unpin ' + p.label" title="Unpin">×</button>
          <span class="sr-move"><button type="button" @click="nudge(i,-1)" :aria-label="'Move ' + p.label + ' up'">↑</button><button type="button" @click="nudge(i,1)" :aria-label="'Move ' + p.label + ' down'">↓</button></span>
        </li>
        <li v-if="!store.pins.length" class="pin-empty">Nothing pinned. Use <b>Pin view</b> in a page header or <b>⋮ › Pin</b> on a row.</li>
      </ol>
    </div>
    <div v-for="seg in store.nav" :key="seg.segment">
      <div class="nav-label"><span>{{ seg.segment }}</span></div>
      <nav class="nav">
        <a v-for="p in seg.pages" :key="p.id" :href="p.path" :class="{active: p.id===active}" :aria-current="p.id===active ? 'page' : null">{{ p.label }}</a>
      </nav>
    </div>
    <div class="side-foot">signed in as <span style="color:var(--fg-soft)">{{ store.user.name }}</span><br>
      <button type="button" class="linkish" @click="logout">log out</button>
      <span ref="art"></span></div>
  </aside>`,
};

/* ---------------- Login (T4) ---------------- */
const LoginView = {
  emits: ['in'],
  data() { return { name: '', password: '', error: '', busy: false }; },
  methods: {
    async submit() {
      this.busy = true; this.error = '';
      try { await api('POST', '/api/login', { name: this.name, password: this.password }); this.password = ''; this.$emit('in'); }
      catch (e) { this.error = e.message; }
      this.busy = false;
    },
  },
  template: `<main class="login">
    <form class="panel login-box" @submit.prevent="submit">
      <div class="brand"><img src="/site/assets/rook-logo.png" alt="" width="30" height="45"><div><b>♜ ROOK</b><small>log in to this hub</small></div></div>
      <label class="field">Name<input class="mono" v-model="name" autocomplete="username" required autofocus></label>
      <label class="field">Password<input class="mono" type="password" v-model="password" autocomplete="current-password" required></label>
      <p v-if="error" class="login-error" role="alert">{{ error }}</p>
      <button class="btn primary" type="submit" :disabled="busy">Log in</button>
    </form></main>`,
};

const Toast = {
  data() { return { store }; },
  template: `<div class="toast" role="status" aria-live="polite" :class="{on: !!store.toast}">{{ store.toast }}</div>`,
};

/* ---------------- Background rook scene (P10) ---------------- */
const SKETCH = `<svg class="rook-sketch" viewBox="0 0 600 760" fill="none" xmlns="http://www.w3.org/2000/svg">
    <defs><pattern id="rook-hatch" width="9" height="9" patternUnits="userSpaceOnUse" patternTransform="rotate(-32)"><path d="M0 0V9" style="stroke:var(--art-hatch)" stroke-width=".6" opacity=".4"/></pattern></defs>
    <g style="stroke:var(--art-line)" stroke-width=".65" opacity=".6"><ellipse cx="310" cy="622" rx="174" ry="62"/><ellipse cx="310" cy="622" rx="205" ry="76" stroke-dasharray="3 5"/><path d="M76 622H543M310 110V716M160 205V635M149 205H173M149 623H173" stroke-dasharray="4 5"/></g>
    <g style="stroke:var(--art-ink)" stroke-width="1.4"><path d="M209 604L215 579L243 564L254 519L266 333L247 310L232 305V236L251 218V183L280 171V208L306 203V162L339 165V204L367 214V176L392 191V235L404 251V306L386 324L365 344L372 528L385 569L411 590V617L378 639L253 639Z" style="fill:var(--art-fill)"/>
    <path d="M254 519L266 333L306 348L302 554L269 568L243 564Z" fill="url(#rook-hatch)"/>
    <path d="M232 305L305 328L404 306M247 310L306 348L386 324M215 579L276 603L385 569M209 604L276 627L411 590M276 627V646M280 171L298 183V211M339 165L351 183V210M367 214L391 236M251 218L278 229L306 221L338 230L368 226L392 241M243 564L302 581L385 569M266 333L302 350L365 344"/>
    </g><g style="stroke:var(--art-ink)" opacity=".22"><path d="M211 608L218 581L247 565L257 520L269 334M233 309L305 333L405 309M247 643L380 642L413 619"/></g>
  </svg>`;
function mountArt() {
  const host = document.createElement('div');
  host.className = 'rook-art'; host.id = 'rook-art'; host.setAttribute('aria-hidden', 'true'); host.dataset.mode = 'sketch';
  host.innerHTML = SKETCH + '<div class="rook-canvas"></div><span class="plate-number">FIG. 01 / CASTELLUM</span><span class="plate-caption"><b>ROOK / YOUR MACHINES, YOUR MOVE</b><br>ENCRYPTED · AGENT MESH · POWERFUL</span>';
  document.body.prepend(host);
  if (navigator.connection && navigator.connection.saveData) return;   // keep the drawing, skip three.js
  import('/site/vendor/rook-scene.js').then(({ mountRook }) => mountRook(host, artToggle)).catch(() => {});
}
/* One pause button for the scene, made once: the scene shows it when it animates, and the
   sidebar holds it while someone is logged in (the login page has no sidebar). */
const artToggle = Object.assign(document.createElement('button'), { className: 'art-toggle', type: 'button', hidden: true, textContent: 'Pause artwork' });

/* ---------------- The app ---------------- */
const App = {
  data() { return { store, page: null, component: null }; },
  async mounted() { await this.load(); },
  methods: {
    async load() {
      try {
        const session = await api('GET', '/api/session');
        store.user = session.user; store.nav = session.nav;
        store.pins = (await api('GET', '/api/pins')).pins;
        const pages = session.nav.flatMap(s => s.pages);
        this.page = pages.find(p => p.path === location.pathname) || pages[0];
        if (location.pathname === '/') showView(this.page.path + location.search);
        this.component = markRaw((await import(this.page.script)).default);
      } catch (e) {
        if (store.user) toast(e.message);
      }
      store.ready = true;
    },
  },
  template: `<template v-if="store.ready">
      <login-view v-if="!store.user" @in="load"></login-view>
      <div v-else class="layout"><side-bar :active="page && page.id"></side-bar>
        <main class="main"><component v-if="component" :is="component"></component></main></div>
    </template><toast-line></toast-line>`,
};

const app = createApp(App);
app.component('side-bar', SideBar);
app.component('login-view', LoginView);
app.component('pin-view', PinView);
app.component('row-menu', RowMenu);
app.component('toast-line', Toast);
app.mount('#app');
mountArt();
