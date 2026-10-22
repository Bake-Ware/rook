// 4. Security reporting — "Vigilance that's active."
// 2 AM: a red pulse gets into the NAS. A security-watch job wakes an agent, which blocks
// and isolates it, calls your phone, rotates keys, restores from last night's snapshot,
// and leaves a report for the morning. Concept: the details shown are demo data.
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, agent, figure, bedside as placeBed } from '../kit.js';

export default {
  title: 'Security reporting', tagline: 'Vigilance<br>that’s active<span class="tdot">.</span>', body: 49, holdTitle: true,
  mood: { key: 'B', bpm: 100, minor: true, energy: .55, theme: 'lofi' },
  assets: ['rook', 'agent', 'nas', 'phone', 'laptop', 'figure', 'gpurig', 'workstation', 'nightstand', 'bed', 'chair'],
  async build(kit) {
    const S = kit.scene;
    kit.board({ size: 70 });
    S.fog = new THREE.Fog(COL.page, 11, 32);
    const NAS = V(3, 0, 1);
    const nasMat = new THREE.MeshToonMaterial({ color: 0xaaa17d, gradientMap: kit.ramp, emissive: 0xe0675c, emissiveIntensity: 0 });
    const nas = kit.make('nas', { at: NAS, rot: -.4, mats: { body: nasMat } });
    const hub = goldRook(kit, V(-1, 0, -5), .55, .4);
    kit.make('gpurig', { at: V(-5, 0, -2), rot: .5 });
    const ws = kit.make('workstation', { at: V(5.5, 0, -3.5), rot: -.5 });
    const wscr = kit.meshesOf(ws, 'screen')[0];
    const AG = [
      agent(kit, 'security', COL.red, V(1, 0, 3), { scale: .3, chipText: 'Automatic job · Security watch' }),
      agent(kit, 'claude code', COL.teal, V(-3, 0, 2), { scale: .3 }),
      agent(kit, 'ops', COL.blue, V(-1, 0, -1), { scale: .3 }),
    ];
    AG[0].chip.el.classList.add('job');
    // the dark bedroom at the board's edge
    placeBed(kit, V(-7, 0, 6), { rot: .3, side: 1 });
    const phone = kit.make('phone', { at: V(-6.95, .645, 6.05), rot: .5 });
    const pscr = kit.meshesOf(phone, 'screen')[0]; pscr.material = new THREE.MeshBasicMaterial({ color: 0x1a2520 });
    const phoneL = new THREE.PointLight(0xbfe8ff, 0, 5, 1.4); phoneL.position.set(-6.95, 1.0, 6); S.add(phoneL);
    // the morning: someone at the workstation, report on screen
    const fig = figure(kit, V(5.16, 0, -2.88), Math.PI - .5);
    kit.make('chair', { at: V(5.16, 0, -2.88), rot: -.5 + Math.PI });
    // remediation pieces: walls around the NAS square, a key, open sessions
    const walls = [];
    for (const [dx, dz, ry] of [[.5, 0, 0], [-.5, 0, 0], [0, .5, Math.PI / 2], [0, -.5, Math.PI / 2]]) {
      const w = new THREE.Mesh(new THREE.BoxGeometry(.06, 1, 1.02), new THREE.MeshToonMaterial({ color: COL.goldHi, gradientMap: kit.ramp, transparent: true, opacity: .55 }));
      w.position.set(NAS.x + dx, .5, NAS.z + dz); w.rotation.y = ry; S.add(w); walls.push(w);
      w.add(new THREE.LineSegments(new THREE.EdgesGeometry(w.geometry), new THREE.LineBasicMaterial({ color: 0x5a4320 })));
    }
    const key = new THREE.Group(); key.position.set(NAS.x, 1.25, NAS.z); S.add(key);
    { const km = kit.toon(COL.goldHi);
      const ring = new THREE.Mesh(new THREE.TorusGeometry(.16, .045, 6, 16), km); ring.position.x = -.3; key.add(ring);
      const shaft = new THREE.Mesh(new THREE.BoxGeometry(.5, .07, .05), km); shaft.position.x = .07; key.add(shaft);
      for (const x of [.22, .3]) { const t = new THREE.Mesh(new THREE.BoxGeometry(.05, .12, .05), km); t.position.set(x, -.08, 0); key.add(t); }
      key.traverse(o => { if (o.isMesh) o.add(new THREE.LineSegments(new THREE.EdgesGeometry(o.geometry), kit.inkMat)); }); }
    const sess = [V(-5, .4, -2), V(5.5, 1.0, -3.5), V(-3, .4, 2)].map(p => {
      const g = new THREE.BufferGeometry().setFromPoints([NAS.clone().setY(.3), p]);
      const l = new THREE.Line(g, new THREE.LineDashedMaterial({ color: COL.red, dashSize: .12, gapSize: .08, transparent: true }));
      l.computeLineDistances(); S.add(l); return l;
    });

    // ---------------------------------------------------------- overlays
    kit.caption('2:14 AM', .3, 9.6, 'stamp');
    kit.caption('7:40 AM', 41.8, 48.6, 'stamp');
    kit.caption('Blocked · Isolated · Keys rotated', 30.2, 34, 'label');
    const concept = kit.el('CONCEPT · DEMO DATA', 'conceptTag');
    kit.updaters.push(t => kit.show(concept, fade(t, .5, 1, 48, 48.8) * .8));
    const e1 = kit.say(11.2, 'rook', 'Intrusion on the NAS. Unknown login at two fourteen. I’ve blocked the address and cut it off from the band.', 'a');
    kit.say(e1 + .5, 'rook', 'Degraded services: media server, image backups, database backups. Files accessed: the photo share, read only.', 'b');
    const call = pane(kit, { title: '<b>rook</b> · calling', w: 380, body: `<div class="hi">● incoming · Rook</div><div class="dim">security watch</div>` });
    const report = pane(kit, { title: '<b>incident</b> · nas · 02:14', w: 760, cls: 'red', body: `
      <div class="dim">Login origin</div><div>Frankfurt, DE · 185.220.101.x · <span class="dim">curl/8.5.0</span></div>
      <div class="dim" style="margin-top:10px">Touched</div><div>photos share · read only · 3 folders</div>
      <div class="dim" style="margin-top:10px">Not touched</div><div>backups · database · keys</div>
      <div class="dim" style="margin-top:10px">Recovery</div><div class="r1">› address blocked · NAS isolated from the band</div><div class="r2">› credentials rotated · sessions closed</div>
      <div class="r3 ok">✓ restored from last night’s snapshot · verified</div>` });
    const morning = pane(kit, { title: '<b>incident report</b> · resolved', w: 440, cls: 'ok', body: `
      <div class="ok">✓ contained in 41 s</div><div>✓ restore verified</div><div class="dim">2 follow-ups for you</div>` });
    const st = document.createElement('style'); st.textContent = `
      .conceptTag{left:auto;right:56px;top:72px;font:400 18px var(--mono);letter-spacing:.25em;color:var(--dim);border:1px solid #3b4232;border-radius:6px;padding:6px 12px}
      .r1,.r2,.r3{opacity:0}`;
    document.head.appendChild(st);
    const R = [1, 2, 3].map(i => report.el.querySelector('.r' + i));

    // pulses: the intruder (red) from the board's edge; the watch to your phone
    // the opening: the title's full stop turns red, the words and the rook fade, the Board fades in,
    // and the stop drops onto the Board as the intruder
    // timeline: words fade (0.1-0.6), the stop glides to the centre of the screen (0.7-1.2), the Board fades in
    // around it with the camera on the intruder's square (1.2-1.9), then the stop is the intruder and the camera
    // follows it into the NAS (1.9-4.6), easing into the NAS shot by 5.6
    const START = V(9, 0, 1), T0 = 1.9;
    const intruder = kit.pulse(START, NAS, T0, 4.6 - T0, { color: COL.red, size: .16 });
    let ttl = null, words, dotSpan;                       // the bookends make the title after build: set it up on first use
    const fly = kit.el('', 'dotfly');
    const dst = document.createElement('style'); dst.textContent = `
      .dotfly{width:18px;height:18px;border-radius:50%;background:#e0675c;box-shadow:0 0 24px 6px rgba(224,103,92,.7);transform:translate(-50%,-50%)}`;
    document.head.appendChild(dst);
    let home = null;
    const CHASE = V(Math.sin(.44) * Math.cos(.56), Math.sin(.56), Math.cos(.44) * Math.cos(.56)).multiplyScalar(4);
    function opening(tau) {
      if (!ttl) {
        ttl = kit.stingTitle;
        ttl.innerHTML = '<div class="nm"><span class="w">Security reporting</span><span class="dot">.</span></div>';
        words = ttl.querySelector('.w'); dotSpan = ttl.querySelector('.dot');
        const stage = document.getElementById('stage');      // above the fade-to-black, so it can hold over the cut
        for (const el of [ttl, fly]) { stage.appendChild(el); el.style.zIndex = 20; }
      }
      // the camera: on the intruder's square, then chasing it, then easing into the clip's own NAS shot
      if (tau < 5.6) {
        const head = intruder.at(ease.io(clamp((tau - T0) / intruder.dur))).clone();
        const cam = kit.camera, w = ease.io(inv(4.6, 5.6, tau));
        const pos = head.clone().add(CHASE), look = head;
        cam.position.copy(pos.lerp(cam.position, w)); cam.fov = lerp(38, cam.fov, w);
        cam.lookAt(look.clone().lerp(kit.camLook ?? look, w)); cam.updateProjectionMatrix(); cam.updateMatrixWorld();
      }
      const t = tau + 2;                                   // sting time
      kit.show(ttl, tau < 1.6 ? fade(t, .25, .6, 99, 99) : 0);
      words.style.opacity = 1 - inv(.1, .6, tau);
      const red = inv(-.4, 0, tau);
      dotSpan.style.color = red > 0 ? `rgb(${Math.round(lerp(226, 224, red))},${Math.round(lerp(227, 103, red))},${Math.round(lerp(217, 92, red))})` : '';
      if (tau > -1 && !home) {                            // where the stop sits on the stage
        const s = ttl.parentElement.getBoundingClientRect(), r = dotSpan.getBoundingClientRect(), k = s.width / 1920;
        if (r.width) home = { x: (r.left + r.width / 2 - s.left) / k, y: (r.bottom - s.top) / k - 14 };
      }
      dotSpan.style.visibility = tau > .7 ? 'hidden' : 'visible';
      if (home && tau > .7 && tau < T0 + .03) {
        const go = ease.io(inv(.7, 1.2, tau));
        fly.style.left = lerp(home.x, 960, go) + 'px'; fly.style.top = lerp(home.y, 540, go) + 'px';
        kit.show(fly, 1);
      } else kit.show(fly, 0);
      if (tau < T0) kit.dim *= clamp((tau - 1.2) / .7);    // hold on the stop, then the Board fades in around it
    }
    kit.pulse(V(1, 0, 3), V(-7, 0, 6), 9.4, 1.0, { zFirst: true });
    kit.events.push({ t: 5.0, type: 'alarm' }, { t: 10.4, type: 'ring', n: 1 }, { t: 24.6, type: 'whoosh' }, { t: 26.2, type: 'tap' },
      { t: 27.6, type: 'shimmer' }, { t: 29.0, type: 'tap' }, { t: 33.2, type: 'shimmer' }, { t: 41.6, type: 'whoosh' });

    const nasBox = [V(.6, 0, .4), V(3.6, 1.1, 3.4)], bedside = [V(-7.4, 0, 5.6), V(-6.5, 1.1, 6.4)], fix = [V(2.4, 0, .4), V(3.6, 1.6, 1.6)];
    const am = [V(4.8, 0, -4.0), V(6.1, 1.5, -2.4)];
    kit.camPath([
      { t: 0, pos: [2, 8.5, 12.5], look: [1.5, 0, 0], fov: 38 },
      { t: 4.9, pos: [2.4, 8.0, 11.6], look: [1.8, 0, .4], e: 'lin' },
      { t: 5.6, frame: { on: nasBox, yaw: 38, pitch: 18, fill: .95 }, e: 'outE' },
      { t: 9.3, frame: { on: nasBox, yaw: 32, pitch: 18, fill: 1.0 }, e: 'lin' },
      { t: 10.6, frame: { on: bedside, yaw: 28, pitch: 20, fill: .55 }, e: 'io' },
      { t: 24, frame: { on: bedside, yaw: 14, pitch: 16, fill: .65 }, e: 'lin' },
      { t: 25, frame: { on: fix, yaw: 55, pitch: 20, fill: .7 }, e: 'io' },
      { t: 34, frame: { on: fix, yaw: 15, pitch: 24, fill: .72 }, e: 'lin' },
      { t: 35, pos: [7.5, 4.5, 7.5], look: [2.2, .8, 0], e: 'io' },
      { t: 41.5, pos: [7.0, 4.2, 7.0], look: [2.2, .8, 0], e: 'lin' },
      { t: 42.5, frame: { on: am, yaw: 8, pitch: 14, fill: 1.15 }, e: 'io' },
      { t: 49, frame: { on: am, yaw: 2, pitch: 14, fill: 1.25 }, e: 'lin' },
    ]);

    return tau => {
      opening(tau);
      { // the end card's full stop: red, then grey, then it drops off
        const td = document.querySelector('.tdot');
        if (td) {
          const g = inv(52.0, 52.4, tau), f = clamp((tau - 52.5) / .8);
          td.style.display = 'inline-block';
          td.style.color = `rgb(${Math.round(lerp(224, 120, g))},${Math.round(lerp(103, 124, g))},${Math.round(lerp(92, 114, g))})`;
          td.style.transform = `translateY(${f * f * 420}px) rotate(${f * 70}deg)`; td.style.opacity = 1 - inv(.6, 1, f);
        }
      }
      const night = tau < 41.6;
      const morningK = inv(41.6, 43, tau);
      kit.hemi.intensity = lerp(.45, 1.4, morningK); kit.key.intensity = lerp(.5, 2.2, morningK);
      kit.boardU.uDim.value = lerp(.7, 1, morningK);
      // the NAS: hit at 4.6, flashes red until isolated, glows normal in the morning
      const hit = tau > 4.6 && tau < 33;
      nasMat.emissiveIntensity = hit ? .35 + .3 * Math.max(0, Math.sin(tau * 9)) * (1 - inv(26, 33, tau)) : 0;
      kit.glowSquare(NAS.x, NAS.z, hit ? .9 : .5 * morningK, hit ? 2 : 0);
      if (tau > 4.6 && tau < 5.2) kit.flash = .25 * (1 - inv(4.6, 5.2, tau));
      // every agent turns to face it; the watch acts
      AG.forEach((a, i) => {
        const look = Math.atan2(NAS.x - a.obj.position.x, NAS.z - a.obj.position.z);
        a.obj.rotation.y = lerp(i * 1.3, look, ease.io(inv(5.2 + i * .15, 6.2 + i * .15, tau)));
        a.mat.emissiveIntensity = .2 + (i === 0 ? .6 * fade(tau, 5.6, 6, 34, 35) : 0);
        a.chip.place(a.top(), i === 0 ? fade(tau, 5.8, 6.2, 24.4, 24.8) + fade(tau, 34.6, 35, 41.4, 41.8) : 0);
      });
      // the phone lights the dark room and rings
      const ring = fade(tau, 10.2, 10.5, 23.6, 24.2);
      phoneL.intensity = 3.2 * ring;
      pscr.material.color.setHex(ring > .5 ? 0x9fd6ee : 0x1a2520);
      phone.position.y = .645 + (tau > 10.2 && tau < 11.2 ? Math.abs(Math.sin(tau * 60)) * .01 : 0);
      call.place(V(-6.95, 1.0, 6), fade(tau, 10.4, 10.7, 23.4, 24), { ay: 1, dy: -14 });
      kit.glowSquare(-7, 6, .6 * ring);
      // remediation: walls rise, the key turns over, sessions snap off
      walls.forEach((w, i) => { const s = ease.out(inv(25.4 + i * .2, 26.4 + i * .2, tau)); w.scale.y = Math.max(.001, s); w.position.y = .5 * s; w.visible = s > .01 && tau < 41.6; });
      key.visible = tau > 26.8 && tau < 34;
      key.rotation.x = lerp(0, Math.PI, ease.io(inv(27.2, 28.4, tau)));
      key.rotation.y = tau * .5; key.scale.setScalar(Math.max(.001, .6 * ease.out(inv(26.8, 27.3, tau)) * (1 - inv(33.4, 34, tau))));
      sess.forEach((l, i) => { const cut = tau > 28.8 + i * .25; l.visible = tau > 5 && !cut; l.material.opacity = .9; });
      // the report
      report.fixed(1100, 160, fade(tau, 34.6, 35.0, 41.2, 41.6), .9);
      R.forEach((el, i) => { el.style.opacity = inv(36.2 + i * 1.2, 36.5 + i * 1.2, tau); });
      // the morning
      fig.rest(); fig.sit();
      fig.obj.position.y = -.47; fig.obj.visible = tau > 41.6;
      morning.place(V(5.58, 1.55, -3.66), fade(tau, 43, 43.4, 48.4, 49), { ay: .5, scale: .8 });
      wscr.material.color.setHex(tau > 41.6 ? 0x2d4a40 : 0x1a2520);
    };
  },
};
