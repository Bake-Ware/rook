# Pages

The hub site's pages: the ones plugins ship (`pages:` in a manifest) and
the ones agents make.

| Cap | What it does |
|---|---|
| `page.list` | The pages on the site, by section; `archived=true` lists the archived ones. |
| `page.view` | Open a page: what it may call and hear. The site calls it when you open one. |
| `page.get` | One page: its title, caps and events; for a page an agent made, who made it, its layout and its note (why it exists and every change); for a plugin's page, which plugin. |
| `page.create` | Make a page: `{name, title, note, html \| layout, caps?, events?, section?, task?}`. |
| `page.update` | Change one: `{page, change, ...}`; `change` says what and why, kept in its history. |
| `page.archive` / `page.restore` | Take a page off the site without losing it, or bring it back. |
| `page.components` | The ready-made components a layout can use, with their props and caps. |
| `page.site` | Your start page and the agent in the chat dock. |
| `page.start` | Set the page the site opens on: yours, or (acting for nobody) the hub's. |

A `layout` is a list of components (`[{component, props?, title?, wide?}]`).
Core ships `page/text` (a note), `page/value` (any cap's result, refreshed
every few seconds if you like), `hub/workers`, `approval/holds` and
`journal/recent`; plugins ship more.

Every call a page makes runs as the person viewing it, and only for the
caps the page declared. With the workbench, each page an agent makes is
built under a task assigned to the person to review.

Setting: `dock` (hub), the agent in the site's chat dock (default `house`;
blank: no dock).

How pages are built and the `window.rook` API: `docs/castling-dashboard.md`.
