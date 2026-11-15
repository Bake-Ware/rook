// Your account page's look and feel (Rookie's style, agent.look): the
// agents package's part of the account page. Loaded on every shell page; it
// runs on /account only.
"use strict";

(function () {
  if (location.pathname !== "/account") return;
  const $ = (id) => document.getElementById(id);
  let csrf = "";

  async function lookCall(args) {
    const r = await fetch("/web/api/call", { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Rook-CSRF": csrf },
      body: JSON.stringify({ cap: "agent.look", args, why: "The person changed their look and feel." }) });
    const j = await r.json().catch(() => ({}));
    if (!j.ok) throw new Error(j.error || "That didn't work.");
    return j.result;
  }

  // Sound, Rookie's style and theme are yours; its name is the house agent's
  // (owners rename it). Previews take the theme you have.
  let look = null;

  // A still picture of Rookie (Rookie.bake): no live Rookie on this page.
  function thumb(skin, theme) {
    if (!window.Rookie || !Rookie.bake || skin === "none" || !Rookie.SKINS.includes(skin)) return document.createElement("span");
    const c = document.createElement("canvas");
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
    for (const n of document.querySelectorAll(".agent-name")) n.textContent = look.name;      // never a fixed "Rookie"
    pickers();
  }

  async function looks() {
    const r = await fetch("/web/api/me", { credentials: "same-origin" });
    if (r.status === 401) return;
    csrf = (await r.json()).csrf;
    try { look = await lookCall({}); } catch (x) { return; }
    const box = document.createElement("div");
    box.id = "look";
    box.innerHTML = `
    <h2>Look and feel</h2>
    <label><input type="checkbox" id="sound"> Sound</label>
    <div class="muted"><span class="agent-name">Rookie</span>'s style</div>
    <div id="looks" class="looks"></div>
    <div class="muted"><span class="agent-name">Rookie</span>'s theme</div>
    <div id="themes" class="looks"></div>
    <form id="name-form" class="login inner">
      <label for="name">Your house agent's name</label>
      <input id="name" type="text" maxlength="40">
      <button type="submit">Rename</button>
    </form>
    <div id="look-out" class="bad" aria-live="polite"></div>`;
    $("connector").before(box);
    $("sound").onchange = () => change({ sound: $("sound").checked });
    $("name-form").onsubmit = (e) => {
      e.preventDefault();
      if ($("name").value.trim()) change({ name: $("name").value.trim() });
    };
    render();
  }

  looks();
})();
