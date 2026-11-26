/* The Settings page's Phone app section (brief AP1): the same QR code, button and line as the
   public page /app, for a person signed in. */
import { drawPhoneApp } from '/site/phone-app.js';

export default {
  data() { return { error: '' }; },
  async mounted() {
    try { await drawPhoneApp(this.$refs.box); } catch (e) { this.error = e.message; }
  },
  template: `<div>
    <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
    <section class="panel">
      <div class="panel-head"><h2>Phone app</h2><span class="sub">Rook for Android, from this hub; it opens this hub with nothing typed</span></div>
      <div class="panel-body" ref="box"></div>
    </section>
  </div>`,
};
