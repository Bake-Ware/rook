"""Rookie's variants (Bake, 2026-11-10: "have variants of the same actions so
it's not just doing the same 10 things over and over again"): a state keeps
one meaning and gets a pool of animations; Rookie picks at random, never the
same one twice running; a skin weights its pool; a variant a skin lacks plays
as the base; Rookie.variant(state, name) forces one."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOKIE = Path(__file__).resolve().parents[1] / "rook/plugins/agents/shell/rookie.js"

SCRIPT = r"""
const fs = require("fs");
// a page with nothing on it: every canvas call is accepted and counted
const calls = { n: 0 };
const ctx = new Proxy({}, { get: (_, k) => (k === "canvas" ? {} : (...a) => { calls.n++; }), set: () => true });
const canvas = () => ({ getContext: () => ctx, style: {}, remove() {}, width: 0, height: 0 });
const store = {};
globalThis.sessionStorage = { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; } };
globalThis.document = { createElement: canvas };
globalThis.window = {};
eval(fs.readFileSync(process.argv[2], "utf8"));
const R = window.Rookie, out = {};
const expect = (name, got, want) => { if (JSON.stringify(got) !== JSON.stringify(want)) out[name] = [got, want]; };
const pool = R.VARIANTS.enter;
expect("base first", pool[0], "transporter");
expect("named ones", ["smoke", "ring", "oval", "trapdoor"].every((n) => pool.includes(n)), true);
expect("exit has a pool", R.VARIANTS.exit.length >= 5, true);
const bot = R.mount({ appendChild() {} }, { skin: "pixel", state: "hidden" });
// never the same one twice running, and every one turns up
for (const st of ["enter", "exit"]) {
  const seen = {}; let prev = null, twice = 0;
  for (let i = 0; i < 600; i++) { bot.set(st); if (bot.variant === prev) twice++; prev = bot.variant; seen[prev] = (seen[prev] || 0) + 1; }
  expect(st + " twice running", twice, 0);
  expect(st + " all seen", Object.keys(seen).sort(), R.VARIANTS[st].slice().sort());
}
expect("the last pick is kept for the next page", JSON.parse(store["rookie.last"]).exit, bot.variant);
// a skin weights its pool: comic throws the smoke bomb most
bot.setSkin("comic");
const n = {}; for (let i = 0; i < 2000; i++) { bot.enter(); n[bot.variant] = (n[bot.variant] || 0) + 1; }
expect("comic favours smoke", n.smoke > n.ring * 1.5 && n.smoke > n.oval * 1.5, true);
// a skin that lacks the variants plays the base, forced or not
bot.setSkin("none");
for (let i = 0; i < 20; i++) { bot.enter(); expect("none: base " + i, bot.variant, "transporter"); }
expect("force", R.variant("enter", "ring"), "ring");
bot.enter(); expect("none: forced one it lacks -> base", bot.variant, "transporter");
bot.setSkin("flat");
for (let i = 0; i < 5; i++) { bot.enter(); expect("forced " + i, bot.variant, "ring"); }
bot.exit(); expect("exit is not forced", R.VARIANTS.exit.includes(bot.variant), true);
expect("unknown name changes nothing", R.variant("enter", "nope"), "ring");
expect("let go", R.variant("enter", null), null);
expect("no pool for idle", R.variant("idle", "x"), null);
bot.set("thinking"); expect("no variant outside a pool", bot.variant, null);
// every skin draws every variant, every frame, without an error; and the sheet has a row for each
for (const skin of R.SKINS) for (const st of ["enter", "exit"]) for (const v of R.VARIANTS[st]) {
  const row = v === R.VARIANTS[st][0] ? st : st + "-" + v;
  expect("frames " + row, R.FRAMES[row], 14);
  try { calls.n = 0; R.bake({ skin, rows: [row] }); if (!calls.n) out["drew nothing " + skin + " " + row] = 1; }
  catch (e) { out["draw " + skin + " " + row] = String(e); }
}
// a variant's own sound, else the state's
expect("sounds", ["enter-smoke", "exit-ring", "enter", "exit"].every((k) => R.sfx.names.includes(k)), true);
bot.destroy();
console.log(JSON.stringify(out));
"""


def test_variants_pick_weigh_fall_back_and_draw(tmp_path):
    node = shutil.which("node")
    if node is None:
        pytest.skip("no node")
    script = tmp_path / "variants.js"
    script.write_text(SCRIPT)
    r = subprocess.run([node, str(script), str(ROOKIE)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == {}
