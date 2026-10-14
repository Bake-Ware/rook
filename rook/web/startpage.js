// Start page: this account's own page of widgets in ordered columns:
// bookmarks, news feeds, notes and band widgets (workers, tasks, rooms,
// knowledge, sessions), plus any a plugin adds. Edit page turns on the
// editor: drag a widget by its handle (or use its arrow buttons) to move it,
// the pencil to change it, + Add widget at the foot of a column. Saved per
// account through /account/ui/api (the ui.startpage setting).
// See docs/web/start-page.md.
const search = new URL(import.meta.url).search;
const [{h, el}, W, T] = await Promise.all([
  import('/account/bands/assets/manage.js' + search), import('/account/ui/assets/widgets.js' + search),
  import('/account/ui/assets/themes.js' + search)]);
const REFRESH_MS = 60000;
const clone = x => JSON.parse(JSON.stringify(x));
const newId = () => 'w' + Math.random().toString(36).slice(2, 9);

export async function mountStartPage(root) {
  root.classList.add('sp');
  root.replaceChildren(el('p', 'Loading…', 'empty'));
  let state, layout, saved, editing = false, active = false, timer = null, dragId = null;
  const loadedModules = new Set();

  async function load() {
    state = await T.uiState(true);
    saved = clone(state.startpage);
    layout = clone(saved);
    // Plugin widget kinds: import each declared module once; it registers itself.
    await Promise.all((state.widgets || []).map(async w => {
      if (loadedModules.has(w.module)) return;
      loadedModules.add(w.module);
      try { await import(w.module); } catch (e) { console.warn('start page widget', w.type, e); }
    }));
  }
  const post = body => T.uiPost(body);

  // -- view ---------------------------------------------------------------------
  function widgetCard(w, ci, wi) {
    const spec = W.widgetType(w.type);
    const title = h('h3', {text: w.title || spec?.title || w.type});
    const body = h('div', {class: 'sp-body'});
    const card = h('section', {class: 'sp-widget sp-' + w.type.replace(/\W/g, '-'), 'data-id': w.id},
      h('header', null, editing ? h('span', {class: 'sp-handle', title: 'Drag to move', 'aria-hidden': 'true', text: '⠿'}) : null, title,
        editing ? h('span', {class: 'sp-tools'},
          tool('↑', 'Move up', () => move(ci, wi, ci, wi - 1), wi === 0),
          tool('↓', 'Move down', () => move(ci, wi, ci, wi + 1), wi === layout.columns[ci].length - 1),
          tool('←', 'Move to the column on the left', () => move(ci, wi, ci - 1, layout.columns[ci - 1]?.length ?? 0), ci === 0),
          tool('→', 'Move to the column on the right', () => move(ci, wi, ci + 1, layout.columns[ci + 1]?.length ?? 0), ci === layout.columns.length - 1),
          tool('✎', 'Change this widget', () => openEditor(w, ci)),
          tool('✕', 'Remove this widget', () => { layout.columns[ci].splice(wi, 1); render(); })) : null),
      body);
    if (editing) {
      card.draggable = true;
      card.addEventListener('dragstart', e => { dragId = w.id; card.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', w.id); });
      card.addEventListener('dragend', () => { dragId = null; card.classList.remove('dragging'); root.querySelectorAll('.drop-before').forEach(x => x.classList.remove('drop-before')); });
    }
    const ctx = {band: () => W.band(), post, setTitle: t => { title.textContent = t; }};
    if (!spec) body.append(W.failed('This widget needs a plugin that is not loaded (' + w.type + ').'));
    else Promise.resolve().then(() => spec.render(body, w, ctx)).catch(e => body.replaceChildren(W.failed(e.message)));
    return card;
  }
  function tool(text, label, onclick, disabled) {
    return h('button', {type: 'button', class: 'sp-tool', text, title: label, 'aria-label': label, onclick, disabled});
  }
  function move(ci, wi, cj, wj) {
    if (!layout.columns[cj]) return;
    const [w] = layout.columns[ci].splice(wi, 1);
    layout.columns[cj].splice(Math.max(0, Math.min(wj, layout.columns[cj].length)), 0, w);
    render();
    root.querySelector(`[data-id="${CSS.escape(w.id)}"] .sp-tool`)?.focus();
  }
  function find(id) {
    for (let ci = 0; ci < layout.columns.length; ci++) {
      const wi = layout.columns[ci].findIndex(w => w.id === id);
      if (wi >= 0) return [ci, wi];
    }
    return null;
  }
  function column(col, ci) {
    const c = h('div', {class: 'sp-col', 'data-col': String(ci)}, col.map((w, wi) => widgetCard(w, ci, wi)));
    if (editing) {
      c.append(h('button', {type: 'button', class: 'sp-add', text: '+ Add widget', onclick: () => openPicker(ci)}));
      c.addEventListener('dragover', e => {
        if (!dragId) return; e.preventDefault();
        root.querySelectorAll('.drop-before').forEach(x => x.classList.remove('drop-before'));
        const before = [...c.querySelectorAll('.sp-widget')].find(x => x.dataset.id !== dragId && e.clientY < x.getBoundingClientRect().top + x.offsetHeight / 2);
        (before || c.querySelector('.sp-add'))?.classList.add('drop-before');
      });
      c.addEventListener('drop', e => {
        e.preventDefault(); if (!dragId) return;
        const from = find(dragId); if (!from) return;
        const before = c.querySelector('.sp-widget.drop-before');
        const [w] = layout.columns[from[0]].splice(from[1], 1);
        const target = layout.columns[ci];
        const at = before ? target.findIndex(x => x.id === before.dataset.id) : target.length;
        target.splice(at < 0 ? target.length : at, 0, w);
        dragId = null; render();
      });
    }
    return c;
  }
  function bar() {
    if (!editing) return h('div', {class: 'sp-bar'},
      h('span', {class: 'sp-hint'}),
      h('button', {type: 'button', class: 'sp-btn', text: 'Edit page', onclick: () => { editing = true; render(); }}));
    const cols = h('select', {'aria-label': 'Columns', onchange: e => setColumns(+e.target.value)},
      [1, 2, 3, 4].map(n => h('option', {value: String(n), text: n + (n === 1 ? ' column' : ' columns'), selected: n === layout.columns.length})));
    return h('div', {class: 'sp-bar editing'},
      h('span', {class: 'sp-hint', text: 'Drag widgets by ⠿ or use the arrows. Changes are kept when you save.'}),
      h('label', {class: 'sp-cols'}, 'Layout ', cols),
      h('button', {type: 'button', class: 'sp-btn', text: 'Reset to default', onclick: reset}),
      h('button', {type: 'button', class: 'sp-btn', text: 'Cancel', onclick: () => { layout = clone(saved); editing = false; render(); }}),
      h('button', {type: 'button', class: 'sp-btn primary', text: 'Save', onclick: save}),
      h('span', {class: 'sp-status', role: 'status'}));
  }
  function setColumns(n) {
    const all = layout.columns.flat();
    const cols = Array.from({length: n}, (_, i) => layout.columns[i] ? [...layout.columns[i]] : []);
    // Widgets of removed columns go to the last one kept, in order.
    for (let i = n; i < layout.columns.length; i++) cols[n - 1].push(...layout.columns[i]);
    if (cols.flat().length !== all.length) return;
    layout.columns = cols; render();
  }
  async function save() {
    const st = root.querySelector('.sp-status'); st.textContent = 'Saving…';
    try {
      const out = await post({action: 'startpage', layout});
      saved = clone(out.startpage); layout = clone(saved); state.startpage = saved; editing = false; render();
    } catch (e) { st.textContent = e.message; }
  }
  async function reset() {
    if (!confirm('Put the default widgets back? Your bookmarks and feeds on this page are removed.')) return;
    try { const out = await post({action: 'startpage_reset'}); saved = clone(out.startpage); layout = clone(saved); editing = false; render(); }
    catch (e) { root.querySelector('.sp-status').textContent = e.message; }
  }
  function render() {
    root.classList.toggle('editing', editing);
    root.replaceChildren(bar(), h('div', {class: 'sp-cols-' + layout.columns.length + ' sp-grid'}, layout.columns.map(column)));
    if (!state.saved && editing) root.querySelector('.sp-status').textContent = 'Settings are unavailable on this hub, so changes cannot be saved.';
  }

  // -- widget picker and editor --------------------------------------------------------
  const dialog = h('dialog', {class: 'sp-dialog', 'aria-labelledby': 'sp-dialog-title'});
  document.body.append(dialog);
  function openPicker(ci) {
    const plugins = new Map((state.widgets || []).map(w => [w.type, w]));
    const kinds = W.widgetTypes().map(k => ({...k, ...(plugins.get(k.type) || {})}));
    dialog.replaceChildren(h('h2', {id: 'sp-dialog-title', text: 'Add a widget'}),
      h('div', {class: 'sp-kinds'}, kinds.map(k => h('button', {type: 'button', class: 'sp-kind', onclick: () => {
        dialog.close(); openEditor({id: newId(), type: k.type, title: '', options: clone(k.defaults || {})}, ci, true);
      }}, h('b', {text: k.title}), h('small', {text: k.description || ''})))),
      h('div', {class: 'sp-actions'}, h('button', {type: 'button', class: 'sp-btn', text: 'Cancel', onclick: () => dialog.close()})));
    dialog.showModal();
  }
  function openEditor(w, ci, isNew) {
    const spec = W.widgetType(w.type) || {title: w.type};
    const form = h('form', {method: 'dialog', class: 'sp-form'});
    const title = h('input', {type: 'text', value: w.title || '', placeholder: spec.title, maxlength: '80'});
    const err = h('p', {class: 'sp-error', role: 'alert'});
    form.append(h('h2', {id: 'sp-dialog-title', text: (isNew ? 'Add: ' : 'Change: ') + spec.title}), h('label', null, 'Title', title));
    const read = optionsEditor(w, form);
    form.append(err, h('div', {class: 'sp-actions'},
      h('button', {type: 'button', class: 'sp-btn', text: 'Cancel', onclick: () => dialog.close()}),
      h('button', {type: 'submit', class: 'sp-btn primary', text: isNew ? 'Add' : 'Done'})));
    form.onsubmit = e => {
      e.preventDefault();
      let options;
      try { options = read(); } catch (x) { err.textContent = x.message; return; }
      const next = {...w, title: title.value.trim(), options};
      if (isNew) layout.columns[ci].push(next);
      else { const at = find(w.id); if (at) layout.columns[at[0]][at[1]] = next; }
      dialog.close(); render();
    };
    dialog.replaceChildren(form);
    if (!dialog.open) dialog.showModal();
    title.focus();
  }
  // The form fields for a widget's options; returns a function that reads
  // them back (and throws a plain message when something is wrong).
  function optionsEditor(w, form) {
    const o = w.options || {};
    const limit = (def, top = 30) => {
      const input = h('input', {type: 'number', min: '1', max: String(top), value: String(o.limit || def)});
      form.append(h('label', null, 'How many to show', input));
      return () => { const n = +input.value; if (!(n >= 1 && n <= top)) throw Error('Show between 1 and ' + top + '.'); return Math.round(n); };
    };
    if (w.type === 'bookmarks') {
      const groups = h('div', {class: 'sp-groups'});
      const addGroup = g => {
        const name = h('input', {type: 'text', value: g.name || '', placeholder: 'Group name (optional)', maxlength: '60'});
        const lines = h('textarea', {rows: '6', spellcheck: 'false', placeholder: 'Jellyfin | https://media.example.com\nRouter | http://192.0.2.1'});
        lines.value = (g.links || []).map(l => l.label + ' | ' + l.url).join('\n');
        const box = h('fieldset', {class: 'sp-group'}, h('legend', {text: 'Group'}), h('label', null, 'Group name', name),
          h('label', null, 'One link per line: name | address', lines),
          h('button', {type: 'button', class: 'sp-btn', text: 'Remove group', onclick: () => box.remove()}));
        box._read = () => ({name: name.value.trim(), links: lines.value.split('\n').map(x => x.trim()).filter(Boolean).map((line, i) => {
          const cut = line.lastIndexOf('|');
          const label = (cut > 0 ? line.slice(0, cut) : line).trim(), url = (cut > 0 ? line.slice(cut + 1) : line).trim();
          if (!W.safeUrl(url)) throw Error('Line ' + (i + 1) + ' of ' + (name.value || 'a group') + ': addresses start with http://, https:// or # (a dashboard page).');
          return {label: label || url, url};
        })});
        groups.append(box);
      };
      (o.groups?.length ? o.groups : [{name: '', links: []}]).forEach(addGroup);
      form.append(groups, h('button', {type: 'button', class: 'sp-btn', text: '+ Add group', onclick: () => addGroup({})}));
      return () => ({groups: [...groups.querySelectorAll('.sp-group')].map(b => b._read())});
    }
    if (w.type === 'feed') {
      const url = h('input', {type: 'url', value: o.url || '', placeholder: 'https://example.com/feed.xml', required: true});
      const sums = h('input', {type: 'checkbox', checked: !!o.summaries});
      form.append(h('label', null, 'Feed address (RSS or Atom)', url));
      const n = limit(8);
      form.append(h('label', {class: 'sp-check'}, sums, ' Show a short summary under each headline'),
        h('p', {class: 'sp-help', text: 'Your hub fetches the feed and keeps it for a few minutes. Addresses on your own network work only if the operator allows them (Settings > Dashboard).'}));
      return () => {
        if (!/^https?:\/\//i.test(url.value.trim())) throw Error('The feed address starts with http:// or https://.');
        return {url: url.value.trim(), limit: n(), summaries: sums.checked};
      };
    }
    if (w.type === 'note') {
      const text = h('textarea', {rows: '6', maxlength: '2000'}); text.value = o.text || '';
      form.append(h('label', null, 'Text', text));
      return () => ({text: text.value});
    }
    if (['workers', 'tasks', 'rooms', 'knowledge', 'sessions'].includes(w.type)) {
      const n = limit(6); return () => ({limit: n()});
    }
    const spec = W.widgetType(w.type);
    if (spec?.editor) return spec.editor(form, w);
    const raw = h('textarea', {rows: '8', spellcheck: 'false'}); raw.value = JSON.stringify(o, null, 1);
    form.append(h('label', null, 'Options (JSON)', raw));
    return () => { try { const v = JSON.parse(raw.value || '{}'); if (v && typeof v === 'object' && !Array.isArray(v)) return v; } catch {} throw Error('Options must be a JSON object.'); };
  }

  try { await load(); render(); } catch (e) { root.replaceChildren(W.failed(e.message)); }
  const tick = () => { if (active && !editing && !document.hidden && !dialog.open) render(); };
  return {
    activate() { if (active) return; active = true; if (state) render(); timer = setInterval(tick, REFRESH_MS); },
    deactivate() { active = false; clearInterval(timer); },
  };
}
