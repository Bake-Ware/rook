/* The Workers page (P4, P7, B2, B5, R2, R4, W6, D1, D4, D6, D8): workers and devices
   grouped by band, with a group for roaming ones; a filter for workers, devices or
   both; the stats strip; pending approvals; unrecognised networks; band rename; a
   worker expanded to its facts, health, caps, streams, the devices pinned to it and
   whether it serves devices; a device expanded to its driver, route, latest status
   and caps. A stream on the card opens in place as a terminal (P8), its closed
   streams on request (S5); a view may hold one stream open (P6). It asks the hub
   again every few seconds, so it stays up to date. */
import { api, toast, viewHref, showView } from '/site/shell.js';
import StreamTerminal from '/site/terminal.js';

const REFRESH_MS = 5000;

function bytes(n) {
  if (typeof n !== 'number') return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)) + ' ' + units[i];
}

export default {
  components: { 'stream-terminal': StreamTerminal },
  data() {
    const view = new URLSearchParams(location.search);
    return {
      data: null, error: '', q: view.get('q') || '', only: view.get('worker') || '', open: {},
      show: ['workers', 'devices'].includes(view.get('show')) ? view.get('show') : 'all',
      renaming: {}, rename: {}, newBand: {}, addTo: {}, timer: null,
      attached: {}, closedOf: {}, focus: view.get('stream') || '', focusStream: null,
    };
  },
  computed: {
    view() { return { q: this.q, worker: this.only, show: this.show === 'all' ? '' : this.show, stream: this.focus }; },
    href() { return viewHref('/workers', this.view); },
    onlyName() {
      const w = [...this.allWorkers, ...this.allDevices].find(x => x.moniker === this.only);
      return w ? w.name : this.only;
    },
    pinLabel() {
      if (this.focus && this.focusStream) return 'Stream: ' + this.focusStream.title;
      if (this.only) return 'Workers: ' + this.onlyName;
      return 'Workers' + (this.show !== 'all' || this.q ? ': ' : '') + [this.show !== 'all' ? this.show : '', this.q].filter(Boolean).join(' · ');
    },
    allWorkers() { return this.data ? this.data.groups.flatMap(g => g.workers) : []; },
    allDevices() { return this.data ? this.data.groups.flatMap(g => g.devices) : []; },
    groups() {
      if (!this.data) return [];
      return this.data.groups
        .map(g => ({ ...g, shown: this.rows(g) }))
        .filter(g => g.shown.length || (g.band && !this.only && !this.q && this.show === 'all'));
    },
    bands() { return this.data ? this.data.groups.filter(g => g.band).map(g => g.band) : []; },
  },
  watch: { view: { handler() { showView('/workers', this.view); }, deep: true } },
  mounted() {
    if (this.only) this.open[this.only] = true;
    if (this.focus) this.openStream(this.focus);
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
      return [w.name, w.moniker, w.owner.name, w.owner.moniker, w.driver, ...w.caps.map(c => c.name)]
        .some(s => (s || '').toLowerCase().includes(q));
    },
    /* A band's rows: each worker followed by the devices pinned to it, then its other devices (P4). */
    rows(g) {
      const workers = this.show === 'devices' ? [] : g.workers.filter(this.match);
      const devices = this.show === 'workers' ? [] : g.devices.filter(this.match);
      const out = [];
      for (const w of workers) {
        out.push({ w, child: false });
        devices.filter(d => d.route.via === w.moniker).forEach(d => out.push({ w: d, child: true }));
      }
      devices.filter(d => !out.some(r => r.w === d)).forEach(d => out.push({ w: d, child: false }));
      return out;
    },
    count(show) {
      return (show === 'devices' ? 0 : this.allWorkers.length) + (show === 'workers' ? 0 : this.allDevices.length);
    },
    pinnedTo(w) { return this.allDevices.filter(d => d.route.via === w.moniker); },
    route(d) { return d.route.kind === 'pinned' ? 'via ' + this.nameOf(d.route.via) : d.route.kind; },
    nameOf(moniker) { const w = this.allWorkers.find(x => x.moniker === moniker); return w ? w.name : moniker; },
    reach(d) { return d.reachable === null ? 'not polled yet' : d.reachable ? 'reachable' : 'unreachable'; },
    serve(w, serves) {
      return this.act(() => api('POST', `/api/workers/${w.moniker}/serves-devices`, { serves }),
        serves ? w.name + ' serves devices again' : w.name + ' no longer serves devices');
    },
    status(s) {
      if (s === null || typeof s !== 'object' || Array.isArray(s)) return null;
      const pairs = Object.entries(s);
      return pairs.every(([, v]) => v === null || typeof v !== 'object') ? pairs.map(([k, v]) => [k, String(v)]) : null;
    },
    json(value) { return JSON.stringify(value, null, 2); },
    toggle(moniker) { this.open[moniker] = !this.open[moniker]; },
    /* A pinned view with a stream open: its worker expanded, the stream attached (P6, P8). */
    async openStream(moniker) {
      try {
        const { stream } = await api('GET', '/api/streams/' + encodeURIComponent(moniker));
        this.open[stream.worker] = true;
        if (!stream.open) await this.loadClosed(stream.worker);
        this.attach(stream);
      } catch (e) { toast(e.message); this.focus = ''; }
    },
    /* A worker's streams on its card: the open ones, then the closed ones once asked for (S5). */
    streamsOf(w) {
      const open = new Set(w.streams.map(s => s.moniker));
      return [...w.streams, ...(this.closedOf[w.moniker] || []).filter(s => !open.has(s.moniker))];
    },
    async loadClosed(worker) {
      this.closedOf[worker] = (await api('GET', `/api/streams?worker=${encodeURIComponent(worker)}&open=false`)).streams;
    },
    toggleClosed(w) {
      if (this.closedOf[w.moniker]) { delete this.closedOf[w.moniker]; return; }
      this.loadClosed(w.moniker).catch(e => toast(e.message));
    },
    attach(s) { this.attached[s.moniker] = true; this.focus = s.moniker; this.focusStream = s; },
    detach(s) {
      delete this.attached[s.moniker];
      if (this.focus === s.moniker) { this.focus = ''; this.focusStream = null; }
    },
    /* An attached stream closed: keep it on the card, read-only, among the closed ones. */
    closedNow(w, s) {
      this.closedOf[w.moniker] = [{ ...s, open: false }, ...(this.closedOf[w.moniker] || []).filter(x => x.moniker !== s.moniker)];
      this.loadClosed(w.moniker).catch(() => {});
    },
    streamMenu(w, s) {
      return [{ label: 'Pin this stream', pin: { label: 'Stream: ' + s.title, href: viewHref('/workers', { worker: w.moniker, stream: s.moniker }) } }];
    },
    who(s) { return s.opened_by ? s.opened_by.name : 'the hub'; },
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
    rowMenu(w) { return [{ label: w.device ? 'Pin this device' : 'Pin this worker', pin: { label: 'Workers: ' + w.name, href: viewHref('/workers', { worker: w.moniker }) } }]; },
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
        <p v-if="data">{{ data.stats.online }} online · {{ data.stats.pending }} pending · {{ data.stats.devices }} devices · grouped by band</p>
      </div>
      <div class="spacer"></div>
      <div class="filter-row">
        <input class="mono" type="search" placeholder="filter by name, cap, owner…" v-model="q" style="width:220px" aria-label="Filter workers">
        <div v-if="data" class="chips" role="group" aria-label="Show">
          <button v-for="f in ['all', 'workers', 'devices']" :key="f" class="chip" type="button" :aria-pressed="show===f" @click="show=f">{{ f }} <span class="n">{{ count(f) }}</span></button>
        </div>
      </div>
    </header>

    <div class="wrap">
      <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
      <template v-if="data">
      <section class="stats" aria-label="At a glance">
        <div class="tile mini online"><div class="k">{{ data.stats.online }}</div><div class="l">workers online</div></div>
        <div class="tile mini pend"><div class="k">{{ data.stats.pending }}</div><div class="l">pending</div></div>
        <div class="tile mini dev"><div class="k">{{ data.stats.devices }}</div><div class="l">devices</div></div>
      </section>

      <div v-if="only" class="one-banner">Showing one: <b>{{ onlyName }}</b><span style="flex:1"></span><button class="btn sm" type="button" @click="only=''">show all workers</button></div>

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
        <p v-if="!g.shown.length" class="note" style="margin:0;padding:10px 14px">Nothing on this band right now.</p>
        <template v-for="{ w, child } in g.shown" :key="w.moniker">
          <div v-if="w.device" class="wcols wsite wrow" :class="{open: open[w.moniker], child}" @click="toggle(w.moniker)" role="button" tabindex="0"
               :aria-expanded="!!open[w.moniker]" @keydown.enter="toggle(w.moniker)">
            <span class="caret">{{ open[w.moniker] ? '▾' : '▸' }}</span>
            <span class="sdot" :class="{off: !w.reachable}" :title="reach(w)"></span>
            <div class="wname"><span class="n">{{ w.name }} <span class="via">{{ route(w) }}</span><span class="badge plain">device · {{ w.driver }}</span><span v-if="w.reachable === false" class="badge bad">unreachable</span></span><span class="m">{{ w.moniker }}</span></div>
            <span class="owner" :title="w.owner.moniker">{{ w.owner.name }}</span>
            <span class="owner" :title="reach(w)">{{ w.polled_at ? 'polled ' + ago(w.polled_at) : 'not polled' }}</span>
            <row-menu :items="rowMenu(w)"></row-menu>
          </div>
          <div v-if="w.device && open[w.moniker]" class="wdetail">
            <div class="dblock"><h4>Device</h4>
              <div class="kv"><span class="k">Driver</span><span class="v">{{ w.driver }}</span>
                <span class="k">Route</span><span class="v">{{ w.route.kind }}<template v-if="w.route.via"> · through {{ nameOf(w.route.via) }} ({{ w.route.via }})</template></span>
                <span class="k">Band</span><span class="v">{{ g.band ? g.band.name : 'none' }}</span>
                <span class="k">Status poll</span><span class="v">{{ w.interval ? 'every ' + w.interval + ' s' : 'not polled' }}</span>
                <span class="k">Last poll</span><span class="v">{{ w.polled_at ? ago(w.polled_at) + ' · ' + reach(w) : 'never' }}</span></div></div>
            <div class="dblock"><h4>Status <span class="dim2">latest poll</span></h4>
              <div v-if="status(w.status)" class="kv"><template v-for="[k, v] in status(w.status)" :key="k"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></template></div>
              <pre v-else-if="w.status !== null" class="json">{{ json(w.status) }}</pre>
              <p v-else class="note" style="margin:0">No status yet.</p></div>
            <div class="dblock"><h4>Caps <span class="dim2">{{ w.caps.length }} from driver {{ w.driver }}</span></h4>
              <div v-for="c in w.caps" :key="c.name" class="capline"><code>{{ c.name }}</code><span class="note">{{ c.description }}</span></div>
              <p v-if="!w.caps.length" class="note" style="margin:0">No caps: no installed plugin declares driver {{ w.driver }}.</p></div>
          </div>
          <div v-if="!w.device" class="wcols wsite wrow" :class="{open: open[w.moniker]}" @click="toggle(w.moniker)" role="button" tabindex="0"
               :aria-expanded="!!open[w.moniker]" @keydown.enter="toggle(w.moniker)">
            <span class="caret">{{ open[w.moniker] ? '▾' : '▸' }}</span>
            <span class="sdot" :class="{off: !w.online}" :title="w.online ? 'online' : 'offline'"></span>
            <div class="wname"><span class="n">{{ w.name }}<span v-if="!w.online" class="badge plain">offline</span><span v-if="!w.serves_devices" class="badge plain" title="opted out of serving devices">no devices</span></span><span class="m">{{ w.moniker }}</span></div>
            <span class="owner" :title="w.owner.moniker">{{ w.owner.name }}</span>
            <span class="owner">{{ ago(w.last_heartbeat) }}</span>
            <row-menu :items="rowMenu(w)"></row-menu>
          </div>
          <div v-if="!w.device && open[w.moniker]" class="wdetail">
            <div class="dblock"><h4>Runs on</h4>
              <div class="kv"><template v-for="[k, v] in facts(w.facts)" :key="k"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></template></div>
              <p v-if="!facts(w.facts).length" class="note" style="margin:0">It has not reported yet.</p></div>
            <div class="dblock"><h4>Health <span class="dim2">latest heartbeat</span></h4>
              <div class="kv"><template v-for="[k, v] in health(w.health)" :key="k"><span class="k">{{ k }}</span><span class="v">{{ v }}</span></template></div>
              <p v-if="!health(w.health).length" class="note" style="margin:0">No health reported.</p></div>
            <div class="dblock"><h4>Caps <span class="dim2">{{ w.caps.length }} served</span></h4>
              <div v-for="c in w.caps" :key="c.name" class="capline"><code>{{ c.name }}</code><span v-if="c.returns==='stream'" class="badge plain">stream</span><span class="note">{{ c.description }}</span></div>
              <p v-if="!w.caps.length" class="note" style="margin:0">No caps.</p></div>
            <div class="dblock" :class="{full: streamsOf(w).some(s => attached[s.moniker])}"><h4>Streams <span class="dim2">{{ w.streams.length }} open</span></h4>
              <div v-for="s in streamsOf(w)" :key="s.moniker" class="stream" :class="{attached: attached[s.moniker]}">
                <span class="k">{{ s.moniker }}</span>
                <div><b>{{ s.title }}<span v-if="!s.open" class="badge plain" style="margin-left:6px">closed</span></b><p>{{ s.description }}</p>
                  <p class="dim2 mono small">opened by {{ who(s) }} {{ ago(s.opened_at) }}<template v-if="!s.open && s.closed_at"> · closed by {{ s.closed_by === 'worker' ? 'the worker' : s.closed_by ? s.closed_by.name : 'someone' }} {{ ago(s.closed_at) }}</template></p></div>
                <span style="display:flex;gap:4px;align-items:start">
                  <button class="btn sm" :class="{accent: attached[s.moniker]}" type="button" @click.stop="attached[s.moniker] ? detach(s) : attach(s)">{{ attached[s.moniker] ? 'detach' : s.open ? 'attach' : 'open read-only' }}</button>
                  <row-menu :items="streamMenu(w, s)" label="Stream actions"></row-menu></span>
                <stream-terminal v-if="attached[s.moniker]" :stream="s" :worker="w.name" @detach="detach(s)" @closed="closedNow(w, s)"></stream-terminal>
              </div>
              <p v-if="!w.streams.length" class="note" style="margin:0">Nothing open.</p>
              <button class="btn sm ghost" type="button" style="margin-top:6px" @click.stop="toggleClosed(w)">{{ closedOf[w.moniker] ? 'hide closed streams' : 'show closed streams' }}</button>
              <p v-if="closedOf[w.moniker] && !closedOf[w.moniker].length" class="note" style="margin:0">No closed streams.</p></div>
            <div class="dblock full"><h4>Devices <span class="dim2">{{ pinnedTo(w).length }} pinned to {{ w.name }}</span></h4>
              <div class="optout"><div class="p">Serves devices<small>Carries out network steps for devices: those pinned to it, and floating ones on its band.</small></div>
                <span class="state" :class="{out: !w.serves_devices}">{{ w.serves_devices ? 'allowed' : 'opted out' }}</span>
                <button class="toggle" type="button" :class="{on: w.serves_devices}" :aria-pressed="w.serves_devices" :aria-label="'Serve devices from ' + w.name"
                  @click.stop="serve(w, !w.serves_devices)"></button></div>
              <div v-for="d in pinnedTo(w)" :key="d.moniker" class="devcard" style="margin-top:8px">
                <span class="ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M4 5h16v11H4zM9 20h6M12 16v4"/></svg></span>
                <div><b>{{ d.name }}</b> <span class="dim2 mono small">· {{ d.moniker }}</span><br><span class="note">driver {{ d.driver }} · {{ d.caps.length }} caps</span></div>
                <span class="badge" :class="{good: d.reachable, bad: d.reachable === false}">{{ reach(d) }}</span>
              </div></div>
          </div>
        </template>
      </section>
      </template>
    </div>
  </div>`,
};
