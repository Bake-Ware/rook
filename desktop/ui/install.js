'use strict';
// "Install a hub on this computer": runs the bundled hub installer (Linux)
// or explains the Docker/WSL route elsewhere.
let summary = null;

invoke('install_plan').then(plan => {
  $('#title').textContent = plan.title;
  $('#message').textContent = plan.message;
  const e = plan.existing;
  $('#options').hidden = !plan.supported || !!e;
  $('#manual').hidden = plan.supported && !(e && e.mode === 'system');
  $('#manual-cmd').textContent = plan.manual;
  $('#terminal-cmd').textContent = plan.manual;
  if (e) {
    $('#existing').hidden = false;
    $('#e-url').textContent = e.dashboard || 'Unknown (its settings are not readable here)';
    const state = e.running === true ? 'running' : e.running === false ? 'stopped' : 'status unknown';
    $('#e-unit').textContent = e.unit + (e.mode === 'user' ? ' (your user)' : ' (system)') + ', ' + state;
    $('#e-manage').textContent = e.manage;
    $('#e-connect').hidden = !e.can_connect;
    $('#e-update').hidden = e.mode !== 'user';
  }
});

$('#e-connect').addEventListener('click', async ev => {
  ev.target.disabled = true;
  try {
    await invoke('install_connect', { dashboard: '', password: '' });
    setMsg($('#msg'), 'Connected. Opening your dashboard…', 'ok');
    await invoke('open_dashboard_cmd', { path: '/' });
  } catch (err) {
    setMsg($('#msg'), String(err), 'err');
  } finally {
    ev.target.disabled = false;
  }
});

$('#copy').addEventListener('click', () => navigator.clipboard.writeText($('#manual-cmd').textContent).then(
  () => setMsg($('#msg'), 'Copied.', 'ok'), () => setMsg($('#msg'), 'Select the command and copy it.', 'err')));

const log = $('#log');
listen('install-log', line => {
  log.textContent += line + '\n';
  log.scrollTop = log.scrollHeight;
});

$('#e-update').addEventListener('click', () => $('#go').click());

$('#go').addEventListener('click', async e => {
  e.target.disabled = true;
  $('#e-update').disabled = true;
  document.querySelectorAll('input[name=reach]').forEach(i => { i.disabled = true; });
  log.hidden = false;
  log.textContent = '';
  setMsg($('#msg'), 'Installing… this takes a few minutes.');
  const localOnly = document.querySelector('input[name=reach]:checked').value === 'local';
  try {
    summary = await invoke('install_run', { localOnly });
    setMsg($('#msg'), '');
    $('#d-url').textContent = summary.dashboard;
    $('#d-pass').textContent = summary.password;
    $('#done').hidden = false;
  } catch (err) {
    setMsg($('#msg'), String(err), 'err');
    $('#terminal').hidden = false;
    e.target.disabled = false;
    $('#e-update').disabled = false;
    document.querySelectorAll('input[name=reach]').forEach(i => { i.disabled = false; });
  }
});

$('#connect').addEventListener('click', async e => {
  e.target.disabled = true;
  try {
    await invoke('install_connect', { dashboard: summary.dashboard, password: summary.password });
    setMsg($('#msg'), 'Connected. Opening your dashboard…', 'ok');
    await invoke('open_dashboard_cmd', { path: '/' });
  } catch (err) {
    setMsg($('#msg'), String(err), 'err');
    e.target.disabled = false;
  }
});
