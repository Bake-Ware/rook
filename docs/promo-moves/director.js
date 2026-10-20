// Director mode (index.html?clip=N&director=1, served by director.py): scrub a clip, fly the camera,
// and save camera keys back to clips/cN.keys.json, which then replace the clip's own camera path.
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
      #dir button.on{border-color:#8fc79a;color:#8fc79a}#dir .row{display:flex;gap:10px;align-items:center;margin-top:6px;flex-wrap:wrap}
      #dir .k{display:inline-block;padding:2px 8px;border:1px solid #3b4232;border-radius:5px;margin:2px;cursor:pointer}#dir .k.cur{border-color:#f0c98c;color:#f0c98c}
      html,body{overflow:auto;height:auto}
    </style>
    <div id="dir"><input id="scrub" type="range" min="0" max="${T}" step="0.0333" value="0">
      <div class="row"><b id="tt"></b>
        <button id="play">▶ play</button><button id="free">free camera</button><button id="key">＋ key at this time</button>
        <button id="del">delete key</button><button id="cut">toggle cut</button><button id="save">save keys</button><button id="reset">use the clip's own keys</button>
        <span id="msg"></span></div>
      <div class="row" id="list"></div>
      <div class="row" style="opacity:.7">Free camera: drag to orbit, right-drag to pan, wheel to zoom. Times are clip time (after the ${STING}s sting).</div></div>`;
  document.body.appendChild(ui);
  document.body.style.overflow = 'auto';
  const $ = id => document.getElementById(id);
  const controls = new OrbitControls(kit.camera, document.getElementById('ui'));
  document.getElementById('ui').style.pointerEvents = 'auto';
  controls.enabled = false;
  let t = 0, playing = false, sel = -1, last = performance.now();
  const draw = () => {
    window.render(t);
    $('tt').textContent = `t ${t.toFixed(2)}s · clip ${(t - STING).toFixed(2)}s / ${(T - STING - 5).toFixed(1)}s`;
    $('list').innerHTML = keys.map((k, i) => `<span class="k ${i === sel ? 'cur' : ''}" data-i="${i}">${k.t.toFixed(2)}${k.e === 'cut' ? ' ✂' : ''}</span>`).join('');
    [...$('list').querySelectorAll('.k')].forEach(el => el.onclick = () => { sel = +el.dataset.i; t = keys[sel].t + STING; $('scrub').value = t; draw(); });
  };
  $('scrub').oninput = e => { t = +e.target.value; draw(); };
  $('play').onclick = () => { playing = !playing; $('play').textContent = playing ? '❚❚ pause' : '▶ play'; last = performance.now(); };
  $('free').onclick = () => {
    kit.freeCam = !kit.freeCam; controls.enabled = kit.freeCam; $('free').classList.toggle('on', kit.freeCam);
    if (kit.freeCam && kit.camLook) controls.target.copy(kit.camLook);
  };
  $('key').onclick = () => {
    const tau = r(t - STING);
    const k = { t: tau, pos: kit.camera.position.toArray().map(r), look: controls.target.toArray().map(r), fov: r(kit.camera.fov) };
    const i = keys.findIndex(x => Math.abs(x.t - tau) < .02);
    if (i >= 0) keys[i] = { ...keys[i], ...k }; else keys.push(k);
    keys.sort((a, b) => a.t - b.t); sel = keys.findIndex(x => x.t === tau); apply(); draw();
  };
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
    if (playing) { t = Math.min(T, t + (now - last) / 1000); $('scrub').value = t; if (t >= T) playing = false; draw(); }
    else if (kit.freeCam) { controls.update(); kit.composer.render(); }
    last = now; requestAnimationFrame(loop);
  };
  draw(); requestAnimationFrame(loop);
}
