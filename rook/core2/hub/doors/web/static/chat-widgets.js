// What the house agent can put in a chat message besides words: a block in
// [[...]], best on a line of its own (Bake, Nov 9: "it needs to provide clickable suggestions
// more often"; "I want rookie to open like a work pane with the vault ... I
// want it to guide the setup entirely"):
//
//   [[choices: Just this computer | Other devices at home | From anywhere]]
//       buttons under the message; a click sends that text as the answer
//   [[open: vault | new=host_password]]
//       opens a view in the work pane (left): vault, account, network, or
//       "page hub/workers" (a hub page); key=value lines pre-fill it, and it
//       focuses the field the person needs to touch
//   [[point: vault.value | Enter the password here]]
//       Rookie walks over to that target in the work pane and back; the
//       words after | are his speech bubble there (default "Right here")
//   [[say: The dashboard task is done]]
//       a speech bubble beside Rookie, a few words (Bake, Nov 9: "I want
//       rookie to make little chat bubbles too")
//
// parse(text) -> [{kind: "text", text} | {kind: "choices", items} |
//                 {kind: "open", view, arg, args} | {kind: "point", target, say} |
//                 {kind: "say", text}]
// No DOM here: tests run it under node.
"use strict";

(function (root) {
  const BLOCK = /\[\[\s*(choices|open|point|say)\s*:\s*([^\]]*?)\s*\]\]/gi;

  // One [[...]] block -> a part, or null (empty: left as words).
  function block(kind, body) {
    const parts = body.split("|").map((s) => s.trim()).filter(Boolean);
    if (!parts.length) return null;
    kind = kind.toLowerCase();
    if (kind === "choices") return { kind, items: parts };
    if (kind === "point") return { kind, target: parts[0], say: parts.slice(1).join(" | ") };
    if (kind === "say") return { kind, text: parts.join(" | ") };
    const [view, ...rest] = parts[0].split(/\s+/);
    const args = {};
    for (const p of parts.slice(1)) {
      const eq = p.indexOf("=");
      if (eq > 0) args[p.slice(0, eq).trim()] = p.slice(eq + 1).trim();
    }
    return { kind, view: view.toLowerCase(), arg: rest.join(" "), args };
  }

  // Blocks count wherever they are in the message (agents don't always give
  // them a line of their own); the words around them stay words.
  function parse(text) {
    text = String(text || "");
    const out = [];
    const words = (t) => {
      t = t.replace(/^[ \t]*\n+|\n+[ \t]*$/g, "").trim();
      if (t) out.push({ kind: "text", text: t });
    };
    let at = 0, m;
    BLOCK.lastIndex = 0;
    while ((m = BLOCK.exec(text))) {
      const part = block(m[1], m[2]);
      if (!part) continue;
      words(text.slice(at, m.index));
      out.push(part);
      at = m.index + m[0].length;
    }
    words(text.slice(at));
    return out;
  }

  const api = { parse };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ChatWidgets = api;
})(typeof window !== "undefined" ? window : this);
