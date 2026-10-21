// Director mode (index.html?clip=N&director=1, served by director.py): scrub a clip, fly the camera,
// and save camera keys back to clips/cN.keys.json, which then replace the clip's own camera path.
// Notes (clips/cN.notes.json) are comments for Claude at a clip time; they autosave and never change the camera.
import { THREE, STING } from './kit.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

export function director(kit, clip) {
  const T = clip.T;
  const keys = (kit.camKeys ? kit.camKeys() : []).map(k => ({ t: k.t, pos: k.pos.toArray().map(r), look: k.look.toArray().map(r), fov: r(k.fov), e: k.e }));
  function r(x) { return Math.round(x * 1000) / 1000; }
  const ui = document.createElement('div');
  ui.innerHTML = `
    <style>
      #dir{position:fixed;left:0;right:0;bottom:0;background:rgba(9,12,9,.94);border-top:1px solid #514b35;font:14px 'DejaVu Sans Mono',monospace;color:#cfd3bd;padding:10px 16px;z-index:99}
      #dir input[type=range]{width:100%}#dir button{font:inherit;background:#20261d;color:#e2e3d9;border:1px solid #514b35;border-radius:6px;padding:4px 10px;margin-right:6px;cursor:pointer}
      #dir select{font:inherit;background:#20261d;color:#e2e3d9;border:1px solid #514b35;border-radius:6px;padding:4px 6px;max-width:320px}#dir label{display:inline-flex;gap:4px;align-items:center;margin-right:6px}
      #dir button.on{border-color:#8fc79a;color:#8fc79a}#dir .row{display:flex;gap:10px;align-items:center;margin-top:6px;flex-wrap:wrap}
      #dir textarea{width:100%;min-height:54px;font:inherit;background:#141812;color:#e2e3d9;border:1px solid #514b35;border-radius:6px;padding:6px 8px;resize:vertical}
      #dir .k.n{border-style:dashed}#dir .k.hasn::after{content:' ✎';color:#f0c98c}
      #dir .k{display:inline-block;padding:2px 8px;border:1px solid #3b4232;border-radius:5px;margin:2px;cursor:pointer}#dir .k.cur{border-color:#f0c98c;color:#f0c98c}
      html,body{overflow:hidden!important;height:100%!important;width:100%!important}
      #stage{position:absolute!important;left:0;top:0}
    </style>
    <div id="dir"><input id="scrub" type="range" min="0" max="${T}" step="0.0333" value="0">
      <div class="row"><b id="tt"></b>
        <a href="/" style="color:#f0c98c;text-decoration:none;margin-right:6px">← clips</a><button id="play">▶ play</button><select id="score" title="Score to play under the clip"><option value="">no music</option></select><label title="Score volume">♪ <input id="svol" type="range" min="0" max="1" step="0.05" value="0.6" style="width:80px"></label><label><input id="vox" type="checkbox" checked> voice</label><button id="free">free camera</button><button id="key">＋ key at this time</button>
        <button id="del">delete key</button><button id="cut">toggle cut</button><button id="save">save keys</button><button id="reset">use the clip's own keys</button><button id="note">✎ note at this time</button>
        <span id="msg"></span></div>
      <div class="row" id="list"></div>
      <div class="row" id="nrow" style="display:none"><span id="nlbl"></span><button id="ndel">delete note</button><textarea id="ntext" placeholder="Comment for Claude at this time (saves as you type)"></textarea></div>
      <div class="row" style="opacity:.7">Free camera: drag to orbit, right-drag to pan, wheel to zoom. Times are clip time (after the ${STING}s sting).</div></div>`;
  document.body.appendChild(ui);
  // fit the 1920x1080 stage into the window above the controls
  const stage = document.getElementById('stage');
  document.documentElement.style.width = document.body.style.width = '100%';
  document.documentElement.style.height = document.body.style.height = '100%';
  stage.style.transformOrigin = '0 0';
  const fit = () => {                       // scale to fit, centred, with no page scroll
    const dh = document.getElementById('dir').offsetHeight, s = Math.min(innerWidth / 1920, (innerHeight - dh) / 1080);
    stage.style.transform = `translate(${(innerWidth - 1920 * s) / 2}px, ${Math.max(0, (innerHeight - dh - 1080 * s) / 2)}px) scale(${s})`;
  };
  addEventListener('resize', fit); requestAnimationFrame(fit);
  const $ = id => document.getElementById(id);
  // a transparent layer over the whole stage catches the mouse for the free camera
  const grab = document.createElement('div');
  grab.style.cssText = 'position:absolute;inset:0;z-index:50;pointer-events:none;cursor:grab';
  stage.appendChild(grab);
  const controls = new OrbitControls(kit.camera, grab);
  controls.enabled = false;
  // the clip sets its own camera every frame; while the free camera is on, put ours back before drawing
  const fc = { pos: new THREE.Vector3(), fov: 36 };
  const keepFree = () => { fc.pos.copy(kit.camera.position); fc.fov = kit.camera.fov; };
  controls.addEventListener('change', keepFree);
  kit.afterUpdate = () => {
    if (!kit.freeCam) return;
    kit.camera.position.copy(fc.pos); kit.camera.fov = fc.fov; kit.camera.up.set(0, 1, 0);
    kit.camera.lookAt(controls.target); kit.camera.updateProjectionMatrix(); kit.camera.updateMatrixWorld();
    kit.renderPass.scene = kit.scene; kit.renderPass.camera = kit.camera;
  };
  let t = 0, playing = false, sel = -1, nsel = -1, last = performance.now();
  // notes: [{ t, text, cam: { pos, look, fov }, free }] at clip time
  let notes = [];
  fetch(`notes/${clip.num}`).then(r => r.ok ? r.json() : []).then(n => { notes = n || []; draw(); }).catch(() => {});
  const near = (a, b) => typeof a === 'number' && typeof b === 'number' && Math.abs(a - b) < .02;   // general notes have t: null
  const noteAt = tau => notes.findIndex(n => near(n.t, tau));
  let saveTimer = null;
  const saveNotes = () => { clearTimeout(saveTimer); saveTimer = setTimeout(async () => {
    // keep the clip-wide notes the clip list may have changed meanwhile; this page owns the timed ones
    const cur = await fetch(`notes/${clip.num}`).then(r => r.json()).catch(() => []);
    // keep Claude's done marks and replies unless you changed the note's text since
    const mine = notes.filter(n => typeof n.t === 'number' && n.text.trim()).map(n => {
      const s = cur.find(x => typeof x.t === 'number' && near(x.t, n.t));
      return s && s.text === n.text ? { ...n, done: s.done, reply: s.reply } : { ...n, done: undefined, reply: undefined };
    });
    const body = cur.filter(n => typeof n.t !== 'number').concat(mine);
    const res = await fetch(`notes/${clip.num}`, { method: 'POST', body: JSON.stringify(body, null, 1) });
    $('msg').textContent = res.ok ? `notes saved (${notes.filter(n => n.text.trim()).length})` : 'note save failed';
  }, 500); };
  const openNote = (tau, create) => {
    let i = noteAt(tau);
    if (i < 0 && create) {
      notes.push({ t: tau, text: '', cam: { pos: kit.camera.position.toArray().map(r), look: (kit.freeCam ? controls.target : kit.camLook ?? controls.target).toArray().map(r), fov: r(kit.camera.fov) }, free: !!kit.freeCam });
      notes.sort((a, b) => a.t - b.t); i = noteAt(tau);
    }
    nsel = i;
    $('nrow').style.display = i >= 0 ? 'flex' : 'none';
    fit();
    if (i >= 0) { $('nlbl').textContent = `note @ ${notes[i].t.toFixed(2)}s${notes[i].done ? ' · ✓ done' : ''}${notes[i].reply ? ' · Claude: ' + notes[i].reply : ''}`; $('ntext').value = notes[i].text; if (create) $('ntext').focus(); }
  };
  const frame = () => window.render(t);
  const draw = () => {
    frame();
    $('tt').textContent = `t ${t.toFixed(2)}s · clip ${(t - STING).toFixed(2)}s / ${(T - STING - 5).toFixed(1)}s`;
    // camera keys, plus notes that aren't on a key (dashed)
    const items = keys.map((k, i) => ({ t: k.t, i, key: true }))
      .concat(notes.filter(n => typeof n.t === 'number' && !keys.some(k => near(k.t, n.t))).map(n => ({ t: n.t, key: false }))).sort((a, b) => a.t - b.t);
    $('list').innerHTML = items.map(it => {
      const hasN = noteAt(it.t) >= 0 && notes[noteAt(it.t)].text.trim() && !notes[noteAt(it.t)].done;
      const cur = it.key ? it.i === sel : nsel >= 0 && near(notes[nsel]?.t ?? -9, it.t);
      return `<span class="k ${cur ? 'cur' : ''} ${it.key ? '' : 'n'} ${hasN ? 'hasn' : ''}" data-t="${it.t}" data-i="${it.key ? it.i : -1}" title="${hasN ? notes[noteAt(it.t)].text.replace(/"/g, '&quot;') : ''}">${it.t.toFixed(2)}${it.key && keys[it.i].e === 'cut' ? ' ✂' : ''}</span>`;
    }).join('');
    [...$('list').querySelectorAll('.k')].forEach(el => el.onclick = () => {
      const tau = +el.dataset.t; sel = +el.dataset.i; t = tau + STING; $('scrub').value = t;
      openNote(tau, sel >= 0); draw();             // clicking a key opens (or starts) its note
    });
  };
  // ---- sound: a chosen score under the clip, plus its voice and effects, kept in sync with t
  const music = new Audio(), voice = new Audio(); music.preload = voice.preload = 'auto';
  let voiceUrl = null;
  fetch(`scores/${clip.num}`).then(r => r.json()).then(d => {
    for (const s of d.scores) { const o = document.createElement('option'); o.value = s.url; o.textContent = s.label; $('score').appendChild(o); }
    voiceUrl = d.vox; if (voiceUrl) voice.src = voiceUrl;
    let pick = ''; try { pick = localStorage.getItem('score.' + clip.num) ?? ''; } catch {}
    if (pick === '' && d.scores.length) pick = d.scores[0].url;
    if ([...$('score').options].some(o => o.value === pick)) { $('score').value = pick; music.src = pick; }
  }).catch(() => {});
  $('score').onchange = () => { music.src = $('score').value; try { localStorage.setItem('score.' + clip.num, $('score').value); } catch {} syncAudio(true); };
  const speaking = tt => (window.CLIP?.voice || []).some(v => tt >= v.t - .15 && tt < v.t + v.dur + .3);
  function syncAudio(force) {
    for (const [a, on] of [[music, !!$('score').value], [voice, $('vox').checked && !!voiceUrl]]) {
      if (!on || !a.src) { a.pause(); continue; }
      if (force || Math.abs(a.currentTime - t) > .12) { try { a.currentTime = Math.min(t, (a.duration || 1e9) - .01); } catch {} }
      if (playing && a.paused) a.play().catch(() => {});
      if (!playing && !a.paused) a.pause();
    }
    music.volume = +$('svol').value * (speaking(t) ? .45 : 1);                       // duck the score under the lines
  }
  $('svol').oninput = () => syncAudio(false); $('vox').onchange = () => syncAudio(true);
  $('scrub').oninput = e => { t = +e.target.value; draw(); syncAudio(true); };
  $('play').onclick = () => { playing = !playing; $('play').textContent = playing ? '❚❚ pause' : '▶ play'; last = performance.now(); syncAudio(true); };
  $('free').onclick = () => {
    if (!kit.freeCam) {                      // start from wherever the clip's camera is now
      frame(); controls.target.copy(kit.camLook ?? new THREE.Vector3(0, 0, -1).applyQuaternion(kit.camera.quaternion).add(kit.camera.position));
      keepFree();
    }
    kit.freeCam = !kit.freeCam; controls.enabled = kit.freeCam; grab.style.pointerEvents = kit.freeCam ? 'auto' : 'none';
    $('free').classList.toggle('on', kit.freeCam); frame();
  };
  $('key').onclick = () => {
    const tau = r(t - STING);
    const k = { t: tau, pos: kit.camera.position.toArray().map(r), look: controls.target.toArray().map(r), fov: r(kit.camera.fov) };
    const i = keys.findIndex(x => Math.abs(x.t - tau) < .02);
    if (i >= 0) keys[i] = { ...keys[i], ...k }; else keys.push(k);
    keys.sort((a, b) => a.t - b.t); sel = keys.findIndex(x => x.t === tau); apply(); draw();
  };
  $('note').onclick = () => { openNote(r(t - STING), true); draw(); };
  $('ntext').oninput = e => { if (nsel >= 0) { notes[nsel].text = e.target.value; saveNotes(); } };
  $('ntext').onkeydown = e => e.stopPropagation();
  $('ndel').onclick = () => { if (nsel >= 0) { notes.splice(nsel, 1); nsel = -1; $('nrow').style.display = 'none'; saveNotes(); draw(); } };
  $('del').onclick = () => { if (sel >= 0) { keys.splice(sel, 1); sel = -1; apply(); draw(); } };
  $('cut').onclick = () => { if (sel >= 0) { keys[sel].e = keys[sel].e === 'cut' ? undefined : 'cut'; apply(); draw(); } };
  $('save').onclick = async () => {
    const res = await fetch(`save/${clip.num}`, { method: 'POST', body: JSON.stringify(keys, null, 1) });
    $('msg').textContent = res.ok ? 'saved to clips/c' + clip.num + '.keys.json' : 'save failed';
  };
  $('reset').onclick = async () => { await fetch(`save/${clip.num}`, { method: 'DELETE' }); $('msg').textContent = 'saved keys removed; reload'; };
  function apply() {
    // rebuild the live path from the edited keys
    const V = (a) => new THREE.Vector3(...a);
    const K = keys.map(k => ({ ...k, pos: V(k.pos), look: V(k.look), fov: k.fov ?? 36 }));
    kit.camKeys = () => K;
  }
  const loop = now => {
    if (playing) {
      // follow the music clock when there is one, so picture and sound stay locked
      const clock = !music.paused && music.src ? music : !voice.paused && voice.src ? voice : null;
      t = Math.min(T, clock ? clock.currentTime : t + (now - last) / 1000); $('scrub').value = t;
      if (t >= T) { playing = false; $('play').textContent = '▶ play'; } draw(); syncAudio(false);
    }
    else if (kit.freeCam) { controls.update(); frame(); }
    last = now; requestAnimationFrame(loop);
  };
  // ?t= opens at a clip time (from the clip list); ?note=1 also opens the note there
  const qs = new URLSearchParams(location.search);
  if (qs.has('t')) { t = Math.min(T, +qs.get('t') + STING); $('scrub').value = t; fetch(`notes/${clip.num}`).then(() => setTimeout(() => { if (qs.has('note')) openNote(+qs.get('t'), false); draw(); }, 300)); }
  draw(); requestAnimationFrame(loop);
}
