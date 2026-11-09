// What the house agent can put in a chat message besides words: a line of
// its own in [[...]] (Bake, Nov 9: "it needs to provide clickable suggestions
// more often"; "I want rookie to open like a work pane with the vault ... I
// want it to guide the setup entirely"):
//
//   [[choices: Just this computer | Other devices at home | From anywhere]]
//       buttons under the message; a click sends that text as the answer
//   [[open: vault | new=host_password]]
//       opens a view in the work pane (left): vault, account, network, or
//       "page hub/workers" (a hub page); key=value lines pre-fill it, and it
//       focuses the field the person needs to touch
//   [[point: vault.value]]
//       Rookie walks over to that target in the work pane and back
//
// parse(text) -> [{kind: "text", text} | {kind: "choices", items} |
//                 {kind: "open", view, arg, args} | {kind: "point", target}]
// No DOM here: tests run it under node.
"use strict";

(function (root) {
  const LINE = /^\s*\[\[\s*(choices|open|point)\s*:\s*([\s\S]*?)\s*\]\]\s*$/i;

  function parse(text) {
    const out = [];
    let buf = [];
    const flush = () => {
      const t = buf.join("\n").replace(/^\n+|\n+$/g, "");
      if (t) out.push({ kind: "text", text: t });
      buf = [];
    };
    for (const line of String(text || "").split("\n")) {
      const m = LINE.exec(line);
      const parts = m ? m[2].split("|").map((s) => s.trim()).filter(Boolean) : [];
      if (!m || !parts.length) {
        buf.push(line);
        continue;
      }
      flush();
      const kind = m[1].toLowerCase();
      if (kind === "choices") {
        out.push({ kind, items: parts });
      } else if (kind === "point") {
        out.push({ kind, target: parts[0] });
      } else {
        const [view, ...rest] = parts[0].split(/\s+/);
        const args = {};
        for (const p of parts.slice(1)) {
          const eq = p.indexOf("=");
          if (eq > 0) args[p.slice(0, eq).trim()] = p.slice(eq + 1).trim();
        }
        out.push({ kind, view: view.toLowerCase(), arg: rest.join(" "), args });
      }
    }
    flush();
    return out;
  }

  const api = { parse };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ChatWidgets = api;
})(typeof window !== "undefined" ? window : this);
