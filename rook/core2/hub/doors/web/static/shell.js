// The hub site's shell: lists pages, opens one at a time in a sandboxed
// frame, and carries that frame's calls and events (see bridge.js). The
// shell's own calls are page.* only (plus the caps plugins' shell parts
// declare); a page's calls go with its token. Plugins' shell parts (manifest
// shell:) load after this file and hook in through shellHooks.
"use strict";

const $ = (sel) => document.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let csrf = "";
let current = null;        // {page, token, frame, listening}
// opening: run before a page opens. starting: run once the pages are listed,
// before the start page opens; one that answers true keeps it closed.
const shellHooks = { opening: [], starting: [] };

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

function message(text, bad, home) {
  const m = $("#msg");
  m.textContent = text;
  m.className = bad ? "bad" : "";
  if (home) {
    const a = el("a", { href: "#/" }, "Go to the start page");
    a.onclick = (e) => {
      e.preventDefault();
      history.replaceState(null, "", location.pathname);
      open(wanted());
    };
    m.append(" ", a);
  }
  m.hidden = false;
}

// -- the page list ---------------------------------------------------------

let pages = [];              // every live page, from page.list: the sidebar's and the hidden ones
let site = { start: null };

// A page in the sidebar: its link and a ... menu with what can be done to it
// (Bake, Nov 9: "archive should be a function on the sidebar per page. Like an
// ellipse. in that ellipse I want a way to make a given page publicly accessible").
function link(p) {
  const row = el("div", { className: "nav-row" + (p.public ? " public" : "") });
  const a = el("a", { href: "#/" + p.page }, p.title);
  a.dataset.page = p.page;
  const more = el("button", { type: "button", className: "page-menu-btn", title: "More for " + p.title }, "\u22ef");
  more.setAttribute("aria-label", "More for " + p.title);
  more.setAttribute("aria-haspopup", "menu");
  more.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    pageMenu(more, p);
  };
  row.append(a, more);
  return row;
}

// The ... menu: Pin, Make public (with the public link to copy), Hide, and Archive
// on a page an agent made ("it never deletes a plugin's page, only hides it").
const menuBox = el("div", { id: "page-menu", className: "page-menu" });
menuBox.setAttribute("role", "menu");
menuBox.hidden = true;
document.body.append(menuBox);
function hideMenu() {
  menuBox.hidden = true;
  for (const b of document.querySelectorAll(".page-menu-btn[aria-expanded]")) b.removeAttribute("aria-expanded");
}
document.addEventListener("click", (e) => {
  if (!menuBox.hidden && !menuBox.contains(e.target)) hideMenu();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideMenu();
});

function publicUrl(pid) {
  return location.origin + "/public/" + pid;
}

function pageMenu(trigger, p) {
  const again = !menuBox.hidden && menuBox.dataset.page === p.page;
  hideMenu();
  if (again) return;
  menuBox.dataset.page = p.page;
  menuBox.replaceChildren();
  const item = (label, run, cls) => {
    const b = el("button", { type: "button", className: cls || "" }, label);
    b.setAttribute("role", "menuitem");
    b.onclick = run;
    menuBox.append(b);
    return b;
  };
  item(p.pinned ? "Unpin" : "Pin", async () => {
    hideMenu();
    try {
      await call("page.arrange", { page: p.page, pinned: !p.pinned }, p.pinned ?
        "The person unpinned a page from the sidebar." : "The person pinned a page to the sidebar.");
    } catch (x) {
      return message(x.message, true);
    }
    await loadNav();
  }).dataset.target = "page.arrange";
  const pub = item(p.public ? "\u2713 Public" : "Make public", async () => {
    try {
      await call("page.public", { page: p.page, public: !p.public }, p.public ?
        "The person made a page private again." : "The person made a page public from the sidebar.");
    } catch (x) {
      return message(x.message, true);
    }
    p.public = !p.public;
    await loadNav();
    const row = [...document.querySelectorAll("#nav .nav-row")].find((r) => r.firstChild.dataset.page === p.page);
    hideMenu();
    if (row) pageMenu(row.querySelector(".page-menu-btn"), p);      // open again, with the link
  });
  pub.setAttribute("role", "menuitemcheckbox");
  pub.setAttribute("aria-checked", String(!!p.public));
  pub.dataset.target = "page.public";
  if (p.public) {
    const url = publicUrl(p.page);
    const box = el("div", { className: "public-link" });
    const field = el("input", { type: "text", readOnly: true, value: url });
    field.onclick = () => field.select();
    const copy = el("button", { type: "button" }, "Copy link");
    copy.onclick = async () => {
      try {
        await navigator.clipboard.writeText(url);
      } catch (x) {                    // no clipboard (plain http): select it for a manual copy
        field.select();
        document.execCommand && document.execCommand("copy");
      }
      copy.textContent = "Copied";
      setTimeout(() => (copy.textContent = "Copy link"), 1500);
    };
    box.append(el("div", { className: "muted" }, "Anyone with this link can view it, no sign-in."), field, copy);
    menuBox.append(box);
  }
  item("Hide", async () => {
    hideMenu();
    try {
      await call("page.arrange", { page: p.page, hidden: true }, "The person hid a page from their sidebar.");
    } catch (x) {
      return message(x.message, true);
    }
    await loadNav();
  }).dataset.target = "page.arrange";
  if (p.made) {
    item("Archive", async () => {
      hideMenu();
      try {
        await call("page.archive", { page: p.page }, "The person archived a page from the sidebar.");
      } catch (x) {
        return message(x.message, true);
      }
      await loadNav();
      if (current && current.page === p.page) {
        history.replaceState(null, "", location.pathname);
        open(wanted());
      }
    }, "danger");
  }
  menuBox.hidden = false;
  trigger.setAttribute("aria-expanded", "true");
  const r = trigger.getBoundingClientRect();
  menuBox.style.left = Math.max(8, Math.min(innerWidth - menuBox.offsetWidth - 8, r.left)) + "px";
  menuBox.style.top = Math.max(8, Math.min(innerHeight - menuBox.offsetHeight - 8, r.bottom + 4)) + "px";
}

// A segment: its heading opens or folds it (kept through page.arrange); folded,
// its pages are one quiet line of links, so what exists still shows.
function segment(s) {
  const head = el("button", { type: "button", className: "section seg-head" }, s.segment);
  head.setAttribute("aria-expanded", String(s.open));
  head.dataset.target = "page.arrange";
  head.onclick = async () => {
    try {
      await call("page.arrange", { segment: s.segment, open: !s.open }, s.open ?
        "The person folded a sidebar segment." : "The person opened a sidebar segment.");
    } catch (x) {
      return message(x.message, true);
    }
    await loadNav();
  };
  const box = el("div", { className: "segment" + (s.open ? "" : " folded") });
  box.append(head);
  if (s.open) {
    for (const p of s.pages) box.append(link(p));
  } else {
    const line = el("div", { className: "folded-pages" });
    s.pages.forEach((p, i) => {
      const a = el("a", { href: "#/" + p.page }, p.title);
      a.dataset.page = p.page;
      line.append(...(i ? [" \u00b7 ", a] : [a]));
    });
    box.append(line);
  }
  return box;
}

// A fold at the bottom of the sidebar (Archived, Hidden): one row per page with
// the button that brings it back.
function fold(id, list, label, back) {
  const ul = $("#" + id + " ul");
  ul.replaceChildren();
  for (const p of list) {
    const li = el("li");
    const b = el("button", { className: "link" }, label);
    b.onclick = async () => {
      try {
        await back(p);
      } catch (x) {
        return message(x.message, true);
      }
      await loadNav();
      location.hash = "#/" + p.page;
    };
    li.append(el("span", {}, p.title), b);
    ul.append(li);
  }
  $("#" + id).hidden = list.length === 0;
}

async function loadNav() {
  const [live, gone] = await Promise.all([call("page.list"), call("page.list", { archived: true })]);
  pages = live.segments.flatMap((s) => s.pages).concat(live.hidden);
  const box = $("#pages");
  box.replaceChildren();
  if (live.pinned.length) {
    const pinned = el("div", { className: "segment pinned" });
    pinned.append(el("div", { className: "section" }, "Pinned"), ...live.pinned.map(link));
    box.append(pinned);
  }
  box.append(...live.segments.map(segment));
  fold("hidden", live.hidden, "Show", (p) =>
    call("page.arrange", { page: p.page, hidden: false }, "The person put a hidden page back in their sidebar."));
  fold("archived", gone.pages, "Restore", (p) => call("page.restore", { page: p.page }));
  mark();
}

// The open page's link lit, and its title bar under the name the person gave it.
function mark() {
  for (const a of document.querySelectorAll("#nav a")) {
    a.classList.toggle("on", !!current && a.dataset.page === current.page);
  }
  const row = current && pages.find((p) => p.page === current.page);
  if (row) $("#title").textContent = row.title;
}

function wanted() {
  const h = decodeURIComponent(location.hash.replace(/^#\/?/, ""));
  if (h) return h;
  const has = (pid) => pid && pages.some((p) => p.page === pid);
  if (has(site.start)) return site.start;
  const first = pages.find((p) => p.page === "hub/workers") || pages[0];
  return first ? first.page : "";
}

// -- one open page -----------------------------------------------------------

let opening = 0;

async function open(pid) {
  const n = ++opening;
  document.body.classList.remove("nav-open");
  closePage();
  for (const f of shellHooks.opening) f();
  if (!pid) return message("No pages yet.");
  // an address that names no page (Bake's review, Nov 9): say so, with the way home
  if (pages.length && !pages.some((p) => p.page === pid)) return message(`No such page: ${pid}.`, true, true);
  const r = await api("/web/api/open", { page: pid });
  if (n !== opening) return;      // another page was asked for meanwhile
  if (!r.ok) return message(typeof r.error === "string" && r.error ? r.error : "This page can't be opened.", true, true);
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
  $("#note-btn").hidden = false;
  mark();
}

function closePage() {
  if (current) current.frame.remove();
  current = null;
  mark();
  $("#title").textContent = "";
  $("#note").hidden = $("#note-btn").hidden = true;
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

window.addEventListener("hashchange", () => open(wanted()));

// After every plugin's shell part has run (they load after this file).
document.addEventListener("DOMContentLoaded", async function start() {
  try {
    csrf = (await api("/web/api/me")).csrf;
    site = await call("page.site");
    await loadNav();
    const kept = await Promise.all(shellHooks.starting.map((f) => f().catch(() => false)));
    if (!kept.some(Boolean)) open(wanted());
  } catch (x) {
    message(String((x && x.message) || x), true);
  }
});
