# Pages

The hub site's pages: the ones plugins ship (`pages:` in a manifest) and
the ones agents make.

| Cap | What it does |
|---|---|
| `page.list` | Your sidebar, ready to draw: `{pinned, segments: [{segment, open, pages}]}`; each page has its `segment`, `opens` (how often you opened it) and `pinned`. `archived=true`: `{pages}`, the archived ones. |
| `page.view` | Open a page: what it may call and hear. The site calls it when you open one. |
| `page.get` | One page: its title, caps and events; for a page an agent made, who made it, its layout and its note (why it exists and every change); for a plugin's page, which plugin. |
| `page.create` | Make a page: `{name, title, note, html \| layout, caps?, events?, segment?}`; no segment: Pages. |
| `page.update` | Change one: `{page, change, ...}`; `change` says what and why, kept in its history. |
| `page.archive` / `page.restore` | Take a page off the site without losing it, or bring it back. |
| `page.public` | Make a page readable by anyone at `/public/<page>` without signing in (a report, a reference), or private again with `public: false`. Its calls act as whoever made it public. |
| `page.components` | The ready-made components a layout can use, with their props and caps. |
| `page.site` | Your start page. |
| `page.start` | Set the page the site opens on: yours, or (acting for nobody) the hub's. |
| `page.arrange` | Change your sidebar (acting for nobody: the hub's own): `{page, pinned}` pins or unpins a page, `{segment, open}` opens or folds a segment. Answers the sidebar as `page.list` does. |

## The sidebar

Every page is in it, in a segment: a plugin's page in its plugin's
`segment:` (none: Hub), an agent-made page in the one it was made with
(none: Pages; a page made before segments keeps its old `section`).
Agents, Machines, Work and Hub come first, then the others by name.
Pinned pages come first, in the order pinned, and stay in their segment.

The layout is kept per person in `<data>/pages.json` under `layout`,
beside `start`, only what differs from the default:
`{"person:<name>": {"pages": {"<page>": {"pinned": <when>}}, "segments":
{"<segment>": {"open": true|false}}}}`; `*` is the hub's own, which anyone
without their own gets, and a person's first change starts from it.

A segment someone opened or folded by hand stays that way. Otherwise it
is open when it holds one of the three pages that person opens most
(`page.view` counts each open in `<data>/page-opens.json`); until they
have opened three different pages, the segment of their start page, or
every segment when there is no start page.

A `layout` is a list of components (`[{component, props?, title?, wide?}]`).
Core ships `page/text` (a note), `page/value` (any cap's result, refreshed
every few seconds if you like), `hub/workers` and
`journal/recent`; plugins ship more.

Every call a page makes runs as the person viewing it, and only for the
caps the page declared. Making or changing a page emits `page.created` or
`page.updated`.

How pages are built and the `window.rook` API: `docs/castling-dashboard.md`.
