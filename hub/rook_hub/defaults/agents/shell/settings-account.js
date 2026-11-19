/* Your account's look and feel (Rookie's sound, style, theme and the house agent's name; agent.look),
   on the Settings page's "Your account" tab (brief HA3). Ported from 0.5's shell/look.js, which drew
   the same pickers on 0.5's /account page; core has no account page, so it is a settings section,
   a Vue component as core's sections are. The same controls, words and calls. */
import { call } from '/site/shell.js';

const WHY = 'The person changed their look and feel.';

export default {
  data() { return { look: null, name: '', error: '' }; },
  async mounted() {
    try { this.look = await call('hub', 'agent.look', {}, WHY); this.name = this.look.name; }
    catch (e) { this.error = e.message; }
  },
  methods: {
    async change(args) {
      try { this.look = await call('hub', 'agent.look', args, WHY); this.name = this.look.name; this.error = ''; }
      catch (e) { this.error = e.message; }
    },
    rename() { if (this.name.trim()) this.change({ name: this.name.trim() }); },
    /* A still picture of Rookie (Rookie.bake): previews take the theme you have. */
    thumb(skin, theme) {
      if (!window.Rookie || !Rookie.bake || skin === 'none' || !Rookie.SKINS.includes(skin)) return '';
      const { canvas, manifest } = Rookie.bake({ skin, theme, scale: 2, rows: ['happy'] });
      const [w, h] = manifest.cell;
      const c = document.createElement('canvas');
      c.width = w; c.height = h;
      c.getContext('2d').drawImage(canvas, 0, 0, w, h, 0, 0, w, h);
      return c.toDataURL();
    },
    styleFace(k) { return this.thumb(k, this.look.theme); },
    themeFace(k) { return this.thumb(this.look.rookie === 'none' ? 'pixel' : this.look.rookie, k); },
  },
  template: `<div id="look">
    <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
    <section v-if="look" class="panel">
      <div class="panel-head"><h2>Look and feel</h2></div>
      <div class="panel-body form-stack">
        <label><input type="checkbox" :checked="look.sound" @change="change({sound: $event.target.checked})"> Sound</label>
        <div class="muted"><span class="agent-name">{{ look.name }}</span>'s style</div>
        <div class="looks">
          <button v-for="k in look.styles" :key="k" type="button" :title="k" :class="{on: k === look.rookie}" @click="change({rookie: k})">
            <div class="face"><img v-if="styleFace(k)" :src="styleFace(k)" alt=""></div><span>{{ k === 'none' ? 'none (hide it)' : k }}</span>
          </button>
        </div>
        <div class="muted"><span class="agent-name">{{ look.name }}</span>'s theme</div>
        <div class="looks">
          <button v-for="k in look.themes" :key="k" type="button" :title="k" :class="{on: k === look.theme}" @click="change({theme: k})">
            <div class="face"><img v-if="themeFace(k)" :src="themeFace(k)" alt=""></div><span>{{ k }}</span>
          </button>
        </div>
        <form class="login inner" @submit.prevent="rename">
          <label class="field">Your house agent's name<input type="text" v-model="name"></label>
          <button class="btn" type="submit">Rename</button>
        </form>
      </div>
    </section>
  </div>`,
};
