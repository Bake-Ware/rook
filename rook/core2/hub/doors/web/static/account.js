// The account page: who you are, change your password, your look and feel
// (Rookie's style, page.look), and the address claude.ai's connector takes. Served outside the page sandbox, like /login.
"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "";

function noPassword(none) {
  $("none").hidden = !none;
  $("current").hidden = $("current-label").hidden = none;
  $("current").required = !none;
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

async function looks() {
  let look;
  try { look = await lookCall({}); } catch (x) { $("look").hidden = true; return; }
  const box = $("looks");
  for (const k of look.styles) {
    const b = document.createElement("button");
    b.type = "button";
    b.title = k;
    b.className = k === look.rookie ? "on" : "";
    const face = document.createElement("div");
    face.className = "face";
    const name = document.createElement("span");
    name.textContent = k === "none" ? "none (hide it)" : k;
    b.append(face, name);
    if (k !== "none" && window.Rookie && Rookie.SKINS.includes(k)) Rookie.mount(face, { scale: 2, skin: k });
    b.onclick = async () => {
      try {
        const now = await lookCall({ rookie: k });
        for (const o of box.children) o.className = o.title === now.rookie ? "on" : "";
        $("look-out").textContent = "";
      } catch (x) {
        $("look-out").textContent = x.message;
      }
    };
    box.append(b);
  }
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
    out.textContent = $("current").hidden ? "Password set. Sign in with your user name and this password from now on." : "Password changed.";
    $("change").reset();
    $("first").hidden = true;
    noPassword(false);
  } else {
    out.className = "bad";
    out.textContent = j.error || "That didn't work.";
  }
});

load().then(looks);
