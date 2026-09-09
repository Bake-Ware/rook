export async function mountTokens(root) {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  root.innerHTML = `<p class="settings-intro">Named credentials for agents and integrations. Revoking a token stops clients that use it.</p>
    <div class="settings-status" role="status" aria-live="polite"></div>
    <section><div class="section-heading"><h2>API tokens</h2><button data-create>Create token</button></div><div class="token-list"></div></section>
    <section><h2>Chat & agent pictures</h2><p class="muted">Pictures appear in chat and activity. A base agent picture also applies to its host variants.</p><div class="identity-list"></div>
    <form class="identity-form"><label>Another identity<input name="identity" placeholder="agent:claude_kaiju" maxlength="200" required></label><button>Add picture…</button></form></section>
    <section class="settings-links"><h2>Worker pairing & band access</h2><p>Pairing codes, invitations, and enrollment controls now live with your bands.</p><a href="#account?section=access">Open band access →</a><a href="#bands">Manage bands & migrations →</a></section>
    <input class="avatar-file" type="file" accept="image/png,image/jpeg,image/webp" hidden>
    <dialog class="settings-dialog"><div class="dialog-heading"><h2></h2><button type="button" data-close aria-label="Close">×</button></div><div class="dialog-content"></div></dialog>`;
  const $ = s => root.querySelector(s), dialog = $('dialog');
  let csrf = '', identity = '', busy = false;
  const date = value => value ? new Date(value*1000).toLocaleDateString() : '—';
  async function api(data) {
    const response = await fetch('/account/tokens/api', data ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...data, csrf})} : {});
    if (!response.headers.get('content-type')?.includes('application/json')) throw Error('Sign in to your operator account to manage tokens.');
    const result = await response.json();
    if (!response.ok) throw Error(result.error || 'Token request failed.');
    return result;
  }
  async function refresh() {
    const session = await fetch('/account/session');
    if (!session.ok) throw Error('Sign in to your operator account.');
    const info = await session.json(); csrf = info.user.csrf;
    const data = await api();
    $('.token-list').innerHTML = data.tokens.length ? data.tokens.map(t => `<article class="token-row"><div><h3>${esc(t.name)}</h3><code>${esc(t.preview)}</code><p class="muted">Created ${date(t.created_at)} · Last used ${date(t.last_used_at)} · ${t.expires_at ? 'Expires '+date(t.expires_at) : 'No expiry'}</p></div><button class="danger" data-revoke="${esc(t.id)}" data-name="${esc(t.name)}">Revoke…</button></article>`).join('') : '<p class="empty">No named API tokens yet.</p>';
    const identities = [...new Set(['user:operator','agent:static',...data.tokens.map(t=>'agent:'+(t.name||t.id)),...Object.keys(data.avatars)])];
    $('.identity-list').innerHTML = identities.map(id => `<div class="identity-row">${data.avatars[id] ? `<img class="avatar" src="/api/avatar?id=${encodeURIComponent(id)}&v=${data.avatars[id]}" alt="">` : `<span class="avatar initials">${esc(id.split(':').pop().slice(0,2).toUpperCase())}</span>`}<code>${esc(id)}</code><button data-picture="${esc(id)}">${data.avatars[id]?'Change':'Set'} picture</button>${data.avatars[id]?`<button data-clear="${esc(id)}">Clear</button>`:''}</div>`).join('');
  }
  function open(title, html) {dialog.setAttribute('aria-label',title); $('.dialog-heading h2').textContent = title; $('.dialog-content').innerHTML = html; dialog.showModal();}
  $('[data-close]').onclick = () => dialog.close();
  // Clear secrets from the DOM as soon as the one-time dialog closes.
  dialog.addEventListener('close', () => $('.dialog-content').replaceChildren());
  $('[data-create]').onclick = () => open('Create API token', `<form class="token-create"><label>Label<input name="name" maxlength="64" placeholder="e.g. bakedash, claude, ci-runner" required></label><label>Expires<select name="ttl"><option value="2592000">30 days</option><option value="86400">1 day</option><option value="604800">7 days</option><option value="7776000">90 days</option><option value="31536000">1 year</option><option value="">Never</option></select></label><p class="muted">This token grants access to the operator’s MCP service. Store it in the client’s secret configuration.</p><p class="dialog-error" role="alert"></p><button>Create token</button></form>`);
  async function perform(action) {
    if (busy) return; busy = true;
    try {await action();} catch(error) {const target = dialog.open ? $('.dialog-error') : $('.settings-status'); (target || $('.settings-status')).textContent = error.message;}
    finally {busy = false;}
  }
  root.addEventListener('submit', event => {
    event.preventDefault(); const form = event.target;
    if (form.matches('.identity-form')) {identity = form.elements.identity.value.trim(); $('.avatar-file').click(); return;}
    if (form.matches('.token-create')) perform(async () => {
      const data = await api({op:'create', name:form.elements.name.value, ttl:form.elements.ttl.value ? Number(form.elements.ttl.value) : null});
      $('.dialog-heading h2').textContent = 'Copy your new token';
      $('.dialog-content').innerHTML = '<p>This secret is shown once. Copy it before closing.</p><pre class="token-secret" tabindex="0"></pre><button type="button" data-copy>Copy token</button><p class="copy-status" role="status"></p>';
      $('.token-secret').textContent = data.token;
      $('[data-copy]').onclick = async () => {try {await navigator.clipboard.writeText(data.token); $('.copy-status').textContent = 'Copied.';}catch { $('.copy-status').textContent = 'Select and copy the token above.';}};
      await refresh();
    });
    if (form.matches('.token-revoke')) perform(async () => {await api({op:'revoke', id:form.dataset.id, confirm:true}); dialog.close(); await refresh(); $('.settings-status').textContent = 'Token revoked.';});
  });
  root.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.dataset.revoke) open('Revoke token', `<form class="token-revoke" data-id="${esc(button.dataset.revoke)}"><p>Revoke <strong>${esc(button.dataset.name)}</strong>? Clients using it will lose access.</p><p class="dialog-error" role="alert"></p><button class="danger">Revoke token</button></form>`);
    if (button.dataset.picture) {identity = button.dataset.picture; $('.avatar-file').click();}
    if (button.dataset.clear) perform(async () => {await api({op:'avatar_clear', identity:button.dataset.clear}); await refresh();});
  });
  $('.avatar-file').onchange = () => perform(async () => {
    const file = $('.avatar-file').files[0]; if (!file) return;
    const bitmap = await createImageBitmap(file), canvas = document.createElement('canvas'); canvas.width = canvas.height = 96;
    const side = Math.min(bitmap.width, bitmap.height);
    canvas.getContext('2d').drawImage(bitmap,(bitmap.width-side)/2,(bitmap.height-side)/2,side,side,0,0,96,96); bitmap.close();
    await api({op:'avatar', identity, data:canvas.toDataURL('image/png')}); $('.avatar-file').value = ''; await refresh();
  });
  await refresh();
  return {refresh, deactivate(){if(dialog.open)dialog.close();}};
}
