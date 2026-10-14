'use strict';
// Shared helpers for the app's own pages. window.__TAURI__ is the app
// bridge (withGlobalTauri); these pages are the only ones allowed to use it.
const T = window.__TAURI__;
const invoke = (cmd, args) => T.core.invoke(cmd, args || {});
const listen = (ev, fn) => T.event.listen(ev, e => fn(e.payload));
const $ = (s, el) => (el || document).querySelector(s);

function setMsg(el, text, kind) {
  el.textContent = text || '';
  el.className = 'msg' + (kind ? ' ' + kind : '');
}

// "Ctrl+Alt+Space" from a key press, in the form the shortcut parser reads.
function shortcutFromEvent(e) {
  const code = e.code || '';
  if (/^(Control|Alt|Shift|Meta|OS)/.test(code)) return null; // still holding modifiers
  let key = code;
  if (/^Key[A-Z]$/.test(code)) key = code.slice(3);
  else if (/^Digit[0-9]$/.test(code)) key = code.slice(5);
  const mods = [];
  if (e.ctrlKey) mods.push('Ctrl');
  if (e.altKey) mods.push('Alt');
  if (e.shiftKey) mods.push('Shift');
  if (e.metaKey) mods.push('Super');
  if (!mods.length && !/^F[0-9]{1,2}$/.test(key)) return null; // a bare key would steal typing
  return mods.concat([key]).join('+');
}

// A "Change" button next to a shortcut field: the next key press sets it.
function shortcutRecorder(button) {
  const input = document.getElementById(button.dataset.record);
  button.addEventListener('click', () => {
    const before = input.value;
    input.value = 'Press the keys…';
    button.disabled = true;
    const done = value => {
      input.value = value;
      button.disabled = false;
      window.removeEventListener('keydown', onKey, true);
    };
    const onKey = e => {
      e.preventDefault();
      if (e.key === 'Escape') return done(before);
      if (e.key === 'Backspace' || e.key === 'Delete') return done('');
      const s = shortcutFromEvent(e);
      if (s) done(s);
    };
    window.addEventListener('keydown', onKey, true);
  });
}
