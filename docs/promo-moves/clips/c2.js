// 2. Build it, don't wait up — "Make ideas, not time."
// Late night: the folio from yesterday's review floats over the workstation. The agent
// applies the changes you asked for (hinge stops at flat, the pen is the pin, corner lips),
// you say print it and leave; it slices, prints, checks the first layer, and leaves you a
// voice message by morning.
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, agent, figure, bedside } from '../kit.js';

export default {
  title: 'Build it, don’t wait up', tagline: 'Make ideas,<br>not time.', body: 58,
  mood: { key: 'A', bpm: 88, minor: true, energy: .6 },
  assets: ['rook', 'agent', 'workstation', 'printer', 'folio', 'figure', 'nightstand', 'phone', 'chair', 'table'],
  async build(kit) {
    const S = kit.scene;
    kit.board({ size: 60 });
    S.fog = new THREE.Fog(COL.page, 9, 26);
    kit.hemi.intensity = 1.0; kit.key.intensity = 1.2;

    // ---------------------------------------------------------- the workshop corner
    const desk = kit.make('workstation', { at: V(0, 0, 0) });
    const scr = kit.meshesOf(desk, 'screen')[0];
    const cab = kit.make('nightstand', { at: V(2, 0, -.05), rot: -.3 });  // the printer's cabinet
    for (const n of ['lampbase', 'lampstem', 'lampshade']) { const o = kit.node(cab, n); if (o) o.visible = false; }
    const printer = kit.make('printer', { at: V(2, .645, -.05), rot: -.3 });
    const bed = kit.node(printer, 'bed'), head = kit.node(printer, 'head');
    const chairO = kit.make('chair', { at: V(0, 0, .72), rot: Math.PI });
    const lampL = new THREE.PointLight(0xffc27a, 0, 5, 1.4); lampL.position.set(-.6, 1.12, .0); S.add(lampL);
    const dawn = new THREE.DirectionalLight(0xffc996, 0); dawn.position.set(8, 3, 6); S.add(dawn);
    const hub = goldRook(kit, V(-7, 0, -8), .45, .6);
    const ag = agent(kit, 'claude code', COL.teal, V(1, 0, 2), { scale: .26 });

    // the folio, floating over the desk
    // (the review model is shown about 3x life size so the changes read; the printed one is real size)
    const holder = new THREE.Group(); holder.position.set(0, 1.58, .1); S.add(holder);
    const folio = kit.make('folio', { parent: holder, scale: 4 });
    folio.rotation.x = .85;
    const fr = folio.children[0];                                   // the model's own root (its units)
    const front = kit.node(folio, 'front'), pen = kit.node(folio, 'pen'), lips = kit.node(folio, 'lips'), pad = kit.node(folio, 'notepad');
    // the latch: loops on the opening edge of both covers; the pen slides through them and locks it shut
    // (it replaces the red snap clasp). Model units: covers 0.9 x 1.3, the opening edge at x = 1.0.
    function latch(fr, frontNode, mat) {
      const loops = [];
      const ring = () => { const m = new THREE.Mesh(new THREE.TorusGeometry(.045, .013, 8, 20), mat); m.add(new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry, 25), kit.inkMat)); return m; };
      for (const z of [-.5, 0, .5]) { const m = ring(); m.position.set(1.0, .1, z); fr.add(m); loops.push(m); }
      for (const z of [-.25, .25]) { const m = ring(); m.position.set(-1.0, .055, z); frontNode.add(m); loops.push(m); }
      return loops;
    }
    const loops = latch(fr, front, kit.toon(COL.goldHi));
    pen.position.set(1.0, .1, 0);
    // the front cover pivots higher as it closes, so it lands on the notepad instead of through it
    const frontLift = ang => .025 + .13 * (1 - Math.cos(ang)) / 2;
    // the old design: a snap latch and an elastic band, which fade out in the rebuild
    const oldMat = kit.toon(0x9a4a3a);
    const elastic = new THREE.Mesh(new THREE.BoxGeometry(.05, .02, 1.42), oldMat); elastic.position.set(.75, -.045, 0); fr.add(elastic);
    const snap = new THREE.Mesh(new THREE.BoxGeometry(.12, .08, .16), oldMat); snap.position.set(.97, .03, 0); fr.add(snap);
    for (const m of [elastic, snap]) m.add(new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry), kit.inkMat));
    const stop = new THREE.Mesh(new THREE.BoxGeometry(.1, .06, 1.2), kit.toon(COL.goldHi)); stop.position.set(0, .07, 0); fr.add(stop);
    stop.add(new THREE.LineSegments(new THREE.EdgesGeometry(stop.geometry), kit.inkMat));
    const ghostMat = new THREE.MeshBasicMaterial({ color: 0x79d2c8, transparent: true, opacity: .0, depthWrite: false, blending: THREE.AdditiveBlending });

    // the printed parts on the bed: two covers rising layer by layer
    const prints = [];
    for (const x of [-.14, .14]) {
      const m = new THREE.Mesh(new THREE.BoxGeometry(.24, 1, .35), kit.toon(0xd8ad6d));
      m.add(new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry), kit.inkMat));
      m.position.set(x, .01, 0); bed.add(m); prints.push(m);
    }
    const chamber = new THREE.PointLight(0xfff0d0, 0, 1.6, 1.5); chamber.position.set(0, .9, 0); printer.add(chamber);
    const done = kit.make('folio', { parent: bed, scale: 1 / .45 }); done.rotation.y = .3;
    done.position.set(-.133 * Math.cos(.3), .05, .133 * Math.sin(.3));     // centred on the plate (the model's origin is its spine)
    { const df = kit.node(done, 'front'); df.rotation.z = -Math.PI; df.position.y = .155;
      latch(done.children[0], df, kit.toon(COL.goldHi)); kit.node(done, 'pen').position.set(1.0, .1, 0);
      kit.node(done, 'lips').visible = true; }
    done.visible = false;
    // the person
    const fig = figure(kit, V(0, 0, .72), Math.PI, 1, { f: true });
    // the nightstand and phone, across the board, for the morning
    // you went out for drinks: the voice message lands on a pub table, two pints, a warm pendant
    const pub = kit.make('table', { at: V(-3.6, 0, 3.4) }); pub.scale.set(.28, 1.4, .28);          // a 0.7 m bar table, 1.1 m tall
    const glassMat = new THREE.MeshToonMaterial({ color: 0xcfe0d8, gradientMap: kit.ramp, transparent: true, opacity: .35, depthWrite: false });
    for (const [dx, dz, full] of [[.12, -.1, .8], [-.14, .05, .35]]) {
      const gl = new THREE.Mesh(new THREE.CylinderGeometry(.045, .036, .15, 20, 1, true), glassMat); gl.position.set(-3.6 + dx, 1.106 + .075, 3.4 + dz); S.add(gl);
      const beer = new THREE.Mesh(new THREE.CylinderGeometry(.042, .035, .15 * full, 20), kit.toon(0xc98a2e)); beer.position.set(-3.6 + dx, 1.106 + .075 * full, 3.4 + dz); S.add(beer);
      const foam = new THREE.Mesh(new THREE.CylinderGeometry(.043, .042, .015, 20), kit.toon(0xf0e6cc)); foam.position.set(-3.6 + dx, 1.106 + .15 * full + .007, 3.4 + dz); S.add(foam);
    }
    for (const [dx, dz] of [[.55, .3], [-.5, -.4]]) {                                                // two bar stools
      const seat = new THREE.Mesh(new THREE.CylinderGeometry(.18, .18, .05, 20), kit.toon(0x5e4a2e)); seat.position.set(-3.6 + dx, .76, 3.4 + dz); S.add(seat);
      const leg = new THREE.Mesh(new THREE.CylinderGeometry(.03, .03, .74, 10), kit.toon(0x3a3c32)); leg.position.set(-3.6 + dx, .37, 3.4 + dz); S.add(leg);
      for (const m of [seat, leg]) m.add(new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry, 25), kit.inkMat));
    }
    const pendant = new THREE.PointLight(0xffb870, 0, 4, 1.6); pendant.position.set(-3.6, 2.2, 3.4); S.add(pendant);
    const shade = new THREE.Mesh(new THREE.ConeGeometry(.16, .14, 20, 1, true), kit.toon(0x3a3c32)); shade.position.set(-3.6, 2.25, 3.4); S.add(shade);
    const bulb = new THREE.Mesh(new THREE.SphereGeometry(.04, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffd9a0 })); bulb.position.set(-3.6, 2.2, 3.4); S.add(bulb);
    const phone = kit.make('phone', { at: V(-3.55, 1.106, 3.45), rot: .6 });
    const pscr = kit.meshesOf(phone, 'screen')[0]; pscr.material = new THREE.MeshBasicMaterial({ color: 0x1a2520 });

    // ---------------------------------------------------------- overlays
    kit.caption('11:48 PM', .3, 6, 'stamp');
    kit.caption('1:41 AM', 45.8, 52.6, 'stamp');
    kit.caption('Hinge stops at flat · Pen is the pin · Corner lips', 21.5, 28, 'label');
    kit.caption('Sliced · First layer checked', 41.8, 45.6, 'label');
    kit.say(7.0, 'you_f', 'Limit the hinge to flat. Use the pen as the latching pin. Add a lip in each corner for the notepad.', 'a');
    const yEnd = kit.say(28.6, 'you_f', 'I dig it. Print it. Send me a voice message when it’s done. I’m meeting Tom for drinks.', 'b');
    kit.say(Math.max(34.2, yEnd + .3), 'rook', 'Sure thing, have fun. I’ll keep an eye on the print.', 'c');
    kit.say(47.6, 'rook', 'Folio’s done and on the bed. Hinge stops at flat, and the pen locks it shut.', 'd');
    const review = pane(kit, { title: '<b>review</b> · folio v6 · yesterday 6:12 PM', w: 470, body: `
      <div class="dim">hinge</div><div class="sl"><i></i></div><div class="ang hi">0°</div>` });
    const changes = pane(kit, { title: '<b>claude code</b> → folio v7', w: 520, body: `
      <div class="c1">› hinge: stop at 180°</div><div class="c2">› pen: latching pin through the loops</div>
      <div class="c3">› lips: 2 mm L in each corner</div><div class="c4 dim">› snap latch, elastic: removed</div>` });
    const slicer = pane(kit, { title: '<b>slicer</b> · folio_v7.3mf → X1C', w: 470, body: `
      <div>1 h 52 m · <span class="hi">79 g PLA</span></div><div class="dim">0.2 mm layers · 0.4 nozzle</div><div class="lay">layer 1 / 61</div>` });
    const camp = pane(kit, { title: '<b>camera</b> · first layer', w: 300, body: `<div class="camimg"></div><div class="ok">✓ adhesion good</div>` });
    const note = pane(kit, { title: '<b>rook</b> · voice message', w: 360, body: `<div>▶ 0:06 · folio done</div>` });
    const st = document.createElement('style'); st.textContent = `
      .sl{height:10px;border-radius:5px;background:#20261d;margin:10px 0;position:relative}
      .sl i{position:absolute;top:-6px;width:22px;height:22px;border-radius:50%;background:#f0c98c;left:0}
      .camimg{height:120px;border-radius:6px;margin-bottom:8px;background:repeating-linear-gradient(90deg,#3a3524 0 6px,#d8ad6d 6px 9px);opacity:.85}
      .c1,.c2,.c3,.c4{opacity:0}`;
    document.head.appendChild(st);
    const knob = review.el.querySelector('.sl i'), angEl = review.el.querySelector('.ang'), lay = slicer.el.querySelector('.lay');
    const cl = [1, 2, 3, 4].map(i => changes.el.querySelector('.c' + i));

    // pulses: hub -> workstation (the agent works), workstation -> printer (print)
    kit.pulse(V(-7, 0, -8), V(0, 0, 1), 14.6, 1.6);
    kit.pulse(V(0, 0, 1), V(2, 0, 0), 37.0, 1.3);
    kit.pulse(V(2, 0, 0), V(-4, 0, 3), 45.6, 1.6, { zFirst: true });
    kit.events.push({ t: 16.2, type: 'shimmer' }, { t: 39.5, type: 'print', dur: 6 }, { t: 47.2, type: 'buzz' }, { t: 33.2, type: 'tap' },
      { t: 6, type: 'whoosh' }, { t: 45.6, type: 'whoosh' });

    const desk3 = [V(-.8, 0, -.4), V(.8, 1.3, .9)], fol = [V(-.45, 1.3, -.2), V(.45, 1.85, .45)], prn = [V(1.75, .6, -.3), V(2.25, 1.2, .2)];
    const night = [V(-4.0, .5, 3.0), V(-3.2, 1.4, 3.8)], wide = [V(-1, 0, -.6), V(2.4, 1.7, 1.4)];
    kit.camPath([
      { t: 0, frame: { on: wide, yaw: 34, pitch: 22, fill: .8 } },
      { t: 6, frame: { on: wide, yaw: 28, pitch: 20, fill: .9 }, e: 'lin' },
      { t: 8.5, frame: { on: fol, yaw: 32, pitch: 14, fill: .6 }, e: 'io' },
      { t: 16, frame: { on: fol, yaw: 26, pitch: 14, fill: .65 }, e: 'lin' },
      { t: 19, frame: { on: fol, yaw: 55, pitch: 16, fill: .6 } },
      { t: 23, frame: { on: fol, yaw: 20, pitch: 30, fill: .62 } },
      { t: 27.6, frame: { on: fol, yaw: -45, pitch: 16, fill: .6 } },
      { t: 28.2, frame: { on: [V(-.4, 0, -.2), V(.4, 1.7, 1.3)], yaw: -28, pitch: 8, fill: .8 }, e: 'cut' },
      { t: 36.5, frame: { on: [V(-.4, 0, -.2), V(1.4, 1.7, 1.6)], yaw: -18, pitch: 10, fill: .85 }, e: 'lin' },
      { t: 38.5, frame: { on: prn, yaw: 22, pitch: 16, fill: .6 }, e: 'io' },
      { t: 45.2, frame: { on: prn, yaw: 14, pitch: 12, fill: .72 }, e: 'lin' },
      { t: 46.6, frame: { on: night, yaw: 35, pitch: 24, fill: .7 }, e: 'io' },
      { t: 52.6, frame: { on: night, yaw: 28, pitch: 22, fill: .8 }, e: 'lin' },
      { t: 53.3, frame: { on: [V(1.85, .76, -.2), V(2.15, .9, .1)], yaw: 8, pitch: 26, fill: .7 }, e: 'cut' },
      { t: 58, frame: { on: [V(-.8, 0, -.5), V(2.3, 1.3, .6)], yaw: 20, pitch: 20, fill: .85 }, e: 'out' },
    ]);

    return tau => {
      // ---- the folio: hinge sweep (old design) then rebuild
      let ang;                                                         // pi = closed, 0 = open flat, < 0 = past flat
      if (tau < 16) {
        const sweep = .5 - .5 * Math.cos((tau - 6) / 10 * Math.PI * 3);
        ang = tau < 6 ? Math.PI / 2 : lerp(Math.PI * .92, -.7, sweep);   // the old hinge swings past flat
      } else if (tau < 19.5) {
        ang = lerp(-.7, 0, ease.outE(inv(16, 17.6, tau))) + .05 * Math.sin(inv(16.8, 18, tau) * Math.PI * 3) * (1 - inv(16.8, 19, tau));
      } else ang = tau < 29 ? 0 : lerp(0, Math.PI, ease.io(inv(30.5, 32.5, tau)));
      front.rotation.z = -ang; front.position.y = frontLift(Math.max(0, ang));
      const repl = tau >= 6 && tau < 16;
      const open = Math.PI - ang;
      knob.style.left = (clamp(open / 3.84) * 400) + 'px';
      angEl.textContent = Math.round(open * 180 / Math.PI) + '°';
      stop.scale.set(1, Math.max(.001, ease.out(inv(16, 17, tau))), 1); stop.visible = tau >= 16;
      pen.position.z = lerp(1.5, 0, ease.io(inv(19.5, 21.5, tau))); pen.visible = tau > 19.4;
      lips.scale.y = Math.max(.001, ease.out(inv(21.5, 23, tau))); lips.visible = tau >= 21.5;
      const lp = ease.out(inv(17.2, 18.2, tau)); loops.forEach(m => { m.scale.setScalar(Math.max(.001, lp)); m.visible = lp > .01; });
      pad.position.y = lerp(.9, 0, ease.in(inv(23, 24.2, tau))) + .05; pad.visible = tau > 22.9;
      const gone = 1 - inv(16.8, 17.4, tau);                   // the red snap clasp and elastic go as the latch loops arrive
      elastic.scale.setScalar(Math.max(.001, gone)); snap.scale.setScalar(Math.max(.001, gone));
      elastic.visible = snap.visible = gone > .01;
      const sw = t => .3 * Math.sin(t * .3);
      holder.rotation.y = tau < 28 ? sw(tau) : lerp(sw(28), .9, ease.io(inv(29, 33, tau)));
      holder.position.y = 1.58 + Math.sin(tau * .9) * .015;
      holder.visible = tau < 37.5;
      // ---- the person: works, turns the model, nods, walks off
      fig.rest();
      if (tau < 33.5) {
        fig.obj.position.set(0, -.47, .72); fig.obj.rotation.y = Math.PI;       // seated: hips on the chair seat
        fig.sit();
        if (tau > 28.8 && tau < 33) { fig.armR.rotation.x = -1.6 + Math.sin(tau * 2) * .1; fig.foreR.rotation.x = -.4; }
        fig.head.rotation.x = tau > 32.4 && tau < 33.2 ? Math.sin((tau - 32.4) * 12) * .18 : -.15;
      } else {
        const w = tau - 33.5;
        fig.obj.rotation.y = lerp(Math.PI, Math.PI / 2, ease.out(clamp(w / .6)));
        fig.obj.position.set(Math.max(0, w - .3) * 1.1, lerp(-.47, 0, ease.out(clamp(w / .4))), .72 + .5 * ease.out(clamp(w / .6)) + Math.max(0, w - .3) * .3);
        fig.walk(w * 7);
      }
      fig.obj.visible = tau < 38;
      // ---- light: the lamp goes out at 33.4; the pub from 45.6
      lampL.intensity = tau < 33.4 ? 2.2 : 2.2 * (1 - inv(33.4, 33.6, tau));
      dawn.intensity = 0; pendant.intensity = 2.4 * inv(45.6, 46.4, tau);
      kit.hemi.intensity = tau < 45.6 ? (tau < 33.5 ? 1.0 : .7) : lerp(.7, .95, inv(45.6, 48, tau));
      scr.material.color.setHex(tau < 33.4 ? 0x2d4a40 : 0x1a2520);
      // ---- the agent glows while it works
      ag.mat.emissiveIntensity = .28 + .5 * fade(tau, 15.8, 16.4, 27, 28) + .4 * fade(tau, 38, 38.4, 45, 45.6);
      // ---- printing (time-lapse 39.5-45.5)
      const pr = inv(39.6, 45.4, tau);
      chamber.intensity = tau > 38 ? 1.6 : 0;
      done.visible = tau > 53;
      prints.forEach(m => { if (tau > 53) { m.visible = false; return; } m.scale.y = Math.max(.0001, .035 * pr); m.position.y = .01 + .035 * pr / 2; m.visible = pr > 0; });
      bed.position.y = lerp(.4, .4, pr);
      head.position.x = pr > 0 && pr < 1 ? Math.sin(tau * 22) * .3 : 0;
      head.position.z = pr > 0 && pr < 1 ? Math.sin(tau * 7.3) * .2 : 0;
      lay.textContent = `layer ${Math.max(1, Math.round(pr * 61))} / 61`;
      // ---- phone buzz
      const bz = tau > 47.2 && tau < 48.2 ? Math.sin(tau * 120) * .003 : 0;
      phone.position.x = -3.55 + bz;
      pscr.material.color.setHex(tau > 47.2 && tau < 53 ? 0x6a8a78 : 0x1a2520);
      // ---- squares
      kit.glowSquare(0, 1, .5 * fade(tau, 16.4, 17, 27, 28));
      kit.glowSquare(2, 0, .6 * fade(tau, 38.3, 38.8, 52, 53));
      kit.glowSquare(-4, 3, .6 * fade(tau, 47.2, 47.6, 52.4, 53));
      // ---- panes
      review.fixed(90, 150, repl ? fade(tau, 6.4, 6.8, 15.4, 15.9) : 0);
      changes.fixed(1310, 150, fade(tau, 16.4, 16.8, 27.4, 27.9));
      cl.forEach((el, i) => { el.style.opacity = inv([16.6, 19.6, 21.6, 17.0][i], [16.9, 19.9, 21.9, 17.3][i], tau); });
      slicer.place(V(2, 1.25, -.05), fade(tau, 38.5, 38.9, 45.2, 45.6), { ax: 1.08, ay: .2 });
      camp.place(V(2, 1.25, -.05), fade(tau, 41.4, 41.7, 45.2, 45.6), { ax: -.1, ay: .2 });
      note.place(V(-3.55, 1.35, 3.45), fade(tau, 47.4, 47.8, 52.4, 52.8), { ay: 1, dy: -20 });
      ag.chip.place(ag.top(), fade(tau, 14.8, 15.2, 46, 46.4));
    };
  },
};
