/* The Secrets page (X3): every secret grouped by scope, never a value until the signed-in
   person reveals one (journaled at the hub), and Add secret through the secret.create cap.
   A revealed value lives only in this page's memory: it is hidden again after a while,
   and dropped when the person leaves the page. It never goes into the address, a pin or
   the browser's storage. */
import { api, toast, viewHref, showView } from '/site/shell.js';
import { ago } from '/site/pages/parts.js';

const REFRESH_MS = 5000;
const SHOW_MS = 20000;   // how long a revealed value stays on screen, as the mock-up has it

export default {
  data() {
    const view = new URLSearchParams(location.search);
    return {
      data: null, error: null, q: view.get('q') || '', only: view.get('secret') || '',
      shown: {}, timers: {}, adding: false, busy: false, timer: null,
      form: { kind: 'hub', target: '', text: '', name: '', value: '' },
    };
  },
  computed: {
    view() { return { q: this.q, secret: this.only }; },
    href() { return viewHref('/secrets', this.view); },
    onlyName() { const s = this.secrets.find(x => x.moniker === this.only); return s ? s.scope + ' · ' + s.name : this.only; },
    pinLabel() { return 'Secrets: ' + (this.only ? this.onlyName : this.q || 'all'); },
    secrets() { return this.data ? this.data.secrets : []; },
    groups() {
      const q = this.q.trim().toLowerCase(), groups = new Map();
      for (const s of this.secrets) {
        if (this.only ? s.moniker !== this.only : q && ![s.name, s.scope, s.moniker].some(x => x.toLowerCase().includes(q))) continue;
        if (!groups.has(s.scope)) groups.set(s.scope, []);
        groups.get(s.scope).push(s);
      }
      return [...groups].map(([scope, list]) => ({ scope, list }));
    },
    scope() {
      const f = this.form;
      return f.kind === 'hub' ? 'hub' : f.kind === 'text' ? f.text.trim() : f.target ? f.kind + ':' + f.target : '';
    },
  },
  watch: { view: { handler() { showView('/secrets', this.view); }, deep: true } },
  mounted() { this.load(); this.timer = setInterval(this.load, REFRESH_MS); },
  unmounted() {
    clearInterval(this.timer);
    Object.values(this.timers).forEach(clearTimeout);
    this.shown = {}; this.form.value = '';
  },
  methods: {
    async load() {
      try { this.data = await api('GET', '/api/secrets'); this.error = null; }
      catch (e) { this.error = e.data && e.data.code ? e.data : { message: e.message }; this.data = null; }
    },
    hide(m) { clearTimeout(this.timers[m]); delete this.timers[m]; delete this.shown[m]; },
    async reveal(s) {
      if (s.moniker in this.shown) { this.hide(s.moniker); return; }
      try {
        this.shown[s.moniker] = (await api('POST', `/api/secrets/${s.moniker}/reveal`)).value;
        this.timers[s.moniker] = setTimeout(() => this.hide(s.moniker), SHOW_MS);
        toast('Revealed ' + s.name + ' · journaled under your name');
      } catch (e) { toast(e.message); }
    },
    cancel() { this.adding = false; this.form.value = ''; },
    async add() {
      this.busy = true;
      try {
        await api('POST', '/api/secrets', { scope: this.scope, name: this.form.name.trim(), value: this.form.value });
        toast('Added ' + this.form.name.trim());
        this.form.value = ''; this.form.name = ''; this.adding = false;
        await this.load();
      } catch (e) { toast(e.message); }
      this.busy = false;
    },
    rowMenu(s) { return [{ label: 'Pin this secret', pin: { label: 'Secrets: ' + s.scope + ' · ' + s.name, href: viewHref('/secrets', { secret: s.moniker }) } }]; },
    ago,
  },
  template: `<div>
    <header class="topbar">
      <div><div class="title-row"><h1>Secrets</h1><pin-view :label="pinLabel" :href="href"></pin-view></div>
        <p>Grouped by scope. Values stay hidden until you reveal one; every reveal is journaled.</p></div>
      <div class="spacer"></div>
      <input class="mono" type="search" v-model="q" placeholder="filter secrets…" aria-label="Filter secrets" style="width:200px">
      <button v-if="data && !adding" class="btn accent" type="button" @click="adding=true">+ Add secret</button>
    </header>

    <div class="wrap">
      <div v-if="error" class="callout bad" role="alert"><b>{{ error.message }}</b> {{ error.next }}</div>

      <section v-if="adding" class="panel">
        <div class="panel-head"><h2>Add secret</h2><span class="sub">calls use it as secret:&lt;name&gt;, found in the worker’s scope, then its band’s, then the hub’s</span></div>
        <form class="panel-body form-stack" @submit.prevent="add" autocomplete="off">
          <div class="row2">
            <label class="field">Scope<select class="mono" v-model="form.kind" @change="form.target=''">
              <option value="hub">the hub</option><option value="worker">a worker</option><option value="band">a band</option><option value="text">a plain name</option></select></label>
            <label v-if="form.kind==='worker'" class="field">Worker<select class="mono" v-model="form.target">
              <option value="" disabled>choose a worker</option><option v-for="w in data.scopes.workers" :key="w.moniker" :value="w.moniker">{{ w.name }}</option></select></label>
            <label v-if="form.kind==='band'" class="field">Band<select class="mono" v-model="form.target">
              <option value="" disabled>choose a band</option><option v-for="b in data.scopes.bands" :key="b.moniker" :value="b.moniker">{{ b.name }}</option></select></label>
            <label v-if="form.kind==='text'" class="field">Scope name<input class="mono" v-model="form.text" placeholder="jellyfin"></label>
          </div>
          <label class="field">Name<input class="mono" v-model="form.name" placeholder="deploy-key" spellcheck="false"></label>
          <label class="field">Value<textarea class="mono" v-model="form.value" spellcheck="false" autocomplete="off" placeholder="never shown again unless revealed"></textarea></label>
          <div style="display:flex;gap:8px">
            <button class="btn primary" type="submit" :disabled="busy || !scope || !form.name.trim() || !form.value">Add secret</button>
            <button class="btn ghost" type="button" @click="cancel">Cancel</button>
          </div>
        </form>
      </section>

      <div v-if="only" class="one-banner">Showing one secret: <b>{{ onlyName }}</b><span style="flex:1"></span><button class="btn sm" type="button" @click="only=''">show all secrets</button></div>
      <p v-if="data && !secrets.length" class="note">No secrets yet.</p>

      <section v-for="g in groups" :key="g.scope" class="panel">
        <div class="panel-head"><h2>{{ g.scope }}</h2><div class="spacer"></div><span class="sub">{{ g.list.length }} {{ g.list.length === 1 ? 'secret' : 'secrets' }}</span></div>
        <div class="tbl" style="--cols:minmax(200px,1.3fr) minmax(220px,1.3fr) minmax(130px,.8fr) 30px">
          <div class="th"><span>name</span><span>value</span><span>made</span><span></span></div>
          <div v-for="s in g.list" :key="s.moniker" class="tr" :class="{one: only === s.moniker}">
            <span><span class="cell-title mono" style="font-size:12px">{{ s.name }}</span><span class="cell-sub mono">{{ s.reference }} · {{ s.moniker }}</span></span>
            <span class="secret-val">
              <button class="eye" :class="{on: s.moniker in shown}" type="button" @click="reveal(s)" :aria-pressed="s.moniker in shown"
                :aria-label="(s.moniker in shown ? 'Hide ' : 'Reveal ') + s.name" :title="s.moniker in shown ? 'Hide value' : 'Reveal value (journaled)'">
                <svg v-if="!(s.moniker in shown)" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>
                <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 3l18 18M10.6 5.1A10 10 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4.1M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a9.6 9.6 0 0 0 5.4-1.6M9.9 9.9a3 3 0 0 0 4.2 4.2"/></svg>
              </button>
              <span class="v" :class="{shown: s.moniker in shown}">{{ s.moniker in shown ? shown[s.moniker] : '••••••••••••' }}</span>
            </span>
            <span><span class="cell-mono">{{ ago(s.created_at) }}</span><span class="cell-sub">by {{ s.created_by ? s.created_by.name : 'unknown' }}<template v-if="s.token"> · {{ s.token }}</template></span></span>
            <span class="right"><row-menu :items="rowMenu(s)"></row-menu></span>
          </div>
        </div>
      </section>
    </div>
  </div>`,
};
