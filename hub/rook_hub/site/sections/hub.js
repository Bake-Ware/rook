/* The Settings page's Hub section ("Hubs have names"; P4 Settings, hub name): the hub's
   name, shown in the sidebar and the browser tab. Saving it shows at once. */
import { api, toast, store } from '/site/shell.js';

export default {
  data() { return { name: '', saved: null, error: '', busy: false }; },
  async mounted() {
    try { this.saved = (await api('GET', '/api/hub')).name; this.name = this.saved || ''; }
    catch (e) { this.error = e.message; }
  },
  methods: {
    async save() {
      this.busy = true;
      try {
        this.saved = (await api('POST', '/api/hub/name', { name: this.name })).name;
        this.name = this.saved; store.hub = { name: this.saved };
        toast('The hub is called ' + this.saved);
      } catch (e) { toast(e.message); }
      this.busy = false;
    },
  },
  template: `<div>
    <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
    <section class="panel">
      <div class="panel-head"><h2>Hub name</h2><span class="sub">{{ saved ? 'shown in the sidebar and the browser tab' : 'not named yet: the sidebar shows its address' }}</span></div>
      <form class="panel-body form-stack" @submit.prevent="save">
        <div style="display:flex;gap:8px;align-items:end">
          <label class="field" style="flex:1">Name<input v-model="name" placeholder="Home" spellcheck="false"></label>
          <button class="btn accent" type="submit" :disabled="busy || !name.trim() || name.trim() === saved">Save</button>
        </div>
      </form>
    </section>
  </div>`,
};
