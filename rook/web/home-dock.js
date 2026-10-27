// The house agent's chat dock: a launcher at the bottom right of every page
// that opens the same conversation as Intelligence sources > Home agent (the operator's 1:1
// huddle, see homeChat in home.js). index.html mounts it once, only when the
// UI state says the agent is set up, so it survives page switches; whether it
// is open is remembered per browser. It polls only while open, and hides on
// the Home agent page, which shows the same chat.
// Versioned like this module (?v=...), so a stale edge cache cannot pair it
// with an old home.js or manage.js.
const search = new URL(import.meta.url).search;
const {h, btn, useCss} = await import('/account/bands/assets/manage.js' + search);
const {homeChat} = await import('/account/settings/assets/home.js' + search);
const {uiState} = await import('/account/ui/assets/themes.js' + search);

const KEY = 'rook.homeDock.open';
const remembered = () => { try { return localStorage.getItem(KEY) === '1'; } catch { return false; } };
const remember = open => { try { localStorage.setItem(KEY, open ? '1' : '0'); } catch {} };

export function mountDock(home) {
  if (document.querySelector('.hd')) return;
  useCss();
  const chat = homeChat({
    toForm() { if (window.showView) window.showView('home'); },
    async status() {
      const s = await uiState(true);
      if (s.home) chat.update(s.home, s.home.timeout);
      else dock.hidden = true;   // turned off or unset since the page loaded
      return s;
    }});
  const close = btn('×', 'icon hd-close', () => toggle(false), {'aria-label': 'Close chat', title: 'Close'});
  chat.el.querySelector('.mg-panel-head')?.append(close);
  const panel = h('div', {class: 'hd-panel', role: 'dialog', 'aria-label': 'Chat with the house agent'}, chat.el);
  const launcher = btn('@' + home.name, 'primary hd-launch', () => toggle(true),
    {'aria-label': 'Chat with @' + home.name, title: 'Chat with @' + home.name});
  const dock = h('div', {class: 'mg hd'}, panel, launcher);
  document.body.append(dock);
  chat.update(home, home.timeout);

  let open = false;
  const onHomePage = () => document.body.dataset.view === 'home';
  function sync() {
    const live = open && !onHomePage();
    dock.classList.toggle('open', open);
    panel.hidden = !open;
    launcher.hidden = open;
    if (live) chat.start(); else chat.stop();
  }
  function toggle(want) {
    open = want;
    remember(open);
    sync();
    if (open) panel.querySelector('textarea')?.focus({preventScroll: true});
    else launcher.focus({preventScroll: true});
  }
  panel.addEventListener('keydown', e => { if (e.key === 'Escape') { e.stopPropagation(); toggle(false); } });
  // The Home agent page has this chat itself: stop polling while it is shown.
  new MutationObserver(sync).observe(document.body, {attributes: true, attributeFilter: ['data-view']});
  open = remembered();
  sync();
}
