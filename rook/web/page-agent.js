// The page you chat from, for the house agent (rook/hub/plugins/home/page.py).
// snapshot(): the page you are on, your selection, the text on screen and its
// controls, each with a short ref (b3 a button, i5 a field) that stays the same
// while the control is on the page. run(action): the agent's page_go,
// page_point, page_fill, page_click or page_read, done here in your browser
// with your own session, then a fresh snapshot. What it may do is decided by
// page-rules.js; it never reads password fields or shown-once secrets.
const search = new URL(import.meta.url).search;
const rules = await import('/account/settings/assets/page-rules.js' + search);
// Every name a control goes by: its text, aria-label, aria-labelledby, title,
// value and image alt (an icon button shows a glyph but says "Delete").
function names(el) {
  const by = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map(id => document.getElementById(id)?.textContent);
  const alts = [...el.querySelectorAll('img[alt]')].map(i => i.alt);
  return [el.textContent, el.getAttribute('aria-label'), ...by, el.getAttribute('title'),
    el.tagName === 'INPUT' && !['password', 'hidden'].includes(el.type) ? el.value : '', ...alts]
    .map(squash).filter(Boolean).join(' ').slice(0, 300);
}
// One id per open tab: the hub hands a page action only to the tab it was asked from.
export const TAB = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2)).replace(/-/g, '').slice(0, 16);
// Huddle action cards and questions waiting for the person (huddles.js, huddle.js).
const CARDS = '[data-card], .hx-card, .hd-q, .hd-opts, [data-question], [data-ask]';
const CONTROLS = 'a[href], button, input, select, textarea, [role=button], [role=tab], summary';
// [data-secret]: shown-once tokens, invite links and pairing codes (manage.js, tokens.js).
const SKIP = '.hd, .app-sidebar, .tabbar, .rook-art, script, style, [data-secret], .token-secret, .pairing-code, .pairing-commands, [aria-hidden=true]';
const MAX_VISIBLE = 2500, MAX_ITEMS = 50, MAX_SELECTION = 1500;

let next = 1;
const byRef = new Map();          // ref -> WeakRef(element)
const refOf = new WeakMap();      // element -> ref

const view = () => document.body.dataset.view || '';
const locked = () => !rules.ACTS.has(view());
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
  if (t === 'SUMMARY') return 'summary';
  return 'button';
}
const LETTER = {link: 'l', select: 's', textarea: 't', input: 'i', checkbox: 'c', tab: 'a', summary: 'd', button: 'b'};
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

// A ref counts only while its control is on screen in the page shown now:
// never a hidden page's (read Trust, open Overview, press a Trust ref).
function find(r) {
  const el = byRef.get(String(r || ''))?.deref();
  return el && el.isConnected && roots().some(root => root.contains(el)) && shown(el) ? el : null;
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
    const no = rules.goRefusal(id, a.query);
    if (no) throw Error(no);
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
  if (el.closest('.hd, .xterm, .terminal')) throw Error('not the chat or a terminal');
  const label = labelOf(el);
  // The control must still be the one the agent saw (Pause can become Resume).
  if (a.label !== undefined && squash(a.label) !== label)
    throw Error(a.ref + ' now reads "' + label + '", not "' + a.label + '": look again (page_read)');
  const c = rules.describeControl({view: view(), kind: kindOf(el), type: el.type, names: names(el), label,
    href: el.getAttribute('href') || '', inDialog: !!el.closest('dialog'), inCard: !!el.closest(CARDS),
    readOnly: el.readOnly,
    disabled: el.disabled});
  if (a.do === 'fill') {
    const no = rules.fillRefusal(c);
    if (no) throw Error(no);
    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, String(a.value ?? ''));
    el.dispatchEvent(new Event('input', {bubbles: true}));   // never 'change': that saves on some pages
    point(el);
    say('@home filled ' + (label || a.ref));
    return 'filled ' + (label || a.ref) + ' (not saved: the person presses Save)';
  }
  if (a.do === 'click') {
    const no = rules.pressRefusal(c);
    if (no) throw Error(no);
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
