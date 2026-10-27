'use strict';
// The settings window.
let S = null; // last state from the app

const FIELDS = ['hub_url', 'ask_shortcut', 'poll_seconds', 'voice_url', 'ptt_shortcut', 'speak_answers', 'wake_model', 'wake_threshold'];
const CHECKS = ['notifications', 'wake_word', 'worker_autostart', 'start_at_login'];

function fill(st) {
  S = st;
  const s = st.settings;
  for (const f of FIELDS) {
    const el = document.getElementById(f);
    if (el && document.activeElement !== el) el.value = s[f] ?? '';
  }
  for (const f of CHECKS) document.getElementById(f).checked = !!s[f];
  if (![...$('#poll_seconds').options].some(o => o.value == s.poll_seconds)) {
    $('#poll_seconds').add(new Option(s.poll_seconds + ' seconds', s.poll_seconds));
    $('#poll_seconds').value = s.poll_seconds;
  }
  $('#wake-val').textContent = Number(s.wake_threshold).toFixed(2);
  $('#version').textContent = 'v' + st.version;

  const pill = $('#signin-pill');
  pill.textContent = st.signed_in ? 'Signed in' : (st.hub ? 'Not signed in' : 'Not set up');
  pill.className = 'pill ' + (st.signed_in ? 'ok' : 'warn');
  $('#signin-note').textContent = st.signed_in
    ? 'Signed in to ' + st.hub + (st.principal ? ' as ' + st.principal : '') + '.'
    : 'Sign in opens your dashboard in a window. Sign in there as usual and this app picks it up.';
  $('#signout').hidden = !st.signed_in;
  $('#signin-window').textContent = st.signed_in ? 'Sign in again' : 'Sign in';

  $('#voice_key').placeholder = st.voice_key_set ? 'Saved (type to replace)' : 'Not set';
  $('#agent_token').placeholder = st.agent_token_set ? 'Saved (type to replace)' : 'Leave empty to ask in a private chat';
  $('#clear-token').hidden = !st.agent_token_set;

  const mic = $('#mic-pill');
  mic.textContent = st.microphone ? 'Mic: ' + st.microphone.slice(0, 28) : (st.features.voice ? 'No microphone' : 'Voice not in this build');
  mic.className = 'pill ' + (st.microphone ? 'ok' : '');
  const voiceOn = st.features.voice;
  for (const id of ['voice_url', 'voice_key', 'ptt_shortcut', 'speak_answers']) document.getElementById(id).disabled = !voiceOn;

  const u = st.update;
  $('#update-pill').textContent = u ? 'Update: v' + u.version : 'v' + st.version;
  $('#update-pill').className = 'pill ' + (u ? 'warn' : 'ok');
  $('#update-box').hidden = !u;
  if (u) $('#update-ver').textContent = u.version;
  $('#update-note').textContent = u
    ? 'Version ' + u.version + ' is out (' + u.name + '). It downloads in your browser; run it to update. The installers aren\'t signed yet, so your computer may ask you to confirm.'
    : (st.signed_in ? 'Your hub tells the app when a new version is published.' : '');
  const wakeOn = st.features.wake_word;
  for (const id of ['wake_word', 'wake_model', 'wake_threshold']) document.getElementById(id).disabled = !wakeOn;
  $('#wake-note').textContent = wakeOn
    ? 'The microphone stays on while this is on. Nothing is sent until the wake word is heard.'
    : 'This build has no wake word. Push-to-talk works; see the README to build with the wake word.';

  const w = st.worker;
  const wp = $('#worker-pill');
  wp.textContent = { running: 'Running', stopped: 'Stopped', 'needs-pairing': 'Not paired', 'not-installed': 'Not added' }[w.status] || w.status;
  wp.className = 'pill ' + (w.status === 'running' ? 'ok' : w.status === 'stopped' ? '' : 'warn');
  $('#worker-detail').textContent = w.detail;
  $('#worker-start').hidden = !w.can_start;
  $('#worker-stop').hidden = !w.can_stop;
  $('#worker-add').hidden = w.status !== 'not-installed' && w.status !== 'needs-pairing';
  $('#autostart-box').hidden = w.mode !== 'bundle';
}

function collect() {
  const s = Object.assign({}, S.settings);
  for (const f of FIELDS) s[f] = document.getElementById(f).value.trim();
  for (const f of CHECKS) s[f] = document.getElementById(f).checked;
  s.poll_seconds = Number(s.poll_seconds) || 30;
  s.wake_threshold = Number(s.wake_threshold) || 0.5;
  return s;
}

async function refresh() { fill(await invoke('get_state')); }

async function save() {
  const msg = $('#save-msg');
  setMsg(msg, 'Saving…');
  try {
    const secrets = {};
    if ($('#voice_key').value) secrets.voiceKey = $('#voice_key').value;
    if ($('#agent_token').value) secrets.agentToken = $('#agent_token').value;
    if (Object.keys(secrets).length) await invoke('save_secrets', secrets);
    $('#voice_key').value = ''; $('#agent_token').value = '';
    fill(await invoke('save_settings', { settings: collect() }));
    setMsg(msg, 'Saved.', 'ok');
  } catch (e) {
    setMsg(msg, String(e), 'err');
    refresh();
  }
}

async function run(button, msgEl, fn) {
  button.disabled = true;
  try { await fn(); } catch (e) { setMsg(msgEl, String(e), 'err'); } finally { button.disabled = false; }
}

document.querySelectorAll('[data-record]').forEach(shortcutRecorder);
$('#save').addEventListener('click', save);
$('#wake_threshold').addEventListener('input', e => { $('#wake-val').textContent = Number(e.target.value).toFixed(2); });

$('#signin-window').addEventListener('click', e => run(e.target, $('#hub-msg'), async () => {
  await invoke('sign_in_browser', { hub: $('#hub_url').value });
  setMsg($('#hub-msg'), 'Sign in in the dashboard window. This page updates when you are in.');
}));
$('#signin-password-toggle').addEventListener('click', () => {
  $('#password-form').hidden = !$('#password-form').hidden;
  if (!$('#password-form').hidden) $('#password').focus();
});
$('#password-form').addEventListener('submit', e => {
  e.preventDefault();
  const btn = e.target.querySelector('button');
  run(btn, $('#hub-msg'), async () => {
    setMsg($('#hub-msg'), 'Signing in…');
    fill(await invoke('sign_in_password', { hub: $('#hub_url').value, username: $('#username').value, password: $('#password').value }));
    $('#password').value = '';
    $('#password-form').hidden = true;
    setMsg($('#hub-msg'), 'Signed in.', 'ok');
  });
});
$('#open-dashboard').addEventListener('click', e => run(e.target, $('#hub-msg'), () => invoke('open_dashboard_cmd', {})));
$('#signout').addEventListener('click', e => run(e.target, $('#hub-msg'), async () => {
  fill(await invoke('sign_out'));
  setMsg($('#hub-msg'), 'Signed out.');
}));
$('#try-ask').addEventListener('click', () => invoke('show_window', { name: 'overlay' }));
$('#clear-token').addEventListener('click', e => run(e.target, $('#save-msg'), async () => fill(await invoke('save_secrets', { agentToken: '' }))));
$('#test-notify').addEventListener('click', () => invoke('test_notification'));
$('#worker-start').addEventListener('click', e => run(e.target, $('#save-msg'), async () => fill(await invoke('worker_action', { action: 'start' }))));
$('#worker-stop').addEventListener('click', e => run(e.target, $('#save-msg'), async () => fill(await invoke('worker_action', { action: 'stop' }))));
$('#worker-add').addEventListener('click', e => run(e.target, $('#save-msg'), () => invoke('open_dashboard_cmd', { path: '/#install' })));
$('#open-install').addEventListener('click', () => invoke('show_window', { name: 'install' }));
$('#update-get').addEventListener('click', e => run(e.target, $('#save-msg'), () => invoke('open_update')));

listen('state-changed', refresh);
listen('goto', id => document.getElementById(id)?.scrollIntoView());
refresh().then(() => { if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView(); });
