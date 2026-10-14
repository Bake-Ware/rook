'use strict';
// "Install a hub on this computer": runs the bundled hub installer (Linux)
// or explains the Docker/WSL route elsewhere.
let summary = null;

invoke('install_plan').then(plan => {
  $('#title').textContent = plan.title;
  $('#message').textContent = plan.message;
  $('#options').hidden = !plan.supported;
  $('#manual').hidden = plan.supported;
  $('#manual-cmd').textContent = plan.manual;
  $('#terminal-cmd').textContent = plan.manual;
});

$('#copy').addEventListener('click', () => navigator.clipboard.writeText($('#manual-cmd').textContent).then(
  () => setMsg($('#msg'), 'Copied.', 'ok'), () => setMsg($('#msg'), 'Select the command and copy it.', 'err')));

const log = $('#log');
listen('install-log', line => {
  log.textContent += line + '\n';
  log.scrollTop = log.scrollHeight;
});

$('#go').addEventListener('click', async e => {
  e.target.disabled = true;
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
