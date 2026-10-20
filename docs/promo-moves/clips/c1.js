// 1. Secretary — "The best secretary you've never had."
// The Board is a phone's screen on a nightstand; a hologram rook briefs you as the
// background swaps from bedroom to bathroom, car and desk; you ask it to move a
// meeting, we dive into the Board, a pulse moves the 2 PM card and emails the room.
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, holoRook, figure } from '../kit.js';

export default {
  title: 'Secretary', tagline: 'The best secretary<br>you’ve never had.', body: 41,
  mood: { key: 'D', bpm: 96, feel: 'morning' },
  assets: ['rook', 'phone', 'calendar', 'mail', 'car', 'laptop', 'nas', 'nightstand', 'bed', 'counter', 'dash', 'workstation', 'chair', 'figure', 'coffee', 'agent'],
  async build(kit) {
    const S = kit.scene;
    S.fog = new THREE.Fog(COL.page, 6, 16);

    // ---------------------------------------------------------- the Board world (inside the phone)
    const bw = new THREE.Scene();
    bw.fog = new THREE.Fog(COL.page, 9, 22);
    bw.add(new THREE.HemisphereLight(0xe3d8ad, 0x2b3323, 1.6));
    const bk = new THREE.DirectionalLight(0xffedc4, 2.4); bk.position.set(-6, 12, 7); bw.add(bk);
    const bcam = new THREE.PerspectiveCamera(40, .47, .05, 200);
    const dcam = new THREE.PerspectiveCamera(36, 16 / 9, .05, 200);
    let boardView = bcam;
    kit.board({ scene: bw, cam: () => boardView });
    const rook = goldRook(kit, V(0, 0, 0), .5, -.4); bw.add(rook.obj);
    const cal = kit.make('calendar', { at: V(3, 0, -2), parent: bw, scale: 1.2, rot: -.3 });
    const mail = kit.make('mail', { at: V(-3, 0, -2), parent: bw, scale: 1.2, rot: .3 });
    kit.make('car', { at: V(-3, 0, 3), parent: bw, scale: .8, rot: .5 });
    kit.make('laptop', { at: V(3, 0, 3), parent: bw, rot: -.5 });
    kit.make('phone', { at: V(0, 0, 4), parent: bw, scale: 1.4 });
    kit.make('nas', { at: V(-5, 0, 0), parent: bw });

    const rt = new THREE.WebGLRenderTarget(1024, 2176, { samples: 4 });
    rt.texture.colorSpace = THREE.SRGBColorSpace;

    // ---------------------------------------------------------- the phone and its hologram (stay put)
    const phone = kit.make('phone', { at: V(0, 0, 0), rot: .25 });
    const scr = kit.meshesOf(phone, 'screen')[0];
    scr.material = new THREE.MeshBasicMaterial({ map: rt.texture }); kit.planarUV(scr, { u: 'x', v: 'z', flipV: false });
    // flip uv so the board reads upright from the camera side
    const holo = holoRook(kit, .05); holo.obj.position.set(0, .05, 0);
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(.09, .06, .26, 24, 1, true),
      new THREE.MeshBasicMaterial({ color: 0xf0c98c, transparent: true, opacity: .07, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    beam.position.y = .14; S.add(beam);
    const warm = new THREE.PointLight(0xffc98a, .5, 2.5, 2); warm.position.set(0, .4, 0); S.add(warm);

    // ---------------------------------------------------------- the four rooms
    const rooms = {};
    const wall = (g, c, z = -1.6, h = 4) => { const m = new THREE.Mesh(new THREE.PlaneGeometry(9, h), kit.toon(c)); m.position.set(0, h / 2 - 1.2, z); g.add(m); return m; };
    { // bedroom at dawn
      const g = new THREE.Group(); S.add(g); rooms.bed = g;
      const ns = kit.make('nightstand', { parent: g, at: V(.12, -.645, -.05) });
      kit.make('bed', { parent: g, at: V(1.45, -.645, -.6), rot: Math.PI / 2 });
      wall(g, 0x2a2e26);
      const win = new THREE.Mesh(new THREE.PlaneGeometry(1.3, 1.1), new THREE.MeshBasicMaterial({ color: 0xffc98a }));
      win.position.set(-1.4, 1.0, -1.58); g.add(win); g.userData.win = win;
      for (const x of [-1.4]) { const bar = new THREE.Mesh(new THREE.BoxGeometry(.04, 1.1, .02), kit.toon(0x2a2e26)); bar.position.set(x, 1.0, -1.57); g.add(bar);
        const bar2 = new THREE.Mesh(new THREE.BoxGeometry(1.3, .04, .02), kit.toon(0x2a2e26)); bar2.position.set(x, 1.0, -1.57); g.add(bar2); }
    }
    { // bathroom
      const g = new THREE.Group(); S.add(g); rooms.bath = g;
      const c = kit.make('counter', { parent: g, at: V(-.2, -.9, -.15) });
      kit.node(c, 'mirror').visible = false; kit.node(c, 'mirror_frame').visible = false;
      wall(g, 0x36403a, -2.2);
      const f = figure(kit, V(-.6, -.9, -1.95), .2); g.add(f.obj); g.userData.fig = f;
      const brush = new THREE.Mesh(new THREE.BoxGeometry(.02, .02, .18), kit.toon(0xd8ad6d)); f.foreR.add(brush); brush.position.set(0, .02, -.36);
    }
    { // car at a red light
      const g = new THREE.Group(); S.add(g); rooms.car = g;
      const d = kit.make('dash', { parent: g, at: V(-.95, -1.025, .05), rot: Math.PI });
      kit.node(d, 'glass').visible = false; kit.node(d, 'hood').visible = false;
      const f = figure(kit, V(-.45, -1.45, -.95), 0); g.add(f.obj); g.userData.fig = f;
      f.legL.rotation.x = f.legR.rotation.x = -1.45;
      f.armL.rotation.x = f.armR.rotation.x = -1.15;
      const night = new THREE.Mesh(new THREE.PlaneGeometry(9, 5), kit.toon(0x161a20)); night.position.set(0, .6, -2.4); g.add(night);
      const lights = [];
      for (let i = 0; i < 9; i++) { const l = new THREE.Mesh(new THREE.CircleGeometry(.02 + rnd2(i) * .025, 12), new THREE.MeshBasicMaterial({ color: i % 3 ? 0xffd08a : 0xe0675c }));
        l.position.set(-3 + i * .75, .2 + rnd2(i + 9) * .7, -2.35); g.add(l); lights.push(l); }
      g.userData.lights = lights;
    }
    { // work desk
      const g = new THREE.Group(); S.add(g); rooms.desk = g;
      kit.make('workstation', { parent: g, at: V(-.55, -.77, -.25), rot: -Math.PI / 2 });
      kit.make('coffee', { parent: g, at: V(-.45, 0, -.35) });
      const ch = kit.make('chair', { parent: g, at: V(-1.25, -.77, -.4), rot: -Math.PI / 2 });
      const f = figure(kit, V(-.2, -1.27, -1.25), 0); g.add(f.obj); g.userData.fig = f;
      f.legL.rotation.x = f.legR.rotation.x = -1.45;
      f.armL.rotation.x = f.armR.rotation.x = -.9;
      kit.make('chair', { parent: g, at: V(-.2, -.77, -1.45), rot: Math.PI });
      wall(g, 0x2f3330, -1.9);
    }
    function rnd2(i) { return ((Math.sin(i * 91.7) * 43758.5453) % 1 + 1) % 1; }

    // ---------------------------------------------------------- overlays
    kit.caption('7:02 AM', .3, 5.5, 'stamp');
    kit.caption('Calendar updated · Attendees emailed', 33.6, 37.2, 'label');
    kit.say(6.2, 'rook', 'Morning. Two meetings today, the first at nine.', 's1');
    kit.say(12.0, 'rook', 'The plugin PR merged overnight.', 's2');
    kit.say(17.0, 'rook', 'The vendor replied about the quote.', 's3');
    kit.say(22.2, 'rook', "That's everything.", 's4');
    kit.say(26.2, 'you', 'Push the two o’clock to tomorrow.', 's5');
    kit.say(37.6, 'rook', 'Done. Moved to tomorrow, and they know.', 's6');
    const calPane = pane(kit, { title: '<b>calendar</b> · this week', w: 560, body: `
      <div class="calg"><div class="col"><div class="dh">TODAY</div></div><div class="col"><div class="dh">TOMORROW</div></div></div>
      <div class="ev e1">9:00 · Standup</div><div class="ev e2">2:00 PM · Vendor sync</div><div class="ev e3">10:30 · Design review</div>` });
    const mailPane = pane(kit, { title: '<b>mail</b> · to 4 attendees', w: 520, body: `
      <div class="dim">Subject:</div><div>Vendor sync moved to tomorrow, 2:00 PM</div><div class="ok sent">✓ sent</div>` });
    const st = document.createElement('style'); st.textContent = `
      .calg{display:flex;gap:16px;height:150px}.calg .col{flex:1;border:1px solid #2b322a;border-radius:8px;padding:8px}
      .calg .dh{font-size:16px;color:#92998c;letter-spacing:.2em}
      .ev{position:absolute;width:226px;font-size:18px;padding:6px 10px;border-radius:6px;background:#20261d;border-left:4px solid #8fc79a;color:#e2e3d9}
      .ev.e1{left:28px;top:118px}.ev.e2{left:28px;top:166px;border-left-color:#f0c98c}.ev.e3{left:296px;top:118px}`;
    document.head.appendChild(st);
    const e2 = calPane.el.querySelector('.e2'), sent = mailPane.el.querySelector('.sent');

    // pulses on the Board during the dive (clip time)
    const p1 = kit.pulse(V(0, 0, 0), V(3, 0, -2), 30.6, 1.3, { scene: bw });
    const p2 = kit.pulse(V(0, 0, 0), V(-3, 0, -2), 32.6, 1.3, { scene: bw, zFirst: true });

    const swaps = [11.0, 16.0, 21.0];                                // bedroom -> bath -> car -> desk
    const order = ['bed', 'bath', 'car', 'desk'];
    kit.events.push({ t: 11, type: 'whoosh' }, { t: 16, type: 'whoosh' }, { t: 21, type: 'whoosh' }, { t: 29.1, type: 'rush' }, { t: 37, type: 'rush' },
      { t: 5.2, type: 'shimmer' });

    // ---------------------------------------------------------- per frame
    const camLock = { pos: V(.75, .42, 1.05), look: V(-.12, .12, -.2) };
    return tau => {
      // which room, and the slide between rooms
      let idx = 0, slide = 0;
      swaps.forEach((s, i) => { if (tau >= s) idx = i + 1; });
      const lastSwap = idx > 0 ? swaps[idx - 1] : -9;
      const u = clamp((tau - lastSwap) / .45);
      const roomIdx = tau >= 37 ? 3 : (tau >= 29 && tau < 37 ? 3 : idx);
      order.forEach((k, i) => {
        const g = rooms[k];
        let x = 99;
        if (i === roomIdx) x = (idx > 0 && u < 1 && tau < 29) ? lerp(6, 0, ease.out(u)) : 0;
        else if (i === roomIdx - 1 && u < 1 && tau < 29) x = lerp(0, -6, ease.in(u));
        g.position.x = x; g.visible = Math.abs(x) < 50;
      });
      // dawn window warms up
      rooms.bed.userData.win.material.color.setHSL(.08, .6, lerp(.18, .42, inv(0, 9, tau)));
      // figures move
      const fb = rooms.bath.userData.fig; fb.rest(); fb.armR.rotation.x = -2.3; fb.armR.rotation.z = .35; fb.foreR.rotation.x = -.9 + Math.sin(tau * 14) * .18;
      fb.head.rotation.x = .1;
      rooms.car.userData.lights.forEach((l, i) => { l.position.x = ((-3 + i * .75 - tau * .25) % 7 + 7) % 7 - 3.5; });
      const fd = rooms.desk.userData.fig;
      fd.head.rotation.y = tau > 25.6 ? lerp(0, -.7, inv(25.6, 26.4, tau)) : 0;

      // hologram: rises at 5-6.5, turns toward the figure at 26-29
      const rise = ease.out(inv(5.0, 6.6, tau));
      holo.obj.scale.setScalar(Math.max(.0001, .05 * rise));
      holo.obj.position.y = lerp(0, .05, rise) + Math.sin(tau * 1.6) * .006;
      holo.obj.rotation.y = tau * .35 + (tau > 26 ? lerp(0, -.9, ease.io(inv(26, 27.4, tau))) : 0);
      holo.mat.opacity = .22 * rise; holo.ink.opacity = .9 * rise * (.85 + .15 * Math.sin(tau * 7));
      beam.material.opacity = .07 * rise; beam.scale.y = Math.max(.001, rise);
      warm.intensity = .25 + .5 * rise;

      // ---- cameras
      const cam = kit.camera;
      if (tau < 5) {                                   // start on the board (the screen, rolled to fill the frame), pull back
        const p = ease.io(inv(.8, 5, tau));
        const q = ease.inE(inv(.8, 5, tau)) * .6 + p * .4;
        const ph = phone.rotation.y;
        const start = V(0, .5, 0), end = camLock.pos;
        cam.position.copy(start.clone().lerp(end, q));
        cam.up.set(Math.cos(ph), 0, -Math.sin(ph)).lerp(V(0, 1, 0), clamp(q * 1.6)).normalize();
        cam.fov = lerp(36, 36, q);
        cam.lookAt(V(0, 0, 0).lerp(camLock.look, q));
        cam.up.set(0, 1, 0);
      } else if (tau < 29 || (tau >= 37)) {
        cam.position.copy(camLock.pos); cam.fov = 36; cam.lookAt(camLock.look);
        if (tau >= 37) {                                 // pulled back out of the hologram
          const p = ease.outE(inv(37, 38.4, tau));
          cam.position.copy(V(0, .16, .05).lerp(camLock.pos, p)); cam.lookAt(V(0, .13, 0).lerp(camLock.look, p));
        }
      } else if (tau < 30) {                           // rush into the hologram
        const p = ease.inE(inv(29, 30, tau));
        cam.position.copy(camLock.pos.clone().lerp(V(0, .16, .02), p)); cam.lookAt(camLock.look.clone().lerp(V(0, .13, 0), p));
        cam.fov = lerp(36, 70, p);
      }
      cam.updateProjectionMatrix();

      // board camera for the phone screen: a slow orbit, top-down
      const a = tau * .12;
      bcam.position.set(Math.sin(a) * 2.2, 9.5, Math.cos(a) * 2.2 + 1.0); bcam.up.set(0, 0, -1); bcam.lookAt(0, 0, .5);
      bcam.up.set(0, 1, 0);

      // the dive into the Board (29.9-37): the frame shows the board world directly
      const dive = tau >= 29.9 && tau < 37;
      if (dive) {
        const d = tau - 29.9;
        const k1 = ease.outE(clamp(d / 1.2));
        dcam.position.copy(V(0, 14, 3).lerp(V(.4, 4.2, 5.2), k1));
        let look = V(0, 0, 0).lerp(V(.2, .2, -.6), k1);
        if (tau > 33.6) { const k2 = ease.io(inv(33.6, 35, tau)); dcam.position.lerp(V(-.6, 3.6, 4.6), k2); }
        dcam.lookAt(look); dcam.fov = 36; dcam.updateProjectionMatrix();
        boardView = dcam;
        kit.renderPass.scene = bw; kit.renderPass.camera = dcam; kit.projCam = dcam;
        kit.flash = .5 * Math.exp(-d / .12);
      } else {
        boardView = bcam; kit.projCam = null;
        kit.boardU.uCam.value.copy(bcam.position);
        kit.renderer.setRenderTarget(rt);
        kit.renderer.render(bw, bcam);
        kit.renderer.setRenderTarget(null);
        if (window.DBG === 'bw') { kit.renderPass.scene = bw; bcam.aspect = 16 / 9; bcam.updateProjectionMatrix(); kit.renderPass.camera = bcam; }
        if (tau >= 37) kit.flash = .35 * Math.exp(-(tau - 37) / .12);
      }
      // glow on the pieces that got work
      if (tau > p1.end) kit.glowSquare(3, -2, .8 * (1 - inv(36, 37, tau)), 0);
      if (tau > p2.end) kit.glowSquare(-3, -2, .8 * (1 - inv(36, 37, tau)), 0);
      kit.glowSquare(0, 0, .35, 0);

      // panes in the dive
      calPane.fixed(1250, 330, dive ? fade(tau, 31.6, 32.0, 36.4, 36.9) : 0);
      mailPane.fixed(110, 330, dive ? fade(tau, 33.6, 34.0, 36.4, 36.9) : 0);
      const mv = ease.io(inv(32.1, 32.9, tau));
      e2.style.transform = `translate(${mv * 268}px, ${-mv * 48}px)`;
      sent.style.opacity = inv(34.6, 34.8, tau);
    };
  },
};
