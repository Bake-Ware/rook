// The account page: who you are, change your password, and the address
// claude.ai's connector takes. Served outside the page sandbox, like /login.
"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "";

async function load() {
  const r = await fetch("/web/api/me", { credentials: "same-origin" });
  if (r.status === 401) { location.href = "/login"; return; }
  const me = await r.json();
  csrf = me.csrf;
  $("who").textContent = me.person.replace(/^person:/, "");
  $("first").hidden = !/[?&]first=1/.test(location.search);
  if (me.connector) {
    $("mcp-url").textContent = me.connector;
    $("connector").hidden = false;
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
    out.textContent = "Password changed.";
    $("change").reset();
    $("first").hidden = true;
  } else {
    out.className = "bad";
    out.textContent = j.error || "That didn't work.";
  }
});

load();
