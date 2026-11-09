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
  $("#work").hidden = true;                 // a hub page replaces a work view
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
const dock = { agent: null, next: 0, shown: 0, replies: 0, kicking: false };
// A turn that failed because no worker serves the agent's backend yet (the
// worker joins a few seconds after an install): not a message; wait and retry.
const NOT_READY = /no single worker serves|nothing provides agent\.backend/i;

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

function agentName() {
  return dock.agent === "house" ? rookie.look.name : dock.agent;
}

function msg(cls, who) {
  const row = el("div", { className: "msg " + cls });
  const label = el("div", { className: "who" });
  label.append(el("b", {}, who));
  row.append(label);
  return row;
}

// A message. The agent's may carry choices (buttons under it) and work-pane
// directives (chat-widgets.js): [[open: ...]] and [[point: ...]] run when the
// message arrives live; on a reload the last [[open: ...]] is shown again.
function bubble(cls, text, live) {
  if (cls === "me" || cls === "them") retire();     // only the newest message's choices stay live
  if (cls === "aside" || cls === "bad") {
    addRow(el("div", { className: "msg " + cls }, text));
    return;
  }
  const row = msg(cls, cls === "them" ? agentName() : "You");
  const parts = cls === "them" && window.ChatWidgets ? ChatWidgets.parse(text) : [{ kind: "text", text }];
  const later = [];
  for (const part of parts) {
    if (part.kind === "text") {
      const box = el("div", { className: "text" });
      if (cls === "them") box.append(...rich(part.text)); else box.textContent = part.text;
      row.append(box);
    } else if (part.kind === "choices") {
      row.append(choices(part));
    } else if (part.kind === "open") {
      work.last = part;
      if (live) later.push(() => openView(part.view, part.arg, part.args));
    } else if (part.kind === "point" && live) {
      later.push(() => setTimeout(() => point(part.target), 700));
    }
  }
  if (row.children.length > 1) addRow(row);
  for (const fn of later) fn();
}

function retire() {
  for (const w of document.querySelectorAll("#dock-log .widget:not(.done)")) {
    w.classList.add("done");
    for (const c of w.querySelectorAll("button, input")) c.disabled = true;
  }
}

function choices(part) {
  const box = el("div", { className: "widget choices" });
  for (const item of part.items) {
    const b = el("button", { type: "button" }, item);
    b.onclick = () => {
      b.classList.add("picked");
      say(item, false);
    };
    box.append(b);
  }
  return box;
}

// -- the work pane: the real thing being set up, driven by the agent ------------------
// Built-in views the shell draws itself, so the agent can pre-fill a field,
// focus it and have Rookie point at it (data-target ids); a hub page opens as
// usual ("page hub/workers"). Bake, Nov 9: "I want it to guide the setup entirely".

const work = { last: null, view: null };

function workView(name, title, note) {
  closePage();
  const pane = $("#work");
  pane.replaceChildren();
  pane.hidden = false;
  pane.dataset.view = work.view = name;
  $("#title").textContent = title;
  const head = el("header");
  head.append(el("h2", {}, title));
  if (note) head.append(el("p", { className: "muted" }, note));
  pane.append(head);
  return pane;
}

function field(form, label, attrs) {
  const id = "w-" + attrs.dataset.target.replace(/\W/g, "-");
  const input = el("input", { id, ...attrs.input });
  input.dataset.target = attrs.dataset.target;
  form.append(el("label", { htmlFor: id }, label), input);
  return input;
}

function openView(view, arg, args) {
  args = args || {};
  if (view === "page") {
    $("#work").hidden = true;
    location.hash = "#/" + arg;
    return;
  }
  const make = VIEWS[view];
  if (!make) return bubble("bad", `There's no ${view} view here yet.`);
  make(args);
}

const VIEWS = {
  async vault(args) {
    const pane = workView("vault", "Vault", "Secrets the hub keeps sealed. Rookie uses them by name; their values are never shown.");
    const list = el("ul", { className: "secrets" });
    list.dataset.target = "vault.list";
    const form = el("form", { className: "work-form tick" });
    form.append(el("h3", {}, "New secret"));
    const key = field(form, "Key", { dataset: { target: "vault.key" }, input: { type: "text", value: args.new || args.key || "", autocomplete: "off", required: true } });
    const value = field(form, "Value", { dataset: { target: "vault.value" }, input: { type: "password", autocomplete: "new-password", required: true } });
    const what = field(form, "What it's for", { dataset: { target: "vault.description" }, input: { type: "text", value: args.description || args.for || "" } });
    const save = el("button", { type: "submit" }, "Save");
    save.dataset.target = "vault.save";
    const out = el("div", { className: "out" });
    form.append(save, out);
    pane.append(form, el("h3", {}, "In the vault"), list);
    const load = async () => {
      const r = await api("/web/api/secrets");
      list.replaceChildren(...(r.secrets || []).map((x) => {
        const li = el("li");
        li.append(el("code", {}, x.name), el("span", { className: "muted" }, x.description || ""));
        return li;
      }));
      if (!(r.secrets || []).length) list.append(el("li", { className: "muted" }, r.ok ? "Nothing yet." : r.error || ""));
    };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const r = await api("/web/api/secret", { name: key.value.trim(), value: value.value, description: what.value.trim() });
      if (!r.ok) {
        out.textContent = r.error || "That didn't save.";
        return;
      }
      value.value = "";
      out.textContent = `Saved as ${r.name}.`;
      load();
      // Never the braces here: a vault reference in chat text would be filled in.
      say(`Secret ${r.name} saved in the vault. The person typed it in the Vault pane; use it as a vault ` +
        `reference (secret:${r.name} in double braces).`, true);
    };
    load();
    (args.new || args.key ? value : key).focus();
    if (args.new || args.key) setTimeout(() => point("vault.value"), 500);
  },

  account(args) {
    const pane = workView("account", "Your account", "Your name, and a password for signing in once setup is done.");
    const form = el("form", { className: "work-form tick" });
    const name = field(form, `What should ${agentName()} call you?`, { dataset: { target: "account.name" }, input: { type: "text", value: args.name || "", autocomplete: "name" } });
    const pw = field(form, "Password", { dataset: { target: "account.password" }, input: { type: "password", autocomplete: "new-password" } });
    const again = field(form, "Password again", { dataset: { target: "account.again" }, input: { type: "password", autocomplete: "new-password" } });
    const save = el("button", { type: "submit" }, "Save");
    save.dataset.target = "account.save";
    const out = el("div", { className: "out" });
    form.append(save, out);
    pane.append(form);
    form.onsubmit = async (e) => {
      e.preventDefault();
      if (pw.value !== again.value) {
        out.textContent = "The passwords don't match.";
        return;
      }
      const setPw = !!pw.value;
      if (setPw) {
        const r = await api("/web/api/password", { password: pw.value });
        if (!r.ok) {
          out.textContent = r.error || "That didn't save.";
          return;
        }
      }
      pw.value = again.value = "";
      out.textContent = "Saved.";
      const who = name.value.trim();
      say(`Account saved. In the Account pane the person ${who ? `asked to be called "${who}"` : "gave no name"}` +
        `${setPw ? " and set a password" : " and set no password"}. Go on.`, true);
    };
    (name.value ? pw : name).focus();
    setTimeout(() => point(name.value ? "account.password" : "account.name"), 500);
  },

  network(args) {
    const pane = workView("network", "Reaching this site", "Where you'll open this site from.");
    const here = location.origin;
    const rows = [
      ["network.local", "Just this computer", `Only a browser on this machine opens it: ${args.local || here}`],
      ["network.home", "Other devices at home", `Phones and laptops on your home network open it at ${args.home || "this machine's address (Rookie finds it)"}. This machine's firewall has to let them in.`],
      ["network.anywhere", "From anywhere", args.anywhere || "Away from home too: through a tunnel, a VPN like Tailscale, or a port forward on your router."],
    ];
    for (const [target, title, text] of rows) {
      const card = el("div", { className: "card tick" });
      card.dataset.target = target;
      card.append(el("h3", {}, title), el("p", {}, text));
      pane.append(card);
    }
    if (args.status) {
      const st = el("pre", { className: "status" }, args.status);
      st.dataset.target = "network.status";
      pane.append(st);
    }
    if (args.point) setTimeout(() => point(args.point), 500);
  },
};

// Setup's work pane before Rookie opens anything: just the brand mark.
function idleWork() {
  const pane = workView("idle", "Setup", "");
  pane.querySelector("header").remove();
  const mark = el("div", { className: "idle" });
  mark.innerHTML = '<svg class="mark" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h3v2h2V3h4v2h2V3h3v5l-2 2v7l2 2v2H5v-2l2-2v-7L5 8z"/></svg>';
  pane.append(mark);
}

// Rookie walks over to a target in the work pane, points at it, and comes back
// (Bake, Nov 9: "make sure rookie like animates to these locations. Like not
// just like pop in at random places"): he lifts off from where he is, hops
// along a curved path to the target, settles, and goes home the same way.
function travel(x, y, done) {
  const h = rookie.holder;
  const from = h.getBoundingClientRect();
  const ms = Math.round(Math.min(1600, Math.max(600, Math.hypot(x - from.left, y - from.top) * 1.4)));
  if (!h.classList.contains("away")) {                  // lift off exactly where he stands
    h.style.transition = "none";
    Object.assign(h.style, { left: from.left + "px", top: from.top + "px" });
    h.classList.add("away");
    void h.offsetWidth;
  }
  rookie.travelling = true;
  rookie.inst.set("happy");                              // hopping along
  // x and y ease differently, so the path curves instead of cutting straight across
  h.style.transition = `left ${ms}ms cubic-bezier(.45,0,.55,1), top ${ms}ms cubic-bezier(.2,.75,.3,1)`;
  Object.assign(h.style, { left: x + "px", top: y + "px" });
  clearTimeout(rookie.arrive);
  rookie.arrive = setTimeout(() => {
    rookie.travelling = false;
    done();
  }, ms + 30);
}

function point(target) {
  const t = target === "page" ? current && current.frame : document.querySelector(`[data-target="${CSS.escape(target)}"]`);
  if (!t) return;
  t.scrollIntoView({ block: "center", behavior: "smooth" });
  t.classList.add("pointed");
  setTimeout(() => t.classList.remove("pointed"), 6000);
  const h = rookie.holder;
  if (!h || !rookie.inst || document.body.classList.contains("no-rookie")) return;
  clearTimeout(rookie.back);
  setTimeout(() => {                                      // after the scroll settles
    const me = h.getBoundingClientRect(), r = t.getBoundingClientRect();
    const x = r.right + 10 + me.width < innerWidth ? r.right + 10 : Math.max(8, r.left - me.width - 10);
    const y = Math.max(8, r.top + r.height / 2 - me.height / 2);
    travel(x, y, () => {
      rookie.inst.set("talking");                        // there: this one
      rookie.back = setTimeout(() => {
        const home = $("#rookie-home").getBoundingClientRect();
        travel(home.left, home.top, () => {
          h.classList.remove("away");
          h.removeAttribute("style");
          rookie.driver.shown = null;
          rookie.driver.show();
        });
      }, 4000);
    });
  }, 400);
}

function thinking(on) {
  let wait = $("#dock-wait");
  if (on && !wait) {
    wait = msg("them wait", agentName());
    wait.id = "dock-wait";
    wait.append(el("div", { className: "dots" }, "\u2026"));
    $("#dock-log").append(wait);
  } else if (!on && wait) {
    wait.remove();
  }
  $("#dock-log").scrollTop = $("#dock-log").scrollHeight;
}

// -- Rookie: the house agent's avatar beside its messages --------------------------
// rookie.js is a vendored drop-in (Rookie.mount, .set, .say, .setSkin, .setTheme,
// .sound, .enter, Rookie.bake, SKINS, THEMES); rookie-map.js turns what the
// site sees into its states (docs/castling-first-run.md).

// Exactly one live Rookie on a page (Bake, Nov 9: "only one instance of it on
// any page at a time"): the one in the chat's head. The tab icon copies his
// frames; pickers draw static thumbnails (Rookie.bake), never a live one.
const rookie = { inst: null, driver: null, holder: null, picker: null, travelling: false,
  look: { sound: false, rookie: "pixel", theme: "stone", name: "Rookie" } };

const rookieBoth = {
  set(s) { if (rookie.inst && !rookie.travelling) rookie.inst.set(s); },
  say(text, o) { if (rookie.inst && !rookie.travelling) rookie.inst.say(text, o); },
};

// The tab icon: the live Rookie's canvas, copied (no second instance).
function rookieFavicon() {
  let link = document.querySelector('link[rel~="icon"]');
  if (!link) {
    link = el("link", { rel: "icon" });
    document.head.append(link);
  }
  const out = el("canvas", { width: 32, height: 32 });
  const ctx = out.getContext("2d");
  ctx.imageSmoothingEnabled = false;
  let last = "";
  setInterval(() => {
    const c = rookie.inst && rookie.inst.canvas;
    if (!c || document.body.classList.contains("no-rookie")) return;
    ctx.clearRect(0, 0, 32, 32);
    ctx.drawImage(c, 0, 0, c.width, c.height, 0, 3, 32, 25);
    const url = out.toDataURL("image/png");
    if (url !== last) link.href = last = url;
  }, 300);
}

// A still picture of Rookie in a style and theme, for pickers.
function rookieThumb(skin, theme) {
  if (!window.Rookie || !Rookie.bake || skin === "none" || !Rookie.SKINS.includes(skin)) return el("span", { className: "thumb none" });
  const c = el("canvas", { className: "thumb" });
  const { canvas, manifest } = Rookie.bake({ skin, theme, scale: 2, rows: ["happy"] });
  const [w, h] = manifest.cell;
  c.width = w;
  c.height = h;
  c.getContext("2d").drawImage(canvas, 0, 0, w, h, 0, 0, w, h);
  return c;
}

function rookieInit() {
  if (rookie.inst || !window.Rookie) return;
  rookie.holder = el("div", { className: "rookie" });
  $("#rookie-home").append(rookie.holder);
  rookie.inst = Rookie.mount(rookie.holder, { scale: 2, skin: rookie.look.rookie, theme: rookie.look.theme, state: "hidden" });
  rookie.driver = new RookieMap.Driver(rookieBoth);
  rookieFavicon();
  setInterval(() => rookie.driver.tick(), 1000);
  applyLook(rookie.look);
  if (rookie.inst.enter) {            // his first appearance: the transporter, once
    rookie.inst.enter(rookie.driver.state());
    rookie.driver.shown = rookie.driver.state();
  }
}

function lookName(name) {
  if (dock.agent !== "house") return;
  $("#dock-name").textContent = name;
  for (const b of document.querySelectorAll("#dock-log .msg.them .who b")) b.textContent = name;
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
  if (rookie.inst) {
    rookie.inst.sound = !!look.sound;
    if (shown) rookie.inst.setSkin(look.rookie);
    if (rookie.inst.setTheme) rookie.inst.setTheme(look.theme);
  }
  document.body.classList.toggle("no-rookie", look.rookie === "none");
  if (rookie.picker && rookie.picker.step !== look.next) {
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
  const option = (label, skin, theme, onclick) => {
    const b = el("button", { type: "button", title: label });
    const face = el("div", { className: "face" });
    if (skin) face.append(rookieThumb(skin, theme));
    b.append(face, el("span", {}, label));
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
  rookie.picker = { box, step };
  addRow(box);
}

function show(ev, live) {
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
    dock.replies++;
    thinking(false);
    bubble("them", d.text, live);
    lookPicker();
  } else if (ev.type === "status" && d.state === "error") {
    thinking(false);
    if (NOT_READY.test(d.error || "")) {
      if (live && setupMode() && !dock.replies) kickoff();
      return;
    }
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
    show(ev, wait > 0);
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
  $("#dock-close").textContent = on ? "Skip to the hub" : "\u00d7";
  return on;
}

// Calls as they start and end (the feed has every call); Rookie's states.
async function activity() {
  let since = "";
  for (;;) {
    try {
      const r = await api(`/web/api/activity?since=${since}`);
      if (!r.ok) {
        await sleep(5000);
        continue;
      }
      for (const ev of r.events) {
        // Rookie shows what the dock agent does for whoever is here.
        if (ev.agent === "agent:" + dock.agent && ev.person === site.person) rookie.driver.activity(ev);
      }
      since = r.seq;
    } catch (x) {
      await sleep(3000);
    }
  }
}

// Until a worker serves the agent's backend, "Getting ready..." and wait.
async function untilReady() {
  if (site.ready) return;
  let row = $("#dock-ready");
  if (!row) {
    row = el("div", { id: "dock-ready", className: "msg aside" }, "Getting ready\u2026");
    addRow(row);
  }
  while (!site.ready) {
    await sleep(2000);
    try { site = await call("page.site"); } catch (x) { /* next time */ }
  }
  row.remove();
}

// The setup walk-through starts (or starts again, when the agent never got
// to answer): once the backend is there.
async function kickoff() {
  if (dock.kicking) return;
  dock.kicking = true;
  try {
    await untilReady();
    await say(SETUP_KICKOFF + "; setup isn't finished. Greet the person warmly and start " +
      "setting up with them: read the first-run skill (cap agent.skill on worker rook, args {name: \"first-run\"}) and follow it. " +
      "Open with the four look-and-feel questions, one per message, in order: sound, Rookie's style, " +
      "Rookie's theme, Rookie's name (the name becomes yours); hub.setup's look.next says which is next. " +
      "The site shows the person a picker under your question; they may also type the answer (set it with page.look). " +
      "The left of the screen is your work pane: open what you set up there with [[open: ...]] (the skill says how).", true);
  } finally {
    dock.kicking = false;
  }
}

async function startDock(agent) {
  dock.agent = agent;
  $("#dock-name").textContent = agent === "house" ? rookie.look.name : agent;
  rookieInit();
  $("#dock").hidden = $("#dock-btn").hidden = false;
  let open = true;
  try { open = localStorage.getItem("rook.dock") !== "0" && innerWidth > 760; } catch (x) { /* private mode */ }
  try {
    while ((await read(0)) >= 500) { /* catch up on a long conversation */ }
  } catch (x) {                     // not someone who may talk to it: no dock
    $("#dock").hidden = $("#dock-btn").hidden = true;
    return;
  }
  if (setupMode()) {                // a new hub: the chat is the whole site until setup is done
    open = true;
    if (!dock.replies) {           // a new hub, or the agent never got to answer
      idleWork();
      await kickoff();
    } else {
      lookPicker();                 // a reload before they picked
      if (work.last) openView(work.last.view, work.last.arg, work.last.args);    // what we were working on
      else idleWork();
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
    await loadNav();
    if (!(site.dock && site.setup && !site.setup.done)) open(wanted());
    if (site.dock) startDock(site.dock).catch((x) => bubble("bad", x.message));
  } catch (x) {
    message(String((x && x.message) || x), true);
  }
})();
