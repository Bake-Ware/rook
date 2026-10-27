// What the house agent may do on the page you chat from (page-agent.js). Pure
// functions over plain descriptions, so tests/test_page_rules.py checks them
// with node in CI (no browser needed).
//
// It opens and points anywhere. It only fills and presses on the everyday
// pages (ACTS), so it never changes through your sign-in what it could not
// change itself. A press only shows or finds things (a tab, a # link, a
// details toggle, a button named like Open, Show, Filter, Search, Next); it
// types only into text fields and never fires the change that saves one; it
// never answers a dialog; a name that deletes, approves, sends, runs, saves or
// ships anything is always yours to press.
export const ACTS = new Set(['overview', 'start', 'work', 'huddles', 'knowledge', 'memory', 'help']);
// page_go may carry a ?query only to pages whose queries just pick what to show.
export const QUERY_OK = new Set(['huddles', 'knowledge', 'work', 'memory', 'jobs', 'settings', 'help']);
// Word stems, so "deleting", "approved", "sign-out" match too.
export const RISKY = /(delet|remov|revok|eras|wipe|destroy|deauth|sign.?out|log.?out|disconnect|reset|rotat|uninstall|restart|approv|deny|deni|reject|forget|purg|drop|send|sent|post|repl|answer|shar|invit|vot|merg|publish|updat|upgrad|deploy|roll.?out|install|stop|kill|disabl|enabl|allow|grant|confirm|\byes\b|\bok\b|pay|buy|order|\brun|start|cancel|close|archiv|save|apply|submit|accept|retry|clear|paus|resum|assign|\bpin|dismiss|\bdone\b|\bend\b|creat|\badd|\bnew\b|edit|\bmov|renam|import|export|upload|download|restor|link|lock|move|wake|ring|call|speak|play)/i;
// The only buttons it presses: ones that show or find things.
export const SAFE = /^(open|show|hide|view|filter|search|find|next|prev|previous|back|more|less|details|expand|collapse|refresh|reload|sort|list|grid|page|older|newer)\b/i;
// Work's huddle view reads these (knowledge.js drawHuddle): never in a query it opens.
export const HUDDLE_KEYS = new Set(['h', 'hp', 'hb']);
export const TEXT_TYPES = new Set(['', 'text', 'search', 'email', 'url', 'number', 'tel']);

const squash = s => String(s || '').replace(/\s+/g, ' ').trim();

/** One control as the rules see it. */
export function describeControl(c) {
  return {view: c.view, kind: c.kind, type: c.type || '', names: squash(c.names), label: squash(c.label),
    href: c.href || '', inDialog: !!c.inDialog, inCard: !!c.inCard, readOnly: !!c.readOnly,
    disabled: !!c.disabled};
}

/** null when the agent may press it, else why not (said to the agent). */
export function pressRefusal(c) {
  if (!ACTS.has(c.view)) return 'only opening and pointing here: ask the person to do it';
  if (c.disabled) return (c.label || 'that') + ' is disabled';
  if (c.inDialog) return 'answering a dialog is for the person';
  // Cards and questions carry choices agents wrote ("Show me later"): the person's.
  if (c.inCard) return 'answering a card or a question is for the person';
  if (c.kind === 'tab' || c.kind === 'summary') return null;   // they only switch what is shown
  if (RISKY.test(c.names)) return '"' + (c.label || c.names) + '" is for the person to press';
  if (c.kind === 'link') {
    if (!c.href.startsWith('#')) return 'that link leaves the dashboard: ask the person to open it';
    // Where it goes passes the same rule as page_go (#account?invite=... opens a dialog).
    const [page, query] = c.href.slice(1).split('?');
    return goRefusal(page || 'overview', query || '');
  }
  if (c.kind === 'button' && SAFE.test(c.label || c.names)) return null;
  return '"' + (c.label || c.names) + '" is for the person to press (it only presses what shows or finds things)';
}

/** null when the agent may type into it, else why not. */
export function fillRefusal(c) {
  if (!ACTS.has(c.view)) return 'only opening and pointing here: ask the person to do it';
  if (c.inDialog) return 'answering a dialog is for the person';
  if (c.inCard) return 'answering a card or a question is for the person';
  if (c.kind !== 'input' && c.kind !== 'textarea') return (c.label || 'that') + ' saves when it changes: ask the person';
  if (c.kind === 'input' && !TEXT_TYPES.has(c.type)) return 'the person types that field themselves';
  if (c.disabled || c.readOnly) return (c.label || 'that') + ' is read-only';
  if (RISKY.test(c.names)) return '"' + (c.label || c.names) + '" is for the person to fill';
  return null;
}

/** null when page_go may open it, else why not. */
export function goRefusal(page, query) {
  if (!/^[a-z][a-z0-9_-]{0,31}$/.test(String(page || ''))) return 'no dashboard page called ' + page;
  query = String(query || '').replace(/^\?+/, '');
  if (query && !QUERY_OK.has(page)) return 'opening ' + page + ' with a query is for the person';
  // Work's huddle view opens (and so may start) a project's or band's huddle.
  // Judged on the query as the Work router reads it: decoded (v=%68uddle).
  if (page === 'work' && opensHuddle(query))
    return 'opening a huddle from Work can start one: ask the person (or open #huddles)';
  return null;
}

/** Whether a #work query opens its huddle view (v=huddle, or a huddle key). */
export function opensHuddle(query) {
  let params;
  try { params = new URLSearchParams(String(query || '').replace(/^\?+/, '')); } catch { return true; }
  for (const [k, v] of params) {
    const key = k.trim().toLowerCase();
    if (HUDDLE_KEYS.has(key) || (key === 'v' && v.trim().toLowerCase().includes('huddle'))) return true;
  }
  return false;
}
