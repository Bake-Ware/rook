// The house agent beside every page of the hub site: the chat dock, Rookie,
// the work pane and the guided first run. The agents package's part of the
// shell (manifest shell:): it loads after the shell's shell.js and uses its
// helpers ($, el, api, call, openPage, site, current); agent.site says which
// agent is in the dock. Loaded on every shell page; it starts on the main one
// only (shellHooks).
"use strict";

// -- the dock: the house agent beside every page ----------------------------------

const SITE = "[site]";      // a line the site adds for the agent: where the person is
const SHOWN = "[shown]";    // after a site note: the person's own words for it, shown as theirs
const SETUP_KICKOFF = "First visit to a new hub";
const dock = { agent: null, next: 0, shown: 0, replies: 0, kicking: false, kicked: false };
// A turn that failed because no worker serves the agent's backend yet (the
// worker joins a few seconds after an install): not a message; wait and retry.
const NOT_READY = /no single worker serves|nothing provides agent\.backend/i;

// Bake, Nov 9: "I want to have like an X at the top right, and move the rookie
// avatar to the bottom right corner when closed. Remove the chat button from
// the like top nav on pages. Clicking rookie opens the chat sidebar."
function dockOpen(open) {
  const was = document.body.classList.contains("dock-open");
  if (!open && was && rookie.holder && !document.body.classList.contains("setup")) liftOff();   // he walks out before the chat goes
  document.body.classList.toggle("dock-open", open);
  try { localStorage.setItem("rook.dock", open ? "1" : "0"); } catch (x) { /* private mode */ }
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
  while (log.children.length > 200) log.firstChild.remove();
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
      if (live && !pop.window) later.push(() => openView(part.view, part.arg, part.args, true));
    } else if (part.kind === "point" && live && !pop.window) {
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

function hideWork() {
  $("#work").hidden = true;
  work.focus = null;
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
  return String(site.person || "").replace(/^person:/, "");
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
  if (view === "page") {
    hideWork();
    location.hash = "#/" + arg;
    return;
  }
  const make = VIEWS[view];
  if (!make) return bubble("bad", `There's no ${view} view here yet.`);
  make(args, !!live);
}

const VIEWS = {
  async vault(args, live) {
    const pane = workView("vault", "Vault", `Secrets the hub keeps sealed. ${agentName()} uses them by name; their values are never shown.`);
    const list = el("ul", { className: "secrets" });
    list.dataset.target = "vault.list";
    const slot = el("div");
    pane.append(slot, el("h3", {}, "In the vault"), list);
    const wanted = args.new || args.key || "";
    let names = [];
    const load = async () => {
      let r;
      try { r = await call("secret.list", {}, "Listing the vault in the Vault pane."); } catch (x) { r = { secrets: [], error: x.message }; }
      names = r.secrets.map((x) => x.name);
      list.replaceChildren(...r.secrets.map((x) => {
        const li = el("li");
        li.append(el("code", {}, x.name), el("span", { className: "muted" }, x.description || ""));
        return li;
      }));
      if (!r.secrets.length) list.append(el("li", { className: "muted" }, r.error || "Nothing yet."));
    };
    const done = (name) => slot.replaceChildren(doneCard("vault.done", `Saved ${name}`,
      [`It's sealed in the vault; ${agentName()} uses it by name.`], ["Add another secret", () => form("")]));
    const form = (key0) => {
      const f = el("form", { className: "work-form tick" });
      f.append(el("h3", {}, "New secret"));
      const key = field(f, "Key", { dataset: { target: "vault.key" }, input: { type: "text", value: key0, autocomplete: "off", required: true } });
      const value = field(f, "Value", { dataset: { target: "vault.value" }, input: { type: "password", autocomplete: "new-password", required: true } });
      const what = field(f, "What it's for", { dataset: { target: "vault.description" }, input: { type: "text", value: key0 ? args.description || args.for || "" : "" } });
      const save = el("button", { type: "submit" }, "Save");
      save.dataset.target = "vault.save";
      const out = el("div", { className: "out bad" });
      f.append(save, out);
      f.onsubmit = async (e) => {
        e.preventDefault();
        let r;
        try {
          r = await call("secret.set", { name: key.value.trim(), value: value.value, description: what.value.trim() },
            "The person saved a secret in the Vault pane.");
        } catch (x) {
          out.textContent = x.message;
          return;
        }
        value.value = "";
        done(r.name);
        work.focus = null;
        load();
        // Never the braces here: a vault reference in chat text would be filled in.
        say(`Secret ${r.name} saved in the vault. The person typed it in the Vault pane; use it as a vault ` +
          `reference (secret:${r.name} in double braces).`, true, `Saved ${r.name} in the vault`);
      };
      slot.replaceChildren(f);
      focusWork(key0 ? value : key);
      return f;
    };
    await load();
    if (!live && wanted && names.includes(wanted)) return done(wanted);      // saved before the reload
    form(wanted);
    if (wanted && live) setTimeout(() => point("vault.value"), 500);
  },

  async account(args, live) {
    const pane = workView("account", "Your account", "Your name, and a password: once it is set, this site asks for sign-in.");
    const user = userName();
    const slot = el("div");
    pane.append(slot);
    const done = (who, setPw) => slot.replaceChildren(doneCard("account.done", who ? `Saved: ${who}` : "Saved",
      [`You'll sign in as ${user}` + (setPw ? " with the password you set." : "; there's no password yet."),
        "You can change both later on Your account."], ["Change", () => form(who)]));
    const form = (name0) => {
      const f = el("form", { className: "work-form tick" });
      const name = field(f, `What should ${agentName()} call you?`, { dataset: { target: "account.name" }, input: { type: "text", value: name0 || "", autocomplete: "name" } });
      // The sign-in name is the user the installer made; what they're called is just a name
      // (Bake's review, Nov 9: "have the view show 'You'll sign in as: <user name>'").
      const signin = el("p", { className: "signin" });
      signin.dataset.target = "account.user";
      signin.append("You'll sign in as: ", el("b", {}, user));
      const pw = field(f, "Password", { dataset: { target: "account.password" }, input: { type: "password", autocomplete: "new-password" } });
      const again = field(f, "Password again", { dataset: { target: "account.again" }, input: { type: "password", autocomplete: "new-password" } });
      f.insertBefore(signin, pw.previousSibling);
      const save = el("button", { type: "submit" }, "Save");
      save.dataset.target = "account.save";
      const out = el("div", { className: "out bad" });
      f.append(save, out);
      f.onsubmit = async (e) => {
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
        const who = name.value.trim();
        remember("account.name", who);
        done(who, setPw);
        work.focus = null;
        say(`Account saved. In the Account pane the person ${who ? `asked to be called "${who}"` : "gave no name"}` +
          `${setPw ? " and set a password" : " and set no password"}. They sign in as ${user} (the pane shows it), ` +
          "not by the name they gave. Go on.", true, "Saved my account");
      };
      slot.replaceChildren(f);
      focusWork(name.value ? pw : name);
      return f;
    };
    if (!live) {                                           // a reload: done already?
      const me = await api("/web/api/me").catch(() => ({}));
      if (me.password) return done(recall("account.name"), true);
    }
    form(args.name);
    if (live) setTimeout(() => point(args.name ? "account.password" : "account.name"), 500);
  },

  network(args) {
    const pane = workView("network", "Reaching this site", "Where you'll open this site from.");
    const here = location.origin;
    const rows = [
      ["network.local", "Just this computer", `Only a browser on this machine opens it: ${args.local || here}`],
      ["network.home", "Other devices at home", `Phones and laptops on your home network open it at ${args.home || `this machine's address (${agentName()} finds it)`}. This machine's firewall has to let them in.`],
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

  // Look and feel, the first four setup steps (Bake's review, Nov 9): a stage
  // Rookie walks onto (the one live Rookie, shown big) and the picker for the
  // step; a click applies at once and the next one shows, no agent turn.
  look() {
    const look = site.look;
    const pane = workView("look", "Look and feel", `How ${look.name} looks and sounds. You can change all of it later on Your account.`);
    const stage = el("div", { className: "look-stage" });
    stage.dataset.target = "look.stage";
    const spot = el("div", { id: "look-spot" });
    stage.append(spot, el("div", { className: "floor" }));
    const steps = el("ol", { className: "look-steps" });
    const pick = el("div", { className: "look-pick" });
    pane.append(stage, steps, pick);
    lookStep(look.next);
    setTimeout(goHome, rookie.entering ? 1200 : 50);       // onto the stage
  },
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

// The target's box once it is in view: one shown here (a work view, the shell;
// "page" is the open page itself), else a control in the open page (locate).
async function targetBox(target) {
  const t = target === "page" ? current && current.frame : document.querySelector(`[data-target="${CSS.escape(target)}"]`);
  if (!t || !t.getClientRects().length) {
    for (let tries = 0; tries < 10; tries++) {              // a page just opened may not have drawn it yet
      const box = await locate(target);
      if (box) return box;
      await sleep(500);
    }
    return null;
  }
  t.scrollIntoView({ block: "center", behavior: "smooth" });
  t.classList.add("pointed");
  setTimeout(() => t.classList.remove("pointed"), 6000);
  await sleep(400);                                       // after the scroll settles
  return t.getBoundingClientRect();
}

// Where Rookie stands to point at a box: to its right, else its left.
function beside(r, w, ht) {
  return { x: r.right + 10 + w < innerWidth ? r.right + 10 : Math.max(8, r.left - w - 10),
    y: Math.max(8, r.top + r.height / 2 - ht / 2) };
}

async function point(target, said) {
  const r = await targetBox(target);
  const h = rookie.holder;
  if (!r || !h || !rookie.inst || document.body.classList.contains("no-rookie")) return;
  if (pop.out) return visit(r, said);
  clearTimeout(rookie.back);
  const me = h.getBoundingClientRect(), to = beside(r, me.width, me.height);
  travel(to.x, to.y, () => {
    rookie.inst.set("talking");                          // there: this one
    speak(said || "Right here");
    rookie.back = setTimeout(() => {
      const home = homeSpot() || $("#rookie-home").getBoundingClientRect();
      travel(home.left, home.top, settle);
    }, 4000);
  });
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
  if (!text || !box || !h || !rookie.here || document.body.classList.contains("no-rookie")) return;
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

// The calls worth a word when the chat is closed (the agent's own [[say]] wins).
function sayFor(ev) {
  if (ev.phase !== "end") return "";
  if (ev.decision === "held") return "I need your OK";
  if (ev.ok === false) return "";
  const args = JSON.stringify(ev.args || {});
  if (/^task\.(update|done|close)$/.test(ev.cap) && /"(done|closed)"/.test(args)) return "That task is done";
  if (ev.cap === "page.create") return "Your new page is ready";
  if (ev.cap === "page.components" || ev.cap === "page.update") return "Page updated";
  if (/^job\.(create|add)$/.test(ev.cap)) return "Job set up";
  if (ev.cap === "knowledge.create") return "Saved to the wiki";
  return "";
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
// here: he is in this window (not while the chat is out in its own).
const rookie = { inst: null, driver: null, holder: null, travelling: false, entering: false, here: true,
  look: { sound: false, rookie: "pixel", theme: "stone", name: "Rookie" } };

const rookieBoth = {
  set(s) { if (rookie.inst && !rookie.travelling && rookie.here) rookie.inst.set(s); },
  say(text, o) { if (rookie.inst && !rookie.travelling && rookie.here) rookie.inst.say(text, o); },
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
  if (rookie.inst.enter && rookie.here) {     // his first appearance: one enter variant, once
    rookie.entering = true;
    setTimeout(() => (rookie.entering = false), 1200);
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
  const left = look.side === "left";
  if (left !== document.body.classList.contains("chat-left")) {
    document.body.classList.toggle("chat-left", left);
    $("#dock-side").title = `Move the chat to the ${left ? "right" : "left"}`;
    if (rookie.holder && rookie.holder.classList.contains("parked")) goHome();     // over to the other corner
  }
}

const LOOK_TITLES = { sound: "Sound", rookie: "Style", theme: "Theme", name: "Name" };

// What the person chose, in their words (their message in the chat).
function lookSaid(step, value) {
  if (step === "sound") return value ? "Sound on" : "No sound";
  if (step === "rookie") return value === "none" ? "No avatar" : value;
  return String(value);
}

// The agent's line for a step, in its voice (no turn needed).
function lookLine(step, look) {
  const n = look.name;
  if (step === "sound") return `Hi! I'm ${n}, your house agent. I'll set up this hub with you, one step at a time. ` +
    "First a few quick choices, on the left: do you want sound? I make small blips as I work.";
  if (step === "rookie") return "Pick a style for me.";
  if (step === "theme") return "And a colour theme.";
  if (step === "name") return `What should I be called? Type a name, or keep ${n}.`;
  return "";
}
const LOOK_SAY = { sound: "Sound?", rookie: "Pick my style", theme: "Pick a colour", name: "Name me?" };

function lookSoFar(look) {
  return `sound ${look.sound ? "on" : "off"}, style ${look.rookie}, theme ${look.theme}, name ${look.name}`;
}

// The step list (what's chosen; a click goes back to one) and its picker.
function lookStep(step) {
  const look = site.look, pick = $("#work .look-pick"), steps = $("#work .look-steps");
  if (!pick || !step) return;
  steps.replaceChildren(...["sound", "rookie", "theme", "name"].map((k, i) => {
    const li = el("li", { className: (k === step ? "on" : "") + (look.chosen[k] ? " chosen" : "") });
    const b = el("button", { type: "button", className: "link" });
    b.append(el("span", { className: "n" }, String(i + 1)), el("span", {}, LOOK_TITLES[k]));
    if (look.chosen[k]) b.append(el("b", {}, lookSaid(k, k === "theme" ? look.theme : look[k])));
    b.disabled = !look.chosen[k] && k !== step;
    b.onclick = () => lookStep(k);
    li.append(b);
    return li;
  }));
  pick.replaceChildren();
  pick.dataset.step = step;
  pick.dataset.target = "look." + step;
  const box = el("div", { className: "looks" });
  const option = (label, skin, theme, value) => {
    const b = el("button", { type: "button", title: label });
    const face = el("div", { className: "face" });
    if (skin) face.append(rookieThumb(skin, theme));
    else face.append(el("span", { className: "glyph" }, value ? "♪" : "—"));
    b.append(face, el("span", {}, label));
    const cur = step === "theme" ? look.theme : look[step];
    if (look.chosen[step] && cur === value) b.classList.add("picked");
    b.onclick = () => lookAnswer(step, value);
    box.append(b);
    return b;
  };
  let first = null;
  if (step === "sound") {
    first = option("Sound on", null, null, true);
    option("No sound", null, null, false);
  } else if (step === "rookie") {
    for (const k of look.styles) {
      const b = option(k === "none" ? "none (hide me)" : k, k, look.theme, k);
      first = first || b;
    }
  } else if (step === "theme") {
    const skin = look.rookie === "none" ? "pixel" : look.rookie;
    for (const k of look.themes) {
      const b = option(k, skin, k, k);
      first = first || b;
    }
  } else if (step === "name") {
    const form = el("form", { className: "name" });
    const input = el("input", { type: "text", maxLength: 40, placeholder: look.name, value: "" });
    input.dataset.target = "look.name.input";
    const save = el("button", { type: "submit", className: "primary" }, "Name me");
    const keep = el("button", { type: "button" }, `Keep ${look.name}`);
    form.onsubmit = (e) => {
      e.preventDefault();
      lookAnswer("name", input.value.trim() || look.name);
    };
    keep.onclick = () => lookAnswer("name", look.name);
    form.append(input, save, keep);
    box.append(form);
    first = input;
  }
  pick.append(el("h3", {}, LOOK_TITLES[step]), box);
  if (first) focusWork(first);
}

async function lookAnswer(step, value) {
  bubble("me", lookSaid(step, value));
  let out;
  try {
    out = await call("agent.look", { [step]: value }, "The person picked the look and feel on the setup screen.");
  } catch (x) {
    return bubble("bad", x.message);
  }
  site.look = out;
  applyLook(out);
  if (out.next) {
    const ack = step === "sound" ? (value ? "Sound on. " : "Quiet it is. ") : step === "rookie" && value === "none" ? "I'll stay out of sight. " : "";
    bubble("them", ack + lookLine(out.next, out));
    speak(LOOK_SAY[out.next]);
    return lookStep(out.next);
  }
  lookDone(out);
}

// All four chosen: the left goes back to the welcome view, Rookie walks home,
// and the agent hears it once (the setup walk-through starts then).
function lookDone(look) {
  if (lookDone.sent) return;
  lookDone.sent = true;
  bubble("them", `${look.name} it is. You can change all of this later on Your account.`);
  idleWork();
  goHome();
  if (!dock.kicked && !dock.replies) kickoff();
  else say(`The person finished the look and feel on the left: ${lookSoFar(look)}. ${look.name} is your name now. ` +
    "Go on with setup (step 5 of the first-run skill).", true);
}

// The look steps on the left, with the line for the step they're on.
function lookStart() {
  const look = site.look;
  if (!look || !look.next || !setupMode()) return false;
  VIEWS.look();
  bubble("them", lookLine(look.next, look));
  setTimeout(() => speak(LOOK_SAY[look.next]), rookie.entering ? 1800 : 700);
  return true;
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
    Object.assign(site, await call("page.site"), await call("agent.site"));
    applyLook(site.look);
    const setup = setupMode();
    const pick = $("#work .look-pick");
    if (work.view === "look" && pick && site.look && site.look.next !== pick.dataset.step &&
        !(site.look.chosen || {})[pick.dataset.step]) {    // the agent set the one the person typed
      if (site.look.next) lookStep(site.look.next);
      else lookDone(site.look);
    }
    if (pop.window) return;
    await loadNav();
    if (current && !pages.some((p) => p.page === current.page)) {      // archived meanwhile
      history.replaceState(null, "", location.pathname);
      openPage(wanted());
    } else if (!current && !setup) {                                   // setup just finished
      openPage(wanted());
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

// To the agent. What the person typed shows as their message; a site note
// (quiet) goes to the agent out of sight, with `shown` as the person's own
// words for it when there are some ("Saved my account"). `note`: a line of
// context the agent gets ahead of what the person typed.
async function say(text, quiet, shown, note) {
  const at = pop.window ? pop.where : here();
  const where = at ? ` on the page ${at.page} (${at.title})` : "";
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
        if (ev.agent === "agent:" + dock.agent && ev.person === site.person) {
          rookie.driver.activity(ev);
          const line = chatClosed() && sayFor(ev);
          if (line) speak(line);
        }
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
    try { Object.assign(site, await call("agent.site")); } catch (x) { /* next time */ }
  }
  row.remove();
}

// What the agent needs to start setup: the skill, the work pane, and where
// the look and feel is (the site asks those four itself, on the left).
function setupBrief(look) {
  const done = !look || !look.next;
  return SETUP_KICKOFF + "; setup isn't finished. Read the first-run skill (cap agent.skill on worker rook, " +
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
  try { open = localStorage.getItem("rook.dock") !== "0" && innerWidth > 760; } catch (x) { /* private mode */ }
  try {
    while ((await read(0)) >= 500) { /* catch up on a long conversation */ }
  } catch (x) {                     // not someone who may talk to it: no dock
    $("#dock").hidden = true;
    return;
  }
  if (pop.window) {                 // the chat in its own window: just the chat
    open = true;
  } else if (setupMode()) {         // a new hub: the chat is the whole site until setup is done
    open = true;
    if (lookStart()) {             // the look and feel first, on the left; no agent turn yet
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
    await say("First visit to the hub site. Welcome the person, ask how they want to use the site, " +
      "and offer to make pages for what they want to keep an eye on (page.components, page.create) " +
      "and to set their start page (page.start).", true);
  }
  if (!open) document.body.classList.remove("dock-open");
  if (rookie.holder && !open) placeRookie();
  dockOpen(open);
  scrollChat();
  if (rookie.driver) activity();
  for (;;) {
    try {
      await read(55);
    } catch (x) {
      await sleep(5000);
    }
  }
}

// The dock's markup and wiring: the chat beside the page, the work pane on
// the stage, Rookie's corner and his speech bubble. Only when there is a dock.
function mountDock() {
  $("#bar").insertAdjacentHTML("beforeend",
    // no Chat button (Bake, Nov 9): Rookie opens the chat. This one is never shown; the desktop app clicks it (OPEN_DOCK_JS_05).
    '<button id="dock-btn" class="offscreen" tabindex="-1" aria-hidden="true">Chat</button>');
  $("#note").insertAdjacentHTML("beforebegin", '<section id="work" hidden></section>');
  $("main").insertAdjacentHTML("afterend", `<aside id="dock" hidden>
  <div id="dock-head"><div id="rookie-home"></div><span id="dock-name"></span><span class="grow"></span><button id="dock-skip" class="link" hidden>Skip to the hub</button><button id="dock-pop" class="head-btn" aria-label="Open the chat in its own window" title="Open the chat in its own window">&#8599;</button><button id="dock-side" class="head-btn" aria-label="Move the chat to the other side" title="Move the chat to the left">&#8644;</button><button id="dock-close" class="head-btn" aria-label="Close the chat" title="Close the chat">&times;</button></div>
  <div id="dock-log" aria-live="polite"></div>
  <form id="dock-form"><textarea id="dock-text" rows="2" placeholder="Ask, or tell it what to set up"></textarea><button>Send</button></form>
</aside>
<div id="rookie-park" aria-hidden="true"></div>
<div id="rookie-say" role="status" hidden></div>`);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(scrollChat);
  window.addEventListener("resize", keepPlace);
  $("#work").addEventListener("scroll", keepPlace, { passive: true });
  $("#dock-form").onsubmit = (e) => {
    e.preventDefault();
    const text = $("#dock-text").value.trim();
    if (!text) return;
    $("#dock-text").value = "";
    // Typed during the look steps: the agent hears where setup is (once with the skill).
    const look = site.look && site.look.next && setupMode() && work.view === "look" ? site.look : null;
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
  $("#dock-side").onclick = async () => {
    try {
      site.look = await call("agent.look", { side: rookie.look.side === "left" ? "right" : "left" },
        "The person moved the chat to the other side of the page.");
    } catch (x) {
      return bubble("bad", x.message);
    }
    applyLook(site.look);
  };
  $("#dock-pop").onclick = () => {                         // it goes out once the window says so (popHeard)
    if (!window.open("/?chat", "rook-chat", "popup,width=460,height=760")) bubble("bad", "The browser blocked the chat window.");
  };
  $("#dock-log").addEventListener("click", (e) => {        // in the chat window, a link opens in the main one
    const a = pop.window && e.target.closest("a[href]");
    if (!a) return;
    e.preventDefault();
    popSend("go", { href: a.getAttribute("href") });
  });
  $("#dock-btn").onclick = () => dockOpen(true);          // never shown: the desktop app's "ask about this page"
  $("#rookie-say").onclick = () => {                       // the chat, at that message
    $("#rookie-say").hidden = true;
    dockOpen(true);
    const row = bubbleState.seq != null && document.querySelector(`#dock-log [data-seq="${bubbleState.seq}"]`);
    if (row) setTimeout(() => {
      row.scrollIntoView({ block: "center" });
      row.classList.add("pointed");
      setTimeout(() => row.classList.remove("pointed"), 2500);
    }, 50);
  };
  document.addEventListener("click", (e) => {               // Rookie in his corner: open the chat
    if (rookie.holder && rookie.holder.classList.contains("parked") && rookie.holder.contains(e.target)) dockOpen(true);
  });
  $("#dock-close").onclick = () => {
    if (pop.window) return window.close();                // docks it back (pagehide)
    if (document.body.classList.contains("setup")) {
      skipped = true;
      setupMode();
      if (!current) openPage(wanted());
      return;
    }
    dockOpen(false);
  };
}

// -- the chat in its own window ------------------------------------------------------
// The site design: "The chat can pop out into its own browser window: same
// chat, same stream; closing the window docks it back; Rookie leaves one
// window and arrives in the other". The chat window is the site at /?chat
// showing only this dock, reading the same conversation; the two windows
// talk over a BroadcastChannel. The chat window says "out" every 2 s and
// "in" as it goes; the main window takes the chat back when it hears "in" or
// nothing for 5 s. Opening pages and pointing stay in the main window.
const pop = { window: new URLSearchParams(location.search).has("chat"), out: false, seen: 0, chan: null, where: null };
const LEAVE_MS = 2300;          // rookie.js's exit: 14 frames of 150 ms

function popSend(kind, more) {
  if (pop.chan) pop.chan.postMessage({ kind, ...more });
}

function here() {
  return current ? { page: current.page, title: $("#title").textContent } : null;
}

async function popStart() {
  if (!window.BroadcastChannel) return;
  pop.chan = new BroadcastChannel("rook.chat");
  pop.chan.onmessage = (e) => popHeard(e.data || {});
  if (pop.window) {
    document.body.classList.add("chat-window");
    popSend("out");
    setInterval(() => popSend("out"), 2000);
    addEventListener("pagehide", () => popSend("in"));
    return;
  }
  setInterval(() => pop.out && Date.now() - pop.seen > 5000 && comeBack(), 1000);
  popSend("hello");
  await sleep(250);                 // a chat window that is out answers by then
}

function popHeard(m) {
  if (pop.window) {
    if (m.kind === "hello") popSend("out");
    else if (m.kind === "here") pop.where = m.where;
    else if (m.kind === "lend") leave();
    else if (m.kind === "return") arrive();
  } else if (m.kind === "out") {
    pop.seen = Date.now();
    if (!pop.out) goOut();
    popSend("here", { where: here() });
  } else if (m.kind === "in") {
    pop.seen = Date.now() - 3500;   // back in 1.5 s, unless a reloaded chat window says out again
  } else if (m.kind === "go" && /^[#/]/.test(String(m.href))) {
    location.assign(m.href);
  }
}

function goOut() {
  pop.out = true;
  leave();
  document.body.classList.add("chat-out");
}

function comeBack() {
  pop.out = false;
  document.body.classList.remove("chat-out");
  document.body.classList.add("dock-open");
  dockOpen(true);
  arrive();
}

function leave() {              // Rookie leaves this window (rookie.js's exit)
  if (!rookie.here) return;
  rookie.here = false;
  clearTimeout(rookie.back);
  $("#rookie-say").hidden = true;
  if (!rookie.holder || !rookie.inst) return;
  liftOff();
  rookie.inst.exit();
}

function arrive() {             // and arrives in this one (rookie.js's entrance)
  if (rookie.here) return;
  rookie.here = true;
  if (!rookie.holder || !rookie.inst) return;
  settle();
  rookie.inst.enter(rookie.driver.state());
  rookie.driver.shown = rookie.driver.state();
}

// The chat is out: Rookie leaves the chat window, arrives here beside the
// target, points, and goes back the same way.
function visit(r, said) {
  const h = rookie.holder;
  clearTimeout(rookie.back);
  popSend("lend");
  rookie.back = setTimeout(() => {
    h.classList.remove("parked", "staged");
    h.classList.add("away");
    document.body.append(h);
    const to = beside(r, h.offsetWidth, h.offsetHeight);
    Object.assign(h.style, { transition: "none", left: to.x + "px", top: to.y + "px" });
    rookie.here = true;
    rookie.inst.enter("talking");
    speak(said || "Right here");
    rookie.back = setTimeout(() => {
      leave();
      rookie.back = setTimeout(() => popSend("return"), LEAVE_MS);
    }, 5000);
  }, LEAVE_MS);
}

// The shell's start: the agent in the dock (agent.site); none, no dock. In
// setup the chat is the whole site, so the start page stays closed; in the
// chat window, always.
if (typeof shellHooks !== "undefined") {
  shellHooks.starting.push(async () => {
    Object.assign(site, await call("agent.site"));
    if (site.look) rookie.look = site.look;
    if (!site.dock) return false;
    mountDock();
    await popStart();
    shellHooks.opening.push(hideWork);                // a hub page replaces a work view
    startDock(site.dock).catch((x) => bubble("bad", x.message));
    return pop.window || !!(site.setup && !site.setup.done);
  });
}
