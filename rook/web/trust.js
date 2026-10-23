// Trust: what agents may do to what, enforced by the hub (docs/design/trust.md).
// Everything shown comes from the hub and is rendered as text. Every change asks
// for the person's own words, which are recorded with it.
const {h, ago, useCss, btn, info, seg, dialog, more} = await import('/account/bands/assets/manage.js' + new URL(import.meta.url).search);

const LEVEL_TEXT = {
  'sacred': 'Changes only on your direct instruction. Agents may read, propose and prepare.',
  'guarded': 'Agents ask first (later: they vote, act, then tell you).',
  'ship-it': 'Agents just go.',
  'balanced': 'Changes go; admin and destructive actions ask first.',
};
const LEVEL_CLASS = {'sacred': 'mg-bad', 'guarded': 'mg-warn', 'ship-it': 'mg-good', 'balanced': ''};

export async function mountTrust(root, opts = {}){
  useCss();
  let data = null, timer = null, busy = false;
  const status = h('p', {class: 'mg-status', role: 'status'});
  const warn = h('p', {class: 'mg-box mg-pad mg-bad', role: 'alert', hidden: true});
  const version = h('span', {class: 'mg-tag line'});
  const notices = h('div', {style: 'display:flex;flex-direction:column;gap:8px'});
  const entries = h('div', {style: 'display:flex;flex-direction:column;gap:8px'});
  const unlisted = h('div', {class: 'mg-box mg-pad', style: 'display:flex;flex-wrap:wrap;gap:8px;align-items:center'});
  const exceptions = h('div', {style: 'display:flex;flex-direction:column;gap:8px'});
  const ledger = h('div', {style: 'display:flex;flex-direction:column;gap:8px'});
  const ledgerHead = h('div', {class: 'mg-bar', hidden: true}, h('h2', {text: 'Borrowed state'}),
    info('Changes away from a sacred thing’s normal state: what changed, why, how to restore it. Onboarding fills this in.', 'About borrowed state'));
  const history = h('div', {class: 'mg-box'});
  const summary = h('p', {class: 'mg-sub'});
  root.replaceChildren(h('div', {class: 'mg tr'},
    h('div', {class: 'mg-bar'}, h('h2', null, 'Trust map ', version),
      info(h('div', null,
        h('p', {text: 'The hub checks every agent call against this map, and every hold cites the entry it came from. Only you loosen it, here. Agents can propose changes; the house agent may tighten, and you see that here.'}),
        ...['sacred', 'guarded', 'ship-it'].map(l => h('p', null, h('b', {text: l + ': '}), LEVEL_TEXT[l])),
        h('p', {class: 'mg-sub', text: 'What agents are told:'}), summary), 'How the trust map works'),
      h('span', {class: 'mg-grow'}),
      btn('+ Entry', 'soft', () => editEntry(null)),
      btn('↻', 'icon', () => refresh().catch(fail), {'aria-label': 'Refresh', title: 'Refresh'})),
    warn, status, notices, entries,
    h('div', {class: 'mg-bar'}, h('h2', {text: 'Everything else'})), unlisted,
    h('div', {class: 'mg-bar'}, h('h2', {text: 'Exceptions'}), h('span', {class: 'mg-grow'}), btn('+ Exception', 'soft', () => addException())), exceptions,
    ledgerHead, ledger,
    h('details', null, h('summary', {class: 'mg-sub', style: 'cursor:pointer', text: 'History'}), history)));

  function fail(e){ status.textContent = e.message; }

  async function refresh(){
    const r = await fetch('/api/trust', {headers: {Accept: 'application/json'}});
    const d = await r.json().catch(() => ({}));
    if(!r.ok || d.error) throw Error(d.error || 'The trust map is unavailable.');
    data = d; draw();
    if(opts.onCount) opts.onCount(d.waiting || 0);
  }

  // Ask for the person's words, then send one change.
  async function change(op, payload, question, preset = ''){
    if(busy) return false;
    const words = op === 'ack' ? 'acknowledged' : prompt(question || 'In your own words (recorded with the change):', preset);
    if(words === null) return false;
    busy = true; status.textContent = '';
    try{
      const r = await fetch('/api/trust', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({op, data: payload, words})});
      const d = await r.json().catch(() => ({}));
      if(!r.ok || d.error) throw Error(d.error || 'Request failed');
      status.textContent = 'Saved as version ' + d.version + '.';
      return true;
    }catch(e){ status.textContent = e.message; return false; }
    finally{ busy = false; await refresh().catch(fail); }
  }

  const said = by => by ? h('div', {class: 'mg-sub'},
    (by.who || '?') + (by.kind === 'house' ? ' (house agent)' : '') + ' · ' + ago(by.at) + (by.via ? ' · via ' + by.via : ''),
    by.words ? h('q', {text: by.words, style: 'display:block;margin-top:2px'}) : null) : null;
  const levelTag = l => h('span', {class: 'mg-tag ' + (LEVEL_CLASS[l] || ''), text: l, title: LEVEL_TEXT[l] || ''});

  function entryCard(e){
    const can = data.can_edit !== false;
    const levels = seg(data.map.levels.map(l => [l, l, LEVEL_TEXT[l]]), e.level, l => {
      if(l === e.level) return;
      change('set_entry', {...e, level: l}, 'Why make ' + e.title + ' ' + l + '? (your words, recorded)');
    }, 'Level of ' + e.title);
    for(const b of levels.querySelectorAll('button')) b.disabled = !can;
    return h('div', {class: 'mg-box mg-pad', style: 'display:flex;flex-direction:column;gap:6px'},
      h('div', {style: 'display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center'},
        h('b', {text: e.title}), h('span', {class: 'mg-dimtext', text: e.kind + ' · ' + e.id}), levelTag(e.level),
        e.confirmed === false ? h('span', {class: 'mg-tag line', text: 'not confirmed', title: 'Set from your words by someone else (' + ((e.set_by || {}).via || '') + '). Confirm it if it is right.'}) : null,
        h('span', {class: 'mg-grow'}),
        e.confirmed === false ? btn('Confirm', 'primary', () => change('confirm', {id: e.id}, 'Confirm ' + e.title + ' as ' + e.level + '? (your words)', 'yes, that is right'), {disabled: !can}) : null,
        can ? more([{label: 'Edit', run: () => editEntry(e)},
          {label: 'Remove', danger: true, run: () => change('delete_entry', {id: e.id}, 'Remove ' + e.title + ' from the map? Why? (your words)')}], 'Actions for ' + e.title) : null),
      e.description ? h('div', {text: e.description}) : null,
      h('div', {class: 'mg-sub mg-mono', style: 'overflow-wrap:anywhere', text: (e.targets || []).join(' · ')}),
      levels, said(e.set_by));
  }

  function editEntry(e){
    const f = {id: h('input', {value: e?.id || '', placeholder: 'kaiju', disabled: !!e}),
      title: h('input', {value: e?.title || '', placeholder: 'kaiju (AI server)'}),
      kind: h('select', null, ['host', 'project', 'repo', 'resource', 'devices'].map(k => h('option', {value: k, text: k, selected: (e?.kind || 'host') === k}))),
      level: h('select', null, data.map.levels.map(k => h('option', {value: k, text: k, selected: (e?.level || 'guarded') === k}))),
      targets: h('textarea', {rows: 3, placeholder: 'worker:kaiju\nrepo:Bake-Ware/rook#master\ncap:hub.plugin.*', text: (e?.targets || []).join('\n')}),
      description: h('textarea', {rows: 2, text: e?.description || ''}),
      words: h('textarea', {rows: 2, placeholder: 'In your own words: why', required: true})};
    const row = (label, el) => h('label', {style: 'display:flex;flex-direction:column;gap:2px'}, h('span', {class: 'mg-sub', text: label}), el);
    const d = dialog(e ? 'Edit ' + e.title : 'New entry',
      h('div', {style: 'display:flex;flex-direction:column;gap:8px;min-width:min(420px,86vw)'},
        row('Id', f.id), row('Name', f.title), row('Kind', f.kind), row('Level', f.level),
        row('What it covers (one per line)', f.targets), row('Description', f.description), row('Your words (recorded)', f.words),
        h('div', {class: 'mg-actions'}, btn('Save', 'primary', async () => {
          busy = false;
          const words = f.words.value.trim();
          if(!words){ f.words.focus(); return; }
          const payload = {id: f.id.value.trim(), title: f.title.value.trim(), kind: f.kind.value, level: f.level.value,
            targets: f.targets.value.split('\n').map(s => s.trim()).filter(Boolean), description: f.description.value.trim()};
          const r = await fetch('/api/trust', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({op: 'set_entry', data: payload, words})});
          const out = await r.json().catch(() => ({}));
          if(!r.ok || out.error){ status.textContent = out.error || 'Request failed'; return; }
          d.close(); status.textContent = 'Saved as version ' + out.version + '.'; refresh().catch(fail);
        }))));
  }

  function addException(){
    const ids = data.map.entries.map(e => e.id);
    const f = {id: h('input', {placeholder: 'toy-gpu1'}), title: h('input', {placeholder: 'Toy experiment may use GPU 1 on kaiju'}),
      applies: h('input', {placeholder: '* or entry ids, comma separated', value: '*'}),
      excludes: h('input', {placeholder: 'entry ids it never covers', value: ids.filter(i => /famil/.test(i)).join(',')}),
      targets: h('textarea', {rows: 2, placeholder: 'optional: only these targets (worker:kaiju, cap:docker.*)'}),
      expires: h('input', {type: 'datetime-local'}), words: h('textarea', {rows: 2, placeholder: 'In your own words'})};
    const row = (label, el) => h('label', {style: 'display:flex;flex-direction:column;gap:2px'}, h('span', {class: 'mg-sub', text: label}), el);
    const d = dialog('New exception',
      h('div', {style: 'display:flex;flex-direction:column;gap:8px;min-width:min(420px,86vw)'},
        h('p', {class: 'mg-sub', text: 'Lets agents just go on what it covers, until it expires.'}),
        row('Id', f.id), row('What it is', f.title), row('Applies to', f.applies), row('Never covers', f.excludes),
        row('Only these targets', f.targets), row('Ends', f.expires), row('Your words (recorded)', f.words),
        h('div', {class: 'mg-actions'}, btn('Add', 'primary', async () => {
          const words = f.words.value.trim();
          if(!words || !f.expires.value){ (words ? f.expires : f.words).focus(); return; }
          const split = s => s.split(/[\n,]/).map(x => x.trim()).filter(Boolean);
          const payload = {id: f.id.value.trim(), title: f.title.value.trim(), applies_to: split(f.applies.value),
            excludes: split(f.excludes.value), targets: split(f.targets.value), expires: new Date(f.expires.value).toISOString()};
          const r = await fetch('/api/trust', {method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({op: 'add_exception', data: payload, words})});
          const out = await r.json().catch(() => ({}));
          if(!r.ok || out.error){ status.textContent = out.error || 'Request failed'; return; }
          d.close(); refresh().catch(fail);
        }))));
  }

  function draw(){
    const m = data.map, can = data.can_edit !== false;
    warn.hidden = !data.weak_message; warn.textContent = data.weak_message || '';
    version.textContent = 'v' + m.version;
    summary.textContent = data.summary || '';
    if(m.error){ warn.hidden = false; warn.textContent = 'The map file is unreadable; the hub keeps the last good version: ' + m.error; }
    // Only what needs you: the house agent's changes, proposals.
    const nv = (m.notices || []).map(n => h('div', {class: 'mg-box mg-pad', style: 'display:flex;gap:8px;align-items:center;justify-content:space-between'},
      h('span', {class: 'mg-warn', style: 'flex:1;min-width:0', text: 'The house agent tightened ' + n.id + ' (v' + n.version + ', ' + ago(n.at) + ')' + (n.words ? ': “' + n.words + '”' : '')}),
      btn('OK', 'soft', () => change('ack', {version: n.version}), {disabled: !can})));
    const pv = (m.proposals || []).filter(p => p.state === 'pending').map(p => h('div', {class: 'mg-box mg-pad', style: 'display:flex;flex-direction:column;gap:6px'},
      h('div', null, h('b', {text: 'Proposed by ' + p.by}), h('span', {class: 'mg-dimtext', text: ' · ' + ago(p.at)})),
      h('div', {text: p.why}),
      h('pre', {class: 'mg-code', style: 'white-space:pre-wrap;overflow-wrap:anywhere;max-height:160px;overflow:auto;margin:0', text: p.change.op + ' ' + JSON.stringify(p.change.data, null, 1)}),
      h('div', {class: 'mg-actions', style: 'flex-wrap:wrap'},
        btn('Reject', 'danger', () => change('decide_proposal', {id: p.id, accept: false}, 'Why not? (your words)'), {disabled: !can}),
        btn('Accept', 'primary', () => change('decide_proposal', {id: p.id, accept: true}, 'Accept this change? (your words, recorded)'), {disabled: !can}))));
    notices.replaceChildren(...nv, ...pv);
    const order = {'sacred': 0, 'guarded': 1, 'ship-it': 2};
    const list = [...m.entries].sort((a, b) => (order[a.level] - order[b.level]) || a.title.localeCompare(b.title));
    entries.replaceChildren(...(list.length ? list.map(entryCard) : [h('div', {class: 'mg-box mg-empty', text: 'No entries yet. Everything is “everything else”.'})]));
    const us = seg(m.unlisted_levels.map(l => [l, l, LEVEL_TEXT[l]]), m.unlisted, l => {
      if(l !== m.unlisted) change('set_unlisted', {level: l}, 'Treat everything not listed as ' + l + '? Why? (your words)');
    }, 'Everything else');
    for(const b of us.querySelectorAll('button')) b.disabled = !can;
    unlisted.replaceChildren(...[us, h('span', {class: 'mg-sub', text: LEVEL_TEXT[m.unlisted] || ''}), said(m.unlisted_set_by)].filter(Boolean));
    const xs = [...(m.exceptions || [])].sort((a, b) => (b.active - a.active) || (b.expires_at - a.expires_at)).slice(0, 12);
    exceptions.replaceChildren(...(xs.length ? xs.map(x => h('div', {class: 'mg-box mg-pad', style: 'display:flex;flex-direction:column;gap:4px;' + (x.active ? '' : 'opacity:.6')},
      h('div', {style: 'display:flex;flex-wrap:wrap;gap:6px 12px;align-items:center'},
        h('b', {text: x.title || x.id}), h('span', {class: 'mg-tag ' + (x.active ? 'mg-good' : 'line'), text: x.active ? 'until ' + new Date(x.expires_at * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : (x.ended ? 'ended' : 'expired')}),
        h('span', {class: 'mg-grow'}), x.active ? btn('End now', 'danger', () => change('end_exception', {id: x.id}, 'End ' + x.id + ' now? (your words)', 'end it'), {disabled: !can}) : null),
      h('div', {class: 'mg-sub', text: 'Covers ' + ((x.applies_to || ['*']).includes('*') ? 'everything' : x.applies_to.join(', ')) + (x.excludes?.length ? ' except ' + x.excludes.join(', ') : '') + (x.targets?.length ? ' · only ' + x.targets.join(', ') : '') + ' → ' + x.tier}),
      x.description ? h('div', {class: 'mg-sub', text: x.description}) : null,
      said(x.set_by))) : [h('div', {class: 'mg-box mg-empty', text: 'None.'})]));
    const ls = m.ledger || [];
    ledgerHead.hidden = !ls.length;
    ledger.replaceChildren(...ls.map(r => h('div', {class: 'mg-box mg-pad', style: 'display:flex;flex-direction:column;gap:4px'},
      h('div', null, h('b', {text: r.what}), h('span', {class: 'mg-dimtext', text: ' · ' + r.resource + ' · ' + r.state})),
      h('div', {class: 'mg-sub', text: 'Why: ' + r.why}), h('div', {class: 'mg-sub', text: 'Restore: ' + r.restore}),
      r.state === 'open' ? h('div', {class: 'mg-actions'}, btn('Restored', 'soft', () => change('close_lease', {id: r.id, restored: true}, 'Mark restored? (your words)'), {disabled: !can})) : null)));
    history.replaceChildren(...(data.history || []).map(r => h('div', {class: 'mg-row', style: 'display:flex;flex-wrap:wrap;gap:4px 10px;padding:6px 10px'},
      h('span', {class: 'mg-mono', text: 'v' + r.version}), h('span', {text: r.op + ' ' + (r.id || '')}),
      h('span', {class: 'mg-dimtext', text: ((r.actor || {}).who || '?') + ((r.actor || {}).kind === 'house' ? ' (house agent)' : '') + ' · ' + ago(r.at)}),
      r.words ? h('q', {class: 'mg-sub', text: r.words, style: 'flex-basis:100%'}) : null)));
  }

  return {
    activate(){
      refresh().catch(fail);
      clearInterval(timer);
      timer = setInterval(() => { if(!document.hidden && !document.querySelector('dialog[open]')) refresh().catch(fail); }, 15000);
    },
    deactivate(){ clearInterval(timer); timer = null; },
  };
}
