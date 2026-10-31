# The hub site

The hub serves its site on the HTTP door: `http://127.0.0.1:8740/` unless
you changed `doors.http.bind`. It is a shell that lists pages and opens one
at a time; plugins and agents add pages.

## Signing in

Paste a token made for you: the first owner's is printed once by `init` (or
written to `<data>/owner.token`), others come from `python -m
rook.core2.hub --config hub.yaml token --person <name>`
([People and presets](people.md#adding-a-person)). Agent tokens can't sign
in. `doors: {http: {web: false}}` turns the site off.

## What is there

| Installed | You get |
|---|---|
| the hub alone | **Workers** and **Approvals** in the sidebar; **Journal** and **Settings** under More |
| + agents | the house agent's **chat dock** beside every page. On your first visit it asks how you want to use the site; it builds pages for you and can set your start page |
| + workbench | **Tasks**, **Projects** and **Knowledge** pages; every page an agent makes is built under a task, so you can review it |

The site opens on your start page, else the hub's, else Workers. On a
phone, the dock is a sheet behind the Chat button.

- **Workers**: every machine, online or not, its plugins and what it can't serve.
- **Approvals**: calls waiting for you ([Holds](holds.md)).
- **Journal**: what was done, by whom, and why ([The journal](journal.md)).
- **Settings**: the hub's plugin settings and your own ([Settings](settings.md)).

## Pages agents make

Ask the house agent in the dock for a page ("a board with the lights
downstairs and today's tasks") and it makes one. A page is one HTML file,
or a layout of ready-made components. Each page says why it exists and
keeps the history of every change under **About**. **Archive** (in every
page's bar) takes a page off the site without losing it; **Restore** in the
sidebar brings it back.

A page can never do more than the person viewing it: every call it makes
runs as you, through dispatch (rights, gates, holds, journal), and only for
the caps the page declared. It runs sandboxed, with no network of its own,
and it can't answer holds.

## Building pages

How a page talks to the hub (`window.rook`), how plugins ship pages and
components, and the `pages.*` caps agents use are in
`docs/castling-dashboard.md` and the [pages plugin's manual](../../rook/core2/hub/doors/web/builtin/pages/docs/manual.md).
The house agent's playbook is the agents package's `hub-pages` skill.
