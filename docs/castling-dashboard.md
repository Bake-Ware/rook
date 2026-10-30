# Castling 0.5 hub site

The hub's HTTP door serves the hub site on `/`: a shell that lists pages and
opens one at a time. Pages come from plugins (`pages:` in a manifest) and from
agents (`pages.create`). Design: knowledge pages castling-dashboard and
castling-attention. Code: `rook/core2/hub/doors/web/`.

## Sign in

Open `http://127.0.0.1:8740/` and paste a token made for you: the first
owner's is in `<data>/owner.token`, others come from
`python -m rook.core2.hub --config hub.yaml token --person <name>`. An agent's
token can't sign in. Behind TLS, set `doors: {http: {web: {secure_cookie: true}}}`;
`web: false` turns the site off.

## What a page can do

A page is one self-contained HTML file (inline script and style). It runs in a
sandboxed frame with its own opaque origin and no network: no cookies, no hub
API, nothing of the shell's. It reaches the hub only through `window.rook`,
which the hub injects ahead of the page's own code:

```js
const r = await rook.call("hub.workers");                    // the cap's result, or throws
await rook.call("board.note.create", {text: "milk"}, {why: "Added on the board page."});
rook.on("worker.*", (payload, event) => reload());          // events the page declared
rook.page;                                                    // {page, title, caps, events}
rook.esc(text); rook.ago(ts); rook.time(ts);                  // small helpers
```

Every call runs as the person viewing the page, through dispatch like any
other call (rights, gates, holds, journal). The page's `caps` are one more
layer: a cap the page doesn't declare is refused (`denied: page: grants`)
even for an owner. A call that waits for a person throws with `err.held` and
`err.hold`. Events reach the page when both its `events` and the viewer's
rights allow them. The base style (`static/page.css`) gives tables, buttons,
`.card`, `.muted`, `.ok`, `.bad` and light/dark colours.

## Pages from a plugin

```yaml
pages:
  board: { title: Board, file: pages/board.html, caps: ["board.*"], events: ["board.*"], nav: { section: Home } }
```

The core pages are builtin plugins in `rook/core2/hub/doors/web/builtin/`:
Workers (`hub.workers`), Approvals (`approval.list`, `approval.decide`),
Journal (`journal.search`) and Settings (`hub.status`, `setting.list`,
`setting.set`).

## Pages an agent makes

```
pages.create {name, title, html, note, caps?, events?, section?, task?}
pages.update {page, change, title?, html?, note?, caps?, events?, section?}
pages.archive {page, reason?}    pages.restore {page}    pages.get {page}    pages.list {archived?}
```

They live in `<data>/pages/<name>/` (`page.html`, `meta.json`, `NOTE.md`).
`NOTE.md` says why the page exists and keeps the history of every change; the
shell shows it under About. Such a page never does more than any agent or
person who wrote it, and it can't answer holds. Archiving takes a page off the
site without losing anything; Restore (in the sidebar) brings it back.
