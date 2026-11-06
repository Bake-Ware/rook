// The hub site's shell: lists pages, opens one at a time in a sandboxed
// frame, and carries that frame's calls and events (see bridge.js). The
// shell's own calls are page.* only (plus the dock agent's chat); a page's
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
  const [live, gone] = await Promise.all([call("page.list"), call("page.list", { archived: true })]);
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
      await call("page.restore", { page: p.page });
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
  const d = await call("page.get", { page: current.page });
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
    await call("page.archive", { page: pid });
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
const SETUP_KICKOFF = "First visit to a new hub";
const dock = { agent: null, next: 0, shown: 0 };

function dockOpen(open) {
  document.body.classList.toggle("dock-open", open);
  try { localStorage.setItem("rook.dock", open ? "1" : "0"); } catch (x) { /* private mode */ }
  if (open) $("#dock-text").focus();
}

// The agent's replies: plain text with **bold**, `code` and links to this
// hub's own pages (/account, #/page) - built as nodes, never as HTML.
function rich(text) {
  const box = el("div");
  for (const part of text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\((?:\/|#)[^)\s]*\))/)) {
    if (!part) continue;
    let m;
    if ((m = /^\*\*(.+)\*\*$/.exec(part))) box.append(el("b", {}, m[1]));
    else if ((m = /^`(.+)`$/.exec(part))) box.append(/^\/[a-z]*$/.test(m[1]) ? el("a", { href: m[1] }, m[1]) : el("code", {}, m[1]));
    else if ((m = /^\[(.+)\]\(((?:\/|#)[^)]*)\)$/.exec(part))) box.append(el("a", { href: m[2] }, m[1]));
    else box.append(document.createTextNode(part));
  }
  return box.childNodes;
}

// One line in the chat log, above the "thinking" row while there is one.
function addRow(node) {
  const log = $("#dock-log");
  log.insertBefore(node, $("#dock-wait"));
  while (log.children.length > 200) log.firstChild.remove();
  log.scrollTop = log.scrollHeight;
}

function bubble(cls, text) {
  const div = el("div", { className: "said " + cls });
  if (cls === "them") {
    div.append(...rich(text));
    const row = el("div", { className: "row them" });
    row.append(el("div", { className: "gutter" }), div);
    addRow(row);
    rookieBeside(row);
  } else {
    div.textContent = text;
    addRow(div);
  }
}

function thinking(on) {
  let wait = $("#dock-wait");
  if (on && !wait) {
    wait = el("div", { id: "dock-wait", className: "row them" });
    wait.append(el("div", { className: "gutter" }), el("span", { className: "dots" }, "\u2026"));
    $("#dock-log").append(wait);
    rookieBeside(wait);
  } else if (!on && wait) {
    wait.remove();
    const rows = document.querySelectorAll("#dock-log .row.them");
    if (rows.length) rookieBeside(rows[rows.length - 1]);
  }
  $("#dock-log").scrollTop = $("#dock-log").scrollHeight;
}

// -- Rookie: the house agent's avatar beside its messages --------------------------
// rookie.js is a vendored drop-in (Rookie.mount, .set, .say, .setSkin, .setTheme,
// .sound, .enter, Rookie.favicon, SKINS, THEMES); rookie-map.js turns what the
// site sees into its states (docs/castling-first-run.md).

const rookie = { inst: null, fav: null, driver: null, holder: null, picker: null,
  look: { sound: false, rookie: "pixel", theme: "stone", name: "Rookie" } };

// Both the one in the chat and the tab icon follow the driver.
const rookieBoth = {
  set(s) { if (rookie.inst) rookie.inst.set(s); if (rookie.fav) rookie.fav.set(s); },
  say(text, o) { if (rookie.inst) rookie.inst.say(text, o); if (rookie.fav) rookie.fav.set("talking"); },
};

function rookieFavicon() {
  if (rookie.fav || !window.Rookie || !Rookie.favicon) return;
  const skin = Rookie.SKINS.includes(rookie.look.rookie) && rookie.look.rookie !== "none" ? rookie.look.rookie : "pixel";
  rookie.fav = Rookie.favicon({ skin, theme: rookie.look.theme });
  if (!rookie.driver) rookie.driver = new RookieMap.Driver(rookieBoth);
}

function rookieInit() {
  if (rookie.inst || !window.Rookie) return;
  rookie.holder = el("div", { className: "rookie" });
  rookie.inst = Rookie.mount(rookie.holder, { scale: 2, skin: rookie.look.rookie, theme: rookie.look.theme, state: "hidden" });
  if (!rookie.driver) rookie.driver = new RookieMap.Driver(rookieBoth);
  setInterval(() => rookie.driver.tick(), 1000);
  applyLook(rookie.look);
}

// Rookie's first appearance: the transporter, once.
function rookieBeside(row) {
  if (!rookie.holder) return;
  const first = !rookie.holder.parentNode;
  row.querySelector(".gutter").append(rookie.holder);
  if (first && rookie.inst.enter) {
    rookie.inst.enter(rookie.driver.state());
    rookie.driver.shown = rookie.driver.state();
  }
}

function lookName(name) {
  if (dock.agent !== "house") return;
  $("#dock-name").textContent = name;
  document.title = name;
}

// Look and feel: the first four setup steps (sound, Rookie's style, theme,
// name; page.look). Sound, style and theme are the person's; the name is the
// house agent's. The agent sets typed answers itself.
function applyLook(look) {
  if (!look) return;
  rookie.look = look;
  lookName(look.name);
  const skins = window.Rookie ? Rookie.SKINS : [];
  const shown = look.rookie !== "none" && skins.includes(look.rookie);
  for (const b of [rookie.inst, rookie.fav]) {
    if (!b) continue;
    if (b === rookie.inst) {
      b.sound = !!look.sound;
      if (shown) b.setSkin(look.rookie);
    } else {
      b.setSkin(shown ? look.rookie : "pixel");
    }
    if (b.setTheme) b.setTheme(look.theme);
  }
  $("#dock-log").classList.toggle("no-rookie", look.rookie === "none");
  if (rookie.picker && rookie.picker.step !== look.next) {
    for (const i of rookie.picker.insts) i.destroy();
    rookie.picker.box.remove();
    rookie.picker = null;
  }
}

async function lookAnswer(step, value, said) {
  try {
    const out = await call("page.look", { [step]: value }, "The person answered a look-and-feel question.");
    site.look = out;
    applyLook(out);
  } catch (x) {
    return bubble("bad", x.message);
  }
  say(`The person answered look and feel, ${step}: ${said}. ` +
    (site.look.next ? `Ask the next one (${site.look.next}).` : "Go on with setup."), true);
}

function lookPicker() {
  const look = site.look;
  const step = look && look.next;
  if (rookie.picker || !step || !window.Rookie || !document.body.classList.contains("setup")) return;
  const box = el("div", { className: "picker" });
  const insts = [];
  const option = (label, skin, theme, onclick) => {
    const b = el("button", { type: "button", title: label });
    const face = el("div", { className: "face" });
    b.append(face, el("span", {}, label));
    if (skin && skin !== "none" && Rookie.SKINS.includes(skin)) insts.push(Rookie.mount(face, { scale: 2, skin, theme, state: "happy" }));
    b.onclick = onclick;
    box.append(b);
  };
  if (step === "sound") {
    option("Sound on", null, null, () => lookAnswer("sound", true, "yes"));
    option("No sound", null, null, () => lookAnswer("sound", false, "no"));
  } else if (step === "rookie") {
    for (const k of look.styles) option(k === "none" ? "none (hide it)" : k, k, look.theme, () => lookAnswer("rookie", k, k));
  } else if (step === "theme") {
    const skin = look.rookie === "none" ? "pixel" : look.rookie;
    for (const k of look.themes) option(k, skin, k, () => lookAnswer("theme", k, k));
  } else if (step === "name") {
    const form = el("form", { className: "name" });
    const input = el("input", { type: "text", maxLength: 40, placeholder: look.name, value: "" });
    const save = el("button", { type: "submit" }, "Name it");
    const skip = el("button", { type: "button" }, `Keep ${look.name}`);
    form.onsubmit = (e) => {
      e.preventDefault();
      const name = input.value.trim() || look.name;
      lookAnswer("name", name, name);
    };
    skip.onclick = () => lookAnswer("name", look.name, look.name);
    form.append(input, save, skip);
    box.append(form);
  }
  rookie.picker = { box, insts, step };
  addRow(box);
}

function show(ev) {
  const d = ev.data || {};
  if (ev.type === "input" && typeof d.text === "string") {
    let text = d.text;
    if (text.startsWith(SITE)) {
      const nl = text.indexOf("\n");
      const rest = nl < 0 ? "" : text.slice(nl + 1).trim();
      if (!rest) {
        if (text.includes(SETUP_KICKOFF)) return;      // the setup walk-through starting: no banner
        return bubble("aside", text.slice(SITE.length).trim().split(".")[0]);
      }
      text = rest;
    }
    bubble("me", text);
  } else if (ev.type === "turn" && d.role === "reply") {
    thinking(false);
    bubble("them", d.text);
    lookPicker();
  } else if (ev.type === "status" && d.state === "error") {
    thinking(false);
    bubble("bad", d.error || "Something went wrong.");
    lookPicker();
  }
}

async function refresh() {
  try {
    site = await call("page.site");
    applyLook(site.look);
    const setup = setupMode();
    await loadNav();
    if (current && !pages.some((p) => p.page === current.page)) {      // archived meanwhile
      history.replaceState(null, "", location.pathname);
      open(wanted());
    } else if (!current && !setup) {                                   // setup just finished
      open(wanted());
    }
  } catch (x) { /* next time */ }
}

async function read(wait) {
  const r = await call("agent.chat.read", { agent: dock.agent, after: dock.next, limit: 500, wait },
    "Reading the conversation in the site's dock.");
  for (const ev of r.events) {
    show(ev);
    if (wait && rookie.driver) rookie.driver.chat(ev);     // live only: history doesn't animate
  }
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
    await call("agent.chat.send", { agent: dock.agent, text: body, wait: false }, "Talking with the agent in the site's dock.");
  } catch (x) {
    thinking(false);
    bubble("bad", x.message);
  }
}

// Setup mode: first-run setup isn't finished (hub.setup), so the site is the
// house agent's chat; "Skip to the hub" leaves it for this visit.
let skipped = false;
function setupMode() {
  const on = !skipped && !!dock.agent && !!site.setup && !site.setup.done;
  document.body.classList.toggle("setup", on);
  $("#dock-account").hidden = !on;
  $("#dock-close").textContent = on ? "Skip to the hub" : "\u00d7";
  return on;
}

// The dock agent's calls for this person, as they start and end (Rookie's states).
async function activity() {
  let since = "";
  for (;;) {
    try {
      const r = await api(`/web/api/activity?since=${since}`);
      if (!r.ok) {
        await sleep(5000);
        continue;
      }
      for (const ev of r.events) rookie.driver.activity(ev);
      since = r.seq;
    } catch (x) {
      await sleep(3000);
    }
  }
}

async function startDock(agent) {
  dock.agent = agent;
  $("#dock-name").textContent = agent === "house" ? rookie.look.name : agent;
  rookieInit();
  $("#dock").hidden = $("#dock-btn").hidden = false;
  let open = true;
  try { open = localStorage.getItem("rook.dock") !== "0" && innerWidth > 720; } catch (x) { /* private mode */ }
  try {
    while ((await read(0)) >= 500) { /* catch up on a long conversation */ }
  } catch (x) {                     // not someone who may talk to it: no dock
    $("#dock").hidden = $("#dock-btn").hidden = true;
    return;
  }
  if (setupMode()) {                // a new hub: the chat is the whole site until setup is done
    open = true;
    if (dock.shown === 0) {
      await say(SETUP_KICKOFF + "; setup isn't finished. Greet the person warmly and start " +
        "setting up with them: read the first-run skill (cap agent.skill on worker rook, args {name: \"first-run\"}) and follow it. " +
        "Open with the four look-and-feel questions, one per message, in order: sound, Rookie's style, " +
        "Rookie's theme, Rookie's name (the name becomes yours); hub.setup's look.next says which is next. " +
        "The site shows the person a picker under your question; they may also type the answer (set it with page.look).", true);
    } else {
      lookPicker();                 // a reload before they picked
    }
  } else if (dock.shown === 0) {    // first run: the agent asks how the person wants to use the site
    open = true;
    await say("First visit to the hub site. Welcome the person, ask how they want to use the site, " +
      "and offer to make pages for what they want to keep an eye on (page.components, page.create) " +
      "and to set their start page (page.start).", true);
  }
  dockOpen(open);
  if (rookie.driver) activity();
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
$("#dock-text").oninput = () => rookie.driver && rookie.driver.key();
$("#dock-text").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#dock-form").requestSubmit();
  }
};
$("#dock-btn").onclick = () => dockOpen(!document.body.classList.contains("dock-open"));
$("#dock-close").onclick = () => {
  if (document.body.classList.contains("setup")) {
    skipped = true;
    setupMode();
    if (!current) open(wanted());
    return;
  }
  dockOpen(false);
};

window.addEventListener("hashchange", () => open(wanted()));

(async function start() {
  try {
    csrf = (await api("/web/api/me")).csrf;
    site = await call("page.site");
    if (site.look) rookie.look = site.look;
    rookieFavicon();
    await loadNav();
    if (!(site.dock && site.setup && !site.setup.done)) open(wanted());
    if (site.dock) startDock(site.dock).catch((x) => bubble("bad", x.message));
  } catch (x) {
    message(String((x && x.message) || x), true);
  }
})();
