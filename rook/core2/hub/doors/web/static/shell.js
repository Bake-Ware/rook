// The hub site's shell: lists pages, opens one at a time in a sandboxed
// frame, and carries that frame's calls and events (see bridge.js). The
// shell's own calls are pages.* only (plus the dock agent's chat); a page's
// calls go with its token. With the agents package, the house agent's chat
// dock sits beside every page.
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

async function call(cap, args, why) {
  const r = await api("/web/api/call", { cap, args: args || {}, why });
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
let site = { start: null, dock: null };

function link(p) {
  const a = el("a", { href: "#/" + p.page }, p.title);
  a.dataset.page = p.page;
  return a;
}

async function loadNav() {
  const [live, gone] = await Promise.all([call("pages.list"), call("pages.list", { archived: true })]);
  pages = live.pages;
  const box = $("#pages"), more = $("#more div");
  box.replaceChildren();
  more.replaceChildren();
  let section = null;
  for (const p of pages) {
    if (!p.nav) {                   // off the sidebar; its address still opens it
      more.append(link(p));
      continue;
    }
    if (p.section !== section) {
      section = p.section;
      box.append(el("div", { className: "section" }, section));
    }
    box.append(link(p));
  }
  $("#more").hidden = more.children.length === 0;
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
  const has = (pid) => pid && pages.some((p) => p.page === pid);
  if (has(site.start)) return site.start;
  const first = pages.find((p) => p.page === "hub/workers") || pages.find((p) => p.nav) || pages[0];
  return first ? first.page : "";
}

// -- one open page -----------------------------------------------------------

let opening = 0;

async function open(pid) {
  const n = ++opening;
  document.body.classList.remove("nav-open");
  closePage();
  if (!pid) return message("No pages yet.");
  const r = await api("/web/api/open", { page: pid });
  if (n !== opening) return;      // another page was asked for meanwhile
  if (!r.ok) return message(r.error || "This page can't be opened.", true);
  $("#msg").hidden = true;
  const frame = el("iframe", { title: r.page.title });
  frame.setAttribute("sandbox", "allow-scripts");
  frame.setAttribute("referrerpolicy", "no-referrer");
  frame.src = r.frame;
  const cur = { page: pid, token: r.token, frame, listening: false };
  let loads = 0;
  frame.addEventListener("load", () => {
    // A page that navigates its frame away is closed: whatever loaded there isn't the page.
    if (++loads > 1 && cur === current) {
      closePage();
      message("This page tried to leave the hub and was closed.", true);
    }
  });
  $("#stage").prepend(frame);
  current = cur;
  $("#title").textContent = r.page.title;
  $("#note-btn").hidden = $("#archive-btn").hidden = false;
  mark();
}

function closePage() {
  if (current) current.frame.remove();
  current = null;
  mark();
  $("#title").textContent = "";
  $("#note").hidden = $("#note-btn").hidden = $("#archive-btn").hidden = true;
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
  } else if (m.kind === "open" && /^[a-z0-9][a-z0-9_-]*\/[a-z0-9][a-z0-9_-]*$/.test(String(m.page))) {
    location.hash = "#/" + m.page;
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
  if (!current) return;
  if (!box.hidden) {
    box.hidden = true;
    return;
  }
  const d = await call("pages.get", { page: current.page });
  box.replaceChildren();
  if (d.made) {
    const who = (d.made_by.authors || []).map((a) => [a.agent, a.person].filter(Boolean).join(" for "));
    box.append(el("div", { className: "section" }, "Made by " + (who.join(", ") || "?")),
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
$("#nav").onclick = (e) => {
  const a = e.target.closest("a");
  if (!a) return;
  document.body.classList.remove("nav-open");
  if (!current && location.hash === a.getAttribute("href")) open(a.dataset.page);   // reopen a closed page
};

$("#signout").onclick = async () => {
  await api("/logout", {}).catch(() => {});
  location.href = "/login";
};

// -- the dock: the house agent beside every page ----------------------------------

const SITE = "[site]";      // a line the site adds for the agent: where the person is
const dock = { agent: null, next: 0, shown: 0 };

function dockOpen(open) {
  document.body.classList.toggle("dock-open", open);
  try { localStorage.setItem("rook.dock", open ? "1" : "0"); } catch (x) { /* private mode */ }
  if (open) $("#dock-text").focus();
}

function bubble(cls, text) {
  const log = $("#dock-log");
  log.append(el("div", { className: "said " + cls }, text));
  while (log.children.length > 200) log.firstChild.remove();
  log.scrollTop = log.scrollHeight;
}

function thinking(on) {
  $("#dock").classList.toggle("thinking", on);
}

function show(ev) {
  const d = ev.data || {};
  if (ev.type === "input" && typeof d.text === "string") {
    let text = d.text;
    if (text.startsWith(SITE)) {
      const nl = text.indexOf("\n");
      const rest = nl < 0 ? "" : text.slice(nl + 1).trim();
      if (!rest) return bubble("aside", text.slice(SITE.length).trim().split(".")[0]);
      text = rest;
    }
    bubble("me", text);
  } else if (ev.type === "turn" && d.role === "reply") {
    bubble("them", d.text);
    thinking(false);
  } else if (ev.type === "status" && d.state === "error") {
    bubble("bad", d.error || "Something went wrong.");
    thinking(false);
  }
}

async function refresh() {
  try {
    site = await call("pages.site");
    await loadNav();
    if (current && !pages.some((p) => p.page === current.page)) {      // archived meanwhile
      history.replaceState(null, "", location.pathname);
      open(wanted());
    }
  } catch (x) { /* next time */ }
}

async function read(wait) {
  const r = await call("agents.chat.read", { agent: dock.agent, after: dock.next, limit: 500, wait },
    "Reading the conversation in the site's dock.");
  for (const ev of r.events) show(ev);
  if (wait && r.events.some((ev) => ev.type === "turn")) refresh();   // it may have made, archived or started a page
  dock.shown += r.events.length;
  dock.next = r.next;
  return r.events.length;
}

async function say(text, quiet) {
  const where = current ? ` on the page ${current.page} (${$("#title").textContent})` : "";
  const body = quiet ? `${SITE} ${text}` : `${SITE} The person is${where || " on the hub site"}.\n${text}`;
  thinking(true);
  try {
    await call("agents.chat.send", { agent: dock.agent, text: body, wait: false }, "Talking with the agent in the site's dock.");
  } catch (x) {
    thinking(false);
    bubble("bad", x.message);
  }
}

async function startDock(agent) {
  dock.agent = agent;
  $("#dock-name").textContent = agent === "house" ? "House agent" : agent;
  $("#dock").hidden = $("#dock-btn").hidden = false;
  let open = true;
  try { open = localStorage.getItem("rook.dock") !== "0" && innerWidth > 720; } catch (x) { /* private mode */ }
  try {
    while ((await read(0)) >= 500) { /* catch up on a long conversation */ }
  } catch (x) {                     // not someone who may talk to it: no dock
    $("#dock").hidden = $("#dock-btn").hidden = true;
    return;
  }
  if (dock.shown === 0) {           // first run: the agent asks how the person wants to use the site
    open = true;
    await say("First visit to the hub site. Welcome the person, ask how they want to use the site, " +
      "and offer to make pages for what they want to keep an eye on (pages.components, pages.create) " +
      "and to set their start page (pages.start).", true);
  }
  dockOpen(open);
  for (;;) {
    try {
      await read(55);
    } catch (x) {
      await sleep(5000);
    }
  }
}

$("#dock-form").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#dock-text").value.trim();
  if (!text) return;
  $("#dock-text").value = "";
  say(text, false);
};
$("#dock-text").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#dock-form").requestSubmit();
  }
};
$("#dock-btn").onclick = () => dockOpen(!document.body.classList.contains("dock-open"));
$("#dock-close").onclick = () => dockOpen(false);

window.addEventListener("hashchange", () => open(wanted()));

(async function start() {
  try {
    csrf = (await api("/web/api/me")).csrf;
    site = await call("pages.site");
    await loadNav();
    open(wanted());
    if (site.dock) startDock(site.dock).catch((x) => bubble("bad", x.message));
  } catch (x) {
    message(String((x && x.message) || x), true);
  }
})();
