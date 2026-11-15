// A public page (page.public): anyone opens /public/<page> without signing in. Just
// the page, read-only: no sidebar, no chat. Its calls go with a public page
// token and act as whoever made it public, within the page's own grants.
"use strict";

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pid = decodeURIComponent(location.pathname.replace(/^\/public\//, ""));
let cur = null;

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json" } };
  const r = await fetch(path, opts);
  try {
    return await r.json();
  } catch (x) {
    return { ok: false, error: "HTTP " + r.status };
  }
}

function message(text) {
  const m = $("#msg");
  m.textContent = text;
  m.className = "bad";
  m.hidden = false;
}

function toFrame(msg) {
  if (cur && cur.frame.contentWindow) {
    msg.rook = 1;
    cur.frame.contentWindow.postMessage(msg, "*");
  }
}

window.addEventListener("message", async (e) => {
  const m = e.data;
  if (!cur || e.source !== cur.frame.contentWindow || !m || m.rook !== 1) return;
  if (m.kind === "call") {
    let reply;
    try {
      reply = await api("/web/api/public/page", { token: cur.token, cap: m.cap, args: m.args,
        why: m.why || undefined, worker: m.worker || undefined });
    } catch (x) {
      reply = { ok: false, error: String((x && x.message) || x) };
    }
    toFrame({ kind: "reply", id: m.id, reply });
  } else if (m.kind === "listen" && !cur.listening) {
    cur.listening = true;
    listen();
  } else if (m.kind === "open" && /^[a-z0-9][a-z0-9_-]*\/[a-z0-9][a-z0-9_-]*$/.test(String(m.page))) {
    location.href = "/public/" + m.page;          // another public page; a private one says so
  }
});

async function listen() {
  let since = "";
  for (;;) {
    try {
      const r = await api(`/web/api/public/events?token=${encodeURIComponent(cur.token)}&since=${since}`);
      if (!r.ok) {
        await sleep(5000);
        continue;
      }
      for (const ev of r.events) toFrame({ kind: "event", event: ev.event, payload: ev.payload });
      since = r.seq;
    } catch (x) {
      await sleep(3000);
    }
  }
}

(async function start() {
  const r = await api("/web/api/public/open", { page: pid });
  if (!r.ok) return message(r.error || "This page isn't public.");
  document.title = $("#title").textContent = r.page.title;
  const frame = document.createElement("iframe");
  frame.title = r.page.title;
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.src = r.frame;
  let loads = 0;
  frame.addEventListener("load", () => {
    if (++loads > 1) {                     // it navigated away: not the page any more
      frame.remove();
      message("This page tried to leave the hub and was closed.");
    }
  });
  cur = { token: r.token, frame, listening: false };
  $("#stage").prepend(frame);
})();
