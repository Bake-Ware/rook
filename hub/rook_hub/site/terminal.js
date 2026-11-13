/* A stream as a terminal (P8, S2-S5): one Vue component, for any page that shows a stream.
   It shows the stream's recording from the start, byte for byte, then what arrives, by
   asking the hub for more and waiting while there is none (no polling loop on a timer).
   Keys typed into it go to the worker as the logged-in person's input. It closes the
   stream only when the person asks, with a reason; leaving the page or detaching the
   view closes nothing. A closed stream opens read-only. xterm.js draws it. */
import { api, toast } from '/site/shell.js';

const WAIT = 25;            // seconds the hub may hold a read while nothing new arrives
const SCROLLBACK = 100000;  // lines the view keeps; the hub keeps the whole recording

let loading = null;
function loadXterm() {
  if (!loading) {
    const css = document.createElement('link');
    css.rel = 'stylesheet'; css.href = '/site/vendor/xterm/xterm.css';
    document.head.append(css);
    loading = Promise.all([import('/site/vendor/xterm/xterm.mjs'), import('/site/vendor/xterm/addon-fit.mjs')])
      .then(([x, f]) => ({ Terminal: x.Terminal, FitAddon: f.FitAddon }));
  }
  return loading;
}

function colours() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => css.getPropertyValue(name).trim() || fallback;
  return { background: v('--term-bg', '#0d0f12'), foreground: v('--term-fg', '#e6e1cf'), cursor: v('--accent', '#e6b450') };
}

const bytes = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));

export default {
  props: {
    stream: { type: Object, required: true },   // {moniker, title, description, cap, open, opened_by, opened_at}
    worker: { type: String, default: '' },      // the worker's name, for the bar
    height: { type: Number, default: 260 },
  },
  emits: ['detach', 'closed'],
  data() { return { open: this.stream.open, position: 0, failed: '', ready: false, closing: false, why: '', busy: false }; },
  async mounted() {
    try {
      const { Terminal, FitAddon } = await loadXterm();
      if (this.gone) return;
      this.term = new Terminal({ scrollback: SCROLLBACK, fontSize: 12, cursorBlink: this.open, disableStdin: !this.open,
        fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--mono').trim() || 'monospace', theme: colours() });
      this.fit = new FitAddon();
      this.term.loadAddon(this.fit);
      this.term.open(this.$refs.host);
      this.fit.fit();
      this.refit = () => this.fit.fit();
      window.addEventListener('resize', this.refit);
      this.term.onData(data => this.type(data));
      this.ready = true;
    } catch (e) {
      this.failed = 'The terminal could not load: ' + e.message;
      return;
    }
    this.follow();
  },
  beforeUnmount() {
    this.gone = true;
    window.removeEventListener('resize', this.refit);
    if (this.term) this.term.dispose();
  },
  methods: {
    /* Read on from the last position for as long as the view is open (S2). */
    async follow() {
      let caughtUp = false;
      while (!this.gone) {
        let read;
        try {
          read = await api('GET', `/api/streams/${encodeURIComponent(this.stream.moniker)}/read?from=${this.position}&wait=${caughtUp && this.open ? WAIT : 0}`);
        } catch (e) {
          if (this.gone) return;
          this.failed = e.message;
          await new Promise(done => setTimeout(done, 3000));   // then read on from the same position
          continue;
        }
        if (this.gone) return;
        this.failed = '';
        for (const entry of read.entries) {
          if (entry.direction === 'out') this.term.write(bytes(entry.data));   // input shows as the worker echoes it
        }
        this.position = read.next;
        caughtUp = !read.entries.length;
        if (!read.open && this.open) {
          this.open = false;
          this.term.options.disableStdin = true;
          this.term.options.cursorBlink = false;
          this.$emit('closed', this.stream.moniker);
        }
        if (!this.open && caughtUp) return;   // all of a closed stream is shown
      }
    },
    /* Keys go out in the order typed: one request at a time, with whatever was typed meanwhile. */
    type(data) {
      this.typed = (this.typed || '') + data;
      if (this.sending) return;
      this.sending = (async () => {
        while (this.typed && !this.gone) {
          const text = this.typed;
          this.typed = '';
          try { await api('POST', `/api/streams/${encodeURIComponent(this.stream.moniker)}/input`, { text }); }
          catch (e) { toast(e.message); }
        }
        this.sending = null;
      })();
    },
    async close() {
      const why = this.why.trim();
      if (!why) { toast('Say in a few words why it is done.'); return; }
      this.busy = true;
      try {
        await api('POST', `/api/streams/${encodeURIComponent(this.stream.moniker)}/close`, { why });
        this.closing = false; this.why = '';
        toast('Closed ' + this.stream.title);
      } catch (e) { toast(e.message); }
      this.busy = false;
    },
  },
  template: `<div class="term-frame" @click.stop>
    <div class="term-bar"><span class="dots" aria-hidden="true"><i></i><i></i><i></i></span>
      <b>{{ worker ? worker + ' › ' : '' }}{{ stream.title }}</b><span class="k" :title="'opened by calling ' + stream.cap">{{ stream.cap }}</span>
      <span class="badge" :class="open ? 'good live' : 'plain'">{{ open ? 'live' : 'closed · read-only' }}</span>
      <span v-if="failed" class="badge bad" :title="failed">reading again…</span>
      <span style="flex:1"></span>
      <button v-if="open && !closing" class="btn sm ghost" type="button" @click="closing = true">close stream…</button>
      <button class="btn sm ghost" type="button" @click="$emit('detach')" title="Hide the terminal; the stream stays as it is">detach ✕</button></div>
    <div class="term-host" ref="host" :style="{height: height + 'px'}"><pre v-if="failed && !ready" class="term-fallback">{{ failed }}</pre></div>
    <form v-if="closing" class="term-input" @submit.prevent="close">
      <span>why</span><input v-model="why" class="mono" placeholder="say in a few words why it is done" aria-label="Why close the stream">
      <button class="btn sm danger" type="submit" :disabled="busy || !why.trim()">Close stream</button>
      <button class="btn sm ghost" type="button" @click="closing = false; why = ''">cancel</button></form>
  </div>`,
};
