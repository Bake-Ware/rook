// Approvals: agent calls held until a person says yes or no (docs/design/permissions.md 3.11).
// Everything shown comes from the hub and is rendered as text.
const {h, ago, useCss, btn, table, two} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const OUTCOME = {approved: 'approved, not used yet', consumed: 'approved', denied: 'denied', expired: 'expired'};

export async function mountApprovals(root, opts = {}){
  useCss();
  let data = {pending: [], recent: [], grants: [], settings: {}}, timer = null, busy = false;
  const status = h('p', {class: 'mg-status', role: 'status'});
  const note = h('p', {class: 'mg-sub'});
  const waiting = h('div', {style: 'display:flex;flex-direction:column;gap:10px'});
  const recent = h('div', {class: 'mg-box'});
  const grants = h('div');
  root.replaceChildren(h('div', {class: 'mg'},
    h('div', {class: 'mg-bar'}, h('h2', {text: 'Waiting for you'}), btn('Refresh', '', () => refresh().catch(fail))),
    note, status, waiting, grants,
    h('div', {class: 'mg-bar', style: 'margin-top:18px'}, h('h2', {text: 'Recent decisions'})), recent));

  function fail(e){ status.textContent = e.message; }

  async function refresh(){
    const r = await fetch('/api/approvals', {headers: {Accept: 'application/json'}});
    const d = await r.json().catch(() => ({}));
    if(!r.ok || d.error) throw Error(d.error || 'Approvals are unavailable.');
    data = d; draw();
    if(opts.onCount) opts.onCount(data.pending.length);
  }

  async function decide(hold, decision, scope, button){
    if(busy) return;
    busy = true; button.disabled = true; status.textContent = '';
    try{
      const r = await fetch('/api/approvals/' + encodeURIComponent(hold.id), {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({decision, scope})});
      const d = await r.json().catch(() => ({}));
      if(!r.ok || d.error) throw Error(d.error || 'Request failed');
      status.textContent = (decision === 'approve' ? 'Approved ' : 'Denied ') + hold.cap + ' on ' + hold.worker +
        (decision === 'approve' ? '. The call goes ahead now, or when the agent repeats it.' : '.');
    }catch(e){ status.textContent = e.message; }
    finally{ busy = false; button.disabled = false; await refresh().catch(fail); }
  }

  const who = x => x.principal_label ? x.principal_label + ' (' + x.principal + ')' : x.principal;
  const args = x => h('details', null, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'Arguments'}),
    h('pre', {class: 'mg-code', style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;margin:6px 0 0',
      text: JSON.stringify(x.args ?? {}, null, 2)}));

  function card(x, s){
    const ok = btn('Approve', 'primary', e => decide(x, 'approve', 'call', e.currentTarget));
    const no = btn('Deny', 'danger', e => decide(x, 'deny', 'call', e.currentTarget));
    const minutes = Math.round((s.spread_grant_secs || 900) / 60);
    const more = x.kind === 'spread'
      ? btn('Approve, and allow more machines for ' + minutes + ' min', 'soft', e => decide(x, 'approve', 'spread', e.currentTarget),
        {title: 'Approves this call and lets ' + (x.principal_label || x.principal) + ' reach more machines for ' + minutes + ' minutes'})
      : null;
    return h('div', {class: 'mg-box mg-pad', style: 'gap:8px'},
      h('div', {style: 'display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;justify-content:space-between'},
        two(x.cap + ' on ' + x.worker, who(x) + (x.via && x.via.length ? ' · for ' + x.via.join(', ') : ''), true),
        h('span', {class: 'mg-dimtext', text: 'held ' + ago(x.created), title: new Date(x.created * 1000).toLocaleString()})),
      h('div', {class: 'mg-warn', text: 'Held: ' + (x.reason || x.kind)}),
      args(x),
      h('div', {class: 'mg-actions', style: 'flex-wrap:wrap'}, ok, no, more));
  }

  function draw(){
    const s = data.settings || {};
    note.textContent = s.enabled === false
      ? 'Approval holds are off (Settings > Hub > Approvals). Only the permission policy applies.'
      : 'Agents wait up to ' + Math.round(s.wait_secs || 0) + ' s for your answer, then are told to ask you and retry. ' +
        'An approval covers one identical call for ' + Math.round((s.approval_secs || 600) / 60) + ' minutes.';
    waiting.replaceChildren(...(data.pending.length ? data.pending.map(x => card(x, s))
      : [h('div', {class: 'mg-box mg-empty', text: 'Nothing is waiting. Held agent calls show up here.'})]));
    grants.replaceChildren(...(data.grants || []).map(g => h('p', {class: 'mg-sub',
      text: g.principal + ' may reach more machines until ' + new Date(g.until * 1000).toLocaleTimeString() + ' (allowed by ' + g.created_by + ').'})));
    const done = data.recent.map(x => h('div', {class: 'mg-row'},
      two(x.cap + ' on ' + x.worker, who(x), true),
      h('span', {class: x.state === 'denied' ? 'mg-bad' : x.state === 'expired' ? 'mg-dimtext' : 'mg-good',
        text: (OUTCOME[x.state] || x.state) + (x.scope === 'spread' ? ', more machines' : '')}),
      h('span', {class: 'mg-dimtext', text: x.decided_by || '—'}),
      h('span', {class: 'mg-dimtext', text: ago(x.decided_at || x.created)})));
    recent.replaceChildren(table('minmax(0,2fr) minmax(0,1.2fr) minmax(0,1fr) 90px', ['Call · who', 'Outcome', 'Decided by', 'When'],
      done.length ? done : [h('div', {class: 'mg-empty', text: 'No decisions yet.'})]));
  }

  return {
    activate(){
      refresh().catch(fail);
      clearInterval(timer);
      timer = setInterval(() => { if(!document.hidden) refresh().catch(fail); }, 5000);
    },
    deactivate(){ clearInterval(timer); timer = null; },
  };
}
