// First-run setup on the hub site (brief HA3): the work pane's views the house agent opens with
// [[open: ...]] (Vault, Account, Network) and the look-and-feel steps 1 to 4 (VIEWS.look and the
// look* functions the dock calls when they are defined).
//
// Ported from 0.5 (cbe86a47: rook/plugins/agents/shell/dock.js, "the work pane" and "Look and
// feel"): the same views, steps, targets, words and [site] notes. A classic script loaded after
// dock.js (the agents plugin's site.shell list), so it uses dock.js's names as 0.5's code did:
// VIEWS, workView, field, doneCard, focusWork, idleWork, work, say, bubble, point, speak, agentName,
// remember, recall, goHome, applyLook, kickoff, setupMode, call, site, dock, rookie, el and $.
//
// Where core differs (each the smallest change):
// - The vault: core's secret.set takes a scope (here "hub", the whole hub) and keeps no
//   description, so the Vault form has no "What it's for" field and the list shows each secret's
//   scope; a vault reference is the whole string secret:<name>.
// - The account: the sign-in name and whether a password is set come from core's /api/session,
//   and the password goes to core's POST /api/password (which also ends open-until-claimed).
// - "Your account" is the Settings page's "Your account" tab (settings-account.js), core having no
//   account page of its own.
"use strict";

// Core's own site API (fetch, JSON, the error's words), for the two account requests that are not
// cap calls. Separate from dock.js's api(), whose 0.5 shape answered {ok, error}.
async function setupApi(method, path, body) {
  const r = await fetch(path, { method, credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {}, body: body ? JSON.stringify(body) : undefined });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.message ? data.message + (data.next ? " " + data.next : "") : r.statusText);
  return data;
}

// A still picture of Rookie for the pickers (Rookie.bake), as 0.5's rookieThumb; dock.js's when it has one.
function setupThumb(skin, theme) {
  if (typeof rookieThumb === "function") return rookieThumb(skin, theme);
  if (!window.Rookie || !Rookie.bake || skin === "none" || !Rookie.SKINS.includes(skin)) return el("span");
  const c = el("canvas");
  const { canvas, manifest } = Rookie.bake({ skin, theme, scale: 2, rows: ["happy"] });
  const [w, h] = manifest.cell;
  c.width = w;
  c.height = h;
  c.getContext("2d").drawImage(canvas, 0, 0, w, h, 0, 0, w, h);
  return c;
}

// -- the work pane's views ------------------------------------------------------------------

VIEWS.vault = async function (args, live) {
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
      li.append(el("code", {}, x.name), el("span", { className: "muted" }, x.scope === "hub" ? "" : x.scope));
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
    const save = el("button", { type: "submit" }, "Save");
    save.dataset.target = "vault.save";
    const out = el("div", { className: "out bad" });
    f.append(save, out);
    f.onsubmit = async (e) => {
      e.preventDefault();
      let r;
      try {
        r = await call("secret.set", { scope: "hub", name: key.value.trim(), value: value.value },
          "The person saved a secret in the Vault pane.");
      } catch (x) {
        out.textContent = x.message;
        return;
      }
      const name = (r.created || r).name;
      value.value = "";
      done(name);
      work.focus = null;
      load();
      // Never the whole reference alone in the note's text: a call argument that is exactly a vault
      // reference is filled in.
      say(`Secret ${name} saved in the vault. The person typed it in the Vault pane; use it as a vault ` +
        `reference (the whole string secret:${name}).`, true, `Saved ${name} in the vault`);
    };
    slot.replaceChildren(f);
    focusWork(key0 ? value : key);
    return f;
  };
  await load();
  if (!live && wanted && names.includes(wanted)) return done(wanted);      // saved before the reload
  form(wanted);
  if (wanted && live) setTimeout(() => point("vault.value"), 500);
};

VIEWS.account = async function (args, live) {
  const pane = workView("account", "Your account", "Your name, and a password: once it is set, this site asks for sign-in.");
  let me = {};
  try { me = await setupApi("GET", "/api/session"); } catch (x) { /* shown as no name below */ }
  const user = (me.user && me.user.name) || "";
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
        try {
          await setupApi("POST", "/api/password", { password: pw.value });
        } catch (x) {
          out.textContent = x.message || "That didn't save.";
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
  if (!live && me.password) return done(recall("account.name"), true);      // a reload: done already
  form(args.name);
  if (live) setTimeout(() => point(args.name ? "account.password" : "account.name"), 500);
};

VIEWS.network = function (args) {
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
};

// Look and feel, the first four setup steps (Bake's review, Nov 9): a stage Rookie walks onto (the
// one live Rookie, shown big) and the picker for the step; a click applies at once and the next one
// shows, no agent turn.
VIEWS.look = function () {
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
};

// -- look and feel: steps 1 to 4 (sound, Rookie's style, theme, name; agent.look) ---------------
// Sound, style and theme are the person's; the name is the house agent's. They happen on the left
// (VIEWS.look) and are instant (Bake's review, Nov 9: "Make them instant"): a click applies, the next
// step shows with a line in the agent's voice, and the agent hears once, when all four are done (or
// when the person types instead of clicking).

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
    if (skin) face.append(setupThumb(skin, theme));
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
    const input = el("input", { type: "text", placeholder: look.name, value: "" });
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

// All four chosen: the left goes back to the welcome view, Rookie walks home, and the agent hears
// it once (the setup walk-through starts then).
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
