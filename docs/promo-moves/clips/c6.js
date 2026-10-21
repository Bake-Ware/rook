// 6. Comic, song, shed — "Superpowers, for real life."
// Three quick asks from normal life, three things back, cut to a beat: a rough comic about
// a cat stealing socks, a Celtic-flavoured piano tune, a 10 x 12 shed with dimensions.
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, agent, figure, rnd } from '../kit.js';

const CAT = `<svg viewBox="0 0 200 150"><g fill="none" stroke="#2a2418" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">`;
const PANELS = [
  ['1 · the cat', CAT + `<path d="M40 120 Q60 70 100 78 Q140 70 150 120 Z"/><circle cx="96" cy="62" r="22"/><path d="M80 48 L76 28 L90 42 M104 42 L116 28 L114 50"/><path d="M150 112 Q178 100 170 70"/><circle cx="88" cy="60" r="2" fill="#2a2418"/><circle cx="104" cy="60" r="2" fill="#2a2418"/><path d="M20 128 H182"/></g></svg>`],
  ['2 · the drawer', CAT + `<rect x="30" y="40" width="140" height="80" rx="4"/><path d="M30 80 H170"/><path d="M90 60 h20 M90 100 h20"/><path d="M52 40 q8 -22 22 -8 q10 -16 18 4"/><path d="M120 40 q10 -18 24 -4"/></g></svg>`],
  ['3 · the heist', CAT + `<path d="M50 110 Q70 74 108 82 Q140 80 146 110"/><circle cx="120" cy="66" r="18"/><path d="M108 54 L104 38 L116 50 M126 48 L136 36 L134 56"/><path d="M134 76 q18 4 26 22 q-8 6 -18 -4"/><path d="M20 70 h16 M14 86 h22 M22 102 h14"/></g></svg>`],
  ['4 · the pile', CAT + `<path d="M30 130 Q60 92 100 100 Q140 90 172 130 Z"/><path d="M50 118 q10 -14 20 0 M86 108 q12 -16 24 0 M126 116 q10 -12 20 2"/><circle cx="104" cy="70" r="18"/><path d="M92 58 L88 42 L100 54 M110 54 L120 42 L118 60"/><path d="M96 70 q8 6 16 0"/></g></svg>`],
];

export default {
  title: 'Comic, song, shed', tagline: 'Superpowers,<br>for real life.', body: 38,
  mood: { key: 'D', bpm: 112, energy: .9 },
  assets: ['rook', 'agent', 'table', 'chair', 'figure', 'tablet', 'coffee', 'car', 'dash'],
  async build(kit) {
    const S = kit.scene;
    kit.board({ size: 70 });
    S.fog = new THREE.Fog(COL.page, 12, 30);
    goldRook(kit, V(0, 0, -7), .55, .3);
    // kitchen table corner
    const mark = () => S.children.length, since = n => S.children.slice(n);   // which objects each place added
    const m0 = mark();
    const K = V(-12, 0, 0);
    const ktab = kit.make('table', { at: K }); ktab.scale.set(.45, 1, .45);       // a 1.2 m kitchen table
    kit.make('chair', { at: K.clone().add(V(0, 0, .78)), rot: Math.PI });
    const f1 = figure(kit, K.clone().add(V(0, -.47, .74)), Math.PI, 1, { f: true });
    f1.sit(); f1.armL.rotation.x = f1.armR.rotation.x = -.95; f1.foreL.rotation.x = f1.foreR.rotation.x = -.9;   // forearms resting on the table top
    kit.make('tablet', { at: K.clone().add(V(.05, .79, .2)), rot: Math.PI });
    kit.make('coffee', { at: K.clone().add(V(-.32, .79, .22)) });
    // the car at a red light
    const kitchenObjs = since(m0), m1 = mark();
    const C = V(0, 0, 0);
    // the car from outside (wide shots), and its inside for the car scene: dash, wheel, seats, the driver
    const car = kit.make('car', { at: C, rot: Math.PI / 2, tint: 0x9a6a4a });
    const cabin = new THREE.Group(); cabin.position.copy(C); S.add(cabin);
    kit.make('dash', { parent: cabin, at: V(0, .3, -.5) });
    for (const sx of [-.42, .42]) {
      const seat = new THREE.Mesh(new THREE.BoxGeometry(.5, .12, .52), kit.toon(0x3b3a33)); seat.position.set(sx, .55, .56); cabin.add(seat);
      const back = new THREE.Mesh(new THREE.BoxGeometry(.5, .72, .12), kit.toon(0x3b3a33)); back.position.set(sx, .96, .86); back.rotation.x = .2; cabin.add(back);
      const rest = new THREE.Mesh(new THREE.BoxGeometry(.26, .18, .1), kit.toon(0x3b3a33)); rest.position.set(sx, 1.46, .97); rest.rotation.x = .2; cabin.add(rest);
      for (const m of [seat, back, rest]) m.add(new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry, 25), kit.inkMat));
    }
    const doorR = new THREE.Mesh(new THREE.BoxGeometry(.06, .55, 1.2), kit.toon(0x3a3c32)); doorR.position.set(.88, .8, .2); cabin.add(doorR);
    const f2 = figure(kit, V(-.42, .14, .56), Math.PI); cabin.add(f2.obj);
    f2.sit({ hands: 'none' }); f2.armL.rotation.x = f2.armR.rotation.x = -1.15; f2.foreL.rotation.x = f2.foreR.rotation.x = -.35;
    const pole = new THREE.Mesh(new THREE.BoxGeometry(.1, 3.2, .1), kit.toon(0x3a3c32)); pole.position.set(-1.6, 1.6, -4.6); S.add(pole);
    const head = new THREE.Mesh(new THREE.BoxGeometry(.32, .85, .28), kit.toon(0x24261f)); head.position.set(-1.6, 3.1, -4.6); S.add(head);
    const red = new THREE.Mesh(new THREE.CircleGeometry(.1, 16), new THREE.MeshBasicMaterial({ color: 0xff5a48 })); red.position.set(-1.6, 3.36, -4.45); S.add(red);
    for (const o of [pole, head]) o.add(new THREE.LineSegments(new THREE.EdgesGeometry(o.geometry), kit.inkMat));
    // the backyard and a shed in wireframe, real size: 10 x 12 ft = 3.05 x 3.66 m
    const carObjs = since(m1), m2 = mark();
    const B = V(12, 0, 0);
    const f3 = figure(kit, B.clone().add(V(-2.3, 0, 2.5)), -2.3, 1, { f: true });
    const tape = new THREE.Mesh(new THREE.BoxGeometry(.12, .12, .06), kit.toon(0xe8c840)); f3.foreR.add(tape); tape.position.set(0, -.34, 0);
    const W_ = 3.05, D_ = 3.66, H_ = 2.1, R_ = .76;
    const c = (x, y, z) => B.clone().add(V(x - W_ / 2, y, z - D_ / 2));
    const E = [];
    const seg = (a, b) => E.push(a, b);
    // floor, walls, roof ridge, door on the short side (front, z = D)
    const f = [c(0, 0, 0), c(W_, 0, 0), c(W_, 0, D_), c(0, 0, D_)];
    for (let i = 0; i < 4; i++) seg(f[i], f[(i + 1) % 4]);
    for (const p of f) seg(p, p.clone().setY(H_));
    for (let i = 0; i < 4; i++) seg(f[i].clone().setY(H_), f[(i + 1) % 4].clone().setY(H_));
    for (const z of [0, D_]) { seg(c(0, H_, z), c(W_ / 2, H_ + R_, z)); seg(c(W_, H_, z), c(W_ / 2, H_ + R_, z)); }
    seg(c(W_ / 2, H_ + R_, 0), c(W_ / 2, H_ + R_, D_));
    for (let k = 1; k < 4; k++) { const z = D_ * k / 4; seg(c(0, H_, z), c(W_ / 2, H_ + R_, z)); seg(c(W_, H_, z), c(W_ / 2, H_ + R_, z)); }
    const dl = W_ / 2 - .46, dr = W_ / 2 + .46;
    seg(c(dl, 0, D_), c(dl, 2.0, D_)); seg(c(dr, 0, D_), c(dr, 2.0, D_)); seg(c(dl, 2.0, D_), c(dr, 2.0, D_));
    // the footprint, exactly the shed's 10 x 12 ft, and the door panel on the short (10 ft) side
    const foot = new THREE.Mesh(new THREE.PlaneGeometry(W_, D_), new THREE.MeshBasicMaterial({ color: 0x79a7d8, transparent: true, opacity: .0, depthWrite: false }));
    foot.rotation.x = -Math.PI / 2; foot.position.copy(B).setY(.012); S.add(foot);
    const footEdge = new THREE.LineSegments(new THREE.EdgesGeometry(foot.geometry), new THREE.LineBasicMaterial({ color: 0x9cc4f0, transparent: true, opacity: 0 }));
    foot.add(footEdge);
    const doorP = new THREE.Mesh(new THREE.PlaneGeometry(.92, 2.0), new THREE.MeshBasicMaterial({ color: COL.goldHi, transparent: true, opacity: 0, depthWrite: false, side: THREE.DoubleSide }));
    doorP.position.copy(c(W_ / 2, 1.0, D_)); S.add(doorP);
    // a fence on two sides of the yard (behind the shed and to its right)
    const fenceMat = kit.toon(0x7a6a52), picket = new THREE.BoxGeometry(.09, 1.05, .025);
    const runs = [[c(-1.2, 0, -1.4), c(W_ + 2.6, 0, -1.4)], [c(W_ + 2.6, 0, -1.4), c(W_ + 2.6, 0, D_ + 2.2)]];
    for (const [a0, a1] of runs) {
      const len = a0.distanceTo(a1), n = Math.floor(len / .16), dir = a1.clone().sub(a0).normalize(), ang = Math.atan2(dir.x, dir.z);
      const inst = new THREE.InstancedMesh(picket, fenceMat, n); const m4 = new THREE.Matrix4(), q = new THREE.Quaternion().setFromAxisAngle(V(0, 1, 0), ang + Math.PI / 2);
      for (let k = 0; k < n; k++) { m4.compose(a0.clone().addScaledVector(dir, (k + .5) * len / n).setY(.525), q, V(1, 1, 1)); inst.setMatrixAt(k, m4); }
      S.add(inst);
      for (const y of [.25, .8]) { const rail = new THREE.Mesh(new THREE.BoxGeometry(.05, .07, len), fenceMat); rail.position.copy(a0.clone().lerp(a1, .5)).setY(y);
        rail.rotation.y = ang; rail.position.addScaledVector(V(Math.cos(ang), 0, -Math.sin(ang)), .03); S.add(rail);
        rail.add(new THREE.LineSegments(new THREE.EdgesGeometry(rail.geometry), kit.inkMat)); }
    }
    const shedG = new THREE.BufferGeometry().setFromPoints(E);
    const shed = new THREE.LineSegments(shedG, new THREE.LineBasicMaterial({ color: COL.goldHi }));
    S.add(shed);
    const nSeg = E.length / 2;
    const dims = [['10 ft', c(W_ / 2, 0, D_ + .35)], ['12 ft', c(W_ + .35, 0, D_ / 2)], ['6:12 pitch', c(W_ / 2, H_ + R_ + .3, 0)], ['door · 3 ft', c(W_ / 2, 2.2, D_ + .1)]]
      .map(([t, p], i) => ({ el: kit.el(t, 'dim' + (i === 3 ? ' door' : '')), p, i }));
    // notes riding out of the car speaker
    const yardObjs = since(m2);
    const notes = [];
    for (let i = 0; i < 16; i++) notes.push({ el: kit.el(i % 3 ? '♪' : '♫', 'note'), i });

    // ---------------------------------------------------------- overlays
    kit.say(.3, 'you_f', 'Make me a comic about my cat stealing socks.', 'a');
    kit.caption('Comic, rough draft. Mittens on the counter, as usual.', 7.4, 10.8, 'label');
    kit.say(11.6, 'you', 'Hum me a birthday tune for my sister. Celtic vibe, but piano.', 'b');
    kit.caption('Celtic piano, 8 bars', 17.4, 21.6, 'label');
    kit.say(21.9, 'you_f', 'I’ve got ten by twelve feet back here. The door has to be on the short side, though.', 'c');
    kit.caption('3D model with dimensions', 28.5, 32.6, 'label');
    kit.caption('Agents that help <b>where you are.</b>', 33.4, 38, 'big low');
    const panels = PANELS.map(([t, svg]) => pane(kit, { title: t, w: 300, body: `<div class="art">${svg}</div>`, cls: 'comic' }));
    const song = pane(kit, { title: '<b>for your sister</b> · celtic piano', w: 460, body: `<div class="staff">${'<i></i>'.repeat(5)}<span class="nh"></span></div>` });
    const shedP = pane(kit, { title: '<b>shed</b> · 10 × 12 ft', w: 360, body: `<div class="dim">door on the short side · 6:12 roof</div><div class="ok">✓ model + cut list</div>` });
    const st = document.createElement('style'); st.textContent = `
      .comic .pane-b{padding:10px;background:#e8dfc6}.comic .art svg{width:100%;height:auto;display:block}
      .dim.ov{font:400 22px var(--mono);color:var(--hi);background:rgba(9,12,9,.85);border:1px solid #514b35;border-radius:6px;padding:3px 9px;white-space:nowrap}
      .note{font:400 54px 'DejaVu Sans Mono',monospace;color:#f0c98c;text-shadow:0 0 18px rgba(240,201,140,.8)}
      .staff{position:relative;height:110px}.staff i{display:block;height:1.5px;background:#514b35;margin:0 0 18px}
      .staff .nh{position:absolute;left:0;top:0;right:0;bottom:0;background:repeating-linear-gradient(90deg,transparent 0 34px,transparent 34px 36px)}`;
    document.head.appendChild(st);
    const nh = song.el.querySelector('.nh');
    const MEL = [74, 76, 78, 79, 78, 76, 74, 71, 69, 74, 76, 78, 81, 79, 78, 76];
    nh.innerHTML = MEL.map((m, i) => `<b style="position:absolute;left:${10 + i * 27}px;top:${(81 - m) * 8 + 4}px;width:14px;height:11px;border-radius:50%;background:#f0c98c;opacity:0" data-i="${i}"></b>`).join('');
    const heads = [...nh.querySelectorAll('b')];

    kit.events.push({ t: 0, type: 'rush' }, { t: 3.2, type: 'tap' }, { t: 4.8, type: 'tap' }, { t: 6.4, type: 'tap' }, { t: 8.0, type: 'tap' },
      { t: 11.2, type: 'whoosh' }, { t: 15.0, type: 'melody', bpm: 132, bars: 4 }, { t: 21.4, type: 'whoosh' }, { t: 26.2, type: 'shimmer' }, { t: 33.2, type: 'rush' });
    kit.pulse(V(0, 0, -7), K.clone().setY(0).add(V(0, 0, 1)), .8, 1.6);
    kit.pulse(V(0, 0, -7), V(0, 0, 0), 12.4, 1.2);
    kit.pulse(V(0, 0, -7), V(10, 0, 3), 24.4, 1.4);

    const kit3 = [K.clone().add(V(-.7, 0, -.7)), K.clone().add(V(.7, 1.4, 1.2))], carB = [V(-.9, 0, -2.3), V(.9, 1.6, 2.2)];
    const yard = [B.clone().add(V(-2.6, 0, 2.2)), B.clone().add(V(-2.0, 1.8, 2.8))], shedB = [B.clone().add(V(-1.6, 0, -1.9)), B.clone().add(V(1.6, 2.9, 1.9))];
    kit.camPath([
      { t: 0, pos: [-12 + 4, 6.5, 9], look: [-12, .8, 0], fov: 38 },
      { t: 2.6, frame: { on: kit3, yaw: 205, pitch: 18, fill: .62, look: [.7, 0, -.3] }, e: 'outE' },
      { t: 10.9, frame: { on: kit3, yaw: 198, pitch: 18, fill: .68, look: [.7, 0, -.28] }, e: 'lin' },
      { t: 11.6, pos: [.1, 1.34, 1.12], look: [-.32, 1.1, -.7], fov: 50, e: 'cut' },
      { t: 21.3, pos: [.04, 1.32, 1.02], look: [-.34, 1.1, -.7], fov: 50, e: 'lin' },
      { t: 21.6, frame: { on: yard, yaw: 20, pitch: 10, fill: .55 }, e: 'cut' },
      { t: 25.6, frame: { on: yard, yaw: 12, pitch: 10, fill: .6 }, e: 'lin' },
      { t: 28.5, frame: { on: shedB, yaw: 58, pitch: 20, fill: .78 }, e: 'io' },
      { t: 32.8, frame: { on: shedB, yaw: 140, pitch: 24, fill: .78 }, e: 'lin' },
      { t: 34.4, pos: [0, 12, 21], look: [0, 1.6, 0], e: 'io' },
      { t: 38, pos: [0, 13, 23], look: [0, 1.6, 0], e: 'lin' },
    ]);

    return tau => {
      // kitchen: comic panels flip up one at a time
      const fin = tau > 33.2;
      panels.forEach((p, i) => {
        const a = fade(tau, 3.2 + i * 1.6, 3.6 + i * 1.6, 10.8, 11.2);
        if (!fin) p.fixed(150 + (i % 2) * 330, 130 + Math.floor(i / 2) * 290, a, 1);
        else p.fixed(110 + (i % 2) * 250, 120 + Math.floor(i / 2) * 220, fade(tau, 33.6 + i * .1, 34 + i * .1, 37.6, 38), .74);
      });
      f1.head.rotation.x = .25; f1.foreR.rotation.x = tau > .3 && tau < 3 ? -1.1 + Math.sin(tau * 5) * .15 : -.9;
      kit.glowSquare(-12, 1, .6 * fade(tau, 2.4, 2.8, 10.6, 11));
      // the car: notes ride out of the speaker
      notes.forEach(({ el, i }) => {
        const t0 = 14.6 + i * .42, u = (tau - t0) / 3.4;
        const a = u > 0 && u < 1 ? Math.sin(u * Math.PI) : 0;
        kit.show(el, tau < 22 ? a : 0);
        if (a > .003) kit.pin(el, V(.05 - u * .6 + Math.sin(u * 5 + i) * .15, 1.4 + u * .9, -.65 - u * 1.4), { ay: .5, depth: false });   // out of the dash speaker
      });
      song.fixed(1320, 150, fade(tau, 15.4, 15.8, 21, 21.4) + fade(tau, 33.8, 34.2, 37.6, 38) * (tau > 33 ? 1 : 0), tau > 33 ? .85 : 1);
      if (tau > 33) song.fixed(735, 200, fade(tau, 33.8, 34.2, 37.6, 38), .85);
      heads.forEach((h, i) => { h.style.opacity = tau > 33 ? 1 : inv(15 + i * .45, 15.1 + i * .45, tau); });
      kit.glowSquare(0, 0, .6 * fade(tau, 12.8, 13.2, 21, 21.4));
      f2.head.rotation.x = .1;
      // the backyard: tape measure, then the shed draws itself
      f3.rest(); f3.armR.rotation.x = tau > 21.6 && tau < 26 ? -1.3 : -.1; f3.obj.rotation.y = -2.3; f3.foreR.rotation.x = tau > 21.6 && tau < 26 ? -.3 : 0;
      const b = ease.io(inv(25.8, 31.6, tau));
      shedG.setDrawRange(0, Math.floor(b * nSeg) * 2);
      shed.visible = tau > 25.8;
      dims.forEach(d => {
        const a = fade(tau, 29 + d.i * .4, 29.3 + d.i * .4, 33, 33.3);
        kit.show(d.el, a); if (a > .003) kit.pin(d.el, d.p, { ay: .5 });
      });
      shedP.fixed(1360, 200, fade(tau, 33.8, 34.2, 37.6, 38), .9);
      const fp = fade(tau, 24.6, 25.2, 32.6, 33);
      foot.material.opacity = .22 * fp; footEdge.material.opacity = .9 * fp; doorP.material.opacity = .35 * inv(30.8, 31.4, tau) * (tau < 33 ? 1 : 0);
      // only the person in the current scene is shown; the car's inside only for the car scene
      f1.obj.visible = tau < 11.4; f3.obj.visible = tau > 21.4;
      const inCar = tau > 11.4 && tau < 21.6, wide = tau < 2.6 || tau > 33.4;
      kitchenObjs.forEach(o => { o.visible = wide || tau < 11.4; });
      carObjs.forEach(o => { o.visible = wide || inCar; });
      yardObjs.forEach(o => { o.visible = wide || tau > 21.4; });
      cabin.visible = inCar; car.visible = wide && !inCar; f2.obj.visible = inCar;
      shed.visible = shed.visible && tau > 25.8; f1.obj.visible = f1.obj.visible && tau < 11.4; f3.obj.visible = f3.obj.visible && tau > 21.4;
    };
  },
};
