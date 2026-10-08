// A read-only terminal for sessions Rook did not start (sessions.js tiers 2
// and 3): the Claude Code mod's mirror events, or a transcript, written as
// ANSI into one xterm.js instance so they look like the agent's own TUI.
// Session text is untrusted: prompts and replies lose every control
// sequence, tool output keeps only colours (SGR), and the emulator ignores
// clipboard writes (OSC 52) and hyperlinks (OSC 8). See docs/web/sessions.md.

const ESC = '\x1b[';
const A = {reset: ESC + '0m', bold: ESC + '1m', dim: ESC + '2m', italic: ESC + '3m', red: ESC + '31m',
  green: ESC + '32m', yellow: ESC + '33m', cyan: ESC + '36m', grey: ESC + '90m', orange: ESC + '38;5;173m'};
const RESULT_LINES = 6;         // tool output lines shown under a call
const RESULT_LINE_MAX = 400;    // characters kept of each before clipping to the width
export const SCROLLBACK = 10000;

const GLYPHS = {
  claude: {bullet: '●', prompt: '>', result: '⎿'},
  codex: {bullet: '•', prompt: '›', result: '└'},
};

// Prompts and replies: printable text and newlines only.
export function plain(text) {
  return String(text ?? '').replace(/\r\n?/g, '\n')
    .replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\|$)/g, '')
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    .replace(/\x1b[\s\S]?/g, '')
    .replace(/[\x00-\x08\x0b-\x1f\x7f\x9b]/g, '');
}

// Tool output: colours survive, nothing else does. Carriage returns
// (progress bars) keep what the line finally showed.
export function sgrOnly(text) {
  return String(text ?? '').replace(/\r+\n/g, '\n')
    .replace(/\x1b\][\s\S]*?(?:\x07|\x1b\\|$)/g, '')
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, m => /^\x1b\[[0-9;:]*m$/.test(m) ? m : '')
    .replace(/\x1b(?!\[[0-9;:]*m)[\s\S]?/g, '')
    .split('\n').map(line => line.slice(line.lastIndexOf('\r') + 1)).join('\n')
    .replace(/[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f\x9b]/g, '');
}

// The first n visible characters of a line that may hold SGR sequences.
export function clipVisible(line, n) {
  let out = '', seen = 0;
  for (let i = 0; i < line.length;) {
    if (line[i] === '\x1b') { const m = /^\x1b\[[0-9;:]*m/.exec(line.slice(i)); if (m) { out += m[0]; i += m[0].length; continue; } }
    if (seen >= n) return out + '…';
    out += line[i]; seen++; i++;
  }
  return out;
}

// What Claude Code shows in a call's parentheses: its main argument.
export function toolArg(input) {
  if (input == null || input === '') return '';
  let value = input;
  if (typeof value === 'string') { try { value = JSON.parse(value); } catch { return value; } }
  if (typeof value !== 'object') return String(value);
  for (const k of ['command', 'file_path', 'notebook_path', 'path', 'pattern', 'url', 'query', 'description', 'prompt', 'skill', 'subject', 'cap'])
    if (typeof value[k] === 'string' && value[k]) return value[k];
  try { return JSON.stringify(value); } catch { return ''; }
}

// One xterm.js instance, written to and never typed into.
export class Screen {
  constructor(host, loadXterm, themeColors, label) {
    this.el = document.createElement('div');
    this.el.className = 'sx-xterm sx-screen';
    this.el.setAttribute('role', 'log');
    this.el.setAttribute('aria-label', label || 'Session (read-only terminal)');
    host.append(this.el);
    this.el.sxScreen = this;    // tests read the whole buffer through it
    this.queue = []; this.term = null; this.disposed = false;
    this.ready = this.mount(loadXterm, themeColors);
  }
  async mount(loadXterm, themeColors) {
    const {Terminal, FitAddon} = await loadXterm();
    if (this.disposed) return;
    const theme = themeColors();
    const t = this.term = new Terminal({disableStdin: true, convertEol: true, cursorBlink: false, cursorStyle: 'bar',
      cursorInactiveStyle: 'none', scrollback: SCROLLBACK, fontSize: 13,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      theme: {...theme, cursor: theme.background, cursorAccent: theme.background}, allowProposedApi: false});
    // Session text must not reach the clipboard or open links.
    t.parser.registerOscHandler(52, () => true);
    t.parser.registerOscHandler(8, () => true);
    this.fit = new FitAddon(); t.loadAddon(this.fit);
    t.open(this.el);
    this.layout();
    this.resizeObserver = new ResizeObserver(() => this.layout()); this.resizeObserver.observe(this.el);
    t.write(ESC + '?25l');
    const queued = this.queue; this.queue = [];
    for (const s of queued) this.write(s);
  }
  layout() { if (this.term && !this.disposed && this.el.clientWidth) { try { this.fit.fit(); } catch {} } }
  get cols() { return this.term?.cols || 100; }
  write(text) {
    if (!text) return;
    if (!this.term) { this.queue.push(text); return; }
    // Follow the bottom unless the person has scrolled up to read.
    const b = this.term.buffer.active, follow = b.viewportY >= b.baseY;
    this.term.write(text, () => { if (follow && !this.disposed) this.term.scrollToBottom(); });
  }
  text() {
    const b = this.term?.buffer.active, out = [];
    for (let i = 0; b && i < b.length; i++) out.push(b.getLine(i).translateToString(true));
    return out.join('\n').replace(/\n+$/, '');
  }
  reset() { this.queue = []; if (this.term) { this.term.reset(); this.term.write(ESC + '?25l'); } }
  dispose() { this.disposed = true; this.resizeObserver?.disconnect(); this.term?.dispose(); }
}

// Turns session events into blocks styled like Claude Code's TUI: "> " for
// prompts, "● " (Claude Code's ⏺, drawn as text) for replies and tool calls, "  ⎿  " for tool output, a rule
// at each turn's end and a status line under the last block.
export class Painter {
  constructor(screen, agent) {
    this.s = screen; this.g = GLYPHS[agent] || GLYPHS.claude;
    this.reset();
  }
  reset() {
    this.blocks = 0; this.stream = null; this.unfinished = null; this.statusText = ''; this.statusShown = false;
    this.last = null; this.calls = new Map();
  }
  width() { return Math.max(20, this.s.cols); }
  out(text) { this.s.write(text); }
  // Lines of a block: the first after `first`, the rest after `rest`.
  lines(text, first, rest) { return text.split('\n').map((l, i) => (i ? rest : first) + l).join('\r\n') + '\r\n'; }
  begin(kind, tight = false) {
    this.hideStatus(); this.endStream();
    let out = '';
    if (this.blocks++ && !tight) out += '\r\n';
    this.last = kind;
    return out;
  }
  // -- status line ------------------------------------------------------------
  status(text) {
    text = text || '';
    if (text === this.statusText && (this.statusShown || this.stream || !text)) return;
    this.statusText = text;
    if (!this.stream) { this.hideStatus(); this.showStatus(); }
  }
  showStatus() { if (this.statusText && !this.statusShown) { this.out('\r\n' + this.statusText + A.reset); this.statusShown = true; } }
  hideStatus() { if (this.statusShown) { this.out('\r' + ESC + '2K' + ESC + '1A'); this.statusShown = false; } }
  // -- blocks -------------------------------------------------------------------
  note(text) { this.out(this.begin('note') + this.lines(plain(text), A.dim, A.dim) + A.reset); this.showStatus(); }
  banner(lines) {
    const w = Math.min(this.width() - 2, 72), body = lines.map(l => clipVisible(plain(l), w - 4));
    let out = this.begin('banner') + A.orange + '╭' + '─'.repeat(w - 2) + '╮\r\n';
    for (const [i, l] of body.entries()) {
      const text = (i ? '  ' : '✻ ') + l, pad = Math.max(0, w - 4 - [...text].length);
      out += '│ ' + A.reset + (i ? A.dim : A.bold) + text + A.reset + ' '.repeat(pad) + A.orange + ' │\r\n';
    }
    this.out(out + '╰' + '─'.repeat(w - 2) + '╯' + A.reset + '\r\n'); this.showStatus();
  }
  prompt(text, from) {
    let out = this.begin('prompt');
    if (from === 'peer') out += A.dim + '  (message from another session)' + A.reset + '\r\n';
    out += this.lines(plain(text).replace(/\n+$/, '') || ' ', A.grey + this.g.prompt + ' ' + A.reset + A.bold, '  ') + A.reset;
    this.out(out); this.showStatus();
  }
  // Streamed reply text, appended in place.
  delta(text) {
    if (!this.stream) {
      this.out(this.begin('assistant') + this.g.bullet + ' ');
      this.stream = {text: ''};
    }
    this.stream.text += text || '';
    this.out(plain(text).replace(/\n/g, '\r\n  '));
  }
  endStream() {
    if (!this.stream) return;
    this.out('\r\n'); this.unfinished = this.stream.text; this.stream = null;
  }
  // The full reply: only what the streamed pieces did not already show.
  done(text) {
    text = text || '';
    if (this.stream) {
      const seen = this.stream.text;
      if (text.startsWith(seen)) { this.delta(text.slice(seen.length)); this.endStream(); this.unfinished = null; this.showStatus(); return; }
      this.endStream();
    }
    const prior = this.unfinished; this.unfinished = null;
    const rest = prior && text.startsWith(prior) ? text.slice(prior.length) : text;
    if (rest.trim()) this.assistant(rest);
    else this.showStatus();
  }
  assistant(text) {
    this.out(this.begin('assistant') + this.lines(plain(text).replace(/^\n+|\n+$/g, ''), this.g.bullet + ' ', '  '));
    this.showStatus();
  }
  call(id, name, input) {
    name = plain(name || 'Tool').replace(/\s+/g, ' ').slice(0, 60);
    const arg = plain(toolArg(input)).replace(/\s+/g, ' ').trim();
    const room = Math.max(10, this.width() - name.length - 5);
    this.out(this.begin('call:' + (id || '')) + A.green + this.g.bullet + A.reset + ' ' + A.bold + name + A.reset
      + (arg ? '(' + clipVisible(arg, room) + ')' : '') + '\r\n');
    if (id) { this.calls.set(id, name); if (this.calls.size > 200) this.calls.delete(this.calls.keys().next().value); }
    this.showStatus();
  }
  result(id, text, ok) {
    const adjacent = id && this.last === 'call:' + id, name = this.calls.get(id);
    this.calls.delete(id);
    let out = this.begin('result', adjacent || this.last?.startsWith('call:'));
    if (!adjacent && name) out += A.dim + '  (' + name + ')' + A.reset + '\r\n';
    const lines = sgrOnly(text).replace(/\n+$/, '').split('\n');
    const empty = lines.length === 1 && !lines[0].trim();
    const shown = empty ? [ok === false ? 'Error' : '(No output)'] : lines.slice(0, RESULT_LINES);
    const width = Math.max(10, this.width() - 7), tone = ok === false ? A.red : A.dim;
    out += shown.map((l, i) => (i ? '     ' : '  ' + this.g.result + '  ') + tone
      + clipVisible(l.slice(0, RESULT_LINE_MAX), width).replace(/\x1b\[0?m/g, A.reset + tone) + A.reset).join('\r\n') + '\r\n';
    if (!empty && lines.length > shown.length) out += '     ' + A.dim + '… +' + (lines.length - shown.length) + ' lines' + A.reset + '\r\n';
    this.out(out); this.showStatus();
  }
  turnEnd(reason) {
    const label = reason && reason !== 'answer' ? ' turn ended: ' + plain(reason) + ' ' : '';
    const w = Math.min(this.width() - 1, 100);
    const rule = label ? '──' + label + '─'.repeat(Math.max(2, w - label.length - 2)) : '─'.repeat(w);
    this.out(this.begin('turn') + A.dim + (reason && reason !== 'answer' ? A.yellow : '') + rule + A.reset + '\r\n');
    this.showStatus();
  }
}

// A transcript message (sessions.follow) as blocks. Assistant records carry
// tool calls as "[tool_use: Name]" followed by the input as JSON.
export function paintMessage(p, m, prevCall) {
  const text = m.text || '';
  if (m.role === 'tool' || (m.role === 'user' && (m.kind === 'tool_result' || (!m.kind && m.legacy && prevCall))))
    p.result(prevCall?.id || '', text, m.error ? false : undefined);
  else if (m.role === 'user') p.prompt(text);
  else {
    const parts = text.split(/^\[tool_use: ([^\]\n]*)\]$/m);
    let call = null;
    for (let i = 0; i < parts.length; i++) {
      if (i % 2 === 0) { if (parts[i].trim()) p.assistant(parts[i]); continue; }
      const body = (parts[i + 1] || '').replace(/^\n/, ''), nl = body.indexOf('\n');
      const line = nl < 0 ? body : body.slice(0, nl), isInput = /^\s*[{[]/.test(line);
      call = {id: 'm' + m.index + ':' + i, name: parts[i]};
      p.call(call.id, parts[i], isInput ? line : '');
      parts[i + 1] = isInput ? (nl < 0 ? '' : body.slice(nl + 1)) : body;
    }
    if (m.clipped) p.note(`  … ${m.clipped} more characters not shown`);
    return call;
  }
  if (m.clipped) p.note(`  … ${m.clipped} more characters not shown`);
  return null;
}
