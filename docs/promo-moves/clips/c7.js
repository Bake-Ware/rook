// 7. Make and track the promo campaign — "Share your self."
// The series reveals how it was made: an end card mid-render on the workstation, the ask,
// a plan, storyboards that turn into scenes, a render queue on the band, clips going out to
// channels on a calendar, the real star count. Then the ticker lands on "self".
import { THREE, V, COL, clamp, lerp, inv, ease, fade, pane, chip, goldRook, agent, figure, rnd } from '../kit.js';

const CLIPS = ['Secretary', 'Build it, paint it', 'Team huddle', 'Security reporting', 'Research team', 'Comic, song, shed', 'The campaign'];
const PIECES = ['phone', 'printer', 'table', 'nas', 'gpurig', 'car', 'rook'];
const CHANNELS = ['LinkedIn', 'Reddit', 'X', 'Bluesky', 'Mastodon', 'YouTube'];
const STARS = 3;          // github.com/Bake-Ware/rook stargazers on shoot day (2026-10-19)

export default {
  title: 'Make and track the campaign', tagline: 'Share your <span class="tick">self.</span>', body: 46,
  mood: { key: 'G', bpm: 108, energy: .8 },
  assets: ['rook', 'agent', 'workstation', 'table', 'phone', 'printer', 'nas', 'gpurig', 'car', 'figure', 'calendar'],
  async build(kit) {
    const S = kit.scene;
    kit.board({ size: 80 });
    S.fog = new THREE.Fog(COL.page, 13, 36);
    const hub = goldRook(kit, V(0, 0, -6.5), .6, 0);
    const ws = kit.make('workstation', { at: V(-4.2, 0, 1.2), rot: .5 });
    const table = kit.make('table', { at: V(2.4, 0, 1.0) });
    const team = [['planner', COL.teal, V(.9, 0, -.1)], ['storyboard', COL.pink, V(3.9, 0, -.1)], ['renderer', COL.green, V(2.4, 0, 2.7)]]
      .map(([n, c, p]) => ({ n, ...agent(kit, n, c, p, { scale: .34 }), p }));
    // little scenes that grow out of the storyboard cards on the table
    const minis = PIECES.map((name, i) => {
      const a = (i / 7) * Math.PI * 2;
      const o = kit.piece(name, V(2.4 + Math.cos(a) * .85, .79, 1.0 + Math.sin(a) * .85), { size: .22 });   // table-top miniatures
      o.userData.s = o.scale.x; return o;
    });
    const fig = figure(kit, V(-3.15, 0, 1.3), -1.71);
    // channel pieces on a calendar strip at the board's edge
    const chans = CHANNELS.map((c, i) => ({ name: c, p: V(-5 + i * 2, 0, 7.5) }));
    const strip = new THREE.Mesh(new THREE.BoxGeometry(12.6, .04, 1.2), kit.toon(0x3a3c32)); strip.position.set(0, .02, 7.5); S.add(strip);
    strip.add(new THREE.LineSegments(new THREE.EdgesGeometry(strip.geometry), kit.inkMat));
    const cal = chans.map((c, i) => kit.make('calendar', { at: c.p.clone().add(V(0, 0, -.1)), scale: .7 }));

    // ---------------------------------------------------------- overlays
    const endc = kit.el(`<div class="t">THE BEST SECRETARY<br>YOU’VE NEVER HAD.</div><div class="m">Your machines, <b>your move.</b></div>
      <div class="bar"><i></i></div><div class="lbl">rendering · frame <span>1214</span> / 1440</div>`, 'endmini');
    kit.caption('Wait.', 2.2, 6, 'big');
    kit.say(6.8, 'you', 'Plan a campaign for Rook.', 'a');
    const chat = pane(kit, { title: '<b>chat</b> · rook', w: 560, body: '<div class="m1"><span class="hi">you</span> · Plan a campaign for Rook.</div>' });
    const plan = pane(kit, { title: '<b>planner</b> · campaign plan', w: 560, body: ['Goal · stars and users', 'Audiences · seven, one clip each',
      'Channels · LinkedIn, Reddit, X, Bluesky, Mastodon, YouTube', 'Schedule · one clip a week', 'Tracking · stars, traffic, replies']
      .map((s, i) => `<div class="pl p${i}">› ${s}</div>`).join('') });
    kit.caption('Storyboarded', 19.5, 23.6, 'label');
    kit.caption('Rendered on the band', 27.8, 31.6, 'label');
    kit.caption('Posted and tracked', 36.0, 39.8, 'label');
    const cards = CLIPS.map((c, i) => kit.el(`<b>${i + 1}</b> ${c}`, 'sb'));
    const queue = pane(kit, { title: '<b>render queue</b> · cachyrig', w: 600, body: CLIPS.map((c, i) =>
      `<div class="q"><span>${i + 1} · ${c}</span><b><i></i></b><em>✓</em></div>`).join('') });
    const stars = pane(kit, { title: '<b>github</b> · Bake-Ware/rook', w: 380, body: `<div class="st">★ <span>0</span></div><div class="dim">stars · tracked daily</div>` });
    const chanEls = chans.map(c => kit.el(c.name, 'chip'));
    const flyers = CLIPS.map((c, i) => kit.el(`▶ ${i + 1}`, 'chip ok'));
    const ticker = kit.el('Share your <span class="w">message</span>.', 'ticker');
    const made = kit.el('made with Rook', 'made');
    const st = document.createElement('style'); st.textContent = `
      .endmini{width:1920px;height:1080px;background:#0e110e;border:3px solid #514b35;text-align:center;padding-top:560px;transform-origin:0 0}
      .endmini .t{font:900 92px/1 var(--sans);text-transform:uppercase}.endmini .m{margin-top:24px;font:400 40px var(--sans);color:var(--soft)}.endmini .m b{color:var(--acc)}
      .endmini .bar{margin:60px auto 0;width:900px;height:14px;border-radius:7px;background:#20261d;overflow:hidden}.endmini .bar i{display:block;height:100%;width:84%;background:#8fc79a}
      .endmini .lbl{margin-top:14px;font:400 26px var(--mono);color:var(--dim)}
      .pl,.m1{opacity:0}.pl{font-size:20px}
      .sb{font:400 18px var(--mono);background:#e8dfc6;color:#2a2418;border-radius:6px;padding:6px 10px;white-space:nowrap;box-shadow:0 8px 24px rgba(0,0,0,.6)}
      .sb b{color:#9a6a2a;margin-right:4px}
      .q{display:flex;gap:12px;align-items:center;font-size:18px;margin:5px 0}.q span{width:250px;white-space:nowrap}.q b{flex:1;height:10px;border-radius:5px;background:#20261d;overflow:hidden}
      .q i{display:block;height:100%;width:0;background:#8fc79a}.q em{font-style:normal;color:var(--ok);opacity:0;width:16px}
      .st{font:900 64px var(--sans);color:var(--hi)}
      .ticker{left:0;width:1920px;top:820px;text-align:center;font:900 96px var(--sans);letter-spacing:-.02em;text-transform:uppercase;text-shadow:0 0 50px rgba(0,0,0,.9)}
      .ticker .w{color:var(--acc)}
      .endcard .tick{color:var(--acc)}
      .made{left:auto;right:60px;top:auto;bottom:50px;font:400 20px var(--mono);letter-spacing:.2em;color:var(--dim)}`;
    document.head.appendChild(st);
    const frameEl = endc.querySelector('.lbl span'), barEl = endc.querySelector('.bar i');
    const PL = [...plan.el.querySelectorAll('.pl')], m1 = chat.el.querySelector('.m1');
    const QB = [...queue.el.querySelectorAll('.q i')], QE = [...queue.el.querySelectorAll('.q em')];
    const starEl = stars.el.querySelector('.st span'), wEl = ticker.querySelector('.w');
    const WORDS = ['message', 'idea', 'story', 'interest', 'work', 'opinion', 'achievement', 'craft', 'passion', 'voice', 'self'];

    kit.pulse(V(0, 0, -6.5), V(2, 0, 1), 13.2, 1.4);
    CLIPS.forEach((c, i) => kit.pulse(V(2, 0, 1), V(-4, 0, 1), 24.4 + i * .5, 1.0, { sound: i % 2 === 0 }));
    kit.events.push({ t: 2.2, type: 'tap' }, { t: 6.6, type: 'tap' }, { t: 15, type: 'shimmer' }, { t: 32.4, type: 'whoosh' }, { t: 40.2, type: 'rush' });

    const desk7 = [V(-5.0, 0, .5), V(-2.9, 1.8, 2.0)], tbl7 = [V(.9, 0, -.5), V(3.9, 1.1, 2.5)];
    kit.camPath([
      { t: 0, pos: [-4.04, 1.06, 1.499], look: [-4.281, 1.06, 1.057], fov: 36 },
      { t: 1.2, pos: [-4.04, 1.06, 1.499], look: [-4.281, 1.06, 1.057], e: 'lin' },
      { t: 6, frame: { on: desk7, yaw: 32, pitch: 14, fill: .8 }, e: 'io' },
      { t: 13.6, frame: { on: desk7, yaw: 24, pitch: 14, fill: .86 }, e: 'lin' },
      { t: 15, frame: { on: tbl7, yaw: 55, pitch: 26, fill: .85 }, e: 'io' },
      { t: 23.6, frame: { on: tbl7, yaw: 115, pitch: 28, fill: .9 }, e: 'lin' },
      { t: 25, frame: { on: desk7, yaw: 42, pitch: 15, fill: .75, look: [.5, 0, -.42] }, e: 'io' },
      { t: 32, frame: { on: desk7, yaw: 34, pitch: 15, fill: .8, look: [.5, 0, -.38] }, e: 'lin' },
      { t: 33.4, pos: [0, 9.5, 17], look: [0, .5, 3.5], e: 'io' },
      { t: 40, pos: [0, 9.2, 16.4], look: [0, .5, 3.5], e: 'lin' },
      { t: 41, pos: [0, 2.6, -.6], look: [0, 1.7, -6.5], e: 'io' },
      { t: 46, pos: [0, 2.7, -.2], look: [0, 1.7, -6.5], e: 'lin' },
    ]);

    return tau => {
      // 1: the end card is on the workstation monitor, mid-render (start full frame, pull back)
      const MC = V(-4.281, 1.06, 1.057), RV = V(.878, 0, -.479);
      const mon = kit.project(MC), mL = kit.project(MC.clone().addScaledVector(RV, -.29)), mR = kit.project(MC.clone().addScaledVector(RV, .29));
      const msc = Math.hypot(mR.x - mL.x, mR.y - mL.y) / 1920;
      const k = ease.io(inv(1.2, 4.2, tau));
      const sc = lerp(1, msc, k);
      kit.show(endc, fade(tau, -.05, 0, 12.6, 13.2));
      endc.style.left = lerp(0, mon.x - 960 * msc, k) + 'px'; endc.style.top = lerp(0, mon.y - 540 * msc, k) + 'px';
      endc.style.transform = `scale(${sc})`;
      frameEl.textContent = Math.min(1440, 1214 + Math.floor(tau * 20)); barEl.style.width = (Math.min(1, (1214 + tau * 20) / 1440) * 100) + '%';
      // 2: the ask and the plan
      chat.fixed(140, 120, fade(tau, 6.4, 6.8, 13.4, 14)); m1.style.opacity = inv(6.8, 7.0, tau);
      plan.fixed(1180, 120, fade(tau, 8.2, 8.6, 13.4, 14));
      PL.forEach((el, i) => { el.style.opacity = inv(8.8 + i * .8, 9.1 + i * .8, tau); });
      fig.rest(); if (tau > 6.4 && tau < 8) { fig.armR.rotation.x = -.45; fig.armR.rotation.z = -.25; fig.foreR.rotation.x = -.9; }
      fig.obj.visible = tau < 33;                          // (a small open-hand gesture above, clear of the monitor)
      // 3: storyboard cards laid on the table; they turn into scenes
      team.forEach((a, i) => { a.chip.place(a.top(), fade(tau, 14.6 + i * .2, 15 + i * .2, 23.4, 23.8)); a.mat.emissiveIntensity = .25 + .4 * fade(tau, 15, 15.5, 23, 24); });
      CLIPS.forEach((c, i) => {
        const a = (i / 7) * Math.PI * 2;
        const t0 = 15.2 + i * .45;
        const u = ease.out(inv(t0, t0 + .6, tau));
        const p = V(2.4 + Math.cos(a) * .85 * u + .9 * (1 - u), .8, 1.0 + Math.sin(a) * .85 * u);
        const show = fade(tau, t0, t0 + .2, 19 + i * .3, 19.4 + i * .3);
        kit.show(cards[i], tau < 23.8 ? show : 0); if (show > .003) kit.pin(cards[i], p, { ay: 1, depth: true, scale: .9 });
        const g = ease.outE(inv(19 + i * .3, 19.6 + i * .3, tau)) * (1 - inv(23.6, 24.2, tau));
        minis[i].scale.setScalar(Math.max(.0001, minis[i].userData.s * g)); minis[i].visible = g > .01;
        minis[i].rotation.y = tau * .6 + i;
      });
      // 4: the render queue fills on the workstation
      queue.fixed(1180, 150, fade(tau, 24.6, 25, 31.8, 32.4));
      QB.forEach((b, i) => { const p = clamp((tau - 25.2 - i * .7) / 1.6); b.style.width = (p * 100) + '%'; QE[i].style.opacity = p >= 1 ? 1 : 0; });
      kit.glowSquare(-4, 1, .7 * fade(tau, 25, 25.5, 31.8, 32.4));
      // 5: clips fly out to the channels on the calendar strip; the real star count
      chans.forEach((c, i) => { kit.show(chanEls[i], fade(tau, 33.2, 33.6, 39.8, 40.2)); if (tau > 33 && tau < 40.4) kit.pin(chanEls[i], c.p.clone().add(V(0, .9, 0)), { ay: 1 }); });
      flyers.forEach((el, i) => {
        const t0 = 34 + i * .45, u = ease.io(inv(t0, t0 + 1.1, tau));
        const ch = chans[i % chans.length];
        const p = V(-4, 2.2, 1).lerp(ch.p.clone().add(V(0, 1.6, 0)), u); p.y += Math.sin(u * Math.PI) * 1.4;
        const a = fade(tau, t0, t0 + .2, 39.8, 40.2);
        kit.show(el, a); if (a > .003) kit.pin(el, p, { ay: .5 });
        if (u >= 1) kit.glowSquare(Math.round(ch.p.x), Math.round(ch.p.z), .6 * (1 - inv(39.8, 40.2, tau)));
      });
      stars.fixed(1440, 120, fade(tau, 36.2, 36.6, 39.8, 40.2));
      starEl.textContent = Math.round(STARS * ease.out(inv(36.6, 38.2, tau)));
      // 6: back to the gold rook: it turns once; the ticker lands on "self"
      hub.obj.rotation.y = tau > 40.6 ? ease.io(inv(40.6, 44, tau)) * Math.PI * 2 : 0;
      kit.show(made, fade(tau, 41, 41.4, 45.6, 46) * .9);
      kit.show(ticker, 0);
      // the end card: "Share your ..." holds still while the word changes faster and faster, landing on "self."
      const tickEl = document.querySelector('.endcard .tick');
      if (tickEl) {
        if (!tickEl.dataset.w) { tickEl.style.display = 'inline-block'; tickEl.style.textAlign = 'left'; tickEl.style.whiteSpace = 'nowrap';
          tickEl.dataset.w = tickEl.offsetWidth; if (+tickEl.dataset.w) tickEl.style.width = tickEl.dataset.w + 'px'; }
        let idx = 0; const t = tau - 46.5; let acc = 0, d = .42;
        while (t > 0 && idx < WORDS.length - 1 && acc + d < t) { acc += d; d *= .8; idx++; }
        tickEl.textContent = WORDS[tau < 46.5 ? 0 : idx] + '.';
      }
      kit.glowSquare(0, -6, .5);
    };
  },
};
