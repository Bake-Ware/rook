/* The Workers page's graphs and its 3D view, ported from 0.4 (P4, P7, H1, H4, H5):
   - HeartbeatsTile: one bar per worker that drains over a minute from its latest
     heartbeat and flashes when a new one arrives, with a peak that falls slowly;
     a click finds the worker. Its data is the hub's heartbeat history.
   - HealthCharts: a worker's health over the last hour, six hours or day, one chart
     per field it reports, from the same history, grouped by the hub.
   - FleetView: the 0.4 3D scene (vendor/rook-scene.js), the hub as a rook and the
     workers and devices as pawns around it, grouped by band. */
import { api } from '/site/shell.js';

export const HB_WINDOW = 60;   // seconds over which a heartbeat bar drains, as in 0.4

const colour = f => f > 0.5 ? 'var(--good)' : f > 0.22 ? 'var(--warn)' : 'var(--bad)';

function bytes(n) {
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
}

/* The heartbeats graph. `beats` is GET /api/heartbeats with the browser time it came back
   (`at`), so ages follow the hub's clock; `order` is the workers as the page lists them. */
export const HeartbeatsTile = {
  props: { beats: Object, order: Array },
  emits: ['pick'],
  data() { return { t: Date.now() }; },
  created() { this.peaks = {}; this.flash = {}; this.seen = {}; },   // bookkeeping, not shown: kept out of reactivity
  mounted() { this.timer = setInterval(() => { this.t = Date.now(); }, 120); },
  unmounted() { clearInterval(this.timer); },
  watch: {
    beats() {                         // a newer heartbeat than last time: the bar flashes
      for (const w of this.beats.workers) {
        if (this.seen[w.moniker] && w.last_heartbeat && w.last_heartbeat !== this.seen[w.moniker]) this.flash[w.moniker] = Date.now();
        this.seen[w.moniker] = w.last_heartbeat;
      }
    },
  },
  computed: {
    bars() {
      if (!this.beats) return [];
      const now = Date.parse(this.beats.now) / 1000 + (this.t - this.beats.at) / 1000;
      const byMoniker = Object.fromEntries(this.beats.workers.map(w => [w.moniker, w]));
      return this.order.filter(o => byMoniker[o.moniker]).map(o => {
        const w = byMoniker[o.moniker];
        const age = w.last_heartbeat ? Math.max(0, now - Date.parse(w.last_heartbeat) / 1000) : Infinity;
        const f = Math.max(0, 1 - age / HB_WINDOW);
        const p = this.peaks[o.moniker] = Math.max(f, (this.peaks[o.moniker] ?? f) - 0.0045);
        const n = w.counts.reduce((s, c) => s + c.count, 0);
        return { moniker: o.moniker, name: o.name, f, p, beat: this.t - (this.flash[o.moniker] || 0) < 350,
          title: o.name + ' · ' + (age === Infinity ? 'no heartbeat yet' : 'last heartbeat ' + (age > 3600 ? 'over an hour' : Math.round(age) + ' s') + ' ago')
            + ' · ' + n + ' in the last minute' };
      });
    },
  },
  methods: { colour },
  template: `<div class="tile hb">
    <div class="l"><span>heartbeats</span><span class="hint">drains over {{ ${HB_WINDOW} }} s · click a bar to find the worker</span></div>
    <div class="bars">
      <div v-for="b in bars" :key="b.moniker" class="bar" :class="{beat: b.beat}" :style="{'--c': colour(b.f)}" :title="b.title" @click="$emit('pick', b.moniker)">
        <span class="fill" :style="{height: Math.max(3, b.f * 100) + '%'}"></span>
        <span class="peak" :style="{bottom: Math.max(4, b.p * 100) + '%', background: colour(b.p)}"></span>
      </div>
    </div>
    <div class="bar-names" aria-hidden="true"><span v-for="b in bars" :key="b.moniker">{{ b.name }}</span></div>
  </div>`,
};

/* What each health field is called, its unit, and how to show a value (H4). */
const FIELDS = {
  cpu: { label: 'CPU', unit: '%' }, ram: { label: 'RAM', unit: '%' }, disk: { label: 'Disk', unit: '%' },
  gpu: { label: 'GPU', unit: '%' }, vram: { label: 'VRAM', unit: '%' }, npu: { label: 'NPU', unit: '%' },
  battery: { label: 'Battery', unit: '%' }, temperature: { label: 'Temperature', unit: '°C' },
  heap_free: { label: 'Free heap', unit: 'B' }, uptime: { label: 'Uptime', unit: 'h' },
};
const SPANS = [{ s: 3600, label: '1 h' }, { s: 21600, label: '6 h' }, { s: 86400, label: '24 h' }];
const show = (v, unit) => unit === 'B' ? bytes(v) : unit === 'h' ? (v >= 172800 ? (v / 86400).toFixed(1) + ' d' : (v / 3600).toFixed(1) + ' h')
  : Math.round(v) + (unit === '%' ? '%' : '°');

/* One field over the span: x is time from the span's start to now, so gaps stay gaps. */
const HealthChart = {
  props: { field: String, buckets: Array, since: Number, span: Number, step: Number, note: String },
  data() { return { hover: null, W: 240, H: 64 }; },
  computed: {
    unit() { return FIELDS[this.field].unit; },
    points() {
      return this.buckets.filter(b => b[this.field] != null).map(b => ({ at: Date.parse(b.at) / 1000, v: b[this.field] }));
    },
    max() {
      const peak = Math.max(...this.points.map(p => p.v));
      return this.unit === '%' ? 100 : this.unit === '°C' ? Math.max(100, peak) : peak * 1.15 || 1;
    },
    xy() { return this.points.map(p => [(p.at + this.step / 2 - this.since) / this.span * this.W, this.H - p.v / this.max * this.H]); },
    /* A line per run of buckets with no missing bucket between them. */
    runs() {
      const out = [];
      this.xy.forEach((xy, i) => {
        if (!i || this.points[i].at - this.points[i - 1].at > this.step * 1.5) out.push([]);
        out[out.length - 1].push(xy.map(c => c.toFixed(1)).join(','));
      });
      return out.map(run => run.length > 1 ? run.join(' ') : run[0] + ' ' + run[0]);
    },
    last() { return this.points[this.points.length - 1].v; },
    peak() { return Math.max(...this.points.map(p => p.v)); },
  },
  methods: {
    f(v) { return show(v, this.unit); },
    move(e) {
      const r = e.currentTarget.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * this.W;
      let best = 0;
      this.xy.forEach((p, i) => { if (Math.abs(p[0] - x) < Math.abs(this.xy[best][0] - x)) best = i; });
      this.hover = best;
    },
    ago(at) {
      const m = Math.round((this.since + this.span - at) / 60);
      return m <= 1 ? 'now' : m < 60 ? m + ' min ago' : (m / 60).toFixed(1).replace('.0', '') + ' h ago';
    },
  },
  template: `<div class="hchart">
    <!-- the layout is set here: inside a grid card, .wcard .top would otherwise apply -->
    <div class="top" style="display:flex;justify-content:space-between;align-items:baseline;gap:8px;cursor:default"><span>{{ $options.FIELDS[field].label }}</span><b>{{ f(last) }}<small v-if="field !== 'uptime'">peak {{ f(peak) }}</small></b></div>
    <svg :viewBox="'0 0 ' + W + ' ' + H" preserveAspectRatio="none" :height="H" @mousemove="move" @mouseleave="hover=null" role="img"
         :aria-label="$options.FIELDS[field].label + ', now ' + f(last)">
      <line x1="0" :x2="W" :y1="H*0.5" :y2="H*0.5" stroke="var(--line)" stroke-dasharray="2 3" vector-effect="non-scaling-stroke"/>
      <polyline v-for="(run, i) in runs" :key="i" :points="run" fill="none" stroke="var(--series)" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
      <line v-if="hover!=null" :x1="xy[hover][0]" :x2="xy[hover][0]" y1="0" :y2="H" stroke="var(--accent)" stroke-width="1" vector-effect="non-scaling-stroke"/>
    </svg>
    <div v-if="hover!=null" class="tip" :style="{left: (xy[hover][0] / W * 100) + '%'}">{{ f(points[hover].v) }} · {{ ago(points[hover].at) }}</div>
    <div class="axis"><span>{{ ago(since) }}</span><span>{{ note || '' }}</span><span>now</span></div>
  </div>`,
  FIELDS,
};

/* A worker's health charts: what it reports, over the chosen span (P4, H4). */
export const HealthCharts = {
  components: { 'health-chart': HealthChart },
  props: { worker: String },
  data() { return { span: 86400, series: null, error: '' }; },
  computed: {
    since() { return this.series ? Date.parse(this.series.now) / 1000 - this.span : 0; },
    count() { return this.series ? this.series.buckets.reduce((n, b) => n + b.count, 0) : 0; },
  },
  watch: { span() { this.load(); } },
  mounted() { this.load(); this.timer = setInterval(this.load, 30000); },
  unmounted() { clearInterval(this.timer); },
  methods: {
    async load() {
      try { this.series = await api('GET', `/api/heartbeats/${encodeURIComponent(this.worker)}?span=${this.span}`); this.error = ''; }
      catch (e) { this.error = e.message; }
    },
    note(field) {
      const total = this.series.totals[field];
      return total ? 'in use of ' + bytes(total) : '';
    },
  },
  SPANS,
  template: `<div class="dblock full"><h4>Health <span class="dim2" style="display:flex;gap:8px;align-items:center">
      <span v-if="series">{{ count }} heartbeats · {{ Math.round(series.step / 60) || 1 }} min a point</span>
      <span class="seg" role="group" aria-label="Health span"><button v-for="s in $options.SPANS" :key="s.s" type="button" :class="{on: span===s.s}" :aria-pressed="span===s.s" @click.stop="span=s.s">{{ s.label }}</button></span></span></h4>
    <p v-if="error" class="note" style="margin:0">{{ error }}</p>
    <template v-else-if="series">
      <div v-if="series.fields.length" class="charts-grid">
        <health-chart v-for="f in series.fields" :key="f" :field="f" :buckets="series.buckets" :since="since" :span="span" :step="series.step" :note="note(f)"></health-chart>
      </div>
      <p v-else-if="series.buckets.length" class="note" style="margin:0">It heartbeats but reports no health.</p>
      <p v-else class="note" style="margin:0">No heartbeats in this span.</p>
    </template>
  </div>`,
};

/* The 0.4 3D fleet view: the scene draws computers, phones and chips; devices go as chips. */
const KIND = { android: 'phone', esp32: 'chip', firmware: 'chip' };
export const FleetView = {
  props: { pieces: Array },          // [{id, name, group, kind, age, description, version, bandName, battery, caps}]
  emits: ['open'],
  mounted() {
    document.body.classList.add('fleet-on');
    import('/site/vendor/rook-scene.js').then(({ mountFleet }) => {
      if (this.gone) return;
      this.fleet = mountFleet(this.$refs.host, { quietAfter: HB_WINDOW, onOpen: id => this.$emit('open', id) });
      this.push(); this.fleet.enter(); this.fleet.setActive(true);
      this.timer = setInterval(this.push, 1000);
    }).catch(() => { this.$refs.host.dataset.mode = 'unavailable'; });
  },
  beforeUnmount() {
    this.gone = true;
    clearInterval(this.timer);
    if (this.fleet) this.fleet.destroy();
    document.body.classList.remove('fleet-on');
  },
  methods: {
    push() {
      this.fleet.update([{ id: 'hub', name: location.host, hub: true, kind: 'computer', version: '', age: 0, group: '' }, ...this.pieces]);
    },
  },
  KIND,
  template: `<div class="fleet-scene" ref="host" aria-label="3D view: the hub as a rook, workers and devices as pawns, grouped by band"></div>`,
};

export function sceneKind(w) { return w.device ? 'chip' : KIND[(w.facts || {}).form] || 'computer'; }
