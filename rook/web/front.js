// Decisions: the house agent as front door (docs/design/front-door.md).
// Its one-line asks (Yes / No / Explain), the decisions it made for you with
// the entry each one cited and a one-tap Overturn, and its playbook.
// Everything shown comes from the hub and is rendered as text.
const {h, ago, useCss, btn, info} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const API = '/account/huddle/api';
const KIND = {did: 'did', dispatched: 'session', huddle: 'huddle', ruled: 'ruled', tightened: 'tightened'};
const PATH = {answer: 'answered', do: 'did it', dispatch: 'session', huddle: 'huddle', ask: 'asked you', onboard: 'new project'};
const STAGE = {brainstorm: 'brainstorm', offered: 'offered', intake: 'asking', readback: 'plan', done: 'set up', declined: 'declined', cancelled: 'dropped'};

export async function mountFront(root, opts = {}){
  useCss();
  let csrf = '', data = null, timer = null, busy = false;
  const status = h('p', {class: 'mg-status', role: 'status'});
  const off = h('p', {class: 'mg-box mg-pad', hidden: true});
  const count = h('span', {class: 'mg-tag line', hidden: true});
  const asks = h('div', {class: 'fd-list'});
  const asksHead = h('div', {class: 'mg-bar', hidden: true}, h('h2', null, 'For you ', count));
  const decisions = h('div', {class: 'fd-list'});
  const borrowed = h('div', {class: 'fd-list'});
  const borrowedHead = h('div', {class: 'mg-bar', hidden: true}, h('h2', {text: 'Borrowed now'}));
  const projects = h('div', {class: 'fd-list', style: 'margin-top:8px'});
  const projectsBox = h('details', {hidden: true}, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'New projects'}), projects);
  const answered = h('div', {class: 'fd-list'});
  const book = h('textarea', {class: 'mg-input fd-book', rows: 18, spellcheck: false, 'aria-label': 'Playbook'});
  const bookNote = h('p', {class: 'mg-sub'});
  const bookHist = h('div', {class: 'fd-list'});
  const requests = h('div', {class: 'fd-list'});
  const css = h('style', {text: `
    .fd-list{display:flex;flex-direction:column;gap:8px}
    .fd-card{display:flex;flex-direction:column;gap:6px;min-width:0}
    .fd-line{font-weight:600;overflow-wrap:anywhere}
    .fd-meta{display:flex;flex-wrap:wrap;gap:4px 10px;align-items:baseline}
    .fd-acts{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}
    .fd-acts .mg-btn{min-height:40px;min-width:72px}
    .fd-cite{overflow-wrap:anywhere}
    .fd-book{width:100%;box-sizing:border-box;font-family:var(--mono,monospace);font-size:13px;min-height:260px}
    .fd-over{opacity:.65}
    @media (max-width:760px){.fd-acts{justify-content:stretch}.fd-acts .mg-btn{flex:1}}`});
  root.replaceChildren(h('div', {class: 'mg fd'}, css,
    h('div', {class: 'mg-bar'}, h('h2', {text: 'Decisions'}),
      info(h('div', null,
        h('p', {text: 'The house agent takes your requests from voice, the web, chat and other clients: it answers, does quick things the trust map allows, starts coding sessions, or brings in the team. It only asks you when agents disagree and the trust map or its playbook can’t settle it, when something is sacred, or when nothing covers it.'}),
        h('p', {text: 'Overturn tightens what a decision relied on: the trust entry goes one level stricter, or the playbook gets an “Ask me first” rule. The action itself is not undone.'})), 'How the house agent decides'),
      h('span', {class: 'mg-grow'}),
      btn('↻', 'icon', () => refresh().catch(fail), {'aria-label': 'Refresh', title: 'Refresh'})),
    status, off, asksHead, asks, borrowedHead, borrowed,
    h('div', {class: 'mg-bar'}, h('h2', {text: 'Decided for you'})), decisions,
    projectsBox,
    h('details', null, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'Answered'}), answered),
    h('details', null, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'Playbook'}),
      h('div', {class: 'fd-card', style: 'margin-top:8px'}, bookNote, book,
        h('div', {class: 'fd-acts'}, btn('Save playbook', 'primary', e => saveBook(e.currentTarget))),
        h('div', {class: 'mg-sub', text: 'Changes'}), bookHist)),
    h('details', null, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'Recent requests'}), requests)));

  function fail(e){ status.textContent = e.message; }

  async function api(action, fields = {}){
    if(!csrf){
      const r = await fetch(API, {headers: {Accept: 'application/json'}});
      const d = await r.json().catch(() => ({}));
      if(!r.ok || d.error) throw Error(d.error || 'Sign in with an owner account.');
      csrf = d.csrf;
    }
    const r = await fetch(API, {method: 'POST', headers: {'Content-Type': 'application/json', Accept: 'application/json'},
      body: JSON.stringify({csrf, action, ...fields})});
    const d = await r.json().catch(() => ({}));
    if(!r.ok || d.error) throw Error(d.error || 'Request failed');
    return d.result;
  }

  async function refresh(){
    data = await api('front_view');
    draw();
    if(opts.onCount) opts.onCount((data.asks || []).length);
  }

  async function act(button, fn){
    if(busy) return;
    busy = true; if(button) button.disabled = true; status.textContent = '';
    try{ await fn(); }
    catch(e){ status.textContent = e.message; }
    finally{ busy = false; if(button) button.disabled = false; await refresh().catch(fail); }
  }

  function askCard(a){
    const why = h('div', {class: 'mg-sub', hidden: true});
    const moreText = h('div', {class: 'mg-sub', style: 'white-space:pre-wrap', hidden: true});
    const moreBtn = a.has_more ? h('a', {href: '#', class: 'mg-sub', text: 'More', hidden: true, onclick: async e => {
      e.preventDefault();
      try{ const x = await api('front_explain', {ask: a.id, more: true}); moreText.textContent = x.more; moreText.hidden = false; e.target.hidden = true; }
      catch(err){ status.textContent = err.message; }
    }}) : null;
    const explain = btn('Explain', 'soft', () => {
      why.hidden = !why.hidden;
      if(moreBtn) moreBtn.hidden = why.hidden;
      if(why.hidden) moreText.hidden = true;
    });
    why.textContent = a.explain + (a.cite ? ' (' + a.cite + ')' : '');
    const yes = btn('Yes', 'primary', e => act(e.currentTarget, async () => {
      const x = await api('front_answer', {ask: a.id, decision: 'yes'});
      status.textContent = (x.note || 'Done.') + (x.remember ? ' I asked whether to remember it.' : '');
    }));
    const no = btn('No', 'danger', e => act(e.currentTarget, async () => {
      const x = await api('front_answer', {ask: a.id, decision: 'no'});
      status.textContent = x.note || 'Nothing was done.';
    }));
    return h('div', {class: 'mg-box mg-pad fd-card'},
      h('div', {class: 'fd-line', text: a.line}),
      h('div', {class: 'fd-meta mg-dimtext'}, h('span', {text: a.name || a.id}), h('span', {text: ago(a.created)})),
      why, moreBtn, moreText,
      h('div', {class: 'fd-acts'}, no, explain, yes));
  }

  function decisionRow(d){
    const o = d.overturned;
    const over = o ? null : btn('Overturn', 'soft', e => {
      const words = prompt('Overturn “' + d.summary + '”? In your words (recorded):', 'Ask me next time');
      if(words === null) return;
      act(e.currentTarget, async () => {
        const x = await api('front_overturn', {decision: d.id, words});
        status.textContent = 'Overturned: ' + (x.effects || []).join('; ') + '. ' + (x.note || '');
      });
    });
    return h('div', {class: 'mg-box mg-pad fd-card' + (o ? ' fd-over' : '')},
      h('div', {class: 'fd-meta'}, h('span', {class: 'mg-tag line', text: KIND[d.kind] || d.kind}),
        h('span', {class: 'fd-line', text: d.summary})),
      d.cite ? h('div', {class: 'mg-sub fd-cite', text: 'Cited: ' + d.cite}) : null,
      o ? h('div', {class: 'mg-sub', text: 'Overturned by ' + o.by + ' ' + ago(o.at) + ((o.effects || []).length ? ': ' + o.effects.join('; ') : '')}) : null,
      h('div', {class: 'fd-meta mg-dimtext'}, h('span', {text: (d.name || d.id) + ' · ' + ago(d.created)}), h('span', {class: 'mg-grow'}), over));
  }

  function leaseCard(r){
    return h('div', {class: 'mg-box mg-pad fd-card'},
      h('div', {class: 'fd-meta'}, h('span', {class: 'fd-line', text: r.what}),
        r.project ? h('span', {class: 'mg-tag line', text: r.project}) : null),
      r.words ? h('q', {class: 'mg-sub', text: r.words}) : null,
      h('div', {class: 'mg-sub fd-cite', text: 'Restore: ' + r.restore}),
      h('div', {class: 'fd-meta mg-dimtext'}, h('span', {text: r.resource + (r.exception ? ' · exception ' + r.exception : '')}),
        r.until ? h('span', {text: 'until ' + r.until}) : null));
  }

  function threadCard(t){
    const steps = t.stage === 'done' ? (t.result || []) : [];
    return h('div', {class: 'mg-box mg-pad fd-card'},
      h('div', {class: 'fd-meta'}, h('span', {class: 'mg-tag line', text: STAGE[t.stage] || t.stage}),
        h('span', {class: 'fd-line', text: t.title || 'Brainstorm'})),
      t.stage === 'readback' && t.readback ? h('div', {class: 'mg-sub', text: t.readback}) : null,
      ...steps.map(x => h('div', {class: 'mg-sub'},
        h('span', {class: x.ok ? 'mg-good' : 'mg-bad', text: x.ok ? 'done' : x.asked ? 'asks you' : 'not done'}),
        ' ' + x.step + (x.error ? ': ' + x.error : '') + (x.asked ? ': ' + x.asked : '')
        + (x.proposal ? ' · accept ' + x.proposal + ' on Trust' : ''))),
      h('div', {class: 'fd-meta mg-dimtext'}, h('span', {text: (t.notes ? t.notes + ' note' + (t.notes > 1 ? 's' : '') + ' · ' : '') + (t.source || '') + ' · ' + ago(t.updated)})));
  }

  async function saveBook(button){
    const words = prompt('What changed, in your words (recorded with the playbook):', '');
    if(words === null) return;
    act(button, async () => {
      const x = await api('front_playbook_save', {text: book.value, words});
      const n = (x.changes || []).length;
      status.textContent = 'Playbook saved' + (n ? ' (' + n + ' change' + (n > 1 ? 's' : '') + ')' : '') + '.'
        + ((x.problems || []).length ? ' ' + x.problems.join('; ') : '');
      book.dataset.dirty = '';
    });
  }

  function draw(){
    off.hidden = data.front_door !== false;
    off.textContent = 'The front door is off: the house agent only answers. Turn it on under Intelligence sources > Home agent (Front door).';
    const a = data.asks || [];
    asksHead.hidden = !a.length; count.hidden = !a.length; count.textContent = String(a.length);
    asks.replaceChildren(...a.map(askCard));
    const ls = data.borrowed || [];
    borrowedHead.hidden = !ls.length;
    borrowed.replaceChildren(...ls.map(leaseCard));
    const ts = data.onboarding || [];
    projectsBox.hidden = !ts.length;
    projects.replaceChildren(...ts.map(threadCard));
    const ds = data.decisions || [];
    decisions.replaceChildren(...(ds.length ? ds.map(decisionRow) : [h('div', {class: 'mg-box mg-empty', text: 'Nothing decided for you yet.'})]));
    answered.replaceChildren(...(data.answered || []).map(x => h('div', {class: 'mg-row fd-meta'},
      h('span', {class: x.state === 'no' ? 'mg-bad' : x.state === 'yes' ? 'mg-good' : 'mg-dimtext', text: x.state}),
      h('span', {text: x.line}), h('span', {class: 'mg-dimtext', text: (x.decided_by || '') + ' ' + ago(x.decided) + (x.note ? ' · ' + x.note : '')}))));
    const pb = data.playbook || {};
    if(!book.dataset.dirty) book.value = pb.text || '';
    book.oninput = () => { book.dataset.dirty = '1'; };
    bookNote.textContent = (pb.problems || []).length ? pb.problems.join('; ') : 'One rule per line under a heading. Adding to “Just do it”, coding sessions or “Settling disagreements” lets the house agent do more without asking you.';
    bookHist.replaceChildren(...(pb.history || []).slice(0, 12).map(r => h('div', {class: 'mg-sub'},
      (r.op || '') + (r.text ? ' “' + r.text + '”' : '') + (r.direction ? ' (' + r.direction + ')' : '') + ' · ' + (r.who || '') + ' · ' + ago(r.at)
      + (r.words ? ' · “' + r.words + '”' : ''))));
    requests.replaceChildren(...(data.requests || []).map(r => h('div', {class: 'mg-row fd-meta'},
      h('span', {class: 'mg-tag line', text: PATH[r.path] || r.path || '…'}),
      h('span', {text: r.text}), h('span', {class: 'mg-dimtext', text: r.source + ' · ' + ago(r.created)}))));
  }

  return {
    activate(){
      refresh().catch(fail);
      clearInterval(timer);
      timer = setInterval(() => { if(!document.hidden) refresh().catch(fail); }, 10000);
    },
    deactivate(){ clearInterval(timer); timer = null; },
  };
}
