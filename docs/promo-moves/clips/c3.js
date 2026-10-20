// 3. Team huddle — "Focus on the results, not the work."
// One request goes into a room of expert agents. They split it, claim owners, send worker
// agents out to your machines, review each other, and report back with one question.
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, agent, figure } from '../kit.js';

export default {
  title: 'Team huddle', tagline: 'Focus on the results,<br>not the work.', body: 58,
  mood: { key: 'E', bpm: 104, minor: true, energy: .8 },
  assets: ['rook', 'agent', 'table', 'gpurig', 'workstation', 'nas', 'phone', 'figure'],
  async build(kit) {
    const S = kit.scene;
    kit.board({ size: 70 });
    S.fog = new THREE.Fog(COL.page, 12, 34);
    const table = kit.make('table', { at: V(0, 0, 0) });
    goldRook(kit, V(0, 0, -9), .6, .3);
    const A = [
      ['Planner', COL.teal, V(-1.3, 0, -1.3)], ['Builder', COL.green, V(1.3, 0, -1.3)],
      ['Reviewer', COL.pink, V(-1.3, 0, 1.3)], ['Ops', COL.blue, V(1.3, 0, 1.3)],
    ].map(([n, c, p]) => ({ n, ...agent(kit, n, c, p, { scale: .44 }), p }));
    const machines = [
      { name: 'gpu rig', obj: kit.make('gpurig', { at: V(4, 0, -2), rot: -.7 }), at: V(4, 0, -2) },
      { name: 'workstation', obj: kit.make('workstation', { at: V(-4.1, 0, -2.2), rot: .7 }), at: V(-4, 0, -2) },
      { name: 'build box', obj: kit.make('nas', { at: V(4, 0, 3), rot: -.4 }), at: V(4, 0, 3) },
    ];
    const workers = machines.map((m, i) => agent(kit, 'worker', [COL.green, COL.green, COL.green][i], m.at.clone().add(V(-.8, 0, .5)), { scale: .2, chipText: 'worker · builder' }));
    const fig = figure(kit, V(-3, 0, 3), Math.PI * .8);
    // the phone, real size, in the left hand
    const phone = kit.make('phone', { parent: fig.foreL, at: V(0, -.33, .04) }); phone.rotation.set(-Math.PI / 2 + .3, 0, 0);
    const pscr = kit.meshesOf(phone, 'screen')[0]; pscr.material = new THREE.MeshBasicMaterial({ color: 0x1a2520 });

    // ---------------------------------------------------------- overlays
    kit.caption('<b>HUDDLE</b>', .5, 5.8, 'title');
    kit.caption('Owners decided', 18.6, 22, 'label');
    kit.caption('Work delegated to 3 machines', 28.5, 33.8, 'label');
    kit.say(7.6, 'you', 'Ship the plugin manager this week.', 'a');
    kit.say(36.6, 'reviewer', 'Missing a test for uninstall.', 'b');
    kit.say(45.6, 'rook', 'Three of four merged. Ops needs your call on the default install path.', 'c');
    const chat = pane(kit, { title: '<b>#huddle</b> · plugin manager · 4 agents', w: 760, body: `
      <div class="m1"><span class="hi">you</span> · Ship the plugin manager this week.</div>
      <div class="m2"><span class="ag">planner</span> · Splitting it into four. Claim what you own.</div>` });
    const TASKS = [['API + manifest', 0], ['loader + install CLI', 1], ['tests + review', 2], ['packaging + default path', 3]];
    const cards = TASKS.map(([t, owner]) => {
      const c = kit.el(`<div class="tt">${t}</div><div class="own"></div>`, 'task');
      c.querySelector('.own').innerHTML = `<i style="background:#${new THREE.Color(A[owner].color).getHexString()}"></i>owner · ${A[owner].n.toLowerCase()}`;
      return { el: c, owner, own: c.querySelector('.own') };
    });
    const consoles = machines.map((m, i) => pane(kit, { title: `<b>console</b> · ${m.name}`, w: 380, body: '<div class="scroll"></div>' }));
    const LOG = [['$ git checkout -b plugin-manager', 'dim'], ['$ npm test -- loader', ''], ['✓ 42 passing', 'ok'], ['$ build manifest schema', ''],
      ['compiling 18 files …', 'dim'], ['✓ build ok · 12.4 s', 'ok'], ['$ rook_call install --dry-run', ''], ['✓ install plan · 3 steps', 'ok'], ['$ pytest tests/plugins', ''],
      ['........................', 'dim'], ['✓ 61 passed', 'ok']];
    const note = kit.el('✗ Missing a test for uninstall.', 'rednote');
    const sum = pane(kit, { title: '<b>rook</b> → your phone', w: 520, body: `
      <div><span class="ok">✓</span> API + manifest · merged</div><div><span class="ok">✓</span> loader + install CLI · merged</div>
      <div><span class="ok">✓</span> tests + review · merged</div><div class="q"><span class="hi">?</span> default install path</div>
      <div class="ans"><span class="btn">~/.rook/plugins</span><span class="btn alt">/opt/rook</span></div>` });
    const st = document.createElement('style'); st.textContent = `
      .task{font:400 20px var(--mono);background:rgba(9,12,9,.94);border:2px solid #514b35;border-radius:10px;padding:8px 14px;white-space:nowrap}
      .task .tt{color:var(--fg)}.task .own{margin-top:4px;font-size:16px;color:var(--dim);opacity:0}.task .own i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px}
      .task.red{border-color:var(--red)}.task.ok{border-color:#8fc79a}.task.ok .tt::before{content:'✓ ';color:var(--ok)}
      .rednote{font:400 20px var(--mono);color:#fff;background:rgba(56,32,27,.96);border:1.5px solid var(--red);border-radius:8px;padding:6px 12px;white-space:nowrap}
      .scroll{height:150px;overflow:hidden;font-size:17px;line-height:1.45}
      .btn{display:inline-block;margin:8px 10px 0 0;padding:4px 12px;border:1.5px solid #514b35;border-radius:8px;font-size:18px}
      .btn.on{border-color:var(--ok);color:var(--ok);background:rgba(143,199,154,.12)}
      .m1,.m2{opacity:0}`;
    document.head.appendChild(st);
    const m1 = chat.el.querySelector('.m1'), m2 = chat.el.querySelector('.m2'), btn = sum.el.querySelector('.btn');

    // pulses
    machines.forEach((m, i) => kit.pulse(A[1].p, m.at, 22.6 + i * .6, 1.6, { zFirst: i === 2 }));
    kit.pulse(V(1, 0, 1), V(-3, 0, 3), 44.0, 1.6);
    kit.pulse(V(-3, 0, 3), V(1, 0, 1), 53.4, 1.4, { zFirst: true });
    kit.events.push({ t: 1.0, type: 'whoosh' }, { t: 7.4, type: 'tap' }, { t: 9.8, type: 'tap' }, { t: 35.8, type: 'alarm' },
      { t: 40.5, type: 'shimmer' }, { t: 45.4, type: 'buzz' }, { t: 53.2, type: 'tap' }, { t: 55.0, type: 'shimmer' });

    const tbl = [V(-1.9, 0, -1.9), V(1.9, 1.1, 1.9)], room = [V(-4.6, 0, -2.8), V(4.4, 1.4, 3.6)], you = [V(-3.35, 0, 2.7), V(-2.65, 1.85, 3.3)];
    kit.camPath([
      { t: 0, frame: { on: tbl, yaw: 0, pitch: 84, fill: .55, fov: 40 } },
      { t: 6, frame: { on: tbl, yaw: 0, pitch: 42, fill: .8 }, e: 'io' },
      { t: 12, frame: { on: tbl, yaw: 8, pitch: 34, fill: .9 }, e: 'lin' },
      { t: 15, frame: { on: tbl, yaw: 50, pitch: 26, fill: .85 } },
      { t: 20, frame: { on: tbl, yaw: 115, pitch: 26, fill: .85 }, e: 'lin' },
      { t: 23, frame: { on: room, yaw: 0, pitch: 42, fill: .95 }, e: 'io' },
      { t: 33.5, frame: { on: room, yaw: 6, pitch: 40, fill: .98 }, e: 'lin' },
      { t: 35, frame: { on: [V(-1.6, 0, -1.6), V(1.6, 1.3, 1.6)], yaw: 62, pitch: 24, fill: .85 }, e: 'io' },
      { t: 43.5, frame: { on: [V(-1.6, 0, -1.6), V(1.6, 1.3, 1.6)], yaw: 50, pitch: 22, fill: .9 }, e: 'lin' },
      { t: 45.5, frame: { on: you, yaw: 205, pitch: 8, fill: .62 }, e: 'io' },
      { t: 52.4, frame: { on: you, yaw: 196, pitch: 8, fill: .68 }, e: 'lin' },
      { t: 54, frame: { on: tbl, yaw: 35, pitch: 30, fill: .85 }, e: 'io' },
      { t: 58, frame: { on: tbl, yaw: 48, pitch: 34, fill: .78 }, e: 'lin' },
    ]);

    return tau => {
      // the table rises out of the board; agents take seats
      const rise = ease.out(inv(.4, 2.6, tau));
      table.position.y = lerp(-.9, 0, rise);
      A.forEach((a, i) => {
        const s = ease.outE(inv(2.2 + i * .45, 2.9 + i * .45, tau));
        a.obj.scale.setScalar(Math.max(.001, .44 * s));
        a.obj.rotation.y = Math.atan2(-a.p.x, -a.p.z) + Math.PI;
        a.chip.place(a.top(), s * fade(tau, 0, .1, 57, 58) * (tau < 44 || tau > 52.6 ? 1 : 0));
        a.mat.emissiveIntensity = .28;
      });
      // who is working right now glows
      const glowA = (i, a) => { A[i].mat.emissiveIntensity = .28 + .6 * a; };
      glowA(0, fade(tau, 9.6, 10, 13, 14));
      glowA(1, fade(tau, 22, 22.4, 33, 34) + fade(tau, 39, 39.4, 40.6, 41));
      glowA(2, fade(tau, 35.2, 35.6, 38.4, 39));
      glowA(3, fade(tau, 53.6, 54, 56, 57));
      // chat pane
      chat.fixed(580, 90, fade(tau, 6.4, 6.8, 13.4, 14));
      m1.style.opacity = inv(7.4, 7.6, tau); m2.style.opacity = inv(9.8, 10, tau);
      // task cards: from the planner across the table to each owner
      cards.forEach((c, i) => {
        const t0 = 12.6 + i * .7;
        const p = ease.io(inv(t0, t0 + 1.4, tau));
        const owner = A[c.owner].p;
        let pos = A[0].p.clone().lerp(V(0, 0, 0), .5).lerp(owner.clone().multiplyScalar(.55), p).setY(.8);
        // the reviewer sends the builder's card back with a red note
        if (c.owner === 1 && tau > 35.4 && tau < 41) {
          const q = ease.io(inv(35.6, 36.6, tau)) * (1 - ease.io(inv(39.6, 40.4, tau)));
          pos = owner.clone().multiplyScalar(.55).lerp(A[2].p.clone().multiplyScalar(.55), q * .45).setY(.8);
        }
        const show = fade(tau, t0 - .1, t0 + .2, 57.4, 58) * (tau < 22.6 || (tau > 34.6 && tau < 44) || tau > 52.6 ? 1 : 0);
        kit.show(c.el, show);
        if (show > .003) kit.pin(c.el, pos, { ay: .5, depth: true, scale: .8 });
        c.own.style.opacity = inv(t0 + 1.5, t0 + 1.8, tau);
        let cls = 'ov task';
        if (c.owner === 1 && tau > 35.6 && tau < 40.5) cls += ' red';
        else if ((c.owner < 3 && tau > 40.5) || (c.owner === 3 && tau > 55)) cls += ' ok';
        if (c.el.className !== cls) c.el.className = cls;
        if (c.owner === 1) { kit.show(note, fade(tau, 36, 36.3, 39.6, 40)); if (tau > 36 && tau < 40) kit.pin(note, pos, { ay: -.6, depth: true }); }
      });
      // delegation: worker rooks pop up next to each machine, consoles scroll
      machines.forEach((m, i) => {
        const t0 = 24.2 + i * .6;
        const s = ease.outE(inv(t0, t0 + .5, tau)) * (1 - inv(42, 43, tau));
        workers[i].obj.scale.setScalar(Math.max(.001, .2 * s));
        workers[i].chip.place(workers[i].top(), s * (tau < 34 ? 1 : 0));
        kit.glowSquare(Math.round(m.at.x), Math.round(m.at.z), .7 * s);
        const cp = consoles[i];
        cp.place(m.at.clone().add(V(0, 1.4, 0)), fade(tau, t0 + .3, t0 + .7, 33.4, 34), { ay: 1, scale: .85 });
        const n = Math.floor(clamp((tau - t0 - .6) / 7) * LOG.length);
        const lines = LOG.slice(0, n).slice(-5).map(([l, c]) => `<div class="${c}">${l}</div>`).join('');
        cp.set(lines);
      });
      // the phone: a status summary with one question; you tap an answer
      const ph = fade(tau, 45.4, 45.8, 52.8, 53.4);
      sum.place(V(-3, 1.2, 3), ph, { ax: -.45, ay: .5 });
      btn.className = tau > 52.2 ? 'btn on' : 'btn';
      pscr.material.color.setHex(tau > 45.4 && tau < 53.4 ? 0x557f6e : 0x1a2520);
      kit.glowSquare(-3, 3, .7 * ph);
      fig.rest();
      fig.obj.rotation.y = Math.PI * .8;
      fig.armL.rotation.x = -.45; fig.armL.rotation.z = .25; fig.foreL.rotation.x = -1.25;          // holding the phone up
      fig.armR.rotation.x = tau > 51.4 && tau < 53.2 ? -.5 : 0; fig.foreR.rotation.x = tau > 51.4 && tau < 53.2 ? -1.1 - .12 * Math.sin((tau - 51.4) * 6) : 0;
      fig.head.rotation.x = .4;
      kit.glowSquare(0, 0, .25 + .35 * fade(tau, 12, 12.5, 22, 23));
    };
  },
};
