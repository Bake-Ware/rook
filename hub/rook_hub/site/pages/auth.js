/* The Auth page (T3, T5, T6): users, and tokens with purpose, scope, owner and use;
   revoke a token, revoke a user (and every token of theirs), mint an agent token for
   yourself (shown once, T3's one exception), and change your own password.
   A new token lives only in this page's memory until Done or leaving the page; it never
   goes into the address, a pin or the browser's storage. */
import { api, toast, viewHref, showView } from '/site/shell.js';
import { ago, SureButton } from '/site/pages/parts.js';

const REFRESH_MS = 5000;

export default {
  components: { 'sure-button': SureButton },
  data() {
    const view = new URLSearchParams(location.search);
    return {
      data: null, error: '', timer: null, minted: null, password: '', changing: false,
      tab: view.get('tab') === 'tokens' ? 'tokens' : 'users', user: view.get('user') || '', token: view.get('token') || '',
    };
  },
  computed: {
    view() { return { tab: this.tab, user: this.tab === 'users' ? this.user : '', token: this.tab === 'tokens' ? this.token : '' }; },
    href() { return viewHref('/auth', this.view); },
    pinLabel() { return 'Auth: ' + (this.view.user || this.view.token || this.tab); },
    users() { return this.data ? this.data.users.filter(u => !this.view.user || u.moniker === this.view.user) : []; },
    tokens() { return this.data ? this.data.tokens.filter(t => !this.view.token || t.moniker === this.view.token) : []; },
    live() { return this.data ? this.data.tokens.filter(t => !t.revoked_at && !t.spent_at).length : 0; },
  },
  watch: {
    view: { handler() { showView('/auth', this.view); }, deep: true },
    tab() { this.minted = null; this.changing = false; this.password = ''; },
  },
  mounted() { this.load(); this.timer = setInterval(this.load, REFRESH_MS); },
  unmounted() { clearInterval(this.timer); this.minted = null; this.password = ''; },
  methods: {
    async load() {
      try { this.data = await api('GET', '/api/auth'); this.error = ''; } catch (e) { this.error = e.message; }
    },
    async act(path, done) {
      try { await api('POST', path); toast(done); await this.load(); } catch (e) { toast(e.message); }
    },
    async mint() {
      try { this.minted = await api('POST', '/api/tokens'); await this.load(); } catch (e) { toast(e.message); }
    },
    async copy() {
      try { await navigator.clipboard.writeText(this.minted.token); toast('Copied'); }
      catch { this.$refs.minted.select(); toast('Select and copy it by hand'); }
    },
    async savePassword() {
      try {
        await api('POST', '/api/password', { password: this.password });
        toast('Password changed'); this.changing = false;
      } catch (e) { toast(e.message); }
      this.password = '';
    },
    status(t) { return t.revoked_at ? 'revoked' : t.spent_at ? 'spent' : ''; },
    userMenu(u) { return [{ label: 'Pin this user', pin: { label: 'Auth: ' + u.name, href: viewHref('/auth', { tab: 'users', user: u.moniker }) } }]; },
    tokenMenu(t) { return [{ label: 'Pin this token', pin: { label: 'Auth: ' + t.moniker, href: viewHref('/auth', { tab: 'tokens', token: t.moniker }) } }]; },
    ago,
  },
  template: `<div>
    <header class="topbar">
      <div><div class="title-row"><h1>Auth</h1><pin-view :label="pinLabel" :href="href"></pin-view></div><p>Who can sign in, and every token, whose it is and when it was last used.</p></div>
      <div class="spacer"></div>
      <button v-if="tab==='tokens' && !minted" class="btn accent" type="button" @click="mint">+ New token</button>
    </header>

    <div class="wrap">
      <p v-if="error" class="callout bad" role="alert">{{ error }}</p>
      <div class="tabs" role="tablist">
        <button role="tab" type="button" :class="{on: tab==='users'}" :aria-selected="tab==='users'" @click="tab='users'">Users<span class="n">{{ data ? data.users.length : '' }}</span></button>
        <button role="tab" type="button" :class="{on: tab==='tokens'}" :aria-selected="tab==='tokens'" @click="tab='tokens'">Tokens<span class="n">{{ data ? live : '' }}</span></button>
      </div>
      <div v-if="view.user || view.token" class="one-banner">Showing one {{ view.user ? 'user' : 'token' }}: <b>{{ view.user || view.token }}</b><span style="flex:1"></span>
        <button class="btn sm" type="button" @click="user=''; token=''">show all</button></div>

      <section v-if="tab==='users' && data" class="panel">
        <div class="tbl" style="--cols:minmax(170px,1.2fr) 150px 120px minmax(230px,1fr) 30px">
          <div class="th"><span>user</span><span>created</span><span>status</span><span></span><span></span></div>
          <template v-for="u in users" :key="u.moniker">
            <div class="tr" :class="{revoked: u.revoked_at}">
              <span><span class="cell-title strike">{{ u.name }}</span><span class="cell-sub mono">{{ u.moniker }}</span></span>
              <span class="cell-mono">{{ ago(u.created_at) }}</span>
              <span><span v-if="u.revoked_at" class="badge bad">revoked</span><span v-else-if="u.moniker===data.me" class="badge plain good">you</span></span>
              <span class="right" style="display:flex;gap:6px;justify-content:flex-end;align-items:center">
                <button v-if="u.moniker===data.me && !changing" class="btn sm" type="button" @click="changing=true">Change password</button>
                <sure-button v-if="!u.revoked_at" label="Revoke" :question="'Revoke ' + u.name + ' and every token of theirs?'" @yes="act('/api/users/' + u.moniker + '/revoke', 'Revoked ' + u.name)"></sure-button>
              </span>
              <span class="right"><row-menu :items="userMenu(u)"></row-menu></span>
            </div>
            <form v-if="u.moniker===data.me && changing" class="tr" style="display:flex;gap:8px;align-items:end" @submit.prevent="savePassword">
              <label class="field" style="flex:1;max-width:320px">New password<input class="mono" type="password" v-model="password" autocomplete="new-password"></label>
              <button class="btn primary" type="submit" :disabled="!password">Save password</button>
              <button class="btn ghost" type="button" @click="changing=false; password=''">Cancel</button>
            </form>
          </template>
        </div>
      </section>

      <template v-if="tab==='tokens' && data">
        <div v-if="minted" class="callout" role="status">
          <b>Your new agent token, {{ minted.moniker }}.</b> Copy it into your MCP client now; it is not shown again.
          <div style="display:flex;gap:8px;margin-top:8px">
            <input ref="minted" class="mono" readonly :value="minted.token" style="flex:1" aria-label="New token" @focus="$event.target.select()">
            <button class="btn accent" type="button" @click="copy">Copy</button>
            <button class="btn ghost" type="button" @click="minted=null">Done</button>
          </div>
        </div>
        <section class="panel">
          <div class="tbl" style="--cols:minmax(170px,1.1fr) minmax(110px,.8fr) 100px 110px 120px 120px minmax(150px,auto) 30px">
            <div class="th"><span>token</span><span>scopes</span><span>owner</span><span>made</span><span>ends</span><span>last used</span><span></span><span></span></div>
            <div v-for="t in tokens" :key="t.moniker" class="tr" :class="{revoked: t.revoked_at}">
              <span><span class="cell-title strike mono" style="font-size:12px">{{ t.moniker }}</span>
                <span class="cell-sub">{{ t.purpose }}<template v-if="t.minted_by"> · minted by {{ t.minted_by }}</template><template v-if="t.this_session"> · this session</template></span></span>
              <span class="chips"><span v-for="s in t.scopes" :key="s" class="chip">{{ s }}</span></span>
              <span class="cell-mono" :title="t.owner.moniker">{{ t.owner.name }}</span>
              <span class="cell-mono">{{ ago(t.minted_at) }}</span>
              <span class="cell-mono">{{ t.expires_at ? ago(t.expires_at) : 'never' }}</span>
              <span class="cell-mono">{{ ago(t.last_used) }}</span>
              <span class="right">
                <span v-if="status(t)" class="badge bad">{{ status(t) }}</span>
                <sure-button v-else label="Revoke" :question="t.this_session ? 'This logs you out. Revoke?' : 'Revoke ' + t.moniker + '?'" @yes="act('/api/tokens/' + t.moniker + '/revoke', 'Revoked ' + t.moniker)"></sure-button>
              </span>
              <span class="right"><row-menu :items="tokenMenu(t)"></row-menu></span>
            </div>
          </div>
        </section>
      </template>
    </div>
  </div>`,
};
