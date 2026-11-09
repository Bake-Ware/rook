// The account page: who you are, change your password, your look and feel
// (Rookie's style, page.look), and the address claude.ai's connector takes. Served outside the page sandbox, like /login.
"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "";

function noPassword(none) {
  $("none").hidden = !none;
  $("current").hidden = $("current-label").hidden = true;     // never asked for
  $("current").required = false;
  $("change-title").textContent = none ? "Set a password" : "Change your password";
  $("change-btn").textContent = none ? "Set password" : "Change password";
}

async function load() {
  const r = await fetch("/web/api/me", { credentials: "same-origin" });
  if (r.status === 401) { location.href = "/login"; return; }
  const me = await r.json();
  csrf = me.csrf;
  $("who").textContent = me.person.replace(/^person:/, "");
  $("first").hidden = !/[?&]first=1/.test(location.search);
  noPassword(!me.password);
  if (me.connector) {
    $("mcp-url").textContent = me.connector;
    $("connector").hidden = false;
  }
}

async function lookCall(args) {
  const r = await fetch("/web/api/call", { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Rook-CSRF": csrf },
    body: JSON.stringify({ cap: "page.look", args, why: "The person changed their look and feel." }) });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(j.error || "That didn't work.");
  return j.result;
}

// Sound, Rookie's style and theme are yours; its name is the house agent's
// (owners rename it). Previews take the theme you have.
let look = null;

// A still picture of Rookie (Rookie.bake): no live Rookie on this page.
function thumb(skin, theme) {
  const c = document.createElement("canvas");
  if (!window.Rookie || !Rookie.bake || skin === "none" || !Rookie.SKINS.includes(skin)) return c;
  const { canvas, manifest } = Rookie.bake({ skin, theme, scale: 2, rows: ["happy"] });
  const [w, h] = manifest.cell;
  c.width = w;
  c.height = h;
  c.getContext("2d").drawImage(canvas, 0, 0, w, h, 0, 0, w, h);
  return c;
}

function pickers() {
  for (const [box, items, key] of [[$("looks"), look.styles, "rookie"], [$("themes"), look.themes, "theme"]]) {
    box.replaceChildren();
    for (const k of items) {
      const b = document.createElement("button");
      b.type = "button";
      b.title = k;
      b.className = k === look[key] ? "on" : "";
      const face = document.createElement("div");
      face.className = "face";
      const name = document.createElement("span");
      name.textContent = k === "none" ? "none (hide it)" : k;
      b.append(face, name);
      const skin = key === "rookie" ? k : (look.rookie === "none" ? "pixel" : look.rookie);
      const theme = key === "theme" ? k : look.theme;
      face.append(thumb(skin, theme));
      b.onclick = () => change({ [key]: k });
      box.append(b);
    }
  }
}

async function change(args) {
  try {
    look = await lookCall(args);
    $("look-out").textContent = "";
  } catch (x) {
    $("look-out").textContent = x.message;
  }
  render();
}

function render() {
  $("sound").checked = !!look.sound;
  $("name").value = look.name;
  pickers();
}

async function looks() {
  try { look = await lookCall({}); } catch (x) { $("look").hidden = true; return; }
  $("sound").onchange = () => change({ sound: $("sound").checked });
  $("name-form").onsubmit = (e) => {
    e.preventDefault();
    if ($("name").value.trim()) change({ name: $("name").value.trim() });
  };
  render();
}

$("change").addEventListener("submit", async (e) => {
  e.preventDefault();
  const out = $("out");
  out.className = "muted";
  if ($("new").value !== $("again").value) {
    out.className = "bad";
    out.textContent = "The new passwords don't match.";
    return;
  }
  const r = await fetch("/web/api/password", { method: "POST", credentials: "same-origin",
    headers: { "Content-Type": "application/json", "X-Rook-CSRF": csrf },
    body: JSON.stringify({ current: $("current").value, password: $("new").value }) });
  const j = await r.json().catch(() => ({}));
  if (j.ok) {
    out.textContent = "Password set. Sign in with your user name and this password from now on.";
    $("change").reset();
    $("first").hidden = true;
    noPassword(false);
  } else {
    out.className = "bad";
    out.textContent = j.error || "That didn't work.";
  }
});

load().then(looks);
