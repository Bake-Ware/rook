// The page side of the dashboard bridge. Injected ahead of every page's own
// code. A page is sandboxed (its own opaque origin, no network): it reaches
// the hub only through the shell around it, which forwards each call with
// the page's token. The hub decides; the page's declared caps and the
// viewer's rights are the most it can do.
//
//   const r = await rook.call("hub.workers");            // the cap's result
//   await rook.call("approval.decide", {hold, decision: "approve"}, {why: "..."});
//   rook.on("worker.*", (payload, event) => reload());  // events it declared
(function () {
  "use strict";
  var info = window.__rookPage || {};
  try { delete window.__rookPage; } catch (e) { window.__rookPage = undefined; }
  var pending = {}, next = 1, listeners = [];

  function glob(pattern, name) {
    var parts = String(pattern).split("*").map(function (p) {
      return p.replace(/[.+?^${}()|[\]\\]/g, "\\$&");
    });
    return new RegExp("^" + parts.join(".*") + "$").test(name);
  }

  function send(msg) {
    msg.rook = 1;
    window.parent.postMessage(msg, "*");
  }

  window.addEventListener("message", function (e) {
    var m = e.data;
    if (e.source !== window.parent || !m || m.rook !== 1) return;
    if (m.kind === "reply" && pending[m.id]) {
      var p = pending[m.id], r = m.reply || {};
      delete pending[m.id];
      if (r.ok) return p.resolve(r.result);
      var err = new Error(r.error || "failed");
      err.held = !!r.held;
      err.hold = r.hold;
      p.reject(err);
    } else if (m.kind === "event") {
      listeners.forEach(function (l) {
        if (!glob(l[0], m.event)) return;
        try { l[1](m.payload, m.event); } catch (x) { console.error(x); }
      });
    }
  });

  window.rook = {
    page: info,
    call: function (cap, args, opts) {
      opts = opts || {};
      return new Promise(function (resolve, reject) {
        var id = next++;
        pending[id] = { resolve: resolve, reject: reject };
        send({ kind: "call", id: id, cap: cap, args: args || {}, why: opts.why || "", worker: opts.worker || "" });
      });
    },
    on: function (pattern, fn) {
      listeners.push([pattern, fn]);
      if (listeners.length === 1) send({ kind: "listen" });
    },
    esc: function (s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c];
      });
    },
    ago: function (ts) {
      if (!ts) return "never";
      var s = Math.max(0, Date.now() / 1000 - ts);
      if (s < 60) return Math.round(s) + " s ago";
      if (s < 3600) return Math.round(s / 60) + " min ago";
      if (s < 86400) return Math.round(s / 3600) + " h ago";
      return new Date(ts * 1000).toLocaleDateString();
    },
    time: function (ts) {
      return ts ? new Date(ts * 1000).toLocaleString() : "";
    }
  };
})();
