export async function mountAccount(root, {onChange = () => {}} = {}) {
  root.innerHTML = `<nav class="settings-tabs" aria-label="Account sections">
    <button data-section-tab="profile">Profile & login</button><button data-section-tab="access">Band access</button><button data-section-tab="installers">Installer approval</button>
    </nav><div class="settings-status" role="status" aria-live="polite"></div><div class="account-content"></div>
    <dialog class="settings-dialog"><div class="dialog-heading"><h2></h2><button type="button" data-close aria-label="Close">×</button></div><div class="dialog-content"></div></dialog>`;
  const $ = s => root.querySelector(s), content = $('.account-content'), dialog = $('dialog');
  let section = 'profile', pairingTimer = null, busy = false;
  function select(value) {
    section = value;
    root.querySelectorAll('[data-section]').forEach(el => {el.hidden = el.dataset.section !== value;});
    root.querySelectorAll('[data-section-tab]').forEach(el => {
      el.classList.toggle('active', el.dataset.sectionTab === value);
      el.setAttribute('aria-pressed', String(el.dataset.sectionTab === value));
    });
  }
  root.querySelectorAll('[data-section-tab]').forEach(el => {el.onclick = () => select(el.dataset.sectionTab);});
  $('[data-close]').onclick = () => dialog.close();
  dialog.addEventListener('close', () => {clearInterval(pairingTimer); pairingTimer = null; $('.dialog-content').replaceChildren();});
  async function refresh() {
    const query = location.hash.split('?')[1] || '';
    const response = await fetch('/account/component' + (query ? '?' + query : ''));
    if (!response.ok || !response.headers.get('content-type')?.includes('application/json')) throw Error('Sign in to your account and reload this view.');
    const data = await response.json();
    content.innerHTML = data.html;
    // The sign-out form belongs with the profile summary.
    content.querySelector('form')?.setAttribute('data-section', 'profile');
    const params = new URLSearchParams(query);
    if (params.has('invite') || params.get('section') === 'access') section = 'access';
    if (params.has('device')) section = 'installers';
    select(section);
    return data;
  }
  function openResult(title) {
    $('.dialog-heading h2').textContent = title; dialog.setAttribute('aria-label', title);
    $('.dialog-content').replaceChildren();
    if (!dialog.open) dialog.showModal();
    return $('.dialog-content');
  }
  function showPairing(data) {
    const box = openResult('Pair a worker');
    box.innerHTML = '<p class="muted">Use this code on the machine you want to connect.</p><strong class="pairing-code"></strong><p class="pairing-expiry"></p><label>Linux / macOS<pre class="pairing-command"></pre></label><label>Windows PowerShell<pre class="pairing-windows"></pre></label>';
    let grant = data.pairing, pending = false, stopped = false;
    function display() {
      const url = data.origin + '/worker?band=' + grant.code;
      box.querySelector('.pairing-code').textContent = grant.code;
      box.querySelector('.pairing-command').textContent = `curl -fsSL '${url}' | bash`;
      box.querySelector('.pairing-windows').textContent = `iex (irm "${url}&os=windows")`;
    }
    display(); clearInterval(pairingTimer);
    pairingTimer = setInterval(async () => {
      if (stopped) return;
      const left = Math.ceil(grant.expires - Date.now()/1000);
      box.querySelector('.pairing-expiry').textContent = Math.max(0, left) + ' seconds remaining';
      if (left > 0 || pending) return;
      pending = true;
      try {
        const response = await fetch('/account/pairing', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({csrf:data.csrf, band_id:data.band_id, session:grant.session})});
        const next = await response.json();
        if (!response.ok) throw Error(next.error || 'Pairing stopped. Generate a new code.');
        grant = next;
        if (dialog.open && box.isConnected) display();
      } catch (error) {
        box.querySelector('.pairing-expiry').textContent = error.message; stopped = true;
      } finally {pending = false;}
    }, 1000);
  }
  root.addEventListener('submit', async event => {
    const form = event.target;
    if (form.getAttribute('action') !== '/account/action') return;
    event.preventDefault(); if (busy) return;
    busy = true;
    const button = event.submitter; if (button) button.disabled = true;
    $('.settings-status').textContent = '';
    try {
      const response = await fetch('/account/action', {method:'POST', headers:{'X-Rook-View':'account'}, body:new FormData(form)});
      if (response.headers.get('content-type')?.includes('application/json')) {
        const data = await response.json();
        if (data.redirect) {location.assign(data.redirect); return;}
        if (data.pairing) showPairing(data);
        else {await refresh(); onChange(); $('.settings-status').textContent = 'Changes saved.';}
      } else {
        if (response.redirected) throw Error('Your session expired. Sign in again.');
        const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
        const main = doc.querySelector('main');
        if (!main) throw Error('Unable to complete this request.');
        const box = openResult(main.querySelector('h1')?.textContent || 'Account');
        main.querySelectorAll('script,style,link,nav,h1').forEach(el => el.remove());
        box.append(...main.childNodes);
        if (response.ok) onChange();
      }
    } catch (error) {$('.settings-status').textContent = error.message;}
    finally {busy = false; if (button) button.disabled = false;}
  });
  root.addEventListener('click', event => {
    const link = event.target.closest('a');
    if (!link) return;
    const url = new URL(link.href);
    if (url.origin !== location.origin) return;
    if (url.pathname === '/account' && !url.search) {
      event.preventDefault(); if (dialog.open) dialog.close();
      if (url.hash === '#bands') select('access');
      refresh().catch(error => {$('.settings-status').textContent = error.message;});
    } else if (url.pathname === '/account/bands') {
      event.preventDefault(); if (dialog.open) dialog.close(); location.hash = 'bands';
    }
  });
  await refresh();
  return {refresh, deactivate(){if(dialog.open)dialog.close();}, destroy(){clearInterval(pairingTimer);}};
}
