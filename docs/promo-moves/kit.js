// Rook Moves scene kit. Every frame is a pure function of time: a clip builds its
// set once, then render(t) poses everything for t seconds, so render.py can step
// frames in any order and the soundtrack lines up exactly.
//
// The look follows the original film: toon-shaded low-poly pieces with ink edges
// on a dark page, a gold rook, gold pulses that move like a rook (ranks and files
// only), and flat UI panes over the scene.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

export { THREE };
export const W = 1920, H = 1080;
export const STING = 2.0, ENDCARD = 5.0;
export const COL = {
  page: 0x0e110e, ink: 0x070907, gold: 0xc9a868, goldHi: 0xf0c98c, acc: 0xd8ad6d, ok: 0x8fc79a, red: 0xe0675c,
  teal: 0x79d2c8, blue: 0x7f9cf0, pink: 0xe8a0c0, green: 0x9fd37c, tg: 0x3aa6dd,
};
const PALETTE = {
  gold: 0xc9a868, body: 0xaaa17d, dark: 0x3a3c32, dark2: 0x24261f, metal: 0x8c8a7a, wood: 0x8a6c42, wood2: 0x5e4a2e,
  stone: 0x9d9473, iron: 0x2d2a22, accent: 0xd8ad6d, paper: 0xe6dcc0, skin: 0xc79f7c, cloth: 0x6d7a63, cloth2: 0x3b4232,
  cloth3: 0x8f8a76,
};

// ------------------------------------------------------------------ easing + time helpers
export const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
export const lerp = (a, b, t) => a + (b - a) * t;
export const inv = (a, b, x) => clamp((x - a) / (b - a));
export const ease = {
  io: t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2,
  out: t => 1 - Math.pow(1 - t, 3),
  in: t => t * t * t,
  outE: t => t >= 1 ? 1 : 1 - Math.pow(2, -10 * t),
  inE: t => t <= 0 ? 0 : Math.pow(2, 10 * t - 10),
  lin: t => t,
};
export const fade = (t, a, b, c, d) => inv(a, b, t) * (1 - inv(c, d, t));   // in over a..b, out over c..d
export function rnd(seed) {
  let a = (seed + 0x6D2B79F5) | 0;
  a = Math.imul(a ^ (a >>> 15), a | 1);
  a ^= a + Math.imul(a ^ (a >>> 7), a | 61);
  return ((a ^ (a >>> 14)) >>> 0) / 4294967296;
}
export const typed = (s, p) => s.slice(0, Math.floor(clamp(p) * s.length + 1e-6));
const V = (x, y, z) => new THREE.Vector3(x, y, z);
export { V };

// ------------------------------------------------------------------ renderer, scene, post
export class Kit {
  constructor(root) {
    this.root = root;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(W, H, false);
    this.renderer.setClearColor(COL.page, 1);
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    root.querySelector('#gl').appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.Fog(COL.page, 14, 34);
    this.camera = new THREE.PerspectiveCamera(36, W / H, .05, 400);
    this.hemi = new THREE.HemisphereLight(0xe3d8ad, 0x2b3323, 1.6);
    this.key = new THREE.DirectionalLight(0xffedc4, 2.4);
    this.key.position.set(-6, 12, 7);
    this.scene.add(this.hemi, this.key, this.key.target);
    const ramp = new THREE.DataTexture(new Uint8Array([62, 62, 62, 255, 132, 132, 132, 255, 208, 208, 208, 255, 255, 255, 255, 255]), 4, 1, THREE.RGBAFormat);
    ramp.minFilter = ramp.magFilter = THREE.NearestFilter; ramp.needsUpdate = true;
    this.ramp = ramp;
    this.inkMat = new THREE.LineBasicMaterial({ color: COL.ink, transparent: true, opacity: .85 });
    this.mats = {};
    this.composer = new EffectComposer(this.renderer);
    this.composer.setSize(W, H);
    this.renderPass = new RenderPass(this.scene, this.camera);
    this.composer.addPass(this.renderPass);
    this.bloom = new UnrealBloomPass(new THREE.Vector2(W, H), .45, .4, .9);
    this.composer.addPass(this.bloom);
    this.composer.addPass(new OutputPass());
    this.templates = {};
    this.updaters = [];          // (t) => void, called every frame in order
    this.events = [];            // sound cues for the soundtrack: {t, type, ...}
    this.voice = [];             // {id, t, who, text}
    this.overlay = root.querySelector('#ui');
  }

  // ---------------------------------------------------------------- assets
  async load(names) {
    const loader = new GLTFLoader();
    await Promise.all(names.map(n => loader.loadAsync(`assets/${n}.glb`).then(g => { this.templates[n] = g.scene; })));
  }

  toon(color, opts = {}) {
    const key = color + JSON.stringify(opts);
    if (!this.mats[key]) {
      this.mats[key] = new THREE.MeshToonMaterial({ color, gradientMap: this.ramp, polygonOffset: true, polygonOffsetFactor: 1,
        polygonOffsetUnits: 1, ...opts });
    }
    return this.mats[key];
  }

  matFor(name) {
    const base = name.replace(/\.\d+$/, '');
    if (base === 'screen') return new THREE.MeshBasicMaterial({ color: 0x1a2520 });
    if (base === 'light') return new THREE.MeshBasicMaterial({ color: 0xffe2a8 });
    if (base === 'glass') return new THREE.MeshToonMaterial({ color: 0x9fb8b2, gradientMap: this.ramp, transparent: true, opacity: .18, depthWrite: false });
    return this.toon(PALETTE[base] ?? 0xaaa17d);
  }

  // A fresh, inked copy of a model. opts.tint recolours every toon surface (agents);
  // opts.mats maps material names to materials; opts.ink=false skips the lines.
  make(name, opts = {}) {
    const obj = this.templates[name].clone(true);
    const meshes = [];
    obj.traverse(o => { if (o.isMesh) meshes.push(o); });
    for (const m of meshes) {
      const mname = m.material.name || 'body';
      m.userData.mat = mname.replace(/\.\d+$/, '');
      m.material = opts.mats?.[m.userData.mat] ?? (opts.tint !== undefined && !['screen', 'light', 'glass'].includes(m.userData.mat)
        ? opts.tintMat ?? this.toon(opts.tint) : this.matFor(mname));
      if (opts.ink !== false && !['light', 'glass'].includes(m.userData.mat)) {
        const e = new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry, 25), opts.inkMat ?? this.inkMat);
        e.renderOrder = 1; m.add(e); m.userData.ink = e;
      }
    }
    if (opts.scale) obj.scale.setScalar(opts.scale);
    if (opts.at) obj.position.copy(opts.at);
    if (opts.rot !== undefined) obj.rotation.y = opts.rot;
    (opts.parent ?? this.scene).add(obj);
    return obj;
  }

  // give a box-like mesh planar uvs across its two largest axes (Blender boxes export without uvs)
  planarUV(m, { u = null, v = null, flipU = false, flipV = false } = {}) {
    const g = m.geometry; g.computeBoundingBox();
    const bb = g.boundingBox, sz = bb.getSize(V(0, 0, 0));
    const ax = [['x', sz.x], ['y', sz.y], ['z', sz.z]].sort((a, b) => b[1] - a[1]);
    u = u ?? ax[0][0]; v = v ?? ax[1][0];
    const pos = g.attributes.position, uv = new Float32Array(pos.count * 2);
    for (let i = 0; i < pos.count; i++) {
      const p = { x: pos.getX(i), y: pos.getY(i), z: pos.getZ(i) };
      let a = (p[u] - bb.min[u]) / (sz[u] || 1), b = (p[v] - bb.min[v]) / (sz[v] || 1);
      uv[2 * i] = flipU ? 1 - a : a; uv[2 * i + 1] = flipV ? 1 - b : b;
    }
    g.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
  }

  node(obj, name) {
    let found = null;
    obj.traverse(o => { if (!found && o.name === name) found = o; });
    return found;
  }

  meshesOf(obj, mat) {
    const out = [];
    obj.traverse(o => { if (o.isMesh && (!mat || o.userData.mat === mat)) out.push(o); });
    return out;
  }

  // ---------------------------------------------------------------- the Board
  board({ size = 40, center = V(0, 0, 0), y = 0, scene = null, cam = null } = {}) {
    const glow = [];
    for (let i = 0; i < 16; i++) glow.push(new THREE.Vector4(0, 0, 0, 0));
    const u = { uFade: { value: new THREE.Vector2(10, 28) }, uCam: { value: new THREE.Vector3() }, uGlow: { value: glow },
      uPage: { value: new THREE.Color(COL.page) }, uDim: { value: 1 } };
    const mat = new THREE.ShaderMaterial({
      uniforms: u, transparent: false,
      vertexShader: `varying vec3 vW; void main(){ vec4 w = modelMatrix * vec4(position,1.); vW = w.xyz; gl_Position = projectionMatrix * viewMatrix * w; }`,
      fragmentShader: `
        uniform vec2 uFade; uniform vec3 uCam; uniform vec4 uGlow[16]; uniform vec3 uPage; uniform float uDim; varying vec3 vW;
        void main(){
          vec2 q = vW.xz + .5; vec2 c = floor(q); vec2 f = fract(q);
          float chk = mod(c.x + c.y, 2.);
          vec3 a = vec3(.0044,.0056,.0044), b = vec3(.0085,.0102,.0080);
          vec3 col = mix(a, b, chk) * uDim;
          float e = min(min(f.x, 1.-f.x), min(f.y, 1.-f.y));
          col += vec3(.69,.42,.15) * .035 * (1. - smoothstep(.0, .02, e)) * uDim;
          for (int i = 0; i < 16; i++) {
            vec4 g = uGlow[i];
            if (g.w > 0.) {
              vec2 d = abs(vW.xz - g.xy);
              float inside = step(d.x, .5) * step(d.y, .5);
              float edge = inside * (1. - smoothstep(.0, .06, .5 - max(d.x, d.y)));
              vec3 gc = g.z > 1.5 ? vec3(.88,.40,.36) : (g.z > .5 ? vec3(.47,.82,.78) : vec3(.94,.78,.55));
              col += gc * g.w * (.10 * inside + .55 * edge);
            }
          }
          float dist = length(vW - uCam);
          col = mix(col, uPage, smoothstep(uFade.x, uFade.y, dist));
          gl_FragColor = vec4(col, 1.);
          #include <colorspace_fragment>
        }`,
    });
    const geo = new THREE.PlaneGeometry(size, size); geo.rotateX(-Math.PI / 2);
    const m = new THREE.Mesh(geo, mat); m.position.set(center.x, y, center.z);
    (scene ?? this.scene).add(m);
    this.boardMesh = m;
    this.boardU = u;
    this.boardCam = cam;      // a function returning the camera that views the board (default: the kit camera)
    this.updaters.unshift(() => { for (const g of glow) g.w = 0; });
    return m;
  }

  // light a square: kind 0 gold, 1 teal, 2 red. Call from an updater each frame.
  glowSquare(x, z, w, kind = 0) {
    const g = this.boardU.uGlow.value.find(v => v.w === 0);
    if (g && w > .002) g.set(Math.round(x), Math.round(z), kind, w);
  }

  // ---------------------------------------------------------------- pulses (move like a rook)
  // A sealed gold cube that runs from a to b along ranks and files: first along x,
  // then along z (or z first with zFirst). Lives from t0 for dur seconds.
  pulse(a, b, t0, dur, { color = COL.goldHi, zFirst = false, size = .14, y = .3, sound = true, trail = true, scene = null } = {}) {
    const p0 = V(a.x, y, a.z), p2 = V(b.x, y, b.z);
    const p1 = zFirst ? V(a.x, y, b.z) : V(b.x, y, a.z);
    const l1 = p0.distanceTo(p1), l2 = p1.distanceTo(p2), L = l1 + l2 || 1;
    const at = s => { const d = clamp(s) * L; return d <= l1 ? p0.clone().lerp(p1, l1 ? d / l1 : 1) : p1.clone().lerp(p2, l2 ? (d - l1) / l2 : 1); };
    const g = new THREE.Group();
    const cube = new THREE.Mesh(new THREE.BoxGeometry(size, size, size), new THREE.MeshBasicMaterial({ color }));
    cube.add(new THREE.LineSegments(new THREE.EdgesGeometry(cube.geometry), new THREE.LineBasicMaterial({ color: 0x5a4320 })));
    g.add(cube);
    const ghosts = [];
    if (trail) for (let i = 0; i < 7; i++) {
      const gm = new THREE.Mesh(new THREE.BoxGeometry(size * .7, size * .7, size * .7),
        new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .5 * (1 - i / 7), depthWrite: false }));
      g.add(gm); ghosts.push(gm);
    }
    (scene ?? this.scene).add(g);
    if (sound) this.events.push({ t: t0, type: 'pulse' }, { t: t0 + dur, type: 'land' });
    this.updaters.push(t => {
      const s = (t - t0) / dur;
      g.visible = s >= 0 && s <= 1.08;
      if (!g.visible) return;
      const e = ease.io(clamp(s));
      cube.position.copy(at(e));
      cube.rotation.y = e * Math.PI;
      const pop = s > 1 ? 1 - (s - 1) / .08 : 1;
      cube.scale.setScalar(Math.max(.01, pop));
      ghosts.forEach((gm, i) => { gm.position.copy(at(ease.io(clamp(s - (i + 1) * .018)))); gm.visible = s < 1; });
    });
    return { at, t0, dur, end: t0 + dur };
  }

  // ---------------------------------------------------------------- camera rig
  // keys: [{t, pos:[x,y,z], look:[x,y,z], fov, e:'io'|'out'|'in'|'lin'|'cut'}]
  camPath(keys) {
    const K = keys.map(k => ({ ...k, pos: V(...k.pos), look: V(...k.look), fov: k.fov ?? 36 }));
    this.updaters.push(t => {
      let a = K[0], b = K[0], p = 0;
      if (t >= K[K.length - 1].t) { a = b = K[K.length - 1]; }
      else for (let i = 1; i < K.length; i++) if (t < K[i].t) {
        a = K[i - 1]; b = K[i];
        p = b.e === 'cut' ? 0 : (ease[b.e ?? 'io'])(inv(a.t, b.t, t));
        if (b.e === 'cut') { a = b = K[i - 1]; }
        break;
      }
      if (t < K[0].t) a = b = K[0];
      this.camera.position.copy(a.pos).lerp(b.pos, p);
      const look = a.look.clone().lerp(b.look, p);
      this.camera.fov = lerp(a.fov, b.fov, p);
      this.camera.lookAt(look);
      this.camera.updateProjectionMatrix();
    });
  }

  // ---------------------------------------------------------------- DOM overlays
  el(html, cls = '', style = '') {
    const d = document.createElement('div');
    d.className = 'ov ' + cls; d.innerHTML = html; d.style.cssText = style; d.style.opacity = 0;
    this.overlay.appendChild(d);
    return d;
  }

  // px position of a world point
  project(p) {
    const c = this.projCam ?? this.camera;
    const v = p.clone().project(c);
    return { x: (v.x + 1) / 2 * W, y: (1 - v.y) / 2 * H, front: v.z < 1, dist: p.distanceTo(c.position) };
  }

  // anchor an element at a world point (anchor ax, ay as fractions of its box)
  pin(el, p, { ax = .5, ay = 1, dy = 0, dx = 0, scale = 1, depth = false } = {}) {
    const s = this.project(p);
    const k = depth ? clamp(8 / s.dist, .35, 1.6) * scale : scale;
    el.style.left = (s.x + dx) + 'px'; el.style.top = (s.y + dy) + 'px';
    el.style.transform = `translate(${-ax * 100}%, ${-ay * 100}%) scale(${k})`;
    el.style.transformOrigin = `${ax * 100}% ${ay * 100}%`;
    el.style.visibility = s.front ? 'visible' : 'hidden';
  }

  show(el, a) { el.style.opacity = clamp(a); el.style.display = a > .003 ? '' : 'none'; }
}

// ------------------------------------------------------------------ shared pieces built on the kit
// A floating pane above a piece: dark card with a header, unfolds (scaleY) when shown.
export function pane(kit, { title = '', body = '', w = 460, cls = '' } = {}) {
  const el = kit.el(`<div class="pane-h">${title}</div><div class="pane-b">${body}</div>`, 'pane ' + cls, `width:${w}px`);
  return {
    el,
    body: el.querySelector('.pane-b'),
    set(html) { if (this._h !== html) { this.body.innerHTML = html; this._h = html; } },
    place(p, a, opts = {}) {
      kit.show(el, a);
      if (a <= .003) return;
      kit.pin(el, p, opts);
      const u = ease.out(clamp(a * 1.6));
      el.style.transform += ` scaleY(${lerp(.04, 1, u)})`;
    },
    fixed(x, y, a, s = 1) {
      kit.show(el, a);
      el.style.left = x + 'px'; el.style.top = y + 'px';
      el.style.transform = `scale(${s}) scaleY(${lerp(.04, 1, ease.out(clamp(a * 1.6)))})`; el.style.transformOrigin = '0 0';
    },
  };
}

// A name chip that floats over a piece.
export function chip(kit, html, cls = '') {
  const el = kit.el(html, 'chip ' + cls);
  return { el, place(p, a, opts = {}) { kit.show(el, a); if (a > .003) kit.pin(el, p, { ay: 1, dy: -8, ...opts }); } };
}

// An agent: a smaller translucent rook in one accent colour, with a name chip.
export function agent(kit, name, color, at, { scale = .32, chipText = name } = {}) {
  const mat = new THREE.MeshToonMaterial({ color, gradientMap: kit.ramp, transparent: true, opacity: .82, emissive: color, emissiveIntensity: .28 });
  const inkMat = new THREE.LineBasicMaterial({ color: new THREE.Color(color).multiplyScalar(.35), transparent: true, opacity: .9 });
  const obj = kit.make('agent', { tint: color, tintMat: mat, inkMat, scale, at });
  const c = chip(kit, `<i style="background:#${new THREE.Color(color).getHexString()}"></i>${chipText}`, 'agent-chip');
  return { obj, mat, chip: c, color, top: () => obj.position.clone().add(V(0, 2.4 * obj.scale.y, 0)) };
}

// A stylized faceless figure; pose(t-free) helpers.
export function figure(kit, at, rot = 0, scale = 1) {
  const obj = kit.make('figure', { at, rot, scale });
  const n = k => kit.node(obj, k);
  return {
    obj, head: n('head'), torso: n('torso'), armL: n('armL'), armR: n('armR'), legL: n('legL'), legR: n('legR'), hips: n('hips'),
    foreL: n('armL').children.find(c => c.name.startsWith('fore')), foreR: n('armR').children.find(c => c.name.startsWith('fore')),
    rest() {
      for (const k of ['head', 'torso', 'armL', 'armR', 'legL', 'legR']) this[k].rotation.set(0, 0, 0);
      this.foreL.rotation.set(0, 0, 0); this.foreR.rotation.set(0, 0, 0);
      this.armL.rotation.z = -.08; this.armR.rotation.z = .08;
    },
    walk(phase, amt = 1) {
      const s = Math.sin(phase) * .45 * amt;
      this.legL.rotation.x = s; this.legR.rotation.x = -s; this.armL.rotation.x = -s * .8; this.armR.rotation.x = s * .8;
    },
  };
}

// The gold rook: the hub. Returns the object and its door pivot.
export function goldRook(kit, at = V(0, 0, 0), scale = .55, rot = 0) {
  const obj = kit.make('rook', { at, scale, rot, mats: { gold: kit.toon(COL.gold), body: kit.toon(COL.gold) } });
  return { obj, door: kit.node(obj, 'door') };
}

// A translucent hologram rook (gold, additive glow) for the phone.
export function holoRook(kit, scale = .1) {
  const mat = new THREE.MeshBasicMaterial({ color: 0xf0c98c, transparent: true, opacity: .22, blending: THREE.AdditiveBlending, depthWrite: false });
  const ink = new THREE.LineBasicMaterial({ color: 0xffd99a, transparent: true, opacity: .9, blending: THREE.AdditiveBlending });
  const allMats = {}; for (const k of ['gold', 'stone', 'wood', 'iron', 'light', 'body']) allMats[k] = mat;
  const obj = kit.make('rook', { scale, mats: allMats, inkMat: ink });
  obj.traverse(o => { if (o.isMesh && o.userData.mat === 'light') o.visible = false; });
  return { obj, mat, ink };
}

// ------------------------------------------------------------------ the standard open and close
// Opening sting: the gold rook turning, 2 s. End card: tagline, "Your machines, your move", repo.
export function bookends(kit, clip) {
  const s = new THREE.Scene();
  s.add(new THREE.HemisphereLight(0xe3d8ad, 0x2b3323, 1.5));
  const k2 = new THREE.DirectionalLight(0xffedc4, 2.6); k2.position.set(-5, 8, 6); s.add(k2);
  const r = kit.make('rook', { parent: s, mats: { gold: kit.toon(COL.gold) } });
  const gc = document.createElement('canvas'); gc.width = gc.height = 256;
  { const x = gc.getContext('2d'), gr = x.createRadialGradient(128, 128, 0, 128, 128, 128);
    gr.addColorStop(0, 'rgba(216,173,109,.55)'); gr.addColorStop(1, 'rgba(216,173,109,0)'); x.fillStyle = gr; x.fillRect(0, 0, 256, 256); }
  const gt = new THREE.CanvasTexture(gc); gt.colorSpace = THREE.SRGBColorSpace;
  const glow = new THREE.Mesh(new THREE.PlaneGeometry(9, 9), new THREE.MeshBasicMaterial({ map: gt, transparent: true, opacity: .35, blending: THREE.AdditiveBlending, depthWrite: false }));
  glow.position.set(0, 2.2, -2); s.add(glow);
  const cam = new THREE.PerspectiveCamera(30, W / H, .1, 100);
  const end = kit.el(`<div class="tag">${clip.tagline}</div><div class="move">Your machines, <b>your move.</b></div>
    <div class="brand">ROOK</div><div class="url">github.com/Bake-Ware/rook</div>`, 'endcard');
  const title = kit.el(`<div class="ep">ROOK MOVES · ${clip.num}</div><div class="nm">${clip.title}</div>`, 'sting-title');
  kit.events.push({ t: 0, type: 'sting', g: true }, { t: clip.T - ENDCARD + .2, type: 'end', g: true });
  return {
    scene: s, cam,
    // returns true when a bookend owns this frame
    frame(t) {
      const T = clip.T;
      const inSting = t < STING, inEnd = t >= T - ENDCARD;
      kit.show(title, inSting ? fade(t, .25, .6, 1.6, 1.95) : 0);
      kit.show(end, inEnd ? inv(T - ENDCARD + .3, T - ENDCARD + 1.0, t) * (1 - inv(T - .6, T, t)) : 0);
      if (!inSting && !inEnd) return false;
      const u = inSting ? t / STING : (t - (T - ENDCARD)) / ENDCARD;
      r.rotation.y = inSting ? lerp(-.9, .5, ease.out(u)) : lerp(.2, -.5, u);
      if (inSting) {
        cam.position.set(0, 2.6, lerp(15, 12.5, ease.out(u))); cam.lookAt(0, 2.05, 0);
      } else {
        cam.position.set(0, 1.6, 26); cam.lookAt(0, -.9, 0);
      }
      cam.updateProjectionMatrix();
      kit.renderPass.scene = s; kit.renderPass.camera = cam;
      kit.dim = inSting ? fade(t, 0, .35, 1.7, 2.0) : inv(T - ENDCARD, T - ENDCARD + .5, t) * (1 - inv(T - .6, T, t));
      return true;
    },
  };
}

// ------------------------------------------------------------------ voice, subtitles, captions
// who: 'rook' (Rook's voice) or 'you' (the figure). Times are clip time (seconds after the sting).
Kit.prototype.say = function (t, who, text, id) {
  id = `c${this.clipNum}_${id ?? who + this.voice.length}`;
  const dur = (window.VOICE_DUR && window.VOICE_DUR[id]) || (text.split(/\s+/).length / 2.7 + .4);
  this.voice.push({ id, t, who, text, dur });
  if (!this.subs) {
    this.subs = this.el('<span class="who"></span><span class="txt"></span>', 'subs');
    this.updaters.push(tt => {
      const v = this.voice.find(v => tt >= v.t - .1 && tt < v.t + v.dur + .5);
      const a = v ? fade(tt, v.t - .1, v.t + .1, v.t + v.dur + .25, v.t + v.dur + .5) : 0;
      this.show(this.subs, a);
      if (v && this.subs._id !== v.id) {
        this.subs._id = v.id;
        this.subs.className = 'ov subs ' + v.who;
        this.subs.querySelector('.who').textContent = v.who === 'rook' ? 'ROOK' : v.who === 'you' ? 'YOU' : v.who.toUpperCase();
        this.subs.querySelector('.txt').textContent = v.text;
      }
    });
  }
  return t + dur;
};

// A caption that sits in a fixed place: kind 'stamp' (big clock, top left), 'label' (lower middle),
// 'title' (centre). Shown from a to b.
Kit.prototype.caption = function (html, a, b, kind = 'label') {
  const el = this.el(html, 'cap ' + kind);
  this.updaters.push(t => this.show(el, fade(t, a, a + .35, b - .35, b)));
  return el;
};
