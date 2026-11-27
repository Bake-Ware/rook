// The house agent beside every page of the hub site: the chat dock, Rookie,
// the work pane and the guided first run. The agents package's part of the
// shell (manifest shell:): it loads after the shell's shell.js and uses its
// helpers ($, el, api, call, open, site, current); agent.site says which
// agent is in the dock. Loaded on every shell page; it starts on the main one
// only (shellHooks).
//
// Ported to core (house agent brief HA1) from 0.5 (cbe86a47, rook/plugins/agents/shell/dock.js).
// The code is 0.5's; what core's shape changed is marked "core:". The first block below gives
// what 0.5's shell.js gave its shell parts. The work-pane views of setup (vault, account,
// network, look) and the look-and-feel steps are the setup wizard's (brief HA3), in a shell
// file loaded after this one: it adds to VIEWS and defines lookStart, lookStep, lookDone,
// lookSoFar, lookLine and LOOK_SAY, which this file calls only when they are there.
"use strict";

// -- core: what 0.5's shell.js gave its shell parts --------------------------------

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function el(tag, attrs, text) {
  const e = document.createElement(tag);
  Object.assign(e, attrs || {});
  if (text !== undefined) e.textContent = text;
  return e;
}

// 0.5's api(path, body): GET, or POST with a JSON body; the reply as JSON, whatever its status.
async function api(path, body) {
  const opts = { credentials: "same-origin" };
  if (body !== undefined) Object.assign(opts, { method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } });
  const r = await fetch(path, opts);
  if (r.status === 401) {
    location.reload();             // core: the shell shows its login
    throw new Error("signed out");
  }
  return r.json();
}

// 0.5's call(cap, args, why): a hub cap as the signed-in person. Core's site call answers
// "running" for a call that outlasts the hub's wait (45 s); then its stream is followed. Every
// core call has a why (L1); 0.5 called agent.site with none, so such a call says it is the dock's.
async function call(cap, args, why) {
  const r = await rookShell.call("hub", cap, args, why || "The site's chat dock, beside the page.");
  return r && r.running ? r.follow() : r;
}

// call(), for a read with a wait: a page being left lets go of it, and a page shown again from
// the back-forward cache asks it again from the same place (core's rookShell.callHeld; HA7).
// Without that, each page left kept one of the browser's six connections to the hub for 40 s.
async function callHeld(cap, args, why) {
  const r = await rookShell.callHeld("hub", cap, args, why);
  return r && r.running ? r.follow() : r;
}

const site = {};                    // agent.site's reply (0.5 merged page.site's in; core has none)
const shown = rookShell.page;       // the core page this load shows: {id, label, path}
let current = shown ? { page: shown.id, title: shown.label, frame: $("main.main") } : null;

// 0.5 closed the page's frame to show a work view; core keeps the page and hides it.
function closePage() {
  current = null;
  document.body.classList.add("working");
}

// "page hub/workers" (0.5's page ids) or core's own: a page's id, path, label or last word.
function pagePath(arg) {
  const want = String(arg || "").trim().toLowerCase().replace(/^hub\//, "");
  const all = rookShell.store.nav.flatMap((seg) => seg.pages);
  const hit = all.find((p) => [p.id, p.path, p.label.toLowerCase(), p.path.split("/").pop()].includes(want));
  return hit ? hit.path : null;
}

// How long one agent.chat.read waits for news. 0.5 waited 55 s; core's site answers a call
// "still running" after 45 s, so a read waits less than that.
const READ_WAIT = 40;

// -- the dock: the house agent beside every page ----------------------------------

const SITE = "[site]";      // a line the site adds for the agent: where the person is
const SHOWN = "[shown]";    // after a site note: the person's own words for it, shown as theirs
const SETUP_KICKOFF = "First visit to a new hub";
// chose: open or closed as the person last chose it with a control (null: not yet), which startDock
// keeps over the choice it read before the conversation loaded (brief AP6).
const dock = { agent: null, next: 0, shown: 0, replies: 0, kicking: false, kicked: false, chose: null };
// A turn that failed because no worker serves the agent's backend yet (the
// worker joins a few seconds after an install): not a message; wait and retry.
const NOT_READY = /no single worker serves|nothing provides agent\.backend/i;

// A phone-width screen (brief AP1): the chat is the whole screen, and one control (#dock-phone)
// goes to the hub's pages and back. Which of the two shows is kept for the tab (sessionStorage),
// so a new start, the phone app's too, opens on the chat; a wide screen keeps its choice for good.
const PHONE = "(max-width: 760px)";
const phone = () => matchMedia(PHONE).matches;

// Bake, Nov 9: "I want to have like an X at the top right, and move the rookie
// avatar to the bottom right corner when closed. Remove the chat button from
// the like top nav on pages. Clicking rookie opens the chat sidebar."
// The person opens or closes the chat with a control: that choice stands, also against a load under way.
function choose(open) {
  dock.chose = open;
  dockOpen(open);
}

function dockOpen(open) {
  const was = document.body.classList.contains("dock-open");
  if (!open && was && rookie.holder && !document.body.classList.contains("setup")) liftOff();   // he walks out before the chat goes
  document.body.classList.toggle("dock-open", open);
  $("#dock-phone").textContent = open ? "Pages" : "Chat";
  try {
    if (phone()) sessionStorage.setItem("rook.dock.phone", open ? "1" : "0");
    else localStorage.setItem("rook.dock", open ? "1" : "0");
  } catch (x) { /* private mode */ }
  if (open) {
    scrollChat();
    // the field the work pane is open for keeps the cursor (a reload in setup), else the chat
    if (work.focus && work.focus.isConnected && !$("#work").hidden) work.focus.focus();
    else $("#dock-text").focus();
  }
  if (rookie.holder && open !== was) goHome();
}

// The chat shows its newest message when it opens and as messages come
// (Bake, Nov 9: "Scroll chat to the bottom when it starts/opens"). Again on
// the next frame and once the fonts are in: a hidden log has no height yet.
function scrollChat() {
  const log = $("#dock-log");
  log.scrollTop = log.scrollHeight;
  requestAnimationFrame(() => (log.scrollTop = log.scrollHeight));
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
  // core: 0.5 dropped the oldest rows past 200 here; nothing is cut (RULES-HA: no limit comes over).
  scrollChat();
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
function bubble(cls, text, live, seq) {
  if (cls === "me" || cls === "them") retire();     // only the newest message's choices stay live
  if (cls === "aside" || cls === "bad") {
    addRow(el("div", { className: "msg " + cls }, text));
    return;
  }
  const row = msg(cls, cls === "them" ? agentName() : "You");
  if (seq != null) row.dataset.seq = seq;
  let said = "";
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
      if (live) later.push(() => openView(part.view, part.arg, part.args, true));
    } else if (part.kind === "point" && live) {
      said = said || part.say;
      later.push(() => setTimeout(() => point(part.target, part.say), 700));
    } else if (part.kind === "say") {
      if (live && !said) {
        said = part.text;
        later.push(() => setTimeout(() => speak(part.text, seq), 300));
      }
    }
  }
  if (row.children.length > 1) addRow(row);
  // closed chat, no words of its own for Rookie: the reply's first few words
  if (live && cls === "them" && !said && chatClosed()) later.push(() => speak(shortLine(text), seq));
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

// work.focus: the field the open view is for (focused again after a reload
// or when the chat opens; Bake's review, Nov 9).
const work = { last: null, view: null, focus: null };

function workView(name, title, note) {
  closePage();
  const pane = $("#work");
  pane.replaceChildren();
  pane.hidden = false;
  pane.dataset.view = work.view = name;
  work.focus = null;
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

function hideWork() {
  $("#work").hidden = true;
  work.focus = null;
  document.body.classList.remove("working");                                   // core: the page shows again
  current = shown ? { page: shown.id, title: shown.label, frame: $("main.main") } : null;
  if (rookie.holder && rookie.holder.classList.contains("staged")) goHome();     // off the stage
}

function focusWork(input) {
  work.focus = input;
  input.focus();
}

// A view whose job is done shows that, never a blank form the conversation
// has left behind (Bake's review, Nov 9): a tick, what was saved, and a way
// to do it again.
function doneCard(target, title, lines, again) {
  const card = el("div", { className: "card tick done" });
  card.dataset.target = target;
  card.append(el("h3", {}, "✓ " + title));
  for (const line of lines) card.append(el("p", {}, line));
  if (again) {
    const b = el("button", { type: "button", className: "link" }, again[0]);
    b.onclick = again[1];
    card.append(b);
  }
  return card;
}

// The person's sign-in user name (person:<name>): the installer makes it
// from the computer's login.
function userName() {
  return String(site.person || (rookShell.store.user && rookShell.store.user.name) || "").replace(/^person:/, "");
}

function remember(key, value) {
  try { localStorage.setItem("rook." + key, value); } catch (x) { /* private mode */ }
}

function recall(key) {
  try { return localStorage.getItem("rook." + key) || ""; } catch (x) { return ""; }
}

// live: the agent opened it just now; else a reload showing it again, where
// a job already done shows as done.
function openView(view, arg, args, live) {
  args = args || {};
  // On a phone the chat covers the page: what the agent opens live is shown, and #dock-phone comes back.
  const show = () => { if (live && phone() && !setupMode()) dockOpen(false); };
  if (view === "page") {
    const path = pagePath(arg);       // core: a page is its own address, not 0.5's #/<page>
    if (!path) return bubble("bad", `There's no page ${arg} here.`);
    hideWork();
    show();
    if (path !== location.pathname) location.href = path;
    return;
  }
  const make = VIEWS[view];
  if (!make) return bubble("bad", `There's no ${view} view here yet.`);
  make(args, !!live);
  show();
}

// Built-in views, by name: setup's (vault, account, network, look) come with the setup wizard
// (brief HA3), in its shell file, which adds them here.
const VIEWS = {};

// The phone app (brief AP1): its QR code, download button and install line, as the public page
// /app shows them ([[open: app]]).
VIEWS.app = function () {
  const pane = workView("app", "Phone app", "Rook for Android, from this hub: it opens this hub with nothing typed.");
  const box = el("div");
  pane.append(box);
  import("/site/phone-app.js").then((m) => m.drawPhoneApp(box)).catch((x) => bubble("bad", x.message));
};

// The welcome view: setup's work pane when nothing is open.
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
function liftOff() {                                    // out of the chat, exactly where he stands
  const h = rookie.holder;
  if (h.classList.contains("away")) return h.getBoundingClientRect();
  const from = h.getBoundingClientRect();
  h.style.transition = "none";
  Object.assign(h.style, { left: from.left + "px", top: from.top + "px" });
  h.classList.add("away");
  document.body.append(h);                             // stays drawn when the chat closes
  void h.offsetWidth;
  return from;
}

function travel(x, y, done) {
  const h = rookie.holder;
  const from = liftOff();
  const ms = Math.round(Math.min(1600, Math.max(600, Math.hypot(x - from.left, y - from.top) * 1.4)));
  h.classList.remove("parked", "staged");
  rookie.travelling = true;
  rookie.inst.set("happy");                              // hopping along
  // x and y ease differently, so the path curves instead of cutting straight across
  h.style.transition = `left ${ms}ms cubic-bezier(.45,0,.55,1), top ${ms}ms cubic-bezier(.2,.75,.3,1), transform 400ms ease`;
  Object.assign(h.style, { left: x + "px", top: y + "px" });
  clearTimeout(rookie.arrive);
  rookie.arrive = setTimeout(() => {
    rookie.travelling = false;
    done();
  }, ms + 30);
}

// Where Rookie lives: on the look-and-feel stage while that's open (shown
// big), in the chat's head while it's open (or during setup), else parked in
// the bottom-right corner, where a click opens the chat.
function onStage() {
  return work.view === "look" && !$("#work").hidden && !!$("#look-spot");
}

function homeSpot() {
  if (onStage()) return $("#look-spot").getBoundingClientRect();
  const open = document.body.classList.contains("dock-open") || document.body.classList.contains("setup");
  return open ? null : $("#rookie-park").getBoundingClientRect();
}

function settle() {
  const h = rookie.holder;
  if (!homeSpot()) {
    h.classList.remove("away", "parked", "staged");
    h.removeAttribute("style");
    $("#rookie-home").append(h);
  } else {
    h.classList.add(onStage() ? "staged" : "parked");
  }
  rookie.driver.shown = null;
  rookie.driver.show();
}

function goHome() {
  const h = rookie.holder;
  if (!h || !rookie.inst) return;
  clearTimeout(rookie.back);
  requestAnimationFrame(() => {
    const park = homeSpot();
    if (!park && !h.classList.contains("away")) return;          // already home in the chat
    const to = park || $("#rookie-home").getBoundingClientRect();
    travel(to.left, to.top, settle);
  });
}

function keepPlace() {                                  // the corner (or the stage) moves with the window
  const h = rookie.holder, park = homeSpot();
  if (h && park && (h.classList.contains("parked") || h.classList.contains("staged"))) {
    Object.assign(h.style, { transition: "none", left: park.left + "px", top: park.top + "px" });
  }
}

// He starts where he lives (the chat is open, or he's parked), no walk.
function placeRookie() {
  const park = homeSpot();
  if (!park) return;
  const h = rookie.holder;
  h.classList.add("away", "parked");
  Object.assign(h.style, { left: park.left + "px", top: park.top + "px" });
  document.body.append(h);
}

function point(target, said) {
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
      speak(said || "Right here");
      rookie.back = setTimeout(() => {
        const to = homeSpot() || $("#rookie-home").getBoundingClientRect();
        travel(to.left, to.top, settle);
      }, 4000);
    });
  }, 400);
}

// Rookie's speech bubbles (Bake, Nov 9: "I want rookie to make little chat
// bubbles too with short like "Right here" "Enter username and password" "The
// dashboard task is done""): a few words beside him wherever he is. The agent
// says them with [[say: ...]] (or [[point: target | words]]); the site makes
// them for replies and finished work while the chat is closed. They fade; a
// click opens the chat at that message.
const bubbleState = { timer: 0, frame: 0, seq: null };

function shortLine(text) {
  const words = String(text || "").replace(/\[\[[^\]]*\]\]/g, " ").replace(/[*`#>_]/g, "")
    .split(/(?<=[.!?])\s/)[0].trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "";
  return words.length > 8 ? words.slice(0, 7).join(" ") + "\u2026" : words.join(" ");
}

function speak(text, seq) {
  text = String(text || "").trim().slice(0, 120);
  const box = $("#rookie-say"), h = rookie.holder;
  if (!text || !box || !h || document.body.classList.contains("no-rookie")) return;
  box.textContent = text;
  bubbleState.seq = seq == null ? null : seq;
  box.hidden = false;
  box.classList.remove("fade");
  const follow = () => {                                  // stays beside him while he walks
    const r = h.getBoundingClientRect(), w = box.offsetWidth, ht = box.offsetHeight;
    const right = r.left - w - 6 < 8;
    box.classList.toggle("right", right);
    box.style.left = Math.max(8, Math.min(innerWidth - w - 8, right ? r.right + 6 : r.left - w - 6)) + "px";
    box.style.top = Math.max(8, Math.min(innerHeight - ht - 8, r.top + r.height / 2 - ht / 2 - 10)) + "px";
    bubbleState.frame = requestAnimationFrame(follow);
  };
  cancelAnimationFrame(bubbleState.frame);
  follow();
  clearTimeout(bubbleState.timer);
  bubbleState.timer = setTimeout(() => {
    box.classList.add("fade");
    bubbleState.timer = setTimeout(() => {
      box.hidden = true;
      cancelAnimationFrame(bubbleState.frame);
    }, 600);
  }, Math.min(9000, 3200 + text.length * 60));
}

function chatClosed() {
  return !document.body.classList.contains("dock-open") && !document.body.classList.contains("setup");
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
  scrollChat();
}

// -- Rookie: the house agent's avatar beside its messages --------------------------
// rookie.js is a vendored drop-in (Rookie.mount, .set, .say, .setSkin, .setTheme,
// .sound, .enter, Rookie.bake, SKINS, THEMES); rookie-map.js turns what the
// site sees into its states (docs/castling-first-run.md).

// Exactly one live Rookie on a page (Bake, Nov 9: "only one instance of it on
// any page at a time"): the one in the chat's head. The tab icon copies his
// frames; pickers draw static thumbnails (Rookie.bake), never a live one.
const rookie = { inst: null, driver: null, holder: null, travelling: false, entering: false,
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
  let entered = false;                // core: every page is a new load, so "once" is once per tab
  try { entered = sessionStorage.getItem("rook.entered") === "1"; sessionStorage.setItem("rook.entered", "1"); } catch (x) { /* private mode */ }
  if (rookie.inst.enter && !entered) {            // his first appearance: the transporter, once
    rookie.entering = true;
    setTimeout(() => (rookie.entering = false), 1200);
    rookie.inst.enter(rookie.driver.state());
    rookie.driver.shown = rookie.driver.state();
  } else {                            // core: on a later page he is simply there
    rookie.inst.set(rookie.driver.state());
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
// name; agent.look). Sound, style and theme are the person's; the name is the
// house agent's. They happen on the left (VIEWS.look) and are instant (Bake's
// review, Nov 9: "Make them instant"): a click applies, the next step shows
// with a line in the agent's voice, and the agent hears once, when all four
// are done (or when the person types instead of clicking).
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
}

function show(ev, live) {
  const d = ev.data || {};
  if (ev.type === "input" && typeof d.text === "string") {
    let text = d.text;
    if (text.startsWith(SITE)) {
      // The site's own lines are for the agent, never shown as the person's
      // (Bake's review, Nov 9): only what the person typed, or what they chose.
      if (text.includes(SETUP_KICKOFF)) dock.kicked = true;
      const nl = text.indexOf("\n");
      let rest = nl < 0 ? "" : text.slice(nl + 1).trim();
      if (rest.startsWith(SHOWN)) rest = rest.slice(SHOWN.length).trim();
      if (!rest) return;
      text = rest;
    }
    bubble("me", text);
  } else if (ev.type === "turn" && d.role === "reply") {
    dock.replies++;
    thinking(false);
    bubble("them", d.text, live, ev.seq);
  } else if (ev.type === "status" && d.state === "error") {
    thinking(false);
    if (NOT_READY.test(d.error || "")) {
      if (live && setupMode() && !dock.replies) kickoff();
      return;
    }
    bubble("bad", d.error || "Something went wrong.");
  }
}

async function refresh() {
  try {
    Object.assign(site, await call("agent.site"));
    applyLook(site.look);
    const setup = setupMode();
    const pick = $("#work .look-pick");
    if (typeof lookStep === "function" && work.view === "look" && pick && site.look && site.look.next !== pick.dataset.step &&
        !(site.look.chosen || {})[pick.dataset.step]) {    // the agent set the one the person typed
      if (site.look.next) lookStep(site.look.next);
      else lookDone(site.look);
    }
    await rookShell.reloadNav();      // core: a page the agent added shows in the sidebar
    if (!current && !setup && work.view === "idle") hideWork();         // setup just finished
  } catch (x) { /* next time */ }
}

async function read(wait) {
  const r = await (wait ? callHeld : call)("agent.chat.read", { agent: dock.agent, after: dock.next, limit: 500, wait },
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

// To the agent. What the person typed shows as their message; a site note
// (quiet) goes to the agent out of sight, with `shown` as the person's own
// words for it when there are some ("Saved my account"). `note`: a line of
// context the agent gets ahead of what the person typed.
async function say(text, quiet, shown, note) {
  const where = current ? ` on the page ${current.page} (${current.title})` : "";
  const body = quiet ? `${SITE} ${text}` + (shown ? `\n${SHOWN} ${shown}` : "")
    : `${SITE} The person is${where || " on the hub site"}.${note ? " " + note : ""}\n${text}`;
  thinking(true);
  if (setupMode()) await untilReady();
  try {
    await call("agent.chat.send", { agent: dock.agent, text: body, wait: false }, "Talking with the agent in the site's dock.");
  } catch (x) {
    thinking(false);
    bubble("bad", x.message);
  }
}

// Setup mode: first-run setup isn't finished (agent.setup), so the site is the
// house agent's chat; "Skip to the hub" leaves it for this visit.
let skipped = false;
function setupMode() {
  const on = !skipped && !!dock.agent && !!site.setup && !site.setup.done;
  document.body.classList.toggle("setup", on);
  $("#dock-skip").hidden = !on;
  return on;
}

// core: 0.5 also followed the dock agent's calls as they started and ended (/web/api/activity)
// for Rookie's states and a word when the chat is closed. Core has no feed of calls as they
// happen, so Rookie follows the chat and the typing only (RookieMap.Driver.chat and key).

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
    try { Object.assign(site, await call("agent.site")); } catch (x) { /* next time */ }
  }
  row.remove();
}

// What the agent needs to start setup: the skill, the work pane, and where
// the look and feel is (the site asks those four itself, on the left).
function setupBrief(look) {
  const lookSoFar = typeof window.lookSoFar === "function" ? window.lookSoFar : (l) => `name ${l.name}`;
  const done = !look || !look.next;
  // core: 0.5's hub was the worker "rook"; core's hub caps are called with worker "hub".
  return SETUP_KICKOFF + "; setup isn't finished. Read the first-run skill (cap agent.skill on worker hub, " +
    "args {name: \"first-run\"}) and follow it. The left of the screen is your work pane: open what you set up " +
    "there with [[open: ...]] (the skill says how). " + (done
    ? `The person already picked the look and feel on the left: ${lookSoFar(look || rookie.look)}. ` +
      `${(look || rookie.look).name} is your name now. Greet them warmly as ${(look || rookie.look).name}, in a sentence or two, ` +
      "and go on with step 5."
    : `The site is asking the look and feel on the left (so far: ${lookSoFar(look)}; next: ${look.next}). ` +
      "The person typed to you instead of clicking: answer briefly; if it answers that step, set it with agent.look " +
      "and the site moves on. Don't start step 5 until the site says the look and feel is done.");
}

// The setup walk-through starts (or starts again, when the agent never got
// to answer): once the backend is there.
async function kickoff() {
  if (dock.kicking) return;
  dock.kicking = true;
  try {
    await untilReady();
    dock.kicked = true;
    await say(setupBrief(site.look), true);
  } finally {
    dock.kicking = false;
  }
}

async function startDock(agent) {
  dock.agent = agent;
  $("#dock-name").textContent = agent === "house" ? rookie.look.name : agent;
  rookieInit();
  $("#dock").hidden = false;
  let open = true;
  try {
    open = phone() ? sessionStorage.getItem("rook.dock.phone") !== "0" : localStorage.getItem("rook.dock") !== "0";
  } catch (x) { /* private mode */ }
  if (open && phone()) dockOpen(true);     // a phone opens on the chat at once, not on the page while it reads
  try {
    while ((await read(0)) >= 500) { /* catch up on a long conversation */ }
  } catch (x) {                     // not someone who may talk to it: no dock
    $("#dock").hidden = true;
    document.body.classList.remove("dock-open");
    return;
  }
  if (setupMode()) {                // a new hub: the chat is the whole site until setup is done
    open = true;
    if (typeof lookStart === "function" && lookStart()) {   // the look and feel first, on the left; no agent turn yet
    } else if (!dock.replies) {    // a new hub, or the agent never got to answer
      idleWork();
      await kickoff();
    } else if (work.last) {
      openView(work.last.view, work.last.arg, work.last.args, false);       // what we were working on
    } else {
      idleWork();
    }
  } else if (dock.shown === 0) {    // first run: the agent asks how the person wants to use the site
    open = true;
    // core: 0.5 went on "and offer to make pages for what they want to keep an eye on
    // (page.components, page.create) and to set their start page (page.start)"; core has no
    // page caps, so that part is left out.
    await say("First visit to the hub site. Welcome the person and ask how they want to use the site.", true);
  }
  // What the person chose while the conversation loaded wins over what was read before it (brief AP6):
  // a tap on Chat, on Rookie or on the X during the load is never undone here.
  if (dock.chose !== null) open = dock.chose;
  if (!open) document.body.classList.remove("dock-open");
  if (rookie.holder && !open) placeRookie();
  dockOpen(open);
  scrollChat();
  for (;;) {
    try {
      await read(READ_WAIT);
    } catch (x) {
      await sleep(5000);
    }
  }
}

// The dock's markup and wiring: the chat beside the page, the work pane on
// the stage, Rookie's corner and his speech bubble. Only when there is a dock.
function mountDock() {
  document.body.insertAdjacentHTML("beforeend",          // core: no #bar
    // no Chat button (Bake, Nov 9): Rookie opens the chat. This one is never shown; the desktop app clicks it (OPEN_DOCK_JS_05).
    '<button id="dock-btn" class="offscreen" tabindex="-1" aria-hidden="true">Chat</button>');
  // core: the shell has #work at the head of the page area already, and #shell-dock beside it
  $("#shell-dock").insertAdjacentHTML("beforeend", `<aside id="dock" hidden>
  <div id="dock-head"><div id="rookie-home"></div><span id="dock-name"></span><span class="grow"></span><button id="dock-skip" class="link" hidden>Skip to the hub</button><button id="dock-close" aria-label="Close the chat" title="Close the chat">&times;</button></div>
  <div id="dock-log" aria-live="polite"></div>
  <form id="dock-form"><textarea id="dock-text" rows="2" placeholder="Ask, or tell it what to set up"></textarea><button>Send</button></form>
</aside>
<div id="rookie-park" aria-hidden="true"></div>
<div id="rookie-say" role="status" hidden></div>
<button id="dock-phone" type="button">Chat</button>`);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(scrollChat);
  window.addEventListener("resize", keepPlace);
  $("main.main").addEventListener("scroll", keepPlace, { passive: true });     // core: the page area scrolls
  $("#dock-form").onsubmit = (e) => {
    e.preventDefault();
    const text = $("#dock-text").value.trim();
    if (!text) return;
    $("#dock-text").value = "";
    // Typed during the look steps: the agent hears where setup is (once with the skill).
    const look = site.look && site.look.next && setupMode() && work.view === "look" ? site.look : null;
    const lookSoFar = typeof window.lookSoFar === "function" ? window.lookSoFar : (l) => `name ${l.name}`;
    if (look && !dock.kicked && !dock.replies) {
      dock.kicked = true;
      return say(text, false, null, setupBrief(look));
    }
    say(text, false, null, look ? `They are on the look-and-feel step ${look.next}, on the left (so far: ${lookSoFar(look)}); ` +
      "if this answers it, set it with agent.look." : "");
  };
  $("#dock-text").oninput = () => rookie.driver && rookie.driver.key();
  $("#dock-text").onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      $("#dock-form").requestSubmit();
    }
  };
  $("#dock-skip").onclick = () => $("#dock-close").onclick();
  $("#dock-btn").onclick = () => choose(true);          // never shown: the desktop app's "ask about this page"
  $("#dock-phone").onclick = () => choose(!document.body.classList.contains("dock-open"));   // a phone's one control
  $("#rookie-say").onclick = () => {                       // the chat, at that message
    $("#rookie-say").hidden = true;
    choose(true);
    const row = bubbleState.seq != null && document.querySelector(`#dock-log [data-seq="${bubbleState.seq}"]`);
    if (row) setTimeout(() => {
      row.scrollIntoView({ block: "center" });
      row.classList.add("pointed");
      setTimeout(() => row.classList.remove("pointed"), 2500);
    }, 50);
  };
  document.addEventListener("click", (e) => {               // Rookie in his corner: open the chat
    if (rookie.holder && rookie.holder.classList.contains("parked") && rookie.holder.contains(e.target)) choose(true);
  });
  $("#dock-close").onclick = () => {
    if (document.body.classList.contains("setup")) {
      skipped = true;
      setupMode();
      if (!current) hideWork();       // core: back to the page this load shows
      return;
    }
    choose(false);
  };
}

// The shell's start: the agent in the dock (agent.site); none, no dock. In
// setup the chat is the whole site, so the start page stays closed.
if (typeof shellHooks !== "undefined") {
  shellHooks.starting.push(async () => {
    try {
      Object.assign(site, await call("agent.site"));
    } catch (x) {                     // core: no agent.site on this hub (no agents runtime): no dock
      return false;
    }
    if (site.look) rookie.look = site.look;
    if (!site.dock) return false;
    mountDock();
    // core: 0.5 also hid the work view when a hub page opened (shellHooks.opening); a core page is a new load.
    startDock(site.dock).catch((x) => bubble("bad", x.message));
    return !!(site.setup && !site.setup.done);
  });
}
