// Dashboard themes: apply the account's theme and draw the picker on
// Settings > My preferences. A theme is a rook-theme/1 JSON file
// (rook/web/themes/, docs/web/themes.md); the hub validates it and sends the
// CSS (`:root{--bg:…}`), which is put in a <style> after every stylesheet.
const API = '/account/ui/api';
const SAFE = /^:root\{[^{}<>]*\}$/;

export function applyTheme(theme) {
  if (!theme || typeof theme.css !== 'string' || (theme.css && !SAFE.test(theme.css))) return;
  let style = document.getElementById('rook-theme');
  if (!style) { style = document.createElement('style'); style.id = 'rook-theme'; }
  style.textContent = theme.css;
  document.head.append(style);   // (re)appended: it must come after the stylesheets
  document.documentElement.dataset.theme = theme.id || '';
  try { localStorage.setItem('rook.theme', JSON.stringify({id: theme.id, css: theme.css})); } catch {}
  dispatchEvent(new CustomEvent('rook-theme', {detail: theme}));
}

let statePromise = null;
// The account's dashboard state (theme, start page), once per page load
// unless `fresh`.
export function uiState(fresh = false) {
  if (!statePromise || fresh) {
    statePromise = fetch(API, {headers: {Accept: 'application/json'}, cache: 'no-store'}).then(async r => {
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw Error(body.error || 'The dashboard service did not answer (' + r.status + ').');
      return body;
    });
    statePromise.catch(() => { statePromise = null; });
  }
  return statePromise;
}

export async function uiPost(body) {
  const state = await uiState();
  const r = await fetch(API, {method: 'POST', headers: {'Content-Type': 'application/json'},
                              body: JSON.stringify({...body, csrf: state.csrf})});
  const out = await r.json().catch(() => ({}));
  if (!r.ok) throw Error(out.error || 'Request failed (' + r.status + ').');
  return out;
}

// On every dashboard load: the account's saved theme wins over this
// browser's cached one.
export async function syncTheme() {
  try { const s = await uiState(); if (s.theme) applyTheme(s.theme); } catch {}
}

// The picker: a card per theme with its swatches; a click saves it for this
// account and applies it at once.
export async function mountThemePicker(host) {
  const s = await uiState();
  const wrap = document.createElement('section');
  wrap.className = 'theme-picker';
  wrap.innerHTML = '<h2>Theme</h2><p class="muted">How the dashboard looks for your account, on every device you sign in from.</p><div class="theme-cards" role="radiogroup" aria-label="Theme"></div><p class="theme-status" role="status"></p>';
  const cards = wrap.querySelector('.theme-cards'), status = wrap.querySelector('.theme-status');
  let current = s.theme?.id;
  const draw = () => {
    cards.replaceChildren(...(s.themes || []).map(t => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'theme-card'; b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', String(t.id === current)); b.dataset.theme = t.id;
      const sw = document.createElement('span'); sw.className = 'theme-swatches'; sw.setAttribute('aria-hidden', 'true');
      // Swatch colours come from the hub's validated theme list.
      for (const c of t.swatches || []) { const i = document.createElement('i'); i.style.background = c; sw.append(i); }
      const name = document.createElement('b'); name.textContent = t.name;
      const kind = document.createElement('small'); kind.textContent = t.scheme === 'light' ? 'Light' : 'Dark';
      const d = document.createElement('span'); d.className = 'theme-desc'; d.textContent = t.description;
      b.append(sw, name, kind, d);
      b.onclick = async () => {
        if (t.id === current) return;
        status.textContent = 'Saving…';
        try {
          const out = await uiPost({action: 'theme', theme: t.id});
          current = t.id; s.theme = out.theme; applyTheme(out.theme); draw();
          status.textContent = t.name + ' is your theme now.';
        } catch (e) { status.textContent = e.message; }
      };
      return b;
    }));
  };
  draw();
  if (!s.saved) status.textContent = 'Settings are unavailable on this hub, so a choice cannot be saved.';
  host.prepend(wrap);
  return wrap;
}
