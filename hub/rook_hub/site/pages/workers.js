/* The Workers page (P4, P7, B2, B5, R2, R4, W6): workers grouped by band, with a
   group for roaming ones; the stats strip; pending approvals; unrecognised
   networks; band rename; a worker expanded to its facts, health, caps and open
   streams. It asks the hub again every few seconds, so it stays up to date. */
import { api, toast, viewHref, showView } from '/site/shell.js';

const REFRESH_MS = 5000;

function bytes(n) {
  if (typeof n !== 'number') return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
}

export default {
  data() {
    const view = new URLSearchParams(location.search);
    return {
      data: null, error: '', q: view.get('q') || '', only: view.get('worker') || '', open: {},
      renaming: {}, rename: {}, newBand: {}, addTo: {}, timer: null,
    };
  },
  computed: {
    view() { return { q: this.q, worker: this.only }; },
    href() { return viewHref('/workers', this.view); },
    onlyName() {
      const w = this.allWorkers.find(x => x.moniker === this.only);
      return w ? w.name : this.only;
    },
    pinLabel() { return this.only ? 'Workers: ' + this.onlyName : this.q ? 'Workers: ' + this.q : 'Workers'; },
    allWorkers() { return this.data ? this.data.groups.flatMap(g => g.workers) : []; },
    groups() {
      if (!this.data) return [];
      return this.data.groups
        .map(g => ({ ...g, shown: g.workers.filter(this.match) }))
        .filter(g => g.shown.length || (g.band && !this.only && !this.q));
    },
    bands() { return this.data ? this.data.groups.filter(g => g.band).map(g => g.band) : []; },
  },
  watch: { view: { handler() { showView('/workers', this.view); }, deep: true } },
  mounted() {
    if (this.only) this.open[this.only] = true;
    this.load();
    this.timer = setInterval(this.load, REFRESH_MS);
  },
  unmounted() { clearInterval(this.timer); },
  methods: {
    async load() {
      try { this.data = await api('GET', '/api/workers'); this.error = ''; } catch (e) { this.error = e.message; return; }
      for (const gw of this.data.unrecognised) if (!(gw.gateway_mac in this.addTo)) this.addTo[gw.gateway_mac] = '';
    },
    match(w) {
      if (this.only) return w.moniker === this.only;
      const q = this.q.trim().toLowerCase();
      if (!q) return true;
      return [w.name, w.moniker, w.owner.name, w.owner.moniker, ...w.caps.map(c => c.name)]
        .some(s => (s || '').toLowerCase().includes(q));
    },
    toggle(moniker) { this.open[moniker] = !this.open[moniker]; },
    async act(request, done) {
      try { await request(); toast(done); await this.load(); } catch (e) { toast(e.message); }
    },
    approve(k) { return this.act(() => api('POST', '/api/approve', { key: k.key }), 'Approved ' + k.name); },
    saveName(band) {
      const name = (this.rename[band.moniker] || '').trim();
      if (!name) { this.renaming[band.moniker] = false; return; }
      return this.act(() => api('POST', `/api/bands/${band.moniker}/name`, { name }), 'Renamed to ' + name)
        .then(() => { this.renaming[band.moniker] = false; });
    },
    nameBand(gw) {
      const name = (this.newBand[gw.gateway_mac] || '').trim();
      if (!name) { toast('Give the new band a name.'); return; }
      return this.act(() => api('POST', '/api/bands', { name, gateway_mac: gw.gateway_mac }), 'Named ' + name);
    },
    addGateway(gw) {
      const band = this.addTo[gw.gateway_mac];
      if (!band) { toast('Choose a band to add it to.'); return; }
      return this.act(() => api('POST', `/api/bands/${band}/gateways`, { gateway_mac: gw.gateway_mac }), 'Added to the band');
    },
    rowMenu(w) { return [{ label: 'Pin this worker', pin: { label: 'Workers: ' + w.name, href: viewHref('/workers', { worker: w.moniker }) } }]; },
    ago(iso) {
      if (!iso) return 'never';
      const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
      return s < 60 ? s + ' s ago' : s < 3600 ? Math.round(s / 60) + ' min ago' : s < 86400 ? Math.round(s / 3600) + ' h ago' : Math.round(s / 86400) + ' d ago';
    },
    health(h) {
      const size = v => v && typeof v === 'object' ? `${bytes(v.free)} free of ${bytes(v.total)}` : null;
      const pct = v => typeof v === 'number' ? v + '%' : null;
      return [['CPU', pct(h.cpu)], ['RAM', size(h.ram)], ['Disk', size(h.disk)], ['GPU', pct(h.gpu)], ['VRAM', size(h.vram)],
        ['NPU', pct(h.npu)], ['Battery', pct(h.battery)], ['Temperature', typeof h.temperature === 'number' ? h.temperature + ' °C' : null],
        ['Free heap', typeof h.heap_free === 'number' ? bytes(h.heap_free) : null],
        ['Up', typeof h.uptime === 'number' ? this.ago(new Date(Date.now() - h.uptime * 1000).toISOString()).replace(' ago', '') : null]]
        .filter(([, v]) => v);
    },
    facts(f) {
      return [['Hostname', f.hostname], ['System', [f.os, f.arch].filter(Boolean).join(' · ')], ['Form', f.form],
        ['Version', f.version], ['MACs', (f.macs || []).join(', ')]].filter(([, v]) => v);
    },
  },
  template: `<div>
    <header class="topbar">
      <div>
        <div class="title-row"><h1>Workers</h1><pin-view :label="pinLabel" :href="href"></pin-view></div>
        <p v-if="data">{{ data.stats.online }} online · {{ data.stats.pending }} pending · grouped by band</p>
      </div>
      <div class="spacer"></div>
      <div class="filter-row">
        <input class="mono" type="search" placeholder="filter by name, cap, owner…" v-model="q" style="width:220px" aria-label="Filter workers">
      </div>
    </header>

    <div class="wrap">
      <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
      <template v-if="data">
      <section class="stats" aria-label="At a glance">
        <div class="tile mini online"><div class="k">{{ data.stats.online }}</div><div class="l">workers online</div></div>
        <div class="tile mini pend"><div class="k">{{ data.stats.pending }}</div><div class="l">pending</div></div>
      </section>

      <div v-if="only" class="one-banner">Showing one worker: <b>{{ onlyName }}</b><span style="flex:1"></span><button class="btn sm" type="button" @click="only=''">show all workers</button></div>

      <section v-if="!only && data.pending.length" class="panel pending" aria-labelledby="pending-h">
        <div class="panel-head"><h2 id="pending-h">Pending</h2><span class="sub">workers asking to join this hub</span>
          <div class="spacer"></div><span class="badge warn">awaiting approval</span></div>
        <div v-for="k in data.pending" :key="k.key" class="pending-item">
          <div><div class="name">{{ k.name }}</div>
            <div class="facts">knocking since {{ ago(k.first_seen) }}<br>key {{ k.key.slice(0, 16) }}…<br><template v-if="k.macs.length">MAC {{ k.macs.join(', ') }}</template></div></div>
          <div><div class="section-title">Offered caps (read-only)</div>
            <div class="chips"><span v-for="c in k.caps" :key="c.name" class="chip ro" :title="c.description">{{ c.name }}</span></div>
            <p v-if="!k.caps.length" class="note" style="margin:0">It offers no caps.</p></div>
          <div><button class="btn primary" type="button" @click="approve(k)">Approve</button></div>
        </div>
      </section>

      <section v-if="!only && data.unrecognised.length" class="panel" aria-labelledby="unrec-h">
        <div class="panel-head"><h2 id="unrec-h">Unrecognised networks</h2><span class="sub">gateways workers report that no band holds</span></div>
        <div v-for="gw in data.unrecognised" :key="gw.gateway_mac" class="pending-item">
          <div><div class="name mono">{{ gw.gateway_mac }}</div>
            <div class="facts">{{ gw.workers.map(w => w.name).join(', ') }}</div></div>
          <div class="gw-actions">
            <label class="field">Name it as a new band<span class="gw-row"><input class="mono" v-model="newBand[gw.gateway_mac]" placeholder="band name" :aria-label="'New band for ' + gw.gateway_mac">
              <button class="btn accent" type="button" @click="nameBand(gw)">Name band</button></span></label>
            <label v-if="bands.length" class="field">Or add it to a band<span class="gw-row"><select class="mono" v-model="addTo[gw.gateway_mac]" :aria-label="'Band for ' + gw.gateway_mac">
                <option value="" disabled>choose a band</option><option v-for="b in bands" :key="b.moniker" :value="b.moniker">{{ b.name }}</option></select>
              <button class="btn" type="button" @click="addGateway(gw)">Add to band</button></span></label>
          </div>
          <div></div>
        </div>
      </section>

      <section v-for="g in groups" :key="g.band ? g.band.moniker : 'roaming'" class="panel">
        <div class="band-head">
          <template v-if="g.band && renaming[g.band.moniker]">
            <input v-model="rename[g.band.moniker]" @keydown.enter="saveName(g.band)" aria-label="Band name">
            <button class="btn sm accent" type="button" @click="saveName(g.band)">Save</button>
            <button class="btn sm ghost" type="button" @click="renaming[g.band.moniker]=false">Cancel</button>
          </template>
          <h2 v-else>{{ g.band ? g.band.name : 'Roaming' }}</h2>
          <span class="count">{{ g.shown.length }} shown · {{ g.band ? g.band.gateways.join(', ') : 'on networks no band holds, or none' }}</span>
          <div class="spacer"></div>
          <button v-if="g.band && !renaming[g.band.moniker]" class="btn sm ghost" type="button"
            @click="rename[g.band.moniker]=g.band.name; renaming[g.band.moniker]=true">✎ rename</button>
        </div>
        <div class="wcols wsite wlist-head" aria-hidden="true"><span></span><span></span><span>name · moniker</span><span>owner</span><span>last heartbeat</span><span></span></div>
        <p v-if="!g.shown.length" class="note" style="margin:0;padding:10px 14px">No workers on this band right now.</p>
        <template v-for="w in g.shown" :key="w.moniker">
          <div class="wcols wsite wrow" :class="{open: open[w.moniker]}" @click="toggle(w.moniker)" role="button" tabindex="0"
               :aria-expanded="!!open[w.moniker]" @keydown.enter="toggle(w.moniker)">
            <span class="caret">{{ open[w.moniker] ? '▾' : '▸' }}</span>
            <span class="sdot" :class="{off: !w.online}" :title="w.online ? 'online' : 'offline'"></span>
            <div class="wname"><span class="n">{{ w.name }}<span v-if="!w.online" class="badge plain">offline</span></span><span class="m">{{ w.moniker }}</span></div>
            <span class="owner" :title="w.owner.moniker">{{ w.owner.name }}</span>
            <span class="owner">{{ ago(w.last_heartbeat) }}</span>
            <row-menu :items="rowMenu(w)"></row-menu>
          </div>
          <div v-if="open[w.moniker]" class="wdetail">
            <div class="dblock"><h4>Runs on</h4>
              <div class="kv"><template v-for="[k, v] in facts(w.facts)" :key="k"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></template></div>
              <p v-if="!facts(w.facts).length" class="note" style="margin:0">It has not reported yet.</p></div>
            <div class="dblock"><h4>Health <span class="dim2">latest heartbeat</span></h4>
              <div class="kv"><template v-for="[k, v] in health(w.health)" :key="k"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></template></div>
              <p v-if="!health(w.health).length" class="note" style="margin:0">No health reported.</p></div>
            <div class="dblock"><h4>Caps <span class="dim2">{{ w.caps.length }} served</span></h4>
              <div v-for="c in w.caps" :key="c.name" class="capline"><code>{{ c.name }}</code><span v-if="c.returns==='stream'" class="badge plain">stream</span><span class="note">{{ c.description }}</span></div>
              <p v-if="!w.caps.length" class="note" style="margin:0">No caps.</p></div>
            <div class="dblock"><h4>Open streams <span class="dim2">{{ w.streams.length }} open</span></h4>
              <div v-for="s in w.streams" :key="s.moniker" class="stream"><span class="k">{{ s.moniker }}</span><div><b>{{ s.title }}</b><p>{{ s.description }}</p></div><span></span></div>
              <p v-if="!w.streams.length" class="note" style="margin:0">Nothing open.</p></div>
          </div>
        </template>
      </section>
      </template>
    </div>
  </div>`,
};
