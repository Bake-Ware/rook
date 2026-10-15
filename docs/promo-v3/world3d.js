// 3D world for promo v3: the 2D scene logic in film.html stays the model (positions,
// opacities, glows, beams); this module draws it as toon-shaded, ink-edged 3D models.
import * as THREE from 'three';

export function makeWorld(canvas, SW, SH, DEV, AG, TG, MESH) {
  const R3 = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  R3.setPixelRatio(1); R3.setSize(SW, SH, false); R3.setClearColor(0x0e110e, 1);
  R3.shadowMap.enabled = true; R3.shadowMap.type = THREE.PCFSoftShadowMap;
  const S = new THREE.Scene(); S.fog = new THREE.Fog(0x0e110e, 3500, 12000);
  const FOV = 32, TANF = Math.tan(FOV / 2 * Math.PI / 180), EL = 48 * Math.PI / 180;
  const cam = new THREE.PerspectiveCamera(FOV, SW / SH, 10, 40000);
  S.add(new THREE.HemisphereLight(0xe3d8ad, 0x2b3323, 2.0));
  const key = new THREE.DirectionalLight(0xffedc4, 2.7);
  key.position.set(-1700, 3400, 2100); key.castShadow = true; key.shadow.mapSize.set(4096, 4096);
  Object.assign(key.shadow.camera, { left: -3300, right: 3300, top: 3300, bottom: -3300, near: 100, far: 10000 });
  key.shadow.bias = -.0006; key.shadow.normalBias = 1.5; S.add(key); S.add(key.target);
  const ramp = new THREE.DataTexture(new Uint8Array([62, 62, 62, 255, 132, 132, 132, 255, 208, 208, 208, 255, 255, 255, 255, 255]), 4, 1, THREE.RGBAFormat);
  ramp.minFilter = ramp.magFilter = THREE.NearestFilter; ramp.needsUpdate = true;
  const toon = c => new THREE.MeshToonMaterial({ color: c, gradientMap: ramp, polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });
  const M = { body: toon(0xaaa17d), dark: toon(0x33352c), metal: toon(0x8c8a7a), board: toon(0x5f6b45), chip: toon(0x24261f) };
  const inkMat = new THREE.LineBasicMaterial({ color: 0x070907, transparent: true, opacity: .9 });
  const add = (THREE_obj, parent) => { parent.add(THREE_obj); return THREE_obj; };
  function part(geo, mat, x, y, z, parent, rx = 0, ry = 0, rz = 0) {
    const m = new THREE.Mesh(geo, mat); m.position.set(x, y, z); m.rotation.set(rx, ry, rz); m.castShadow = m.receiveShadow = true; parent.add(m);
    const e = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 25), inkMat); e.position.copy(m.position); e.rotation.copy(m.rotation); parent.add(e);
    return m;
  }
  const box = (w, h, d) => new THREE.BoxGeometry(w, h, d);
  function screen(w, h, parent, x, y, z, rx = 0) {
    const mat = new THREE.MeshBasicMaterial({ color: 0x1a2520 });
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), mat); m.position.set(x, y, z); m.rotation.x = rx; parent.add(m);
    // a few faint UI bars on the screen
    const bars = new THREE.Group(); bars.position.copy(m.position); bars.rotation.copy(m.rotation); parent.add(bars);
    for (let i = 0; i < 4; i++) { const b = new THREE.Mesh(new THREE.PlaneGeometry(w * (.35 + .4 * ((i * 37) % 10) / 10), h * .07), new THREE.MeshBasicMaterial({ color: i ? 0x3d4a40 : 0x8a6f45 })); b.position.set(-w * .42 + b.geometry.parameters.width / 2, h * (.32 - i * .17), .5); bars.add(b); }
    return mat;
  }
  const BUILD = {
    laptop(g) { part(box(170, 9, 118), M.body, 0, 4.5, 0, g); const lid = new THREE.Group(); lid.position.set(0, 9, -59); lid.rotation.x = -.3; g.add(lid);
      part(box(170, 112, 7), M.body, 0, 56, 0, lid); return [screen(156, 98, lid, 0, 56, 3.7)]; },
    phone(g) { const p = new THREE.Group(); p.rotation.x = -.32; p.position.y = 4; g.add(p); part(box(66, 132, 10), M.dark, 0, 66, 0, p);
      part(box(80, 8, 46), M.body, 0, 4, 18, g); return [screen(58, 118, p, 0, 66, 5.2)]; },
    desktop(g) { part(box(90, 6, 56), M.body, 0, 3, 0, g); part(box(18, 60, 12), M.body, 0, 36, -6, g); part(box(220, 136, 12), M.body, 0, 128, 0, g);
      return [screen(204, 120, g, 0, 128, 6.2)]; },
    tablet(g) { const p = new THREE.Group(); p.rotation.x = -.5; p.position.y = 6; g.add(p); part(box(180, 124, 9), M.dark, 0, 62, 0, p);
      part(box(150, 8, 40), M.body, 0, 4, 22, g); return [screen(166, 110, p, 0, 62, 4.7)]; },
    box(g) { part(box(96, 160, 150), M.body, 0, 80, 0, g, 0, .35); const f = new THREE.Group(); f.rotation.y = .35; g.add(f);
      const fan = new THREE.Mesh(new THREE.TorusGeometry(30, 4, 6, 24), M.dark); fan.position.set(0, 100, 76); f.add(fan);
      for (let i = 0; i < 3; i++) { const bl = part(box(6, 50, 3), M.dark, 0, 100, 76, f, 0, 0, i * Math.PI / 3); }
      const led = new THREE.Mesh(new THREE.SphereGeometry(4, 8, 6), new THREE.MeshBasicMaterial({ color: 0x8fc79a })); led.position.set(30, 30, 76); f.add(led);
      return []; },
    rack(g) { for (let i = 0; i < 3; i++) { part(box(160, 30, 116), M.body, 0, 17 + i * 34, 0, g);
        const led = new THREE.Mesh(new THREE.SphereGeometry(4, 8, 6), new THREE.MeshBasicMaterial({ color: i === 1 ? 0xf0c98c : 0x8fc79a })); led.position.set(62, 17 + i * 34, 59); g.add(led);
        part(box(90, 4, 2), M.dark, -20, 17 + i * 34, 59, g); }
      return []; },
    nas(g) { part(box(116, 136, 116), M.body, 0, 68, 0, g); for (let i = 0; i < 4; i++) { part(box(90, 22, 6), M.metal, -6, 30 + i * 28, 59, g);
        const led = new THREE.Mesh(new THREE.SphereGeometry(3.5, 8, 6), new THREE.MeshBasicMaterial({ color: 0x8fc79a })); led.position.set(46, 30 + i * 28, 62); g.add(led); }
      return []; },
    pi(g) { part(box(130, 8, 88), M.board, 0, 4, 0, g); part(box(40, 8, 34), M.chip, -10, 12, 0, g); part(box(26, 18, 22), M.metal, 52, 13, 20, g);
      part(box(26, 18, 22), M.metal, 52, 13, -14, g); part(box(80, 8, 8), M.dark, -16, 12, -36, g); return []; },
  };
  // ground, grid, drafting rings
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(20000, 20000), new THREE.ShadowMaterial({ opacity: .38 }));
  ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; S.add(ground);
  const grid = new THREE.GridHelper(9216, 72, 0xd8ad6d, 0xd8ad6d); grid.material.transparent = true; grid.material.opacity = .055; grid.position.y = .5; S.add(grid);
  const lineMat = o => new THREE.LineBasicMaterial({ color: 0xc5af87, transparent: true, opacity: o });
  for (const [r, o] of [[700, .16], [1500, .1], [2600, .07]]) {
    const pts = Array.from({ length: 160 }, (_, i) => new THREE.Vector3(Math.cos(i / 160 * Math.PI * 2) * r, 1, Math.sin(i / 160 * Math.PI * 2) * r));
    S.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(pts), lineMat(o)));
  }
  // mesh links between devices
  const meshMat = new THREE.LineDashedMaterial({ color: 0xd8ad6d, transparent: true, opacity: .25, dashSize: 14, gapSize: 26 });
  const meshLines = MESH.map(([a, b]) => { const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(DEV[a].x, 2, DEV[a].y), new THREE.Vector3(DEV[b].x, 2, DEV[b].y)]), meshMat.clone()); l.computeLineDistances(); S.add(l); return l; });
  // devices
  const glowRingGeo = new THREE.RingGeometry(118, 130, 64);
  DEV.forEach(d => {
    const g = new THREE.Group(); g.position.set(d.x, 0, d.y); S.add(g);
    const inner = new THREE.Group(); inner.rotation.y = Math.atan2(-d.x * .15, 900) ; g.add(inner);
    const screens = BUILD[d.type](inner);
    const ring = new THREE.Mesh(glowRingGeo, new THREE.MeshBasicMaterial({ color: 0xf0c98c, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }));
    ring.rotation.x = -Math.PI / 2; ring.position.y = 1.5; g.add(ring);
    const pool = new THREE.Mesh(new THREE.CircleGeometry(150, 40), new THREE.MeshBasicMaterial({ color: 0xffcf8a, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false }));
    pool.rotation.x = -Math.PI / 2; pool.position.y = 1; g.add(pool);
    d.o3 = { g, screens, ring, pool };
  });
  // telegram: a floating badge
  { const c = document.createElement('canvas'); c.width = c.height = 256; const x = c.getContext('2d');
    x.fillStyle = '#2f9fd8'; x.beginPath(); x.arc(128, 128, 120, 0, Math.PI * 2); x.fill(); x.strokeStyle = '#d8ad6d'; x.lineWidth = 8; x.stroke();
    x.fillStyle = '#fff'; x.beginPath(); x.moveTo(62, 126); x.lineTo(190, 74); x.lineTo(166, 186); x.lineTo(132, 154); x.lineTo(112, 180); x.lineTo(110, 146); x.lineTo(170, 96); x.lineTo(98, 140); x.closePath(); x.fill();
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace;
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: t, transparent: true })); sp.scale.set(120, 120, 1); sp.position.set(TG.x, 90, TG.y); S.add(sp); TG.o3 = sp; }
  // agents: floating hex cores with a halo and a light pillar
  AG.forEach(a => {
    const col = new THREE.Color(a.col);
    const g = new THREE.Group(); g.position.set(a.x, 0, a.y); S.add(g);
    const coreMat = new THREE.MeshToonMaterial({ color: col, gradientMap: ramp, emissive: col, emissiveIntensity: .55 });
    const hex = new THREE.Mesh(new THREE.CylinderGeometry(46, 46, 26, 6), coreMat); hex.position.y = 125; hex.castShadow = true; g.add(hex);
    const hexInk = new THREE.LineSegments(new THREE.EdgesGeometry(hex.geometry), inkMat); hexInk.position.copy(hex.position); g.add(hexInk);
    const haloMat = new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: .9 });
    const halo = new THREE.Mesh(new THREE.TorusGeometry(64, 3, 6, 48), haloMat); halo.rotation.x = Math.PI / 2; halo.position.y = 125; g.add(halo);
    const pillarMat = new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: .22, blending: THREE.AdditiveBlending, depthWrite: false });
    const pillar = new THREE.Mesh(new THREE.CylinderGeometry(5, 5, 112, 8), pillarMat); pillar.position.y = 56; g.add(pillar);
    const baseMat = new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: .35, blending: THREE.AdditiveBlending, depthWrite: false });
    const base = new THREE.Mesh(new THREE.RingGeometry(40, 70, 6), baseMat); base.rotation.x = -Math.PI / 2; base.position.y = 1.5; g.add(base);
    a.o3 = { g, hex, hexInk, halo, coreMat, haloMat, pillarMat, baseMat, col };
  });
  // beams + rings pools
  const up = new THREE.Vector3(0, 1, 0);
  const beamPool = [];
  function beamObj(i) {
    while (beamPool.length <= i) {
      const core = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 1, 8, 1, true), new THREE.MeshBasicMaterial({ transparent: true }));
      const glow = new THREE.Mesh(new THREE.CylinderGeometry(1, 1, 1, 8, 1, true), new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
      const pk = new THREE.Mesh(new THREE.SphereGeometry(13, 10, 8), new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true }));
      S.add(core, glow, pk); beamPool.push({ core, glow, pk });
    }
    return beamPool[i];
  }
  const ringPool = [];
  function ringObj(i) {
    while (ringPool.length <= i) { const m = new THREE.Mesh(new THREE.RingGeometry(.96, 1, 64), new THREE.MeshBasicMaterial({ transparent: true, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide })); m.rotation.x = -Math.PI / 2; S.add(m); ringPool.push(m); }
    return ringPool[i];
  }
  const heightOf = o => o.col ? 125 : o === TG ? 90 : 80;

  const V = new THREE.Vector3();
  const api = {
    setCamera([cx, cy, zoom, rot]) {
      const D = (SH / 2) / (zoom * TANF), az = -rot * Math.PI / 180;
      cam.position.set(cx + D * Math.sin(az) * Math.cos(EL), D * Math.sin(EL), cy + D * Math.cos(az) * Math.cos(EL));
      cam.up.set(0, 1, 0); cam.lookAt(cx, 0, cy); cam.updateMatrixWorld(); cam.updateProjectionMatrix();
      key.position.set(cx - 1700, 3400, cy + 2100); key.target.position.set(cx, 0, cy); key.target.updateMatrixWorld();
    },
    project(x, h, y) { // screen x, screen y, px per world unit at that point
      V.set(x, h, y); const dist = V.distanceTo(cam.position); V.project(cam);
      return [(V.x + 1) / 2 * SW, (1 - V.y) / 2 * SH, (SH / 2) / (dist * TANF), V.z < 1];
    },
    draw({ devState, agState, tgState, meshOp, beams, rings }) {
      DEV.forEach((d, i) => {
        const st = devState[i], o = d.o3;
        o.g.visible = st.op > .01; o.g.scale.setScalar(Math.max(.001, st.scale));
        o.ring.material.opacity = .6 * st.glow; o.pool.material.opacity = .07 * st.glow;
        o.screens.forEach(m => m.color.setRGB(.1 + .14 * st.glow, .145 + .2 * st.glow, .125 + .17 * st.glow));
      });
      AG.forEach((a, i) => {
        const st = agState[i], o = a.o3; o.g.visible = st.op > .01; o.g.scale.setScalar(Math.max(.001, st.scale));
        const c = st.dead ? new THREE.Color(0x5a5f55) : o.col;
        o.coreMat.color.copy(c); o.coreMat.emissive.copy(c); o.coreMat.emissiveIntensity = st.dead ? 0 : .55;
        o.haloMat.color.copy(c); o.haloMat.opacity = .9 * st.op; o.pillarMat.opacity = (st.dead ? .05 : .22) * st.op; o.baseMat.opacity = (st.dead ? .1 : .35) * st.op;
        o.halo.scale.setScalar(st.pulse); o.hex.rotation.y = st.spin; o.hexInk.rotation.y = st.spin;
      });
      TG.o3.visible = tgState.op > .01; TG.o3.material.opacity = tgState.op; TG.o3.scale.setScalar(120 * tgState.scale);
      meshLines.forEach((l, i) => l.material.opacity = meshOp[i]);
      beamPool.forEach(b => { b.core.visible = b.glow.visible = b.pk.visible = false; });
      beams.forEach((bm, i) => {
        const o = beamObj(i), A3 = new THREE.Vector3(bm.a.x, heightOf(bm.a), bm.a.y), B3 = new THREE.Vector3(bm.b.x, heightOf(bm.b), bm.b.y);
        const E = A3.clone().lerp(B3, bm.grow), dir = E.clone().sub(A3), len = Math.max(.01, dir.length());
        const q = new THREE.Quaternion().setFromUnitVectors(up, dir.clone().normalize()); const mid = A3.clone().add(E).multiplyScalar(.5);
        for (const [m, r, op] of [[o.core, 5 * bm.w, bm.op], [o.glow, 20 * bm.w, .28 * bm.op]]) { m.visible = true; m.position.copy(mid); m.quaternion.copy(q); m.scale.set(r, len, r); m.material.color.set(bm.col); m.material.opacity = op; }
        if (bm.packet != null && bm.packet >= 0 && bm.packet <= 1) { o.pk.visible = true; o.pk.position.copy(A3.clone().lerp(B3, bm.packet)); o.pk.material.opacity = bm.op; }
      });
      ringPool.forEach(r => r.visible = false);
      rings.forEach((rg, i) => { const m = ringObj(i); m.visible = true; m.position.set(rg.x, 3, rg.y); const w = Math.max(rg.r, 1); m.scale.set(w, w, w); m.material.color.set(rg.col); m.material.opacity = rg.op; });
      R3.render(S, cam);
    },
  };
  return api;
}
