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
  assets: ['rook', 'agent', 'table', 'chair', 'figure', 'tablet', 'coffee', 'car'],
  async build(kit) {
    const S = kit.scene;
    kit.board({ size: 70 });
    S.fog = new THREE.Fog(COL.page, 12, 30);
    goldRook(kit, V(0, 0, -7), .55, .3);
    // kitchen table corner
    const K = V(-7, 0, 0);
    kit.make('table', { at: K, scale: .8 });
    kit.make('chair', { at: K.clone().add(V(0, 0, 1.25)), rot: Math.PI });
    const f1 = figure(kit, K.clone().add(V(0, -.5, 1.2)), Math.PI);
    f1.legL.rotation.x = f1.legR.rotation.x = -1.45; f1.armL.rotation.x = f1.armR.rotation.x = -1.0;
    kit.make('tablet', { at: K.clone().add(V(.1, .64, .35)), rot: Math.PI, scale: .7 });
    kit.make('coffee', { at: K.clone().add(V(-.45, .64, .3)) });
    // the car at a red light
    const C = V(0, 0, 0);
    const car = kit.make('car', { at: C, rot: Math.PI / 2, scale: 1.7 });
    const f2 = figure(kit, C.clone().add(V(-.35, -.2, -.05)), -Math.PI / 2, .82);
    f2.legL.rotation.x = f2.legR.rotation.x = -1.45; f2.armL.rotation.x = f2.armR.rotation.x = -1.2;
    const pole = new THREE.Mesh(new THREE.BoxGeometry(.08, 2.6, .08), kit.toon(0x3a3c32)); pole.position.set(1.7, 1.3, -1.6); S.add(pole);
    const head = new THREE.Mesh(new THREE.BoxGeometry(.3, .8, .26), kit.toon(0x24261f)); head.position.set(1.7, 2.6, -1.6); S.add(head);
    const red = new THREE.Mesh(new THREE.CircleGeometry(.09, 16), new THREE.MeshBasicMaterial({ color: 0xff5a48 })); red.position.set(1.7, 2.84, -1.46); S.add(red);
    for (const o of [pole, head]) o.add(new THREE.LineSegments(new THREE.EdgesGeometry(o.geometry), kit.inkMat));
    // the backyard and a shed in wireframe (1 unit = 4 ft: 10 x 12 ft -> 2.5 x 3)
    const B = V(7.5, 0, 0);
    const f3 = figure(kit, B.clone().add(V(-1.9, 0, 1.9)), -2.3);
    const tape = new THREE.Mesh(new THREE.BoxGeometry(.12, .12, .06), kit.toon(0xe8c840)); f3.foreR.add(tape); tape.position.set(0, -.34, 0);
    const W_ = 2.5, D_ = 3.0, H_ = 1.9, R_ = .7;
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
    const dl = W_ / 2 - .4, dr = W_ / 2 + .4;
    seg(c(dl, 0, D_), c(dl, 1.6, D_)); seg(c(dr, 0, D_), c(dr, 1.6, D_)); seg(c(dl, 1.6, D_), c(dr, 1.6, D_));
    const shedG = new THREE.BufferGeometry().setFromPoints(E);
    const shed = new THREE.LineSegments(shedG, new THREE.LineBasicMaterial({ color: COL.goldHi }));
    S.add(shed);
    const nSeg = E.length / 2;
    const dims = [['10 ft', c(W_ / 2, 0, D_ + .35)], ['12 ft', c(W_ + .35, 0, D_ / 2)], ['6:12 pitch', c(W_ / 2, H_ + R_ + .3, 0)], ['door · 3 ft', c(W_ / 2, 1.85, D_ + .1)]]
      .map(([t, p], i) => ({ el: kit.el(t, 'dim' + (i === 3 ? ' door' : '')), p, i }));
    // notes riding out of the car speaker
    const notes = [];
    for (let i = 0; i < 16; i++) notes.push({ el: kit.el(i % 3 ? '♪' : '♫', 'note'), i });

    // ---------------------------------------------------------- overlays
    kit.say(.3, 'you', 'Make me a comic about my cat stealing socks.', 'a');
    kit.caption('Comic, rough draft. Mittens on the counter, as usual.', 7.4, 10.8, 'label');
    kit.say(11.6, 'you', 'Hum me a birthday tune for my sister. Celtic vibe, but piano.', 'b');
    kit.caption('Celtic piano, 8 bars', 17.4, 21.6, 'label');
    kit.say(21.9, 'you', 'I’ve got ten by twelve feet back here. The door has to be on the short side, though.', 'c');
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
    kit.pulse(V(0, 0, -7), V(8, 0, 2), 24.4, 1.4);

    kit.camPath([
      { t: 0, pos: [-7 + 5, 6.5, 9], look: [-7, .8, 0], fov: 38 },
      { t: 2.6, pos: [-7 + 2.4, 2.6, 4.4], look: [-7, 1.6, 0], e: 'outE' },
      { t: 10.9, pos: [-7 + 2.1, 2.7, 4.6], look: [-7, 1.9, 0], e: 'lin' },
      { t: 11.6, pos: [2.8, 2.1, 5.2], look: [0, 1.4, 0], e: 'io' },
      { t: 21.3, pos: [3.4, 2.4, 5.6], look: [0, 1.8, 0], e: 'lin' },
      { t: 22.0, pos: [7.5 + 1.0, 2.3, 6.8], look: [7.5 - 1, 1.2, 1.4], e: 'io' },
      { t: 25.6, pos: [7.5 + .6, 2.3, 6.6], look: [7.5 - 1, 1.2, 1.2], e: 'lin' },
      { t: 28.5, pos: [7.5 + 6.2, 4.2, 3.2], look: [7.5, 1.2, 0], e: 'io' },
      { t: 32.8, pos: [7.5 + 2.6, 4.6, -5.2], look: [7.5, 1.2, 0], e: 'lin' },
      { t: 34.4, pos: [0, 9.5, 13], look: [0, 2.4, 0], e: 'io' },
      { t: 38, pos: [0, 10.5, 14.5], look: [0, 2.4, 0], e: 'lin' },
    ]);

    return tau => {
      // kitchen: comic panels flip up one at a time
      const fin = tau > 33.2;
      panels.forEach((p, i) => {
        const a = fade(tau, 3.2 + i * 1.6, 3.6 + i * 1.6, 10.8, 11.2);
        if (!fin) p.fixed(150 + (i % 2) * 330, 130 + Math.floor(i / 2) * 290, a, 1);
        else p.fixed(110 + (i % 2) * 250, 120 + Math.floor(i / 2) * 220, fade(tau, 33.6 + i * .1, 34 + i * .1, 37.6, 38), .74);
      });
      f1.head.rotation.x = .25; f1.foreR.rotation.x = tau > .3 && tau < 3 ? -.6 + Math.sin(tau * 5) * .2 : 0;
      kit.glowSquare(-7, 1, .6 * fade(tau, 2.4, 2.8, 10.6, 11));
      // the car: notes ride out of the speaker
      notes.forEach(({ el, i }) => {
        const t0 = 14.6 + i * .42, u = (tau - t0) / 3.4;
        const a = u > 0 && u < 1 ? Math.sin(u * Math.PI) : 0;
        kit.show(el, tau < 22 ? a : 0);
        if (a > .003) kit.pin(el, V(.6 + u * 3.2, 1.2 + u * 2.2 + Math.sin(u * 7 + i) * .3, .6 + Math.sin(i) * .4), { ay: .5, depth: true });
      });
      song.fixed(1320, 150, fade(tau, 15.4, 15.8, 21, 21.4) + fade(tau, 33.8, 34.2, 37.6, 38) * (tau > 33 ? 1 : 0), tau > 33 ? .85 : 1);
      if (tau > 33) song.fixed(735, 200, fade(tau, 33.8, 34.2, 37.6, 38), .85);
      heads.forEach((h, i) => { h.style.opacity = tau > 33 ? 1 : inv(15 + i * .45, 15.1 + i * .45, tau); });
      kit.glowSquare(0, 0, .6 * fade(tau, 12.8, 13.2, 21, 21.4));
      f2.head.rotation.x = .1;
      // the backyard: tape measure, then the shed draws itself
      f3.rest(); f3.armR.rotation.x = tau > 21.6 && tau < 26 ? -1.3 : -.1; f3.obj.rotation.y = -2.3;
      const b = ease.io(inv(25.8, 31.6, tau));
      shedG.setDrawRange(0, Math.floor(b * nSeg) * 2);
      shed.visible = tau > 25.8;
      dims.forEach(d => {
        const a = fade(tau, 29 + d.i * .4, 29.3 + d.i * .4, 33, 33.3);
        kit.show(d.el, a); if (a > .003) kit.pin(d.el, d.p, { ay: .5 });
      });
      shedP.fixed(1360, 200, fade(tau, 33.8, 34.2, 37.6, 38), .9);
      for (let x = 6; x <= 9; x++) for (let z = -1; z <= 1; z++) if (x === 6 || x === 9 || z === -1 || z === 1) kit.glowSquare(x, z, .45 * fade(tau, 24.6, 25.2, 32.6, 33), 1);
    };
  },
};
