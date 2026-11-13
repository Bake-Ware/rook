/* The Settings page (P4): one tab per section installed at the hub (GET /api/settings), each
   section an ES module whose default export is a Vue component, loaded as the shell loads
   pages. Core installs one section, Plugins; a plugin's sections follow, added the same way (PL1). */
import { api, viewHref, showView } from '/site/shell.js';

const { markRaw } = Vue;

export default {
  data() {
    return { sections: [], section: new URLSearchParams(location.search).get('section') || '', component: null, error: '' };
  },
  computed: {
    current() { return this.sections.find(s => s.id === this.section); },
    view() { return { section: this.section }; },
    href() { return viewHref('/settings', this.view); },
    pinLabel() { return 'Settings: ' + (this.current ? this.current.label : this.section); },
  },
  watch: { section() { showView('/settings', this.view); this.show(); } },
  async mounted() {
    try { this.sections = (await api('GET', '/api/settings')).sections; } catch (e) { this.error = e.message; return; }
    if (!this.current && this.sections.length) this.section = this.sections[0].id; else this.show();
  },
  methods: {
    async show() {
      if (!this.current) return;
      try { this.component = markRaw((await import(this.current.script)).default); this.error = ''; }
      catch (e) {
        this.component = null;
        this.error = (this.current.plugin ? `Plugin ${this.current.plugin}'s section ${this.current.label} failed to load: ` : '') + e.message;
      }
    },
  },
  template: `<div>
    <header class="topbar">
      <div><div class="title-row"><h1>Settings</h1><pin-view :label="pinLabel" :href="href"></pin-view></div><p>The hub’s settings, one section to a tab.</p></div>
    </header>
    <div class="wrap">
      <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
      <div class="tabs" role="tablist">
        <button v-for="s in sections" :key="s.id" role="tab" type="button" :class="{on: section===s.id}" :aria-selected="section===s.id" @click="section=s.id">{{ s.label }}</button>
      </div>
      <component v-if="component" :is="component" :key="section"></component>
    </div>
  </div>`,
};
