// Rook promo soundtrack, synthesized with the Web Audio API in an
// OfflineAudioContext (deterministic, faster than real time). 120 BPM, so one
// bar is 2 s and every scene change in promo.html lands on a bar line.
// Structure: 0-8 intro (pad, riser, hit at 4, arp) · 8-38 groove (drums from 8,
// snare from 16) · 38-42 breakdown + riser · 42 impact · 42-48 outro, fade.

export async function renderSoundtrack(duration = 48, sampleRate = 48000) {
  const ctx = new OfflineAudioContext(2, Math.ceil(duration * sampleRate), sampleRate);
  const BPM = 120, BEAT = 60 / BPM, BAR = BEAT * 4;
  const midi = n => 440 * Math.pow(2, (n - 69) / 12);
  // A minor: Am F C G, one chord per bar
  const CHORDS = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]];
  const chordAt = t => CHORDS[Math.floor(t / BAR) % 4];

  // ---- shared noise + reverb impulse (seeded so every render is identical)
  let seed = 1234567;
  const rnd = () => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  const noise = ctx.createBuffer(1, sampleRate * 2, sampleRate);
  noise.getChannelData(0).forEach((_, i, a) => { a[i] = rnd() * 2 - 1; });
  const ir = ctx.createBuffer(2, sampleRate * 3.2, sampleRate);
  for (let ch = 0; ch < 2; ch++) {
    const d = ir.getChannelData(ch);
    for (let i = 0; i < d.length; i++) d[i] = (rnd() * 2 - 1) * Math.pow(1 - i / d.length, 3.2);
  }

  // ---- master chain: bus -> compressor -> gain -> out, plus a reverb send
  const comp = ctx.createDynamicsCompressor();
  comp.threshold.value = -16; comp.knee.value = 10; comp.ratio.value = 4; comp.attack.value = .004; comp.release.value = .2;
  const master = ctx.createGain(); master.gain.value = .9;
  comp.connect(master).connect(ctx.destination);
  master.gain.setValueAtTime(.9, duration - 3); master.gain.linearRampToValueAtTime(0, duration - .05);
  const bus = ctx.createGain(); bus.connect(comp);
  const verb = ctx.createConvolver(); verb.buffer = ir;
  const verbIn = ctx.createGain(); verbIn.gain.value = .5; verbIn.connect(verb).connect(comp);

  // sidechain-style pump: music bus ducks on every kick (from bar 4)
  const pump = ctx.createGain(); pump.connect(bus);
  for (let t = 8; t < 38; t += BEAT) { pump.gain.setValueAtTime(.45, t); pump.gain.setTargetAtTime(1, t + .02, .09); }
  for (let t = 42; t < 46; t += BEAT) { pump.gain.setValueAtTime(.5, t); pump.gain.setTargetAtTime(1, t + .02, .1); }

  const env = (param, t, a, peak, d, sustain, rel, end) => {
    param.setValueAtTime(0, t); param.linearRampToValueAtTime(peak, t + a);
    param.setTargetAtTime(sustain, t + a, d); param.setTargetAtTime(0, end, rel);
  };

  // ---- pad: 3 detuned saws per chord tone, slow filter sweep
  const padFilter = ctx.createBiquadFilter(); padFilter.type = 'lowpass'; padFilter.Q.value = .7;
  padFilter.frequency.setValueAtTime(300, 0);
  padFilter.frequency.exponentialRampToValueAtTime(2400, 7.5);
  padFilter.frequency.setValueAtTime(1600, 8);
  padFilter.frequency.exponentialRampToValueAtTime(700, 38);
  padFilter.frequency.exponentialRampToValueAtTime(3200, 42);
  padFilter.frequency.exponentialRampToValueAtTime(900, duration);
  const padGain = ctx.createGain(); padGain.gain.value = .06;
  padFilter.connect(padGain); padGain.connect(pump); padGain.connect(verbIn);
  for (let bar = 0; bar * BAR < duration; bar++) {
    const t = bar * BAR, notes = chordAt(t);
    for (const n of [...notes, notes[0] - 12]) for (const det of [-9, 0, 8]) {
      const o = ctx.createOscillator(); o.type = 'sawtooth'; o.frequency.value = midi(n); o.detune.value = det;
      const g = ctx.createGain();
      env(g.gain, t, .5, .5, .5, .42, .35, t + BAR - .05);
      o.connect(g).connect(padFilter); o.start(t); o.stop(t + BAR + 1.5);
    }
  }

  // ---- bass: 8th notes on the root, from 8 s (and in the outro)
  const bassF = ctx.createBiquadFilter(); bassF.type = 'lowpass'; bassF.frequency.value = 520; bassF.Q.value = 3;
  const bassG = ctx.createGain(); bassG.gain.value = .32; bassF.connect(bassG).connect(bus);
  const bassOn = t => (t >= 8 && t < 38) || (t >= 42 && t < 46);
  for (let t = 8; t < duration; t += BEAT / 2) {
    if (!bassOn(t)) continue;
    const root = midi(chordAt(t)[0] - 24);
    for (const [type, mul, lvl] of [['sawtooth', 1, .6], ['sine', .5, .9]]) {
      const o = ctx.createOscillator(); o.type = type; o.frequency.value = root * mul * 2;
      const g = ctx.createGain(); env(g.gain, t, .005, lvl, .08, lvl * .5, .03, t + BEAT / 2 - .03);
      o.connect(g).connect(bassF); o.start(t); o.stop(t + BEAT / 2 + .2);
    }
  }

  // ---- arp: 16th-note plucks over the chord, two octaves, with a synced delay
  const arpF = ctx.createBiquadFilter(); arpF.type = 'lowpass'; arpF.Q.value = 5;
  arpF.frequency.setValueAtTime(1200, 4); arpF.frequency.exponentialRampToValueAtTime(4200, 30);
  arpF.frequency.exponentialRampToValueAtTime(1500, 38); arpF.frequency.exponentialRampToValueAtTime(5000, 42);
  const arpG = ctx.createGain(); arpG.gain.value = .09;
  const delay = ctx.createDelay(1); delay.delayTime.value = BEAT * .75;
  const fb = ctx.createGain(); fb.gain.value = .38;
  arpF.connect(arpG); arpG.connect(pump); arpG.connect(delay); delay.connect(fb).connect(delay);
  delay.connect(verbIn); const dOut = ctx.createGain(); dOut.gain.value = .5; delay.connect(dOut).connect(pump);
  const PATTERN = [0, 1, 2, 3, 2, 1, 4, 2];
  for (let t = 4, i = 0; t < 46; t += BEAT / 4, i++) {
    if (t >= 38 && t < 40) continue;                 // breathe before the riser
    const c = chordAt(t), tones = [c[0], c[1], c[2], c[0] + 12, c[1] + 12];
    const o = ctx.createOscillator(); o.type = 'square'; o.frequency.value = midi(tones[PATTERN[i % 8]] + 12);
    const g = ctx.createGain(); const accent = i % 4 === 0 ? 1 : .6;
    env(g.gain, t, .003, accent, .05, 0, .02, t + .12);
    o.connect(g).connect(arpF); o.start(t); o.stop(t + .3);
  }

  // ---- drums
  const drumBus = ctx.createGain(); drumBus.gain.value = .9; drumBus.connect(bus);
  const kick = t => {
    const o = ctx.createOscillator(); o.frequency.setValueAtTime(160, t); o.frequency.exponentialRampToValueAtTime(42, t + .12);
    const g = ctx.createGain(); g.gain.setValueAtTime(1, t); g.gain.exponentialRampToValueAtTime(.001, t + .42);
    o.connect(g).connect(drumBus); o.start(t); o.stop(t + .45);
  };
  const noiseHit = (t, type, freq, q, lvl, dec, dest = drumBus) => {
    const s = ctx.createBufferSource(); s.buffer = noise; s.playbackRate.value = 1 + rnd() * .1;
    const f = ctx.createBiquadFilter(); f.type = type; f.frequency.value = freq; f.Q.value = q;
    const g = ctx.createGain(); g.gain.setValueAtTime(lvl, t); g.gain.exponentialRampToValueAtTime(.001, t + dec);
    s.connect(f).connect(g).connect(dest); s.start(t, rnd()); s.stop(t + dec + .05);
  };
  const snare = t => {
    noiseHit(t, 'bandpass', 1900, .8, .55, .22); noiseHit(t, 'highpass', 5000, .5, .25, .12);
    const o = ctx.createOscillator(); o.frequency.setValueAtTime(230, t); o.frequency.exponentialRampToValueAtTime(160, t + .08);
    const g = ctx.createGain(); g.gain.setValueAtTime(.4, t); g.gain.exponentialRampToValueAtTime(.001, t + .12);
    o.connect(g).connect(drumBus); o.start(t); o.stop(t + .15);
    const sv = ctx.createGain(); sv.gain.value = .5; noiseHit(t, 'bandpass', 2400, .7, .25, .3, sv); sv.connect(verbIn);
  };
  for (let t = 8; t < 38; t += BEAT) kick(t);
  for (let t = 38; t < 40; t += BEAT) kick(t);                      // breakdown: kick only
  for (let t = 40; t < 42; t += BEAT / (t < 41 ? 2 : 4)) snare(t);  // snare roll into the drop
  for (let t = 42; t < 46; t += BEAT) kick(t);
  for (let t = 16 + BEAT; t < 38; t += BEAT * 2) snare(t);
  for (let t = 8; t < 38; t += BEAT / 2) noiseHit(t + (Math.round(t / (BEAT / 2)) % 2 ? .01 : 0), 'highpass', 8000, .7,
    Math.round(t / (BEAT / 2)) % 2 ? .16 : .08, .05);
  for (let t = 24; t < 38; t += BEAT / 4) noiseHit(t, 'highpass', 10000, .7, .05, .03);    // 16th shimmer
  for (let t = 42; t < 46; t += BEAT / 2) noiseHit(t, 'highpass', 8000, .7, .1, .05);

  // ---- risers, impacts, whooshes
  const riser = (t0, t1, lvl = .35) => {
    const s = ctx.createBufferSource(); s.buffer = noise; s.loop = true;
    const f = ctx.createBiquadFilter(); f.type = 'bandpass'; f.Q.value = 6;
    f.frequency.setValueAtTime(300, t0); f.frequency.exponentialRampToValueAtTime(9000, t1);
    const g = ctx.createGain(); g.gain.setValueAtTime(.0001, t0); g.gain.exponentialRampToValueAtTime(lvl, t1 - .02); g.gain.linearRampToValueAtTime(0, t1);
    s.connect(f).connect(g).connect(bus); g.connect(verbIn); s.start(t0); s.stop(t1 + .05);
    const o = ctx.createOscillator(); o.type = 'sawtooth'; o.frequency.setValueAtTime(110, t0); o.frequency.exponentialRampToValueAtTime(880, t1);
    const og = ctx.createGain(); og.gain.setValueAtTime(.0001, t0); og.gain.exponentialRampToValueAtTime(lvl * .12, t1 - .02); og.gain.linearRampToValueAtTime(0, t1);
    const of = ctx.createBiquadFilter(); of.type = 'lowpass'; of.frequency.value = 2500;
    o.connect(of).connect(og).connect(bus); o.start(t0); o.stop(t1 + .05);
  };
  const impact = (t, lvl = 1) => {
    const o = ctx.createOscillator(); o.frequency.setValueAtTime(90, t); o.frequency.exponentialRampToValueAtTime(28, t + 1.4);
    const g = ctx.createGain(); g.gain.setValueAtTime(lvl, t); g.gain.exponentialRampToValueAtTime(.001, t + 1.8);
    o.connect(g).connect(bus); o.start(t); o.stop(t + 1.9);
    noiseHit(t, 'lowpass', 3000, .5, .6 * lvl, 1.6, verbIn);           // crash wash into the reverb
    noiseHit(t, 'highpass', 4000, .5, .3 * lvl, .9);
  };
  const whoosh = (t, lvl = .18) => {
    const s = ctx.createBufferSource(); s.buffer = noise;
    const f = ctx.createBiquadFilter(); f.type = 'bandpass'; f.Q.value = 3;
    f.frequency.setValueAtTime(600, t - .5); f.frequency.exponentialRampToValueAtTime(5000, t); f.frequency.exponentialRampToValueAtTime(900, t + .4);
    const g = ctx.createGain(); g.gain.setValueAtTime(.0001, t - .5); g.gain.exponentialRampToValueAtTime(lvl, t); g.gain.exponentialRampToValueAtTime(.0001, t + .45);
    s.connect(f).connect(g).connect(bus); g.connect(verbIn); s.start(t - .5, rnd()); s.stop(t + .5);
  };
  riser(1.5, 4, .25); impact(4, .8);
  riser(6, 8, .18); impact(8, .45);
  for (const t of [16, 24, 32, 38]) whoosh(t);
  riser(39, 42, .45); impact(42, 1.1);

  const buf = await ctx.startRendering();
  return encodeWav(buf);
}

function encodeWav(buf) {
  const ch = buf.numberOfChannels, n = buf.length, rate = buf.sampleRate;
  const data = new DataView(new ArrayBuffer(44 + n * ch * 2));
  const str = (o, s) => [...s].forEach((c, i) => data.setUint8(o + i, c.charCodeAt(0)));
  str(0, 'RIFF'); data.setUint32(4, 36 + n * ch * 2, true); str(8, 'WAVE'); str(12, 'fmt ');
  data.setUint32(16, 16, true); data.setUint16(20, 1, true); data.setUint16(22, ch, true);
  data.setUint32(24, rate, true); data.setUint32(28, rate * ch * 2, true); data.setUint16(32, ch * 2, true);
  data.setUint16(34, 16, true); str(36, 'data'); data.setUint32(40, n * ch * 2, true);
  const chans = [...Array(ch)].map((_, c) => buf.getChannelData(c));
  let peak = 0; for (const c of chans) for (const v of c) peak = Math.max(peak, Math.abs(v));
  const norm = peak > .98 ? .98 / peak : 1;       // never clip
  for (let i = 0, o = 44; i < n; i++) for (let c = 0; c < ch; c++, o += 2)
    data.setInt16(o, Math.max(-1, Math.min(1, chans[c][i] * norm)) * 32767, true);
  // base64 in chunks (fast enough for ~9 MB)
  const bytes = new Uint8Array(data.buffer); let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return { wav: btoa(bin), peak };
}
