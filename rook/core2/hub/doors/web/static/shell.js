// The hub site's shell: lists pages, opens one at a time in a sandboxed
// frame, and carries that frame's calls and events (see bridge.js). The
// shell's own calls are pages.* only; a page's calls go with its token.
"use strict";

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let csrf = "";
let current = null;        // {page, token, frame, listening}

async function api(path, body) {
  const opts = { credentials: "same-origin" };
  if (body !== undefined) {
    Object.assign(opts, { method: "POST", body: JSON.stringify(body),
      headers: { "Content-Type": "application/json", "X-Rook-CSRF": csrf } });
  }
  const r = await fetch(path, opts);
  if (r.status === 401) {
    location.href = "/login";
    throw new Error("signed out");
  }
  return r.json();
}

async function call(cap, args) {
  const r = await api("/web/api/call", { cap, args: args || {} });
  if (!r.ok) throw new Error(r.error || "failed");
  return r.result;
}

function el(tag, attrs, text) {
  const e = document.createElement(tag);
  Object.assign(e, attrs || {});
  if (text !== undefined) e.textContent = text;
  return e;
}

function message(text, bad) {
  const m = $("#msg");
  m.textContent = text;
  m.className = bad ? "bad" : "";
  m.hidden = false;
}

// -- the page list ---------------------------------------------------------

let pages = [];

async function loadNav() {
  const [live, gone] = await Promise.all([call("pages.list"), call("pages.list", { archived: true })]);
  pages = live.pages;
  const box = $("#pages");
  box.replaceChildren();
  let section = null;
  for (const p of pages) {
    if (p.section !== section) {
      section = p.section;
      box.append(el("div", { className: "section" }, section));
    }
    const a = el("a", { href: "#/" + p.page }, p.title);
    a.dataset.page = p.page;
    box.append(a);
  }
  const list = $("#archived ul");
  list.replaceChildren();
  for (const p of gone.pages) {
    const li = el("li");
    const restore = el("button", { className: "link" }, "Restore");
    restore.onclick = async () => {
      await call("pages.restore", { page: p.page });
      await loadNav();
      location.hash = "#/" + p.page;
    };
    li.append(el("span", {}, p.title), restore);
    list.append(li);
  }
  $("#archived").hidden = gone.pages.length === 0;
  mark();
}

function mark() {
  for (const a of document.querySelectorAll("#nav a")) {
    a.classList.toggle("on", !!current && a.dataset.page === current.page);
  }
}

function wanted() {
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  if (h) return h;
  const first = pages.find((p) => p.page === "hub/workers") || pages[0];
  return first ? first.page : "";
}

// -- one open page -----------------------------------------------------------

let opening = 0;

async function open(pid) {
  const n = ++opening;
  document.body.classList.remove("nav-open");
  $("#note").hidden = true;
  if (current && current.frame) current.frame.remove();
  current = null;
  $("#title").textContent = "";
  $("#note-btn").hidden = $("#archive-btn").hidden = true;
  if (!pid) return message("No pages yet.");
  const r = await api("/web/api/open", { page: pid });
  if (n !== opening) return;      // another page was asked for meanwhile
  if (!r.ok) return message(r.error || "This page can't be opened.", true);
  $("#msg").hidden = true;
  const frame = el("iframe", { title: r.page.title });
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.src = r.frame;
  $("#stage").prepend(frame);
  current = { page: pid, token: r.token, frame, listening: false, made: r.page.made };
  $("#title").textContent = r.page.title;
  $("#note-btn").hidden = $("#archive-btn").hidden = false;
  mark();
}

function toFrame(cur, msg) {
  if (cur === current && cur.frame.contentWindow) {
    msg.rook = 1;
    cur.frame.contentWindow.postMessage(msg, "*");
  }
}

window.addEventListener("message", async (e) => {
  const cur = current, m = e.data;
  if (!cur || e.source !== cur.frame.contentWindow || !m || m.rook !== 1) return;
  if (m.kind === "call") {
    let reply;
    try {
      reply = await api("/web/api/page", { token: cur.token, cap: m.cap, args: m.args,
        why: m.why || undefined, worker: m.worker || undefined });
    } catch (x) {
      reply = { ok: false, error: String((x && x.message) || x) };
    }
    toFrame(cur, { kind: "reply", id: m.id, reply });
  } else if (m.kind === "listen" && !cur.listening) {
    cur.listening = true;
    listen(cur);
  }
});

async function listen(cur) {
  let since = "";
  while (cur === current) {
    try {
      const r = await api(`/web/api/events?token=${encodeURIComponent(cur.token)}&since=${since}`);
      if (!r.ok) {
        await sleep(5000);
        continue;
      }
      for (const ev of r.events) toFrame(cur, { kind: "event", event: ev.event, payload: ev.payload });
      since = r.seq;
    } catch (x) {
      await sleep(3000);
    }
  }
}

// -- the bar -------------------------------------------------------------------

$("#note-btn").onclick = async () => {
  const box = $("#note");
  if (!box.hidden) {
    box.hidden = true;
    return;
  }
  const d = await call("pages.get", { page: current.page });
  box.replaceChildren();
  if (d.made) {
    const a = d.author || {};
    const who = [a.agent, a.person].filter(Boolean).join(" for ");
    box.append(el("div", { className: "section" }, "Made by " + (who || "?")),
      el("pre", {}, d.note || ""));
  } else {
    box.append(el("div", { className: "section" }, "From a plugin"),
      el("p", {}, `${d.plugin.name} ${d.plugin.version}` + (d.plugin.builtin ? " (built in)" : "")));
  }
  box.append(el("div", { className: "section" }, "May call"), el("pre", {}, (d.caps || []).join("\n") || "nothing"));
  if ((d.events || []).length) box.append(el("div", { className: "section" }, "Hears"), el("pre", {}, d.events.join("\n")));
  box.hidden = false;
};

$("#archive-btn").onclick = async () => {
  const pid = current && current.page;
  if (!pid) return;
  try {
    await call("pages.archive", { page: pid });
  } catch (x) {
    return message(x.message, true);
  }
  history.replaceState(null, "", location.pathname);
  await loadNav();
  open(wanted());
};

$("#menu").onclick = () => document.body.classList.toggle("nav-open");
$("#pages").onclick = () => document.body.classList.remove("nav-open");

$("#signout").onclick = async () => {
  await api("/logout", {}).catch(() => {});
  location.href = "/login";
};

window.addEventListener("hashchange", () => open(wanted()));

(async function start() {
  try {
    csrf = (await api("/web/api/me")).csrf;
    await loadNav();
    open(wanted());
  } catch (x) {
    message(String((x && x.message) || x), true);
  }
})();
