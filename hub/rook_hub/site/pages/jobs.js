/* The Jobs page (P4, J1, J3, J4, J5, J7, P8): the jobs, chains as trees, runs grouped by
   run ID, a run opened to the call's result, and pause, un-pause, delete and New job, each
   a hub cap called as the logged-in user. It asks the hub again every few seconds. */
import { api, toast, viewHref, showView } from '/site/shell.js';
import { ago, SureButton } from '/site/pages/parts.js';

const REFRESH_MS = 5000;

/* The triggers a new job can have: events the hub's bus carries (J2). */
const TRIGGERS = [
  { id: 'schedule', event: 'schedule', desc: 'Every N minutes, or at a time of day on the hub’s clock' },
  { id: 'worker.online', event: 'worker.online', desc: 'When a worker comes online' },
  { id: 'worker.offline', event: 'worker.offline', desc: 'When a worker misses three heartbeats' },
  { id: 'webhook', event: 'webhook', desc: 'When something posts to the hub’s /webhook with a webhook token' },
  { id: 'job success', event: 'job.success', desc: 'After another job succeeds' },
  { id: 'job fail', event: 'job.fail', desc: 'After another job fails' },
  { id: 'custom event', event: '', desc: 'An event any worker or agent emits by name' },
];

const short = v => typeof v === 'string' ? (/\s/.test(v) ? JSON.stringify(v) : v) : JSON.stringify(v);
export const argText = args => Object.entries(args || {}).map(([k, v]) => k + '=' + short(v)).join(' ');

/* A job's call as one line: worker › cap args */
const CapCall = {
  props: { call: Object },
  template: `<span class="capcall" :title="call.name + ' › ' + call.cap + ' ' + args"><span class="w">{{ call.name }}</span> › <span class="c">{{ call.cap }}</span> <span class="a">{{ args }}</span></span>`,
  computed: { args() { return argText(this.call.args); } },
};

/* One job in a chain tree, and the jobs its completion starts under it (J3). */
const ChainNode = {
  name: 'chain-node',
  props: { node: Object, jobs: Object, root: Boolean },
  emits: ['pick'],
  components: { 'cap-call': CapCall },
  computed: {
    job() { return this.jobs[this.node.job] || { title: this.node.job, call: { name: '', cap: '', args: {} } }; },
    edge() { return this.root ? this.node.on : this.node.on === 'job.fail' ? 'on fail' : 'on success'; },
    args() { return argText(this.job.call.args); },
  },
  template: `<li>
    <button type="button" class="tnode" :class="{cycle: node.loop, failed: node.on==='job.fail' && !root}" @click="$emit('pick', node.job)" :title="'Open ' + job.title">
      <span class="edge" :class="node.on==='job.fail' && !root ? 'fail' : root ? 'root' : 'success'">{{ edge }}</span>
      <span v-if="node.loop" title="loops back to a job earlier in this chain">↺</span><span v-else></span>
      <span><b>{{ job.title }}</b> <span v-if="node.loop" class="badge bad">loop</span><span v-if="job.paused" class="badge plain">paused</span><br>
        <span class="tc" v-if="node.loop">loops back to {{ job.title }}</span><span class="tc" v-else>{{ job.call.name }} › {{ job.call.cap }} {{ args }}</span></span>
      <span class="ts"></span>
    </button>
    <ul v-if="node.children.length"><chain-node v-for="c in node.children" :key="c.job + c.on" :node="c" :jobs="jobs" @pick="$emit('pick', $event)"></chain-node></ul>
  </li>`,
};

/* The New job form: a trigger, one cap call with fixed arguments, a title (J1). */
const JobForm = {
  props: { jobs: Array },
  emits: ['save', 'cancel'],
  data() {
    return {
      triggers: TRIGGERS, targets: [], error: '', busy: false,
      d: { trig: 'schedule', schedule: 'every 15m', worker: '', job: '', event: '', payload: '',
           target: 'hub', cap: '', args: {}, json: '', title: '', description: '' },
    };
  },
  async created() {
    try { this.targets = (await api('GET', '/api/jobs/targets')).targets; this.pickCap(); } catch (e) { this.error = e.message; }
  },
  computed: {
    workers() { return this.targets.filter(t => t.kind === 'worker'); },
    target() { return this.targets.find(t => t.worker === this.d.target) || { caps: [] }; },
    cap() { return this.target.caps.find(c => c.name === this.d.cap) || { description: '', schema: {} }; },
    props() { return Object.entries(this.cap.schema.properties || {}); },
    required() { return this.cap.schema.required || []; },
    event() { return this.d.trig === 'custom event' ? this.d.event.trim() : TRIGGERS.find(t => t.id === this.d.trig).event; },
    when() {
      const d = this.d;
      return { schedule: d.schedule, 'worker.online': d.worker || 'any worker', 'worker.offline': d.worker || 'any worker',
        webhook: 'any webhook', 'job success': 'after ' + (d.job || 'any job'), 'job fail': 'after ' + (d.job || 'any job'),
        'custom event': d.event }[d.trig];
    },
    missing() {
      const m = this.required.filter(n => this.d.args[n] === '' || this.d.args[n] == null);
      if (!this.d.title.trim()) m.push('title');
      if (!this.event) m.push('event name');
      if (!this.d.cap) m.push('cap');
      return m;
    },
  },
  watch: { 'd.target'() { this.pickCap(); }, 'd.cap'() { this.resetArgs(); } },
  methods: {
    pickCap() { if (!this.target.caps.some(c => c.name === this.d.cap)) this.d.cap = (this.target.caps[0] || {}).name || ''; this.resetArgs(); },
    resetArgs() {
      const args = {};
      for (const [name, p] of this.props) args[name] = p.type === 'boolean' ? false : '';
      this.d.args = args; this.d.json = '';
    },
    kind(p) { return p.type === 'boolean' ? 'bool' : p.type === 'integer' || p.type === 'number' ? 'number' : p.type === 'string' ? 'text' : 'json'; },
    payload() {
      const d = this.d;
      if (d.trig === 'schedule') return { schedule: d.schedule.trim() };
      if (d.trig.startsWith('worker.')) return d.worker ? { worker_moniker: d.worker } : {};
      if (d.trig.startsWith('job ')) return d.job ? { job_moniker: d.job } : {};
      if (d.trig === 'custom event' && d.payload.trim()) return JSON.parse(d.payload);
      return {};
    },
    args() {
      if (!this.props.length) return this.d.json.trim() ? JSON.parse(this.d.json) : {};
      const out = {};
      for (const [name, p] of this.props) {
        const v = this.d.args[name], k = this.kind(p);
        if (k === 'bool') { if (v || this.required.includes(name)) out[name] = !!v; continue; }
        if (v === '' || v == null) continue;
        out[name] = k === 'number' ? Number(v) : k === 'json' ? JSON.parse(v) : v;
      }
      return out;
    },
    async save() {
      if (this.missing.length) return;
      let body;
      try {
        body = { title: this.d.title.trim(), description: this.d.description.trim(),
          match: { event: this.event, payload: this.payload() }, call: { worker: this.d.target, cap: this.d.cap, args: this.args() } };
      } catch (e) { this.error = 'Not valid JSON: ' + e.message; return; }
      this.busy = true; this.error = '';
      try { this.$emit('save', await api('POST', '/api/jobs', body)); } catch (e) { this.error = e.message; }
      this.busy = false;
    },
  },
  template: `<form class="form-stack" @submit.prevent="save">
    <div>
      <div class="step-h"><i>1</i>Trigger</div>
      <div class="trigger-cards" role="radiogroup" aria-label="Trigger">
        <label v-for="t in triggers" :key="t.id" :class="{on: d.trig===t.id}">
          <b><input type="radio" :value="t.id" v-model="d.trig">{{ t.id }}</b>{{ t.desc }}</label>
      </div>
      <div class="row2" style="margin-top:10px">
        <template v-if="d.trig==='schedule'">
          <label class="field">When<input class="mono" v-model="d.schedule" placeholder="every 15m · at 07:30"></label>
          <div class="note" style="align-self:end">“every N<span class="mono">m</span>” for every N minutes, or “at HH:MM” once a day.</div>
        </template>
        <label v-if="d.trig.startsWith('worker.')" class="field">Worker<select class="mono" v-model="d.worker">
          <option value="">any worker</option><option v-for="w in workers" :key="w.worker" :value="w.worker">{{ w.name }}</option></select></label>
        <div v-if="d.trig==='webhook'" class="note">Runs on every post to <span class="mono" style="color:var(--accent)">/webhook</span> made with a webhook token of this hub.</div>
        <label v-if="d.trig.startsWith('job ')" class="field">After job<select class="mono" v-model="d.job">
          <option value="">any job</option><option v-for="j in jobs" :key="j.moniker" :value="j.moniker">{{ j.title }}</option></select></label>
        <template v-if="d.trig==='custom event'">
          <label class="field">Event name<input class="mono" v-model="d.event" placeholder="door.opened"></label>
          <label class="field">Payload must have (JSON, optional)<input class="mono" v-model="d.payload" placeholder='{"door": "garage"}'></label>
        </template>
      </div>
    </div>

    <div>
      <div class="step-h"><i>2</i>One cap call</div>
      <div class="row2">
        <label class="field">Worker<select class="mono" v-model="d.target">
          <option v-for="t in targets" :key="t.worker" :value="t.worker">{{ t.name }}{{ t.kind==='device' ? ' (device)' : '' }}</option></select></label>
        <label class="field">Cap<select class="mono" v-model="d.cap"><option v-for="c in target.caps" :key="c.name" :value="c.name">{{ c.name }}</option></select></label>
      </div>
      <p class="note" style="margin:6px 0 4px">{{ cap.description }}<span v-if="!target.caps.length">This worker announces no caps.</span></p>
      <div v-for="[name, p] in props" :key="name" class="argrow">
        <span class="an">{{ name }}<i v-if="required.includes(name)">*</i><small>{{ p.type || 'json' }}{{ p.description ? ' · ' + p.description : '' }}</small></span>
        <label v-if="kind(p)==='bool'" class="check"><input type="checkbox" v-model="d.args[name]"> {{ d.args[name] ? 'true' : 'false' }}</label>
        <input v-else class="mono" :type="kind(p)==='number' ? 'number' : 'text'" v-model="d.args[name]"
          :placeholder="kind(p)==='json' ? 'JSON' : required.includes(name) ? 'required' : 'optional'" :aria-label="name">
      </div>
      <label v-if="d.cap && !props.length" class="field">Arguments (JSON object, optional)<input class="mono" v-model="d.json" placeholder="{}"></label>
    </div>

    <div>
      <div class="step-h"><i>3</i>Title and description</div>
      <div class="form-stack" style="gap:8px">
        <label class="field">Title<input class="mono" v-model="d.title" placeholder="e.g. garage-left-open"></label>
        <label class="field">Description<textarea v-model="d.description" placeholder="What it is for, in a sentence a person can read in the journal."></textarea></label>
      </div>
    </div>

    <div class="preview">
      <b>{{ d.title || 'untitled' }}</b> · on <b>{{ event || '…' }}</b> {{ when }}<br>
      → {{ (targets.find(t => t.worker===d.target) || {}).name }} › {{ d.cap }}
      <div v-if="missing.length" style="color:var(--bad)">missing: {{ missing.join(', ') }}</div>
    </div>
    <p v-if="error" class="callout bad" role="alert" style="margin:0">{{ error }}</p>
    <div style="display:flex;gap:8px">
      <button class="btn primary" type="submit" :disabled="!!missing.length || busy">Create job</button>
      <button class="btn ghost" type="button" @click="$emit('cancel')">Cancel</button>
    </div>
  </form>`,
};

/* Runs grouped by run ID (J5); a step opens to its call result (P8). */
const RunsList = {
  props: { runs: Array, sel: String },
  emits: ['open'],
  methods: { ago },
  template: `<div>
    <p v-if="!runs.length" class="note" style="margin:0;padding:10px 14px">No runs yet.</p>
    <div v-for="r in runs" :key="r.run" class="run-group" style="border-top:1px solid var(--line2);padding:8px 14px">
      <div class="dim2 mono small">run {{ r.run.slice(0, 8) }} · {{ ago(r.at) }} · {{ r.steps.length }} {{ r.steps.length === 1 ? 'step' : 'steps' }}</div>
      <div v-for="s in r.steps" :key="s.id" class="tr click" style="display:grid;grid-template-columns:minmax(160px,1fr) minmax(200px,1.4fr) 80px 90px;gap:10px;align-items:center;padding:5px 0"
           :style="sel && s.job !== sel ? 'opacity:.6' : ''" @click="$emit('open', Object.assign({run: r.run}, s))" tabindex="0" @keydown.enter="$emit('open', Object.assign({run: r.run}, s))" title="Open what is behind this run">
        <span class="cell-title">{{ s.title || s.job }}<span v-if="s.title === null" class="dim2"> (deleted)</span></span>
        <span class="cell-mono" :title="s.worker">{{ s.name }} › {{ s.cap }}</span>
        <span><span class="badge" :class="s.ok ? 'good' : 'bad'">{{ s.ok ? 'ok' : 'failed' }}</span></span>
        <span class="cell-mono">{{ ago(s.at) }}</span>
      </div>
    </div>
  </div>`,
};

export default {
  components: { 'chain-node': ChainNode, 'job-form': JobForm, 'cap-call': CapCall, 'runs-list': RunsList, 'sure-button': SureButton },
  data() {
    const view = new URLSearchParams(location.search);
    return {
      data: null, runs: [], error: '', step: null, warning: '', timer: null,
      event: view.get('event') || '', sel: view.get('job') || '', creating: view.get('new') === '1',
    };
  },
  computed: {
    jobs() { return this.data ? this.data.jobs : []; },
    byMoniker() { return Object.fromEntries(this.jobs.map(j => [j.moniker, j])); },
    events() { return [...new Set(this.jobs.map(j => j.match.event))]; },
    shown() { return this.event ? this.jobs.filter(j => j.match.event === this.event) : this.jobs; },
    selJob() { return this.byMoniker[this.sel]; },
    view() { return { new: this.creating ? '1' : '', job: this.creating ? '' : this.sel, event: this.creating || this.sel ? '' : this.event }; },
    href() { return viewHref('/jobs', this.view); },
    pinLabel() { return 'Jobs: ' + (this.creating ? 'new job' : this.selJob ? this.selJob.title : this.event || 'all'); },
  },
  watch: {
    view: { handler() { showView('/jobs', this.view); this.loadRuns(); }, deep: true },
    sel(now, before) { if (before) this.warning = ''; },   // the warning of a save goes once the person moves on
  },
  mounted() {
    this.load();
    this.timer = setInterval(this.load, REFRESH_MS);
    this.onKey = e => { if (e.key === 'Escape') this.step = null; };
    document.addEventListener('keydown', this.onKey);
  },
  unmounted() { clearInterval(this.timer); document.removeEventListener('keydown', this.onKey); },
  methods: {
    async load() {
      try { this.data = await api('GET', '/api/jobs'); this.error = ''; } catch (e) { this.error = e.message; return; }
      await this.loadRuns();
    },
    async loadRuns() {
      if (this.creating) return;
      try { this.runs = (await api('GET', '/api/jobs/runs' + (this.sel ? '?job=' + encodeURIComponent(this.sel) : ''))).runs; } catch (e) { this.error = e.message; }
    },
    trigger(j) {
      const p = j.match.payload, e = j.match.event;
      if (e === 'schedule') return p.schedule;
      if (e === 'job.success' || e === 'job.fail') return p.job_moniker ? 'after ' + ((this.byMoniker[p.job_moniker] || {}).title || p.job_moniker) : 'after any job';
      if (e.startsWith('worker.')) return p.worker_moniker || 'any worker';
      return argText(p);
    },
    trigClass(e) { return { 'job.success': 'ok', 'job.fail': 'fail', schedule: '' }[e] ?? 'neutral'; },
    chainWarning(tree) {
      const find = n => (this.byMoniker[n.job] || {}).warning || n.children.map(find).find(Boolean);
      return find(tree);
    },
    stepTitle(s) { return s.title || s.job; },
    async act(path, done) {
      try { await api('POST', path); toast(done); await this.load(); } catch (e) { toast(e.message); }
    },
    setPaused(j, paused) { return this.act(`/api/jobs/${j.moniker}/${paused ? 'pause' : 'unpause'}`, (paused ? 'Paused ' : 'Un-paused ') + j.title); },
    async remove(j) { if (this.sel === j.moniker) this.sel = ''; await this.act(`/api/jobs/${j.moniker}/delete`, 'Deleted ' + j.title); },
    saved(reply) {
      this.creating = false; this.warning = reply.warnings.join(' ');
      toast('Created job “' + reply.job.title + '”');
      this.load().then(() => { this.sel = reply.job.moniker; });
    },
    rowMenu(j) { return [{ label: 'Pin this job', pin: { label: 'Jobs: ' + j.title, href: viewHref('/jobs', { job: j.moniker }) } }]; },
    json(v) { return JSON.stringify(v, null, 2); },
    ago,
  },
  template: `<div>
    <header class="topbar">
      <div><div class="title-row"><h1>Jobs</h1><pin-view :label="pinLabel" :href="href"></pin-view></div>
        <p>One trigger, one cap call. Chain them with job success and job fail.</p></div>
      <div class="spacer"></div>
      <div class="chips" role="group" aria-label="Trigger filter" v-if="!sel && !creating && data">
        <button class="chip" type="button" :aria-pressed="event===''" @click="event=''">all <span class="n">{{ jobs.length }}</span></button>
        <button v-for="e in events" :key="e" class="chip" type="button" :aria-pressed="event===e" @click="event=e">{{ e }} <span class="n">{{ jobs.filter(j => j.match.event===e).length }}</span></button>
      </div>
      <button class="btn accent" type="button" @click="creating=true; sel=''; warning=''" v-if="!creating">+ New job</button>
    </header>

    <div class="wrap">
      <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
      <p v-if="warning" class="callout bad" role="alert">⚠ {{ warning }} <button class="btn sm ghost" type="button" @click="warning=''">dismiss</button></p>

      <section v-if="creating" class="panel newjob">
        <div class="panel-head"><button class="back" type="button" @click="creating=false">← All jobs</button><h2>New job</h2><span class="sub">a trigger, one cap call, a title</span></div>
        <div class="panel-body"><job-form :jobs="jobs" @save="saved" @cancel="creating=false"></job-form></div>
      </section>

      <template v-else-if="sel && data">
        <section v-if="!selJob" class="panel"><div class="panel-body"><p class="note" style="margin:0">There is no job {{ sel }} any more.
          <button class="btn sm" type="button" @click="sel=''">← All jobs</button></p></div></section>
        <template v-else>
        <section class="panel">
          <div class="panel-head">
            <button class="back" type="button" @click="sel=''">← All jobs</button>
            <h2>{{ selJob.title }}</h2>
            <span class="trig"><span class="t" :class="trigClass(selJob.match.event)">{{ selJob.match.event }}</span></span>
            <span class="sub">{{ trigger(selJob) }}</span>
            <div class="spacer"></div>
            <button class="toggle" type="button" :class="{on: !selJob.paused}" @click="setPaused(selJob, !selJob.paused)" :aria-pressed="!selJob.paused" :aria-label="(selJob.paused ? 'Un-pause ' : 'Pause ') + selJob.title"></button>
            <span class="sub">{{ selJob.paused ? 'paused' : 'on' }}</span>
            <sure-button label="Delete job" :question="'Delete ' + selJob.title + ' for good?'" @yes="remove(selJob)"></sure-button>
            <row-menu :items="rowMenu(selJob)"></row-menu>
          </div>
          <div class="panel-body">
            <p v-if="selJob.description" style="margin:0 0 10px;color:var(--fg-soft)">{{ selJob.description }}</p>
            <cap-call :call="selJob.call" style="display:inline-block;max-width:100%"></cap-call>
            <div class="kv" style="margin-top:12px">
              <span class="k">moniker</span><span class="v">{{ selJob.moniker }}</span>
              <span class="k">matches</span><span class="v">{{ selJob.match.event }} {{ json(selJob.match.payload) === '{}' ? '' : json(selJob.match.payload) }}</span>
              <span class="k">owner</span><span class="v">{{ selJob.owner.name }} <span class="dim2">{{ selJob.owner.moniker }}</span></span>
              <span class="k">created</span><span class="v">{{ ago(selJob.created_at) }}</span>
            </div>
            <p v-if="selJob.warning && selJob.warning !== warning" class="callout bad" style="margin:12px 0 0">⚠ {{ selJob.warning }}</p>
          </div>
        </section>
        <section class="panel">
          <div class="panel-head"><h2>Run history</h2><span class="sub">from the journal · the chain runs {{ selJob.title }} took part in, newest first</span></div>
          <runs-list :runs="runs" :sel="sel" @open="step=$event"></runs-list>
        </section>
        </template>
      </template>

      <template v-else-if="data">
        <section v-for="tree in data.chains" :key="tree.job" class="panel">
          <div class="panel-head">
            <h2>Chain · {{ (byMoniker[tree.job] || {}).title }}</h2>
            <span v-if="chainWarning(tree)" class="badge bad" :title="chainWarning(tree)">⚠ {{ chainWarning(tree).split('. ')[0] }}</span>
          </div>
          <div class="panel-body">
            <ul class="tree"><chain-node :node="tree" :jobs="byMoniker" :root="true" @pick="sel=$event"></chain-node></ul>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><h2>All jobs</h2><span class="sub">{{ shown.length }} of {{ jobs.length }} · click a job for its runs</span></div>
          <div class="tbl" style="--cols:170px minmax(220px,1.2fr) minmax(220px,1.3fr) 110px 46px 26px">
            <div class="th"><span>trigger</span><span>cap call</span><span>title · description</span><span>owner</span><span>on</span><span></span></div>
            <div v-for="j in shown" :key="j.moniker" class="tr click" @click="sel=j.moniker" tabindex="0" @keydown.enter="sel=j.moniker">
              <span class="trig"><span class="t" :class="trigClass(j.match.event)">{{ j.match.event }}</span><small>{{ trigger(j) }}</small></span>
              <cap-call :call="j.call"></cap-call>
              <span><span class="cell-title">{{ j.title }} <span v-if="j.warning" class="badge bad" :title="j.warning">loop</span></span><span class="cell-sub">{{ j.description }}</span></span>
              <span class="cell-mono" :title="j.owner.moniker">{{ j.owner.name }}</span>
              <span @click.stop><button class="toggle" type="button" :class="{on: !j.paused}" @click="setPaused(j, !j.paused)" :aria-pressed="!j.paused" :aria-label="(j.paused ? 'Un-pause ' : 'Pause ') + j.title"></button></span>
              <span @click.stop><row-menu :items="rowMenu(j)"></row-menu></span>
            </div>
            <div v-if="!jobs.length" class="tr"><span class="note" style="grid-column:1/-1">No jobs yet. <a href="#" @click.prevent="creating=true">Create one</a>.</span></div>
          </div>
        </section>

        <section class="panel">
          <div class="panel-head"><h2>Runs</h2><span class="sub">from the journal · steps of one chain run share its run ID · newest first</span></div>
          <runs-list :runs="runs" @open="step=$event"></runs-list>
        </section>
      </template>
    </div>

    <template v-if="step">
      <div class="drawer-scrim" @click="step=null"></div>
      <aside class="drawer" role="dialog" aria-modal="true" :aria-label="'Run of ' + stepTitle(step)">
        <div class="drawer-head">
          <div style="flex:1">
            <div class="dim2 mono small" style="letter-spacing:1px;text-transform:uppercase">job run · run {{ step.run.slice(0, 8) }}</div>
            <h2>{{ stepTitle(step) }}</h2>
            <p>{{ step.name }} › {{ step.cap }}</p>
          </div>
          <span class="badge" :class="step.ok ? 'good' : 'bad'">{{ step.ok ? 'ok' : 'failed' }}</span>
          <button class="btn sm ghost" type="button" @click="step=null" aria-label="Close">✕</button>
        </div>
        <div class="drawer-body">
          <div class="section-title" style="margin:0">Call result</div>
          <pre class="json">{{ json(step.result) }}</pre>
          <div>
            <div class="section-title">Journal entry</div>
            <div class="kv panel" style="padding:12px">
              <span class="k">entry</span><span class="v">{{ step.id }}</span>
              <span class="k">time</span><span class="v">{{ step.at }}</span>
              <span class="k">job</span><span class="v">{{ step.job }}</span>
              <span class="k">worker · cap</span><span class="v">{{ step.name }} <span class="dim2">{{ step.worker }}</span> › <span style="color:var(--accent)">{{ step.cap }}</span></span>
              <span class="k">why</span><span class="v" style="font-family:var(--sans);font-size:12px">{{ step.why }}</span>
              <span class="k">run</span><span class="v">{{ step.run }}</span>
            </div>
          </div>
        </div>
      </aside>
    </template>
  </div>`,
};
