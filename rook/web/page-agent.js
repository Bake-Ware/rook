// The page you chat from, for the house agent (rook/hub/plugins/home/page.py).
// snapshot(): the page you are on, your selection, the text on screen and its
// controls, each with a short ref (b3 a button, i5 a field) that stays the same
// while the control is on the page. run(action): the agent's page_go,
// page_point, page_fill, page_click or page_read, done here in your browser
// with your own session, then a fresh snapshot.
// It opens and points anywhere, but only fills and presses on the everyday
// pages (ACTS: Feed, Work but Sessions, Help), so it never changes through
// your sign-in what it could not change itself (trust, settings, plugins,
// updates, its own instructions, machines, terminals). It never presses a
// button that deletes, approves, sends or ships something, never follows a
// link off the dashboard, and never reads password fields or shown-once
// secrets.
const ACTS = new Set(['overview', 'start', 'work', 'huddles', 'knowledge', 'jobs', 'memory', 'help']);
const RISKY = /\b(delete|remove|revoke|erase|wipe|destroy|deauth|sign ?out|log ?out|disconnect|reset|rotate|uninstall|restart|approve|deny|reject|forget|purge|drop|send|post|reply|answer|share|invite|vote|merge|publish|update|upgrade|deploy|roll ?out|install)\b/i;
// One id per open tab: the hub hands a page action only to the tab it was asked from.
export const TAB = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)).replace(/-/g, '').slice(0, 16);
const CONTROLS = 'a[href], button, input, select, textarea, [role=button], [role=tab], summary';
// [data-secret]: shown-once tokens, invite links and pairing codes (manage.js, tokens.js).
const SKIP = '.hd, .app-sidebar, .tabbar, .rook-art, script, style, [data-secret], .token-secret, .pairing-code, .pairing-commands, [aria-hidden=true]';
const MAX_VISIBLE = 2500, MAX_ITEMS = 50, MAX_SELECTION = 1500;

let next = 1;
const byRef = new Map();          // ref -> WeakRef(element)
const refOf = new WeakMap();      // element -> ref

const view = () => document.body.dataset.view || '';
const locked = () => !ACTS.has(view());
const say = text => { try { window.rookToast?.(text); } catch {} };

function shown(el) {
  if (!el.isConnected || el.closest(SKIP)) return false;
  const r = el.getBoundingClientRect();
  if (!r.width && !r.height) return false;
  if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) return false;
  const st = getComputedStyle(el);
  return st.visibility !== 'hidden' && st.display !== 'none';
}
function kindOf(el) {
  const t = el.tagName;
  if (t === 'A') return 'link';
  if (t === 'SELECT') return 'select';
  if (t === 'TEXTAREA') return 'textarea';
  if (t === 'INPUT') return el.type === 'checkbox' || el.type === 'radio' ? 'checkbox' : 'input';
  if (el.getAttribute('role') === 'tab') return 'tab';
  return 'button';
}
const LETTER = {link: 'l', select: 's', textarea: 't', input: 'i', checkbox: 'c', tab: 'a', button: 'b'};
function ref(el) {
  let r = refOf.get(el);
  if (!r) { r = LETTER[kindOf(el)] + next++; refOf.set(el, r); byRef.set(r, new WeakRef(el)); }
  return r;
}
const squash = s => String(s || '').replace(/\s+/g, ' ').trim();
function labelOf(el) {
  const own = el.getAttribute('aria-label') || el.getAttribute('title');
  const lab = el.labels?.[0] ? squash(el.labels[0].textContent) : '';
  const text = ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) ? '' : squash(el.textContent);
  return squash(text || own || lab || el.placeholder || el.name || '').slice(0, 100);
}
function valueOf(el) {
  if (el.tagName === 'SELECT') return el.selectedOptions?.[0]?.textContent?.trim() || '';
  if (el.tagName === 'INPUT') {
    if (el.type === 'checkbox' || el.type === 'radio') return el.checked ? 'checked' : 'unchecked';
    if (['password', 'hidden', 'file'].includes(el.type)) return '';
  }
  return el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' ? String(el.value || '').slice(0, 200) : '';
}
function roots() {
  return [document.querySelector('.topbar'), document.getElementById('view-' + view()),
    ...document.querySelectorAll('dialog[open]')].filter(Boolean);
}
function visibleText() {
  const parts = [];
  let size = 0;
  for (const root of roots()) {
    const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walk.nextNode(); n && size < MAX_VISIBLE; n = walk.nextNode()) {
      const text = squash(n.nodeValue);
      const el = n.parentElement;
      if (!text || !el || el.closest('input, textarea, select') || !shown(el)) continue;
      if (!/[\p{L}\p{N}]/u.test(text) || /^\p{L}$/u.test(text)) continue;   // icons, not words
      if (parts[parts.length - 1] === text) continue;
      parts.push(text);
      size += text.length + 1;
    }
  }
  return parts.join(' · ').slice(0, MAX_VISIBLE);
}
// Clicking into the chat box clears the page's selection, so the last one made
// on the page (not in the chat) is kept for a while.
let kept = {text: '', at: 0};
function current() {
  const sel = getSelection();
  if (!sel || !sel.anchorNode) return null;
  const node = sel.anchorNode.nodeType === 1 ? sel.anchorNode : sel.anchorNode.parentElement;
  if (node?.closest('.hd')) return null;                 // typing in the chat itself
  if (sel.isCollapsed || node?.closest('[data-secret], .token-secret, .pairing-code, .pairing-commands, input, textarea')) return '';
  return String(sel).trim().slice(0, MAX_SELECTION);
}
document.addEventListener('selectionchange', () => {
  const now = current();
  if (now !== null) kept = {text: now, at: Date.now()};
});
function selection() {
  const now = current();
  if (now) return now;
  return Date.now() - kept.at < 120000 ? kept.text : '';
}
function pages() {
  const out = [];
  for (const group of document.querySelectorAll('.app-sidebar .nav-group')) {
    const section = group.querySelector('nav')?.getAttribute('aria-label') || '';
    for (const a of group.querySelectorAll('a[id^="tab-"]'))
      out.push({id: a.id.slice(4), title: squash(a.firstChild?.textContent || a.textContent), section});
  }
  return out;
}

export function snapshot() {
  const items = [];
  for (const root of roots()) {
    for (const el of root.querySelectorAll(CONTROLS)) {
      if (items.length >= MAX_ITEMS) break;
      if (!shown(el) || (el.tagName === 'INPUT' && el.type === 'hidden')) continue;
      const label = labelOf(el);
      if (!label && kindOf(el) !== 'input' && kindOf(el) !== 'textarea') continue;
      const row = {ref: ref(el), kind: kindOf(el), label};
      const value = valueOf(el);
      if (value) row.value = value;
      if (el.disabled) row.off = true;
      items.push(row);
    }
  }
  const title = document.getElementById('view-title')?.textContent || '';
  return {view: view(), title, hash: location.hash, selection: selection(), visible: visibleText(),
    items, pages: pages(), locked: locked(), tab: TAB};
}

function find(r) {
  const el = byRef.get(String(r || ''))?.deref();
  return el && el.isConnected ? el : null;
}
function byText(text) {
  const want = squash(text).toLowerCase();
  if (!want) return null;
  let best = null;
  for (const root of roots()) {
    for (const el of root.querySelectorAll('*')) {
      if (!squash(el.textContent).toLowerCase().includes(want) || !shown(el)) continue;
      if (!best || best.contains(el)) best = el;
    }
  }
  return best;
}
function point(el) {
  el.scrollIntoView({block: 'center', behavior: 'smooth'});
  el.classList.add('agent-point');
  setTimeout(() => el.classList.remove('agent-point'), 2500);
}
const pause = ms => new Promise(r => setTimeout(r, ms));
function style() {
  if (document.querySelector('style[data-agent-point]')) return;
  const s = document.createElement('style');
  s.dataset.agentPoint = '1';
  s.textContent = '.agent-point{outline:2px solid var(--accent)!important;outline-offset:3px;border-radius:4px}';
  document.head.append(s);
}

async function act(a) {
  if (a.do === 'read') return 'looked at the page';
  if (a.do === 'go') {
    const id = String(a.page || '');
    if (!document.getElementById('tab-' + id) && !['feed', 'intelligence', 'chat'].includes(id))
      throw Error('no dashboard page called ' + id);
    location.hash = '#' + id + (a.query ? '?' + String(a.query).replace(/^\?/, '') : '');
    const lands = {feed: 'overview', intelligence: 'home', chat: 'huddles'}[id] || id;
    for (let i = 0; i < 20 && view() !== lands; i++) await pause(100);
    await pause(600);                       // most pages load their content just after
    say('@home opened ' + (document.getElementById('view-title')?.textContent || id));
    return 'opened ' + (document.getElementById('view-title')?.textContent || id);
  }
  if (a.do === 'point') {
    const el = a.ref ? find(a.ref) : byText(a.text);
    if (!el) throw Error(a.ref ? 'nothing with ref ' + a.ref + ' on the page now' : 'no "' + a.text + '" on the page now');
    point(el);
    return 'pointed at ' + (labelOf(el) || squash(el.textContent).slice(0, 60));
  }
  const el = find(a.ref);
  if (!el) throw Error('nothing with ref ' + a.ref + ' on the page now (page_read for fresh refs)');
  if (locked()) throw Error('only opening and pointing here: ask the person to do it');
  if (el.closest('.hd, .xterm, .terminal')) throw Error('not the chat or a terminal');
  const label = labelOf(el);
  if (a.do === 'fill') {
    if (!['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName)) throw Error(a.ref + ' is not a field');
    if (['password', 'file', 'hidden'].includes(el.type)) throw Error('the person types that field themselves');
    if (el.disabled || el.readOnly) throw Error(label + ' is read-only');
    const v = String(a.value ?? '');
    if (el.type === 'checkbox' || el.type === 'radio') {
      el.checked = /^(true|yes|on|1|checked)$/i.test(v);
    } else if (el.tagName === 'SELECT') {
      const opt = [...el.options].find(o => o.value === v || squash(o.textContent).toLowerCase() === v.toLowerCase());
      if (!opt) throw Error('no option "' + v + '" in ' + label);
      el.value = opt.value;
    } else {
      const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, v);
    }
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    point(el);
    say('@home filled ' + (label || a.ref));
    return 'filled ' + (label || a.ref);
  }
  if (a.do === 'click') {
    if (el.disabled) throw Error(label + ' is disabled');
    if (RISKY.test(label)) throw Error('"' + label + '" is for the person to press');
    if (el.tagName === 'A' && !(el.getAttribute('href') || '').startsWith('#'))
      throw Error('that link leaves the dashboard: ask the person to open it');
    point(el);
    say('@home pressed ' + (label || a.ref));
    el.click();
    await pause(700);
    return 'pressed ' + (label || a.ref);
  }
  throw Error('unknown page action ' + a.do);
}

/** Run one page action; {ok, result, page} for the hub. */
export async function run(a) {
  style();
  try {
    return {ok: true, result: await act(a), page: snapshot()};
  } catch (e) {
    return {ok: false, result: e.message, page: snapshot()};
  }
}
