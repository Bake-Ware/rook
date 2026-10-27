'use strict';
// The quick-ask box. Typed questions go through the `ask` command; voice
// (push-to-talk, wake word) is driven by the app, which sends
// `overlay-state` events as it goes.
const q = $('#q'), statusEl = $('#status'), answerEl = $('#answer'), asked = $('#asked');
let busy = false, hadFocus = false;

const PHASES = {
  idle: '', recording: 'Listening… press again (or let go of the keys) to send',
  wake: 'Listening for your question', transcribing: 'Turning speech into text',
  thinking: 'Asking the home agent',
};

function show(phase, text, question) {
  busy = ['recording', 'wake', 'transcribing', 'thinking'].includes(phase);
  $('#mic').classList.toggle('live', phase === 'recording' || phase === 'wake');
  statusEl.className = 'status' + (phase === 'error' ? ' err' : '') + (busy ? ' dots' : '');
  statusEl.textContent = phase === 'error' ? text : (PHASES[phase] || '');
  if (question) { asked.hidden = false; asked.textContent = question; }
  if (phase === 'answer') answerEl.textContent = text;
  else if (busy) answerEl.textContent = '';
}

async function askTyped(text) {
  $('#open-chat').hidden = true;
  show('thinking', '', text);
  try {
    const a = await invoke('ask', { question: text });
    show('answer', a.text || '(No answer.)', text);
    $('#open-chat').hidden = a.via !== 'chat';
    q.value = '';
  } catch (e) {
    show('error', String(e), text);
  }
}

$('#f').addEventListener('submit', e => {
  e.preventDefault();
  const text = q.value.trim();
  if (text && !busy) askTyped(text);
});
$('#mic').addEventListener('click', () => invoke('ptt_toggle').catch(e => show('error', String(e))));
$('#open-chat').addEventListener('click', () => invoke('open_dashboard_cmd', { path: '/#chat' }));
$('#settings').addEventListener('click', () => invoke('open_settings', { section: 'ask' }));
$('#panel').addEventListener('click', () => { invoke('overlay_hide'); invoke('show_window', { name: 'panel' }); });
$('#ask-page').addEventListener('click', () => invoke('ask_on_page_cmd').catch(e => show('error', String(e))));
$('#stop-speaking').addEventListener('click', () => invoke('stop_speaking'));
// Esc stops the answer being read aloud first, then closes.
let speaking = false;
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (speaking) invoke('stop_speaking'); else invoke('overlay_hide');
});
listen('speaking', on => { speaking = !!on; $('#stop-speaking').hidden = !on; });
listen('speak-error', text => { statusEl.className = 'status err'; statusEl.textContent = 'Not read aloud: ' + text; });
// Close when you click elsewhere, unless an answer is on its way. Reading
// aloud goes on in the background (the tray app keeps it).
window.addEventListener('focus', () => { hadFocus = true; });
window.addEventListener('blur', () => { if (hadFocus && !busy) invoke('overlay_hide'); });

listen('overlay-state', p => {
  if (p.phase === 'answer' || p.phase === 'error') $('#open-chat').hidden = true;
  show(p.phase, p.text, p.question);
});
listen('overlay-show', () => { q.focus(); q.select(); });

invoke('get_state').then(st => {
  const s = st.settings;
  if (st.features.voice && s.ptt_shortcut) $('#ptt-hint').innerHTML = 'hold <kbd></kbd> to talk';
  const k = $('#ptt-hint kbd'); if (k) k.textContent = s.ptt_shortcut;
  if (!st.features.voice) $('#mic').hidden = true;
  $('#ask-page').hidden = !st.signed_in;
  if (!st.signed_in) show('error', st.hub ? 'Sign in to your hub first (Settings).' : 'Set up your hub first (Settings).');
});
q.focus();
