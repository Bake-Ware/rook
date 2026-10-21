// 1. Secretary — "The best secretary you've never had."
// The Board is a phone's screen on a nightstand; a hologram rook briefs you as the
// background swaps from bedroom to bathroom, car and desk; you ask it to move a
// meeting, we dive into the Board, a pulse moves the 2 PM card and emails the room.
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, holoRook, figure, bedside } from '../kit.js';

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
    const cal = kit.piece('calendar', V(3, 0, -2), { parent: bw, rot: -.3, size: .8 });
    const mail = kit.piece('mail', V(-3, 0, -2), { parent: bw, rot: .3, size: .8 });
    kit.piece('car', V(-3, 0, 3), { parent: bw, rot: .5, size: .9 });
    kit.piece('laptop', V(3, 0, 3), { parent: bw, rot: -.5 });
    kit.piece('phone', V(0, 0, 4), { parent: bw, size: .6 });
    kit.piece('nas', V(-5, 0, 0), { parent: bw });

    const rt = new THREE.WebGLRenderTarget(1024, 2176, { samples: 4 });
    rt.texture.colorSpace = THREE.SRGBColorSpace;

    // ---------------------------------------------------------- the phone and its hologram (stay put)
    const phone = kit.make('phone', { at: V(0, 0, 0), rot: .25 });
    const scr = kit.meshesOf(phone, 'screen')[0];
    scr.material = new THREE.MeshBasicMaterial({ map: rt.texture }); kit.planarUV(scr, { u: 'x', v: 'z', flipV: false });
    // flip uv so the board reads upright from the camera side
    const holo = holoRook(kit, .033); holo.obj.position.set(0, .012, 0);
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(.06, .035, .16, 24, 1, true),
      new THREE.MeshBasicMaterial({ color: 0xf0c98c, transparent: true, opacity: .07, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide }));
    beam.position.y = .09; S.add(beam);
    const warm = new THREE.PointLight(0xffc98a, .04, .9, 2); warm.position.set(0, .25, 0); S.add(warm);

    // ---------------------------------------------------------- the four rooms
    const rooms = {};
    const wall = (g, c, z = -1.6, h = 4) => { const m = new THREE.Mesh(new THREE.PlaneGeometry(9, h), kit.toon(c)); m.position.set(0, h / 2 - 1.2, z); g.add(m); return m; };
    { // bedroom at dawn
      const g = new THREE.Group(); S.add(g); rooms.bed = g;
      bedside(kit, V(.12, -.645, -.05), { side: 1, parent: g });      // the bed to the left, headboard on the wall
      wall(g, 0x2a2e26, -.45);
      const win = new THREE.Mesh(new THREE.PlaneGeometry(1.3, 1.1), new THREE.MeshBasicMaterial({ color: 0xffc98a }));
      win.position.set(.9, 1.0, -.44); g.add(win); g.userData.win = win;
      for (const x of [.9]) { const bar = new THREE.Mesh(new THREE.BoxGeometry(.04, 1.1, .02), kit.toon(0x2a2e26)); bar.position.set(x, 1.0, -.43); g.add(bar);
        const bar2 = new THREE.Mesh(new THREE.BoxGeometry(1.3, .04, .02), kit.toon(0x2a2e26)); bar2.position.set(x, 1.0, -.43); g.add(bar2); }
    }
    { // bathroom: the phone on the vanity beside the basin (no one in shot)
      const g = new THREE.Group(); S.add(g); rooms.bath = g;
      kit.make('counter', { parent: g, at: V(-.2, -.86, -.05) });
      wall(g, 0x36403a, -.37);
    }
    { // car at a red light, from the driver's seat: the phone on the dash, the road ahead
      const g = new THREE.Group(); S.add(g); rooms.car = g;
      const d = kit.make('dash', { parent: g, at: V(-.02, -1.048, .14) });
      kit.node(d, 'glass').visible = false;
      const ground = -1.35;
      const road = new THREE.Mesh(new THREE.PlaneGeometry(14, 60), kit.toon(0x23251f)); road.rotation.x = -Math.PI / 2; road.position.set(0, ground, -30); g.add(road);
      for (let k = 0; k < 12; k++) { const m = new THREE.Mesh(new THREE.PlaneGeometry(.12, 1.6), new THREE.MeshBasicMaterial({ color: 0x8a8670 }));
        m.rotation.x = -Math.PI / 2; m.position.set(1.7, ground + .01, -3 - k * 4.5); g.add(m); }
      const sky = new THREE.Mesh(new THREE.PlaneGeometry(80, 30), kit.toon(0x151922)); sky.position.set(0, 6, -55); g.add(sky);
      const lights = [];
      for (let i = 0; i < 26; i++) { const l = new THREE.Mesh(new THREE.CircleGeometry(.12 + rnd2(i) * .2, 12), new THREE.MeshBasicMaterial({ color: i % 4 ? 0xffd08a : 0xe0675c }));
        l.position.set(-30 + i * 2.4 + rnd2(i + 3) * 1.5, ground + 1 + rnd2(i + 9) * 5, -54); g.add(l); lights.push(l); }
      g.userData.lights = lights;
      // the car ahead (tail lights) and the light over the junction
      const ahead = kit.make('car', { parent: g, at: V(-.3, ground, -9.5), rot: Math.PI / 2, tint: 0x5b4740 });
      for (const sx of [-.55, .55]) { const t = new THREE.Mesh(new THREE.PlaneGeometry(.22, .1), new THREE.MeshBasicMaterial({ color: 0xff4a3a }));
        t.position.set(-.3 + sx, ground + .72, -7.35); g.add(t); }
      const arm = new THREE.Mesh(new THREE.BoxGeometry(5, .12, .12), kit.toon(0x3a3c32)); arm.position.set(1.0, ground + 3.9, -12); g.add(arm);
      const head = new THREE.Mesh(new THREE.BoxGeometry(.36, .95, .3), kit.toon(0x24261f)); head.position.set(-.4, ground + 3.3, -12); g.add(head);
      ['#ff5a48', '#3a2a1a', '#1d3a26'].forEach((col, k) => { const l = new THREE.Mesh(new THREE.CircleGeometry(.11, 16), new THREE.MeshBasicMaterial({ color: col }));
        l.position.set(-.4, ground + 3.6 - k * .3, -11.84); g.add(l); });
      const red = new THREE.PointLight(0xff5a48, 1.5, 9, 1.6); red.position.set(-.4, ground + 3.3, -11); g.add(red);
    }
    { // work desk
      const g = new THREE.Group(); S.add(g); rooms.desk = g;
      const ws = kit.make('workstation', { parent: g, at: V(-.25, -.74, -.25) });
      for (const n of ['monitor', 'kb', 'foot', 'neck', 'tower', 'tower_led']) { const o = kit.node(ws, n); if (o) o.visible = false; }
      kit.make('laptop', { parent: g, at: V(-.3, 0, -.38), rot: Math.PI + .15 });
      kit.make('coffee', { parent: g, at: V(.22, 0, -.3) });
      kit.make('chair', { parent: g, at: V(-.3, -.74, -1.05) });
      const f = figure(kit, V(-.3, -1.21, -.92), 0); g.add(f.obj); g.userData.fig = f;
      f.legL.rotation.x = f.legR.rotation.x = -1.45;
      f.armL.rotation.x = f.armR.rotation.x = -.9; f.foreL.rotation.x = f.foreR.rotation.x = -.7;   // hands on the laptop
      wall(g, 0x2f3330, -1.4);
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
      .ev{position:absolute;width:236px;font-size:16px;white-space:nowrap;padding:6px 10px;border-radius:6px;background:#20261d;border-left:4px solid #8fc79a;color:#e2e3d9}
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
    const camLock = kit.frame([V(0, 0, 0)], { yaw: 36, pitch: 10, dist: 1.0, look: [-.14, .2, -.12] });
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
      rooms.car.userData.lights.forEach((l, i) => { l.material.opacity = 1; l.scale.setScalar(.85 + .15 * Math.sin(tau * 3 + i)); });
      const fd = rooms.desk.userData.fig;
      fd.head.rotation.y = tau > 25.6 ? lerp(0, -.7, inv(25.6, 26.4, tau)) : 0;

      // hologram: rises at 5-6.5, turns toward the figure at 26-29
      const rise = ease.out(inv(5.0, 6.6, tau));
      holo.obj.scale.setScalar(Math.max(.0001, .033 * rise));
      holo.obj.position.y = lerp(0, .02, rise) + Math.sin(tau * 1.6) * .003;
      holo.obj.rotation.y = tau * .35 + (tau > 26 ? lerp(0, -.9, ease.io(inv(26, 27.4, tau))) : 0);
      holo.mat.opacity = .22 * rise; holo.ink.opacity = .9 * rise * (.85 + .15 * Math.sin(tau * 7));
      beam.material.opacity = .07 * rise; beam.scale.y = Math.max(.001, rise);
      warm.intensity = .02 + .05 * rise;

      // ---- cameras
      const cam = kit.camera;
      if (tau < 5) {                                   // start on the board (the screen, rolled to fill the frame), pull back
        const p = ease.io(inv(.8, 5, tau));
        const q = ease.inE(inv(.8, 5, tau)) * .6 + p * .4;
        const ph = phone.rotation.y;
        const start = V(0, .17, 0), end = camLock.pos;
        cam.position.copy(start.clone().lerp(end, q));
        cam.up.set(Math.cos(ph), 0, -Math.sin(ph)).lerp(V(0, 1, 0), clamp(q * 1.6)).normalize();
        cam.fov = lerp(36, 36, q);
        cam.lookAt(V(0, 0, 0).lerp(camLock.look, q));
        cam.up.set(0, 1, 0);
      } else if (tau < 29 || (tau >= 37)) {
        // per-room framing: wider and a little higher where a person stands or sits behind the phone
        const adj = [[0, 0], [6, .05], [0, 0], [12, .1]];
        const a = adj[roomIdx], b = adj[Math.max(0, roomIdx - 1)], w = (idx > 0 && tau < 29) ? ease.io(u) : 1;
        const fovA = lerp(b[0], a[0], w), lookA = lerp(b[1], a[1], w);
        cam.position.copy(camLock.pos); cam.fov = 36 + fovA; cam.lookAt(camLock.look.clone().add(V(0, lookA, 0)));
        if (roomIdx === 2 && tau < 29) {                 // in the car: the driver's eyes, the phone on the dash, the road
          cam.position.set(-.36, .26, .95); cam.fov = 52; cam.lookAt(-.05, .02, -1.4);
        }
        if (tau >= 37) {                                 // pulled back out of the hologram
          const p = ease.outE(inv(37, 38.4, tau));
          cam.position.copy(V(0, .085, .03).lerp(camLock.pos, p)); cam.lookAt(V(0, .075, 0).lerp(camLock.look.clone().add(V(0, lookA, 0)), p)); cam.fov = lerp(70, 36 + fovA, p);
        }
      } else if (tau < 30) {                           // rush into the hologram
        const p = ease.inE(inv(29, 30, tau));
        cam.position.copy(camLock.pos.clone().lerp(V(0, .085, .025), p)); cam.lookAt(camLock.look.clone().add(V(0, .1, 0)).lerp(V(0, .075, 0), p));
        cam.fov = lerp(48, 70, p);
      }
      cam.updateProjectionMatrix();

      // board camera for the phone screen: a slow orbit, top-down
      const a = tau * .12;
      bcam.position.set(Math.sin(a) * 2.2, 14, Math.cos(a) * 2.2 + 1.0); bcam.up.set(0, 0, -1); bcam.lookAt(0, 0, .5);
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
      e2.style.transform = `translate(${mv * 268}px, ${mv * 14}px)`;
      sent.style.opacity = inv(34.6, 34.8, tau);
    };
  },
};
