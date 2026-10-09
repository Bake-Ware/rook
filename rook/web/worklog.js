// Live terminals for the Sessions page (sessions.js): one xterm.js view per
// Rook terminal. Terminal bytes arrive 1:1 from the worker PTY:
// /account/work/term/<id> sends binary frames (8-byte big-endian stream
// offset + raw bytes) and JSON state; the browser sends JSON
// input/resize/control. See docs/web/worklog.md.
let xtermLoading = null;
export function loadXterm() {
  if (!xtermLoading) {
    if (!document.querySelector('link[data-xterm-style]')) {
      const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = '/account/work/assets/vendor/xterm.css'; link.dataset.xtermStyle = '1'; document.head.append(link);
    }
    xtermLoading = Promise.all([import('/account/work/assets/vendor/xterm.mjs'), import('/account/work/assets/vendor/addon-fit.mjs')])
      .then(([x, f]) => ({Terminal: x.Terminal, FitAddon: f.FitAddon}));
  }
  return xtermLoading;
}
export function themeColors() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
  return {background: v('--term-bg', '#0d0f12'), foreground: v('--term-fg', '#e6e1cf'), cursor: v('--accent', '#e6b450')};
}

// One live terminal view bound to a Work session.
export class TermView {
  constructor(session, host, csrf, onChange) {
    this.session = session; this.host = host; this.csrf = csrf; this.onChange = onChange;
    this.cursor = null; this.me = null; this.state = {holder: null, viewers: [], running: true};
    this.retry = 0; this.disposed = false; this.decoder = null;
  }
  async mount() {
    const {Terminal, FitAddon} = await loadXterm();
    if (this.disposed) return;
    this.term = new Terminal({convertEol: false, cursorBlink: true, scrollback: 5000, fontSize: 13,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', theme: themeColors(), allowProposedApi: false});
    this.fit = new FitAddon(); this.term.loadAddon(this.fit);
    // No clipboard writes from the host (OSC 52), even if an addon is added later.
    this.term.parser.registerOscHandler(52, () => true);
    this.term.open(this.host);
    this.term.onData(data => this.send({op: 'input', data, enc: 't'}));
    this.term.onBinary(data => this.send({op: 'input', data: btoa(data), enc: 'b'}));
    this.resizeObserver = new ResizeObserver(() => this.layout()); this.resizeObserver.observe(this.host);
    this.connect();
  }
  holder() { return this.state.holder && this.state.holder === this.me; }
  layout() {
    // The holder sizes the PTY to its own window; everyone else renders the
    // PTY's grid as-is so output wraps exactly as it does for the holder.
    // A local terminal (started in someone's own terminal through the session
    // shim) is sized by that terminal, so every viewer renders its grid.
    if (!this.term || this.disposed) return;
    const {cols, rows} = this.state;
    if (this.holder() && !this.state.fixed) {
      try { this.fit.fit(); } catch {}
      if (this.term.cols !== cols || this.term.rows !== rows) this.send({op: 'resize', cols: this.term.cols, rows: this.term.rows});
    } else if (cols && rows) {
      if (this.term.cols !== cols || this.term.rows !== rows) this.term.resize(cols, rows);
    } else {
      try { this.fit.fit(); } catch {}
    }
  }
  connect() {
    if (this.disposed) return;
    const q = this.cursor != null ? '?since=' + this.cursor : '';
    const ws = this.ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/account/work/term/' + encodeURIComponent(this.session) + q);
    ws.binaryType = 'arraybuffer';
    ws.onopen = () => { this.retry = 0; this.note(''); };
    ws.onmessage = e => {
      if (ws !== this.ws) return;
      if (typeof e.data !== 'string') return this.output(e.data);
      const m = JSON.parse(e.data);
      if (m.type === 'hello') { this.me = m.viewer; }
      if (m.type === 'hello' || m.type === 'state') { this.state = m; this.term.options.disableStdin = !!(m.holder && m.holder !== this.me) || !m.running; this.layout(); this.onChange?.(this); }
      else if (m.type === 'reset') { this.term.reset(); this.cursor = null; }
      else if (m.type === 'error') this.note(m.error);
    };
    ws.onclose = e => {
      if (ws !== this.ws || this.disposed) return;
      if (e.code === 1008) return this.note('Session expired. Sign in again.');
      if (!this.state.running) return;
      this.note('Reconnecting…');
      this.retry = Math.min(this.retry + 1, 6);
      this.timer = setTimeout(() => this.connect(), 500 * 2 ** (this.retry - 1));
    };
  }
  output(buffer) {
    const view = new DataView(buffer);
    let start = Number(view.getBigUint64(0)), bytes = new Uint8Array(buffer, 8);
    if (this.cursor != null && start < this.cursor) { // already have part of it
      const skip = this.cursor - start; if (skip >= bytes.length) return;
      bytes = bytes.subarray(skip); start = this.cursor;
    }
    this.term.write(bytes);
    this.cursor = start + bytes.length;
  }
  send(data) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({...data, csrf: this.csrf}));
  }
  note(text) { this.lastNote = text; this.onChange?.(this); }
  focus() { this.term?.focus(); }
  dispose() {
    this.disposed = true; clearTimeout(this.timer); this.resizeObserver?.disconnect();
    try { this.ws?.close(); } catch {}
    this.term?.dispose();
  }
}

