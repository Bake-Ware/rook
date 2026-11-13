/* The Settings page's Plugins section (PL1, PL3, PL4): the installed plugins and what each
   added, and install from a folder on the hub's disk through the plugin.install hub cap.
   Before it installs it says what will happen, including that a plugin of the same name
   is replaced at once. It asks the hub again every few seconds. */
import { api, toast } from '/site/shell.js';
import { ago } from '/site/pages/parts.js';

const REFRESH_MS = 5000;

export default {
  data() { return { plugins: null, error: '', path: '', pending: null, outcome: null, busy: false, timer: null }; },
  mounted() { this.load(); this.timer = setInterval(this.load, REFRESH_MS); },
  unmounted() { clearInterval(this.timer); },
  methods: {
    async load() {
      try { this.plugins = (await api('GET', '/api/plugins')).plugins; this.error = ''; } catch (e) { this.error = e.message; }
    },
    refusal(e) { return e.data && e.data.code ? e.data : { message: e.message }; },
    /* First say what installing from this folder would do; the person then confirms. */
    async check() {
      this.busy = true; this.outcome = null; this.pending = null;
      try { this.pending = await api('POST', '/api/plugins/check', { path: this.path.trim() }); }
      catch (e) { this.outcome = { refused: this.refusal(e) }; }
      this.busy = false;
    },
    async install() {
      this.busy = true;
      try {
        const reply = await api('POST', '/api/plugins', { path: this.path.trim() });
        this.outcome = { installed: reply.installed, replaced: !!this.pending.replaces };
        toast((this.pending.replaces ? 'Replaced ' : 'Installed ') + reply.installed.name);
        this.path = '';
        await this.load();
      } catch (e) { this.outcome = { refused: this.refusal(e) }; }
      this.pending = null; this.busy = false;
    },
    adds(p) {
      return [...p.caps, ...p.record_types.map(t => 'record: ' + t), ...p.drivers.map(d => 'driver: ' + d),
        ...(p.pages || []).map(x => 'page: ' + x.segment + ' › ' + x.title), ...(p.settings_sections || []).map(t => 'settings: ' + t),
        ...(p.components || []).map(c => 'component: ' + c)];
    },
    /* Where the worker part loaded and where it failed, as the workers report them (PL3): each
       worker, its version of the part when it is not the installed one, and why it failed. */
    workers(p, list) {
      return list.map(w => w.worker + (w.version && w.version !== p.version ? ' (' + w.version + ')' : '')
        + (w.error ? ': ' + w.error : '')).join(', ');
    },
    ago,
  },
  template: `<div>
    <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
    <section class="panel">
      <div class="panel-head"><h2>Install from a folder</h2><span class="sub">a plugin folder on the hub’s disk, holding plugin.json</span></div>
      <form class="panel-body form-stack" @submit.prevent="check">
        <div style="display:flex;gap:8px;align-items:end">
          <label class="field" style="flex:1">Folder on the hub’s disk<input class="mono" v-model="path" placeholder="/srv/rook/plugins/notes" spellcheck="false" @input="pending=null"></label>
          <button class="btn accent" type="submit" :disabled="busy || !path.trim()">Install…</button>
        </div>
        <div v-if="pending" class="callout" :class="{bad: pending.replaces}" role="status">
          <template v-if="pending.replaces"><b>This replaces {{ pending.replaces.name }} {{ pending.replaces.version }} at once</b>
            with {{ pending.plugin.version }} from this folder: its code is swapped while the hub runs, with no restart.</template>
          <template v-else><b>Install {{ pending.plugin.name }} {{ pending.plugin.version }}.</b> It loads at once.</template>
          <div style="display:flex;gap:8px;margin-top:8px">
            <button class="btn primary" type="button" :disabled="busy" @click="install">{{ pending.replaces ? 'Replace it' : 'Install' }}</button>
            <button class="btn ghost" type="button" @click="pending=null">Cancel</button>
          </div>
        </div>
        <div v-if="outcome && outcome.refused" class="callout bad" role="alert"><b>{{ outcome.refused.message }}</b> {{ outcome.refused.next }}</div>
        <div v-if="outcome && outcome.installed" class="callout" role="status">
          <b>{{ outcome.replaced ? 'Replaced' : 'Installed' }} {{ outcome.installed.name }} {{ outcome.installed.version }}</b> · {{ outcome.installed.moniker }}.
          It adds {{ adds(outcome.installed).join(', ') || 'nothing yet' }}.</div>
      </form>
    </section>

    <section class="panel">
      <div class="panel-head"><h2>Installed plugins</h2><span class="sub">{{ plugins ? plugins.length : '' }} installed · each loaded at the hub</span></div>
      <div class="tbl" style="--cols:minmax(170px,1fr) 80px 110px minmax(220px,1.6fr) minmax(150px,.8fr)">
        <div class="th"><span>plugin</span><span>version</span><span>installed</span><span>adds</span><span>also has</span></div>
        <div v-for="p in plugins || []" :key="p.guid" class="tr">
          <span><span class="cell-title">{{ p.name }}</span><span class="cell-sub mono">{{ p.moniker }}</span></span>
          <span class="cell-mono">{{ p.version }}</span>
          <span class="cell-mono">{{ ago(p.installed_at) }}</span>
          <span class="chips"><span v-for="a in adds(p)" :key="a" class="chip">{{ a }}</span><span v-if="!adds(p).length" class="dim2 small">—</span></span>
          <span class="chips">
            <span v-for="part in ['worker', 'skill'].filter(x => p[x])" :key="part" class="badge plain">{{ part === 'skill' ? 'skill' : 'worker part' }}</span>
            <span v-if="p.loaded_on.length" class="small worker-line">loaded on {{ workers(p, p.loaded_on) }}</span>
            <span v-if="p.failed_on.length" class="small worker-line bad-text">failed on {{ workers(p, p.failed_on) }}</span>
            <span v-if="!p.worker && !p.skill" class="dim2 small">—</span></span>
        </div>
        <div v-if="plugins && !plugins.length" class="tr"><span class="note" style="grid-column:1/-1">No plugins installed.</span></div>
      </div>
    </section>
  </div>`,
};
