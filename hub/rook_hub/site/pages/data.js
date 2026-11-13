/* The Data page (P4: Journal, Records).
   Journal (L1, L2, J5, L5): entries newest first, filtered by everything the hub's
   journal reader takes, read further back on demand, chain runs grouped by run ID,
   results folded until opened, and a redact that goes through the journal.redact
   hub cap as a call by the logged-in user. The value to redact is held only while
   it is typed: never in the view, a pin, the URL or the browser's storage.
   Records (K1, K3, K4): read-only. Types with the plugin that owns each, browse by
   type, search by text, open a record (fields, body, links both ways, a blob's
   bytes), and the unlinked blobs. The open tab and its filters are the view (P6). */
import { api, toast, viewHref, showView } from '/site/shell.js';

const REFRESH_MS = 5000;
const PAGE = 50;
const RANGES = [{ id: '', label: 'any time' }, { id: '15m', label: 'last 15 min', s: 900 }, { id: '1h', label: 'last hour', s: 3600 },
  { id: '24h', label: 'last 24 h', s: 86400 }, { id: '7d', label: 'last 7 days', s: 604800 }, { id: 'between', label: 'between…' }];
const FILTERS = ['kind', 'user', 'worker', 'cap', 'text', 'range', 'since', 'until', 'job', 'run'];

function bytes(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
}

/* A journal entry's result in a few words, and how it went (L1). */
function outcome(e) {
  const r = e.result;
  if (e.kind === 'call' || e.kind === 'job') {
    if (r && r.type === 'error') return { text: r.code, cls: 'bad' };
    if (r && r.type === 'stream_open') return { text: 'stream', cls: 'good' };
    if (r && r.type === 'running') return { text: 'running', cls: '' };      // not ended yet (L1)
    return { text: 'ok', cls: 'good' };
  }
  if (e.kind === 'event' && r && r.event) return { text: r.event, cls: r.event === 'job.fail' ? 'bad' : '' };
  if (e.kind === 'stream') return { text: 'closed', cls: '' };
  const [key, value] = Object.entries(r || {})[0] || ['', ''];
  const short = typeof value === 'boolean' || (typeof value === 'string' && value.length <= 16);
  return { text: short ? key + ' ' + value : key, cls: '' };
}

const JournalRow = {
  props: ['e', 'open', 'names'], emits: ['set', 'fold'],
  computed: { out() { return outcome(this.e); } },
  methods: {
    time(iso) { const d = new Date(iso); return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); },
    day(iso) { const d = new Date(iso); return d.toDateString() === new Date().toDateString() ? 'today' : d.toLocaleDateString(); },
    json(v) { return JSON.stringify(v, null, 2); },
  },
  template: `<div class="tr">
    <span class="cell-mono" :title="e.at">{{ time(e.at) }}<span class="callid" style="display:block">{{ day(e.at) }} · #{{ e.id }}</span></span>
    <button type="button" class="cell-mono linkcell" @click="$emit('set', 'kind', e.kind)" :title="'Filter kind = ' + e.kind">{{ e.kind }}</button>
    <span><button v-if="e.user" type="button" class="actor linkcell" :class="{job: e.job}" @click="$emit('set', 'user', e.user)" :title="'Filter user = ' + e.user">{{ names.user(e.user) }}</button>
      <span v-else class="dim2 mono small">the hub</span>
      <span v-if="e.token" class="callid" style="display:block" :title="'token ' + e.token">{{ e.token }}</span></span>
    <button v-if="e.worker" type="button" class="cell-mono linkcell" @click="$emit('set', 'worker', e.worker)" :title="'Filter worker = ' + e.worker">{{ names.worker(e.worker) }}</button><span v-else></span>
    <button v-if="e.cap" type="button" class="cell-mono linkcell" style="color:var(--accent)" @click="$emit('set', 'cap', e.cap)" :title="'Filter cap = ' + e.cap">{{ e.cap }}</button><span v-else></span>
    <span class="why">{{ e.why }}<span v-if="e.job || e.run" class="callid" style="display:block">
      <button v-if="e.job" type="button" class="linkcell" @click="$emit('set', 'job', e.job)" :title="'Filter job = ' + e.job">job {{ names.job(e.job) }}</button>
      <template v-if="e.job && e.run"> · </template>
      <button v-if="e.run" type="button" class="linkcell" @click="$emit('set', 'run', e.run)" :title="'Filter run = ' + e.run">run {{ e.run.slice(0, 8) }}</button></span></span>
    <span><button type="button" class="linkcell" @click="$emit('fold', e.id)" :aria-expanded="open" :title="open ? 'Fold the result' : 'Open the whole result'">
      <span class="badge" :class="out.cls">{{ out.text }}</span> <span class="dim2 mono small">{{ open ? '▾' : '▸' }}</span></button></span>
    <pre v-if="open" class="json" style="grid-column:1/-1;margin:0;max-height:420px;overflow:auto;white-space:pre-wrap;word-break:break-all">{{ json(e.result) }}</pre>
  </div>`,
};

export default {
  components: { 'journal-row': JournalRow },
  data() {
    const view = new URLSearchParams(location.search);
    const f = {};
    FILTERS.forEach(k => { f[k] = view.get(k) || ''; });
    if (!RANGES.some(r => r.id === f.range)) f.range = '';
    return {
      tab: view.get('tab') === 'records' ? 'records' : 'journal', error: '', timer: null,
      f, ranges: RANGES, grouped: view.get('flat') !== 'true', entries: null, limit: PAGE, opened: {}, openRuns: {}, choices: null,
      redacting: false, secret: '', redacted: null, busy: false,
      type: view.get('type') || '', rq: view.get('q') || '', rec: view.get('rec') || '', unlinked: view.get('unlinked') === 'true',
      types: [], list: null, recLimit: PAGE, one: null, unlinkedCount: 0,
    };
  },
  computed: {
    state() {
      if (this.tab === 'records') return { tab: 'records', type: this.type, q: this.rq.trim(), rec: this.rec, unlinked: this.unlinked };
      const between = this.f.range === 'between';
      return { ...this.f, text: this.f.text.trim(), since: between ? this.f.since : '', until: between ? this.f.until : '', flat: !this.grouped };
    },
    href() { return viewHref('/data', this.state); },
    filterKey() { return JSON.stringify(this.f); },
    active() { return FILTERS.filter(k => this.f[k] && ((k !== 'since' && k !== 'until') || this.f.range === 'between')); },
    pinLabel() {
      if (this.tab === 'records') {
        if (this.rec) return 'Records: ' + (this.one ? this.one.record.title || this.one.record.moniker : this.rec);
        if (this.unlinked) return 'Records: unlinked blobs';
        return 'Records' + (this.type || this.rq.trim() ? ': ' : '') + [this.type, this.rq.trim() ? '“' + this.rq.trim() + '”' : ''].filter(Boolean).join(' · ');
      }
      const bits = this.active.filter(k => k !== 'since' && k !== 'until').map(k =>
        k === 'text' ? '“' + this.f.text.trim() + '”' : k === 'range' ? RANGES.find(r => r.id === this.f.range).label
          : k === 'run' ? 'run ' + this.f.run.slice(0, 8) : k + ' ' + this.f[k]);
      return 'Journal' + (bits.length ? ': ' + bits.join(', ') : '');
    },
    names() {
      const c = this.choices || { users: [], workers: [], jobs: [] };
      const find = (list, m, key) => { const x = list.find(i => i.moniker === m); return x ? x[key] : m; };
      return {
        user: m => find(c.users, m, 'name'),
        worker: m => { const x = c.workers.find(i => i.moniker === m); return x ? x.name + (x.device ? ' (device)' : '') : m; },
        job: m => find(c.jobs, m, 'title'),
      };
    },
    /* The entries as shown: a chain's entries gathered under its run ID, where its newest entry is (J5). */
    rows() {
      if (!this.entries) return [];
      if (!this.grouped) return this.entries.map(e => ({ id: 'e' + e.id, e }));
      const out = [], runs = {};
      for (const e of this.entries) {
        if (!e.run) { out.push({ id: 'e' + e.id, e }); continue; }
        if (!runs[e.run]) { runs[e.run] = { id: 'r' + e.run, run: e.run, entries: [] }; out.push(runs[e.run]); }
        runs[e.run].entries.push(e);
      }
      return out;
    },
    more() { return this.entries && this.entries.length >= this.limit; },
    total() { return this.types.reduce((n, t) => n + t.count, 0); },
    owning() { const t = this.types.find(x => x.type === this.type); return t ? t.plugin : ''; },
  },
  watch: {
    state: { handler() { showView('/data', this.state); }, deep: true },
    filterKey() { this.limit = PAGE; this.loadJournal(); },
    tab() { this.load(); },
    type() { this.recLimit = PAGE; this.loadRecords(); },
    rq() { this.recLimit = PAGE; clearTimeout(this.typing); this.typing = setTimeout(this.loadRecords, 250); },
    rec() { this.one = null; this.loadRecords(); },
    unlinked() { this.loadRecords(); },
  },
  mounted() {
    this.load();
    this.timer = setInterval(this.load, REFRESH_MS);
  },
  unmounted() { clearInterval(this.timer); clearTimeout(this.typing); this.secret = ''; },
  methods: {
    /* The journal request's query, made at each fetch so "last hour" means the last hour now. */
    query() {
      const q = new URLSearchParams();
      ['kind', 'user', 'worker', 'cap', 'job', 'run'].forEach(k => { if (this.f[k]) q.set(k, this.f[k]); });
      if (this.f.text.trim()) q.set('text', this.f.text.trim());
      const range = RANGES.find(r => r.id === this.f.range);
      if (range && range.s) q.set('since', new Date(Date.now() - range.s * 1000).toISOString());
      if (this.f.range === 'between') {
        if (this.f.since) q.set('since', new Date(this.f.since).toISOString());
        if (this.f.until) q.set('until', new Date(this.f.until).toISOString());
      }
      q.set('limit', this.limit);
      return q;
    },
    load() { return this.tab === 'journal' ? this.loadJournal() : this.loadRecords(); },
    async loadJournal() {
      try {
        const [choices, journal] = await Promise.all([api('GET', '/api/journal/choices'), api('GET', '/api/journal?' + this.query())]);
        this.choices = choices; this.entries = journal.entries;
        this.error = '';
      } catch (e) { this.error = e.message; }
    },
    async loadRecords() {
      try {
        const [types, unlinked] = await Promise.all([api('GET', '/api/record-types'), api('GET', '/api/unlinked-blobs')]);
        this.types = types.types; this.unlinkedCount = unlinked.records.length;
        if (this.rec) this.one = await api('GET', '/api/records/' + encodeURIComponent(this.rec));
        else if (this.unlinked) this.list = unlinked.records;
        else {
          const q = new URLSearchParams({ limit: this.recLimit });
          if (this.type) q.set('type', this.type);
          if (this.rq.trim()) q.set('text', this.rq.trim());
          this.list = (await api('GET', '/api/records?' + q)).records;
        }
        this.error = '';
      } catch (e) { this.error = e.message; }
    },
    further() { this.limit += PAGE; this.loadJournal(); },
    moreRecords() { this.recLimit += PAGE; this.loadRecords(); },
    set(k, v) { this.f[k] = this.f[k] === v ? '' : v; },
    clear() { FILTERS.forEach(k => { this.f[k] = ''; }); },
    fold(id) { this.opened[id] = !this.opened[id]; },
    /* A run went well unless one of its jobs' calls failed (J3); the job.fail events say so when no call is shown. */
    runOutcome(g) {
      const calls = g.entries.filter(e => e.kind === 'job');
      const failed = (calls.length ? calls : g.entries).filter(e => outcome(e).cls === 'bad').length;
      return failed ? { text: failed + ' failed', cls: 'bad' } : { text: 'ok', cls: 'good' };
    },
    runJobs(g) { return [...new Set(g.entries.filter(e => e.job).map(e => this.names.job(e.job)))].reverse().join(' → '); },
    async redact() {
      const value = this.secret;
      if (!value) { toast('Give the exact text to take out.'); return; }
      this.busy = true;
      try {
        this.redacted = await api('POST', '/api/redact', { value });
        this.secret = ''; this.redacting = false;
        await this.loadJournal();
      } catch (e) { toast(e.message); }
      this.busy = false;
    },
    cancelRedact() { this.secret = ''; this.redacting = false; },
    pick(type) { this.unlinked = false; this.rec = ''; this.type = type; },
    openRec(r) { this.rec = r.moniker; window.scrollTo(0, 0); },
    blob(r) { return r.type === 'blob' && r.body && typeof r.body.hash === 'string'; },
    size: bytes,
    json(v) { return JSON.stringify(v, null, 2); },
    when(iso) { return iso ? new Date(iso).toLocaleString() : ''; },
    recMenu(r) {
      return [{ label: 'Pin this record', pin: { label: 'Records: ' + (r.title || r.moniker), href: viewHref('/data', { tab: 'records', rec: r.moniker }) } },
        { label: 'Show only this record', run: () => this.openRec(r) }];
    },
  },
  template: `<div>
    <header class="topbar">
      <div><div class="title-row"><h1>Data</h1><pin-view :label="pinLabel" :href="href"></pin-view></div><p>Every call in the journal; everything kept in records.</p></div>
      <div class="spacer"></div>
      <input v-if="tab==='records'" class="mono" type="search" v-model="rq" placeholder="search records…" aria-label="Search records" style="width:260px">
    </header>

    <div class="wrap">
      <div class="tabs" role="tablist">
        <button role="tab" type="button" :class="{on: tab==='journal'}" :aria-selected="tab==='journal'" @click="tab='journal'">Journal</button>
        <button role="tab" type="button" :class="{on: tab==='records'}" :aria-selected="tab==='records'" @click="tab='records'">Records<span v-if="types.length" class="n">{{ total }}</span></button>
      </div>
      <p v-if="error" class="callout bad" role="alert">{{ error }}</p>

      <!-- ================= Journal ================= -->
      <section v-if="tab==='journal'" class="panel">
        <div class="jtools">
          <input type="search" class="mono" v-model="f.text" placeholder="free text: why, cap, result…" aria-label="Free text filter">
          <label class="check small"><input type="checkbox" v-model="grouped"> group chain runs</label>
          <span v-if="f.run" class="chip on">run {{ f.run.slice(0, 8) }} <button type="button" class="linkcell" @click="f.run=''" aria-label="Clear the run filter">✕</button></span>
          <span class="count" v-if="entries">{{ entries.length }} entries{{ more ? ', newest first' : ', all of them' }}</span>
          <button v-if="active.length" class="btn sm ghost" type="button" @click="clear">clear {{ active.length }} filter{{ active.length > 1 ? 's' : '' }} ✕</button>
          <button v-if="!redacting" class="btn sm" type="button" @click="redacting=true; redacted=null">Redact…</button>
        </div>
        <div v-if="f.range==='between'" class="jtools">
          <label class="field">since<input class="mono" type="datetime-local" step="1" v-model="f.since"></label>
          <label class="field">until<input class="mono" type="datetime-local" step="1" v-model="f.until"></label>
        </div>
        <form v-if="redacting" class="jtools" @submit.prevent="redact" autocomplete="off">
          <label class="field" style="flex:1">Take this value out of the journal and every stream recording
            <input class="mono" type="text" v-model="secret" autocomplete="off" spellcheck="false" placeholder="the exact text" aria-label="Value to redact"></label>
          <button class="btn danger" type="submit" :disabled="busy || !secret">Redact</button>
          <button class="btn ghost" type="button" @click="cancelRedact">Cancel</button>
        </form>
        <p v-if="redacted" class="callout" role="status" style="margin:0;border:0;border-bottom:1px solid var(--line)">Redacted: {{ redacted.redacted }} journal {{ redacted.redacted === 1 ? 'entry' : 'entries' }} and {{ redacted.recordings }} stream {{ redacted.recordings === 1 ? 'recording' : 'recordings' }} changed.
          <button type="button" class="linkcell dim2" @click="redacted=null" aria-label="Dismiss">✕</button></p>

        <div class="tbl jcols" style="--cols:92px 60px 130px 110px 110px minmax(180px,1fr) 170px">
          <div class="th"><span>when</span><span>kind</span><span>who</span><span>worker</span><span>cap</span><span>why</span><span>result</span></div>
          <div class="fr" role="group" aria-label="Column filters">
            <select :class="{set: f.range}" v-model="f.range" aria-label="Time range"><option v-for="r in ranges" :key="r.id" :value="r.id">{{ r.label }}</option></select>
            <select :class="{set: f.kind}" v-model="f.kind" aria-label="Kind"><option value="">any</option><option v-for="k in (choices ? choices.kinds : [])" :key="k">{{ k }}</option></select>
            <select :class="{set: f.user}" v-model="f.user" aria-label="User"><option value="">anyone</option><option v-for="u in (choices ? choices.users : [])" :key="u.moniker" :value="u.moniker">{{ u.name }}</option></select>
            <select :class="{set: f.worker}" v-model="f.worker" aria-label="Worker or device"><option value="">any worker</option><option v-for="w in (choices ? choices.workers : [])" :key="w.moniker" :value="w.moniker">{{ w.name }}{{ w.device ? ' (device)' : '' }}</option></select>
            <input class="mono" :class="{set: f.cap}" v-model.lazy="f.cap" list="data-caps" placeholder="any cap" aria-label="Cap">
            <select :class="{set: f.job}" v-model="f.job" aria-label="Job"><option value="">any job</option><option v-if="f.job && choices && !choices.jobs.some(j => j.moniker === f.job)" :value="f.job">{{ f.job }}</option><option v-for="j in (choices ? choices.jobs : [])" :key="j.moniker" :value="j.moniker">job: {{ j.title }}</option></select>
            <span class="dim2 mono small">click a cell to filter</span>
          </div>
          <datalist id="data-caps"><option v-for="c in (choices ? choices.caps : [])" :key="c" :value="c"></option></datalist>

          <template v-for="row in rows" :key="row.id">
            <div v-if="row.run" class="jgroup">
              <div class="ghead" @click="openRuns[row.run]=!openRuns[row.run]" role="button" tabindex="0" :aria-expanded="!!openRuns[row.run]" @keydown.enter="openRuns[row.run]=!openRuns[row.run]">
                <span>{{ openRuns[row.run] ? '▾' : '▸' }}</span><b>chain run {{ row.run.slice(0, 8) }}</b><span>{{ runJobs(row) }}</span><span class="dim2">· {{ when(row.entries[0].at) }} · {{ row.entries.length }} entries</span>
                <div class="spacer"></div>
                <button type="button" class="btn sm ghost" @click.stop="set('run', row.run)" :title="'Show only run ' + row.run">only this run</button>
                <span class="badge" :class="runOutcome(row).cls">{{ runOutcome(row).text }}</span>
              </div>
              <template v-if="openRuns[row.run]">
                <journal-row v-for="e in row.entries" :key="e.id" :e="e" :open="!!opened[e.id]" :names="names" @set="set" @fold="fold"></journal-row>
              </template>
            </div>
            <journal-row v-else :e="row.e" :open="!!opened[row.e.id]" :names="names" @set="set" @fold="fold"></journal-row>
          </template>
          <div v-if="entries && !entries.length" class="tr"><span class="note" style="grid-column:1/-1">Nothing in the journal matches these filters.</span></div>
          <div v-if="entries && entries.length" class="tr"><span style="grid-column:1/-1;display:flex;align-items:center;gap:10px">
            <button v-if="more" class="btn sm" type="button" @click="further">Read further back</button>
            <span v-else class="note">That is the oldest entry{{ active.length ? ' these filters match' : '' }}.</span></span></div>
        </div>
      </section>

      <!-- ================= Records ================= -->
      <section v-else class="panel records">
        <nav class="typebrowser" aria-label="Record types">
          <div class="section-title" style="padding:0 9px">Types</div>
          <button type="button" :class="{on: !type && !unlinked && !rec}" @click="pick('')">all types<span>{{ total }}</span></button>
          <button v-for="t in types" :key="t.type" type="button" :class="{on: type===t.type && !unlinked && !rec}" @click="pick(t.type)" :title="'owned by plugin ' + t.plugin">{{ t.type }}<span>{{ t.count }}</span></button>
          <div class="section-title" style="padding:0 9px;margin-top:14px">Blobs</div>
          <button type="button" :class="{on: unlinked && !rec}" @click="rec=''; unlinked=true">unlinked<span>{{ unlinkedCount }}</span></button>
          <div class="schema"><template v-if="type && !unlinked">{{ type }} records<br>owned by plugin <b>{{ owning }}</b></template>
            <template v-else>Types and the plugin that owns each:<br><span v-for="t in types" :key="t.type">· {{ t.type }}: {{ t.plugin }}<br></span></template></div>
        </nav>

        <div class="reclist">
          <template v-if="rec && one">
            <div class="one-banner">Showing one record: <b>{{ one.record.title || one.record.moniker }}</b><span style="flex:1"></span><button class="btn sm" type="button" @click="rec=''">back to the list</button></div>
            <article class="rec one">
              <div class="rec-head">
                <div><h3>{{ one.record.title || '(no title)' }}</h3><span class="moniker">{{ one.record.type }} · {{ one.record.moniker }} · {{ one.record.guid }}</span></div>
                <row-menu :items="recMenu(one.record)"></row-menu>
              </div>
              <p v-if="one.record.description">{{ one.record.description }}</p>
              <div class="facts"><span>owner <b>{{ one.record.owner.name }}</b></span>
                <span>creator <b>{{ one.record.creator.name }}</b><template v-if="one.record.creator_token"> through token <b>{{ one.record.creator_token }}</b></template></span>
                <span>created <b>{{ when(one.record.created_at) }}</b></span><span>updated <b>{{ when(one.record.updated_at) }}</b></span></div>
              <div class="rows">
                <template v-if="blob(one.record)">
                  <span class="k">bytes</span>
                  <div class="chips"><span class="blob"><span class="ext">{{ one.record.body.mime }}</span>{{ size(one.record.body.size) }}<small class="mono">sha256 {{ one.record.body.hash }}</small></span>
                    <a class="btn sm accent" :href="'/api/records/' + one.record.moniker + '/bytes'" download>Download</a></div>
                </template>
                <span class="k">links to</span>
                <div class="chips"><a v-for="l in one.links.to" :key="l.guid" class="link" href="#" @click.prevent="openRec(l)"><span class="rel">{{ l.type }}</span>{{ l.title || l.moniker }}</a>
                  <span v-if="!one.links.to.length" class="dim2 small">none</span></div>
                <span class="k">linked from</span>
                <div class="chips"><a v-for="l in one.links.from" :key="l.guid" class="link" href="#" @click.prevent="openRec(l)"><span class="rel">{{ l.type }}</span>{{ l.title || l.moniker }}</a>
                  <span v-if="!one.links.from.length" class="dim2 small">none</span></div>
                <span class="k">body</span>
                <pre class="json" style="margin:0;max-height:480px;overflow:auto;white-space:pre-wrap;word-break:break-all">{{ json(one.record.body) }}</pre>
              </div>
            </article>
          </template>

          <template v-else-if="list">
            <div style="display:flex;align-items:center;gap:10px">
              <span class="section-title" style="margin:0">{{ unlinked ? list.length + ' unlinked blobs: no record links to them, and they link to none' : list.length + (type ? ' ' + type : '') + ' records' + (rq.trim() ? ' matching “' + rq.trim() + '”' : '') }}</span>
            </div>
            <article v-for="r in list" :key="r.guid" class="rec">
              <div class="rec-head">
                <div><h3><a href="#" style="color:inherit" @click.prevent="openRec(r)">{{ r.title || '(no title)' }}</a></h3><span class="moniker">{{ r.type }} · {{ r.moniker }}</span></div>
                <row-menu :items="recMenu(r)"></row-menu>
              </div>
              <p v-if="r.description">{{ r.description }}</p>
              <div class="facts"><span>owner <b>{{ r.owner.name }}</b></span><span>creator <b>{{ r.creator.name }}</b></span><span>updated <b>{{ when(r.updated_at) }}</b></span>
                <span v-if="blob(r)">{{ r.body.mime }} · <b>{{ size(r.body.size) }}</b></span></div>
            </article>
            <p v-if="!list.length" class="note">{{ unlinked ? 'Every blob is linked.' : 'No records here.' }}</p>
            <button v-if="!unlinked && list.length >= recLimit" class="btn sm" type="button" style="align-self:start" @click="moreRecords">Show more</button>
          </template>
        </div>
      </section>
    </div>
  </div>`,
};
