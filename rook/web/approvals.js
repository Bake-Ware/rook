// Approvals: agent calls held until a person says yes or no (docs/design/permissions.md 3.11).
// Everything shown comes from the hub and is rendered as text.
const {h, ago, useCss, btn, table, two, info} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const OUTCOME = {approved: 'approved, not used yet', consumed: 'approved', denied: 'denied', expired: 'expired'};

export async function mountApprovals(root, opts = {}){
  useCss();
  let data = {pending: [], trust: [], recent: [], settings: {}}, timer = null, busy = false;
  const status = h('p', {class: 'mg-status', role: 'status'});
  const warn = h('p', {class: 'mg-box mg-pad mg-bad', role: 'alert', hidden: true});
  const note = h('p');
  const waiting = h('div', {style: 'display:flex;flex-direction:column;gap:10px'});
  const recent = h('div', {class: 'mg-box'});
  const relayed = h('div', {class: 'mg-box', hidden: true});
  const relayHead = h('div', {class: 'mg-bar ap-recent', hidden: true}, h('h2', {text: 'Relayed for you'}),
    info('Decisions an agent passed on from you, with your words. Who may relay is set per client on the Tokens page (May relay my approvals).', 'About relayed decisions'));
  const count = h('span', {class: 'mg-tag line'});
  // How holds work is behind the (i), not in the flow.
  root.replaceChildren(h('div', {class: 'mg ap'},
    h('div', {class: 'mg-bar'}, h('h2', {class: 'ap-h'}, 'Waiting', count), info(note, 'How approvals work'),
      h('span', {class: 'mg-grow'}), btn('↻', 'icon', () => refresh().catch(fail), {'aria-label': 'Refresh', title: 'Refresh'})),
    warn, status, waiting,
    h('div', {class: 'mg-bar ap-recent'}, h('h2', {text: 'Recent'})), recent, relayHead, relayed));

  function fail(e){ status.textContent = e.message; }

  async function refresh(){
    const r = await fetch('/api/approvals', {headers: {Accept: 'application/json'}});
    const d = await r.json().catch(() => ({}));
    if(!r.ok || d.error) throw Error(d.error || 'Approvals are unavailable.');
    data = d; draw();
    if(opts.onCount) opts.onCount(data.pending.length);
  }

  async function decide(hold, decision, button){
    if(busy) return;
    busy = true; button.disabled = true; status.textContent = '';
    try{
      const r = await fetch('/api/approvals/' + encodeURIComponent(hold.id), {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({decision})});
      const d = await r.json().catch(() => ({}));
      if(!r.ok || d.error) throw Error(d.error || 'Request failed');
      status.textContent = (decision === 'approve' ? 'Approved: ' : 'Denied: ') + (hold.plain ? hold.plain.title : hold.cap + ' on ' + hold.worker) + '.';
    }catch(e){ status.textContent = e.message; }
    finally{ busy = false; button.disabled = false; await refresh().catch(fail); }
  }

  const who = x => x.principal_label ? x.principal_label + ' (' + x.principal + ')' : x.principal;
  const args = x => h('details', null, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'Args'}),
    h('pre', {class: 'mg-code', style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;margin:6px 0 0',
      text: JSON.stringify(x.args ?? {}, null, 2)}));

  function card(x){
    const can = data.can_decide !== false;
    const ok = btn('Approve', 'primary', e => decide(x, 'approve', e.currentTarget), {disabled: !can});
    const no = btn('Deny', 'danger', e => decide(x, 'deny', e.currentTarget), {disabled: !can});
    const held = h('span', {class: 'mg-dimtext', text: 'held ' + ago(x.created), title: new Date(x.created * 1000).toLocaleString()});
    const p = x.plain;
    if(!p) return h('div', {class: 'mg-box mg-pad ap-card', style: 'gap:8px'},
      h('div', {style: 'display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;justify-content:space-between'},
        two(x.cap + ' on ' + x.worker, (x.name ? x.name + ' · ' : '') + who(x) + (x.via && x.via.length ? ' · for ' + x.via.join(', ') : ''), true), held),
      h('div', {class: 'mg-warn', text: 'Held: ' + (x.reason || x.kind)}),
      args(x),
      h('div', {class: 'mg-actions ap-acts', style: 'flex-wrap:wrap'}, no, ok));
    // The plain ask (rook/hub/plain.py): title, for, who, what, why, risk; the cap, args and trust entry under Explain.
    const rows = [['For', p.for], ['Who', p.who], ['I want to', p.want]].concat(p.why ? [['Why', p.why]] : []).concat([['Risk', p.risk]]);
    const explain = h('details', null, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'Explain'}),
      h('p', {class: 'mg-sub', style: 'margin:6px 0;overflow-wrap:anywhere', text: p.explain}),
      h('pre', {class: 'mg-code', style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:240px;overflow:auto;margin:6px 0 0',
        text: JSON.stringify(x.args ?? {}, null, 2)}));
    return h('div', {class: 'mg-box mg-pad ap-card', style: 'gap:8px'},
      h('div', {style: 'display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;justify-content:space-between'},
        h('strong', {style: 'font-size:15px', text: p.title}),
        h('span', {class: 'mg-dimtext'}, x.name ? h('span', {class: 'mg-tag line', style: 'margin-right:8px', text: x.name}) : null, held)),
      h('div', {class: 'ap-plain', style: 'display:grid;grid-template-columns:84px minmax(0,1fr);gap:3px 10px;font-size:13.5px'},
        ...rows.flatMap(([k, v]) => [h('span', {class: 'mg-dimtext', text: k}), h('span', {style: 'overflow-wrap:anywhere', text: v})])),
      explain,
      h('div', {class: 'mg-actions ap-acts', style: 'flex-wrap:wrap'}, no, ok));
  }

  // A trust-map proposal: the person decides it on Manage > Trust (their words recorded there).
  function trustCard(x){
    const rows = [['Who', x.by], ['I want to', x.sentence]].concat(x.why ? [['Why', x.why]] : [])
      .concat([['Risk', x.loosens ? 'High: it loosens the trust map' + (x.sacred ? ', on something sacred' : '') + '; only you can say yes' : 'Low: it only makes the trust map stricter']]);
    return h('div', {class: 'mg-box mg-pad ap-card', style: 'gap:8px'},
      h('div', {style: 'display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline;justify-content:space-between'},
        h('strong', {style: 'font-size:15px', text: 'Change the trust map'}),
        h('span', {class: 'mg-dimtext', text: 'proposed ' + ago(x.at), title: new Date(x.at * 1000).toLocaleString()})),
      h('div', {class: 'ap-plain', style: 'display:grid;grid-template-columns:84px minmax(0,1fr);gap:3px 10px;font-size:13.5px'},
        ...rows.flatMap(([k, v]) => [h('span', {class: 'mg-dimtext', text: k}), h('span', {style: 'overflow-wrap:anywhere', text: v})])),
      h('div', {class: 'mg-actions ap-acts'}, btn('Open Trust', 'primary', () => { if(window.showView) window.showView('trust'); else location.hash = '#trust'; })));
  }

  function draw(){
    const s = data.settings || {};
    warn.hidden = !data.weak_message;
    warn.textContent = data.weak_message || '';
    const n = data.pending.length + (data.trust || []).length;
    count.textContent = String(n); count.hidden = !n;
    note.textContent = 'Only the hard stops and the trust map hold a call. Agents wait up to ' + Math.round(s.wait_secs || 0) +
      ' s for your answer, then are told to ask you and retry. An ask waits until you answer, and an approval covers one identical call; neither expires.';
    waiting.replaceChildren(...(n ? data.pending.map(card).concat((data.trust || []).map(trustCard))
      : [h('div', {class: 'mg-box mg-empty', text: 'Nothing waiting.'})]));
    const done = data.recent.map(x => h('div', {class: 'mg-row ap-row'},
      two(x.plain ? x.plain.title : x.cap + ' on ' + x.worker, x.plain ? x.plain.who + ' · ' + x.cap + ' on ' + x.worker : who(x), true),
      h('span', {class: x.state === 'denied' ? 'mg-bad' : x.state === 'expired' ? 'mg-dimtext' : 'mg-good',
        text: OUTCOME[x.state] || x.state}),
      h('span', {class: 'mg-dimtext ap-by', text: x.relayed_text || x.decided_by || '—', title: x.relayed ? 'session: ' + x.relayed.session : ''}),
      h('span', {class: 'mg-dimtext', text: ago(x.decided_at || x.created)})));
    const rel = data.relays || [];
    relayHead.hidden = relayed.hidden = !rel.length;
    relayed.replaceChildren(table('70px minmax(0,2fr) minmax(0,2fr) 90px', ['Kind', 'What', 'Decision', 'When'], rel.map(r => h('div', {class: 'mg-row ap-row'},
      h('span', {class: 'mg-tag line', text: r.kind}), h('span', {text: r.summary || r.item}),
      h('span', {class: r.decision === 'deny' ? 'mg-bad' : 'mg-good', text: r.text + (r.reason ? ' · reason: ' + r.reason : ''), title: 'session: ' + r.session}),
      h('span', {class: 'mg-dimtext', text: ago(r.created)})))));
    recent.replaceChildren(table('minmax(0,2fr) minmax(0,1.2fr) minmax(0,1fr) 90px', ['Ask · who', 'Outcome', 'By', 'When'],
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
