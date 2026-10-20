// 5. Research team — "Explore. More."
// You ask a question into one node of a network. A research team of agents reads, designs
// and analyses; six seed runs go to the GPU rig; one diverges and is restarted; the finding
// lands in the wiki for you to verify. Plots, ETAs and the hypothesis are placeholders.
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, agent, figure, rnd } from '../kit.js';

export default {
  title: 'Research team', tagline: 'Explore.<br>More.', body: 60,
  mood: { key: 'F#', bpm: 84, minor: true, energy: .55 },
  assets: ['rook', 'agent', 'gpurig', 'figure', 'workstation'],
  async build(kit) {
    const S = kit.scene;
    kit.board({ size: 90 });
    S.fog = new THREE.Fog(COL.page, 10, 30);
    // ---------------------------------------------------------- the network in front of you (far corner)
    const NW = V(-24, 2.3, 0);
    const net = new THREE.Group(); net.position.copy(NW); S.add(net);
    const layers = [4, 6, 7, 6, 3], nodes = [];
    const nodeGeo = new THREE.IcosahedronGeometry(.13, 0);
    layers.forEach((n, li) => {
      for (let i = 0; i < n; i++) {
        const m = new THREE.Mesh(nodeGeo, new THREE.MeshToonMaterial({ color: 0x79d2c8, gradientMap: kit.ramp, emissive: 0x79d2c8, emissiveIntensity: .35 }));
        m.position.set((li - 2) * 1.0, (i - (n - 1) / 2) * .48, Math.sin(i * 1.7 + li) * .35);
        m.add(new THREE.LineSegments(new THREE.EdgesGeometry(nodeGeo), kit.inkMat));
        net.add(m); nodes.push({ m, li });
      }
    });
    const pts = [];
    for (const a of nodes) for (const b of nodes) if (b.li === a.li + 1 && rnd(nodes.indexOf(a) * 31 + nodes.indexOf(b)) < .62) pts.push(a.m.position, b.m.position);
    const edges = new THREE.LineSegments(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineBasicMaterial({ color: 0x79d2c8, transparent: true, opacity: .35, blending: THREE.AdditiveBlending, depthWrite: false }));
    net.add(edges);
    const target = nodes.find(n => n.li === 2 && Math.abs(n.m.position.y) < .3).m;
    const fig = figure(kit, NW.clone().add(V(.4, -2.3, 3.2)), Math.PI);

    // ---------------------------------------------------------- inside the node: the team on the Board
    goldRook(kit, V(-1, 0, -6), .55, .3);
    const team = [['Literature', COL.pink, V(-2.4, 0, .6)], ['Design', COL.teal, V(0, 0, -1)], ['Analysis', COL.green, V(2.4, 0, .6)]]
      .map(([n, c, p]) => ({ n, ...agent(kit, n, c, p, { scale: .36 }), p }));
    const rig = kit.make('gpurig', { at: V(6, 0, -2), rot: -.6, scale: 1.4 });
    const rigGlow = kit.meshesOf(rig, 'light');
    // paper pages flying into a stack by the literature agent
    const pages = [];
    for (let i = 0; i < 12; i++) {
      const p = new THREE.Mesh(new THREE.BoxGeometry(.21, .004, .297), kit.toon(0xe6dcc0));
      p.add(new THREE.LineSegments(new THREE.EdgesGeometry(p.geometry), kit.inkMat));
      S.add(p); pages.push({ p, from: V(-2.4 + (rnd(i) - .5) * 8, 2.5 + rnd(i + 5) * 2, .6 + (rnd(i + 9) - .5) * 6), i });
    }

    // ---------------------------------------------------------- overlays
    kit.caption('Literature · Design · Analysis', 11, 16, 'label');
    kit.caption('Agents write it. You verify it.', 50, 54.6, 'label');
    kit.say(1.4, 'you', 'Does recurrence beat depth at equal compute?', 'a');
    kit.say(27.4, 'rook', 'Two of six done. The rest finish around nine tonight.', 'b');
    kit.say(38.8, 'rook', 'Run four diverged. Restarted with a lower learning rate. New ETA nine forty.', 'c');
    const cfg = pane(kit, { title: '<b>design</b> · experiment.yaml', w: 400, body: '<pre class="yaml"></pre>' });
    const YAML = 'model: [recurrent, deep]\nparams: 124M  # equal compute\nseeds: 6\ntokens: 2B\ncontrol: depth-12\nmetric: val_loss';
    const lit = pane(kit, { title: '<b>literature</b> · 14 papers', w: 360, body: '<div class="lit"></div>' });
    const LIT = ['Universal Transformers (2018)', 'Looped transformers (2023)', 'Depth vs width at fixed FLOPs', 'Recurrent depth scaling (2025)'];
    const status = pane(kit, { title: '<b>runs</b> · dormamu', w: 560, body: `
      <div class="st"></div><div class="bars">${[0, 1, 2, 3, 4, 5].map(i => `<div class="bar"><span>run ${i + 1}</span><b><i></i></b></div>`).join('')}</div>
      <div class="eta"><span class="lbl dim">ETA</span> <span class="clock hi">21:00</span> <span class="left dim"></span></div>` });
    const plot = pane(kit, { title: '<b>analysis</b> · val loss', w: 520, body: '<canvas width="480" height="260"></canvas>' });
    const table = pane(kit, { title: '<b>analysis</b> · results', w: 560, body: `
      <table class="res"><tr><th></th><th>val loss</th><th>± seed</th></tr>
      <tr class="r1"><td>recurrent 124M</td><td class="hi">2.914</td><td class="dim">0.011</td></tr>
      <tr class="r2"><td>depth-12 124M</td><td>2.951</td><td class="dim">0.009</td></tr>
      <tr class="r3"><td>difference</td><td class="ok">−0.037</td><td class="dim">3.4 σ</td></tr></table>` });
    const wiki = pane(kit, { title: '<b>knowledge</b> · recurrence vs depth', w: 520, body: `
      <div>At equal compute, the recurrent model reached lower validation loss on all six seeds.</div>
      <div class="badge">awaiting your verification</div>` });
    const st = document.createElement('style'); st.textContent = `
      .yaml{font:400 19px/1.5 var(--mono);color:var(--soft);margin:0;min-height:170px}
      .lit div{font-size:18px;color:var(--soft)} .st div{font-size:18px}
      .bars{margin-top:8px}.bar{display:flex;gap:10px;align-items:center;font-size:17px;margin:4px 0}.bar span{width:60px;color:var(--dim)}
      .bar b{flex:1;height:10px;border-radius:5px;background:#20261d;overflow:hidden}.bar i{display:block;height:100%;width:0;background:#8fc79a}
      .bar.bad i{background:#e0675c}.eta{margin-top:8px;font-size:22px}
      .res{width:100%;border-collapse:collapse;font-size:20px}.res th{color:var(--dim);font-weight:400;text-align:left;padding:4px}.res td{padding:5px 4px;border-top:1px solid #2b322a}
      .res tr.r1,.res tr.r2,.res tr.r3{opacity:0}
      .badge{display:inline-block;margin-top:10px;padding:4px 12px;border:1.5px solid #d8ad6d;border-radius:8px;color:#f0c98c;font-size:18px}`;
    document.head.appendChild(st);
    const yamlEl = cfg.el.querySelector('.yaml'), litEl = lit.el.querySelector('.lit'), stEl = status.el.querySelector('.st');
    const barEls = [...status.el.querySelectorAll('.bar')], clockEl = status.el.querySelector('.clock'), leftEl = status.el.querySelector('.left');
    const ctx = plot.el.querySelector('canvas').getContext('2d');
    const rows = [1, 2, 3].map(i => table.el.querySelector('.r' + i));
    const STATUS = ['6 seed runs queued on Dormamu', 'GPUs idling 3%', 'VRAM 10% and climbing', 'Validating control case'];

    // run progress (0..1) for run i at clip time t; run 4 (index 3) diverges at 36.5 and restarts
    const runP = (i, t) => {
      const start = 18.4 + i * .5, base = clamp((t - start) / (i < 2 ? 9 : 26 + i * 2));
      if (i === 3 && t > 36.6) return clamp((t - 37.6) / 18);
      return base;
    };
    const loss = (i, x, t) => {                               // a curve for run i at progress x
      const rec = i % 2 === 0;
      let y = 2.9 + (rec ? -.04 : 0) + 3.6 * Math.exp(-x * 5.5) + Math.sin(x * 40 + i) * .02;
      if (i === 3 && t > 35.6 && t < 37.6) y += 3.5 * clamp((t - 35.6) / 1.0) * x;   // the spike
      return y;
    };
    function drawPlot(t) {
      ctx.clearRect(0, 0, 480, 260);
      ctx.strokeStyle = '#2b322a'; ctx.lineWidth = 1;
      for (let g = 0; g < 5; g++) { ctx.beginPath(); ctx.moveTo(30, 20 + g * 55); ctx.lineTo(470, 20 + g * 55); ctx.stroke(); }
      const cols = ['#79d2c8', '#d8ad6d', '#79d2c8', '#e0675c', '#79d2c8', '#d8ad6d'];
      for (let i = 0; i < 6; i++) {
        const P = runP(i, t);
        ctx.strokeStyle = i === 3 && t > 35.6 && t < 37.6 ? '#e0675c' : cols[i] === '#e0675c' ? '#9fd37c' : cols[i];
        ctx.lineWidth = 2.2; ctx.beginPath();
        const N = Math.floor(P * 120);
        for (let k = 0; k <= N; k++) {
          const x = k / 120, y = loss(i, x, t);
          const px = 30 + x * 440, py = 240 - (Math.min(6.6, y) - 2.6) / 4 * 220;
          k ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
        }
        ctx.stroke();
      }
    }

    kit.pulse(V(0, 0, -1), V(6, 0, -2), 16.6, 1.4);
    kit.pulse(V(2.4, 0, .6), V(6, 0, -2), 37.0, 1.0, { zFirst: true });
    kit.events.push({ t: 5.6, type: 'rush' }, { t: 6.0, type: 'shimmer' }, { t: 35.8, type: 'alarm' }, { t: 54.2, type: 'rush' }, { t: 55.4, type: 'shimmer' });

    const rigBox = [V(5.6, 0, -2.4), V(6.4, .8, -1.6)];
    kit.camPath([
      { t: 0, pos: [-24 + 2.6, 2.6, 7.6], look: [-24, 2.2, 0], fov: 38 },
      { t: 3.6, pos: [-24 + 1.4, 2.5, 5.6], look: [-24, 2.3, 0], e: 'lin' },
      { t: 6.0, pos: [-24 + .02, 2.3 + .02, .3], look: [-24, 2.3, 0], e: 'in' },
      { t: 6.01, pos: [0, 9, 10], look: [0, .6, 0], e: 'cut' },
      { t: 8, pos: [-2.5, 4.0, 6.8], look: [0, .8, 0], e: 'outE' },
      { t: 16, pos: [4.5, 3.6, 5.8], look: [.6, .8, 0], e: 'lin' },
      { t: 18, frame: { on: rigBox, yaw: 58, pitch: 16, fill: .5, look: [-.25, .15, 0] }, e: 'io' },
      { t: 26, frame: { on: rigBox, yaw: 48, pitch: 15, fill: .55, look: [-.25, .15, 0] }, e: 'lin' },
      { t: 27, frame: { on: rigBox, yaw: 30, pitch: 12, fill: .5, look: [-.42, .35, .24] }, e: 'io' },
      { t: 34, frame: { on: rigBox, yaw: 26, pitch: 12, fill: .52, look: [-.42, .35, .22] }, e: 'lin' },
      { t: 35, pos: [3.2, 7.0, 10.5], look: [2.6, .8, -1], e: 'io' },
      { t: 46, pos: [2.4, 6.6, 10.0], look: [2.6, .8, -1], e: 'lin' },
      { t: 47, pos: [4.4, 2.2, 4.6], look: [2.4, .9, .4], e: 'io' },
      { t: 54, pos: [4.0, 2.2, 4.3], look: [2.4, .9, .4], e: 'lin' },
      { t: 54.01, pos: [-24 + .02, 2.32, .3], look: [-24, 2.3, 0], e: 'cut' },
      { t: 57, pos: [-24 + 2.8, 2.8, 8.0], look: [-24, 2.2, 0], e: 'outE' },
      { t: 60, pos: [-24 + 3.2, 3.0, 8.6], look: [-24, 2.2, 0], e: 'lin' },
    ]);

    return tau => {
      // the network breathes; the asked node swells as we push in, lights gold at the end
      net.rotation.y = Math.sin(tau * .2) * .15;
      nodes.forEach(({ m }, i) => { m.material.emissiveIntensity = .3 + .2 * Math.sin(tau * 2 + i); });
      const swell = ease.in(inv(3.8, 6, tau));
      target.scale.setScalar(1 + swell * 6);
      const done = inv(55, 56, tau);
      target.material.color.setHex(done > 0 ? 0xf0c98c : 0x79d2c8); target.material.emissive.setHex(done > 0 ? 0xd8ad6d : 0x79d2c8);
      target.material.emissiveIntensity = .35 + .6 * done;
      if (tau > 54) target.scale.setScalar(lerp(6, 1.4, ease.outE(inv(54, 56, tau))));
      if (tau > 5.95 && tau < 6.4) kit.flash = .8 * (1 - inv(5.95, 6.4, tau));
      if (tau > 53.9 && tau < 54.4) kit.flash = .8 * (1 - inv(53.9, 54.4, tau));
      fig.rest(); fig.armR.rotation.x = tau > 2.6 && tau < 5.5 ? -1.6 : -.2; fig.head.rotation.x = -.2;
      // the team
      team.forEach((a, i) => { a.chip.place(a.top(), fade(tau, 6.6 + i * .3, 7 + i * .3, 53.6, 54)); a.mat.emissiveIntensity = .25 + .4 * fade(tau, 6.6, 7, 16, 17); });
      // literature: pages fly into a stack
      pages.forEach(({ p, from, i }) => {
        const u = ease.io(inv(7 + i * .5, 8.2 + i * .5, tau));
        p.position.copy(from.clone().lerp(V(-2.4 + 1.0, .01 + i * .006, .6 + .9), u));
        p.rotation.set((1 - u) * 1.5, (1 - u) * 3 + i, (1 - u) * .8);
        p.visible = tau > 6.2 && tau < 54;
      });
      lit.place(V(-2.4, 1.2, .6), fade(tau, 7.4, 7.8, 15.6, 16), { ax: .8, ay: 1 });
      litEl.innerHTML = LIT.slice(0, Math.floor(clamp((tau - 7.6) / 6) * LIT.length + .001)).map(s => `<div>› ${s}</div>`).join('');
      cfg.place(V(0, 1.2, -1), fade(tau, 8, 8.4, 15.6, 16), { ay: 1 });
      yamlEl.textContent = YAML.slice(0, Math.floor(clamp((tau - 8.4) / 5.5) * YAML.length));
      // the rig: glows, six runs, status
      const rigOn = inv(17.8, 18.4, tau) * (1 - inv(54, 54.1, tau));
      rigGlow.forEach(m => m.material.color.setHex(rigOn > .5 ? 0x9fffe0 : 0xffe2a8));
      kit.glowSquare(6, -2, .7 * rigOn, 1);
      const stA = fade(tau, 18.4, 18.8, 53.6, 54);
      const close = ease.io(fade(tau, 26.4, 27.2, 33.4, 34.2));
      status.fixed(lerp(90, 110, close), lerp(130, 250, close), tau < 46 ? stA : 0, lerp(1, 1.2, close));
      stEl.innerHTML = STATUS.slice(0, Math.floor(clamp((tau - 19) / 4) * 4 + .001)).map(s => `<div class="dim">› ${s}</div>`).join('');
      barEls.forEach((b, i) => {
        b.querySelector('i').style.width = (runP(i, tau) * 100).toFixed(1) + '%';
        const bad = i === 3 && tau > 35.6 && tau < 37.6; if (b.classList.contains('bad') !== bad) b.classList.toggle('bad');
      });
      const eta = tau < 37.6 ? 21 * 60 : 21 * 60 + 40;
      const now = 17 * 60 + 5 + Math.max(0, tau - 26) * 9;           // time-lapse clock
      clockEl.textContent = `${Math.floor(eta / 60)}:${String(eta % 60).padStart(2, '0')}`;
      const left = Math.max(0, eta - now);
      leftEl.textContent = `· ${Math.floor(left / 60)} h ${String(Math.floor(left % 60)).padStart(2, '0')} m left`;
      plot.fixed(1300, 130, tau > 21 && tau < 46 ? fade(tau, 21, 21.4, 45.6, 46) * (1 - close) : 0);
      if (tau > 20.8 && tau < 54) drawPlot(tau);
      // the sky darkens through the time-lapse
      const dusk = inv(34, 46, tau);
      kit.hemi.intensity = tau > 54 ? 1.6 : lerp(1.6, .8, dusk);
      kit.boardU.uDim.value = lerp(1, .75, dusk * (tau < 54 ? 1 : 0));
      team[2].mat.emissiveIntensity += .5 * fade(tau, 36.6, 37, 38, 38.6);
      // results and the wiki
      table.place(V(2.4, 1.3, .6), fade(tau, 46.6, 47, 53.6, 54), { ax: 1.02, ay: .5 });
      rows.forEach((r, i) => { r.style.opacity = inv(47.4 + i * .8, 47.7 + i * .8, tau); });
      wiki.place(V(2.4, 1.3, .6), fade(tau, 49.6, 50, 53.6, 54), { ax: -.02, ay: .5 });
      kit.glowSquare(2, 1, .6 * fade(tau, 47, 47.5, 53.6, 54));
    };
  },
};
