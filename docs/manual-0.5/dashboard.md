# The hub site

The hub serves its site on the HTTP door: `http://127.0.0.1:8740/` unless
you changed `doors.http.bind`. It is a shell that lists pages and opens one
at a time; plugins and agents add pages.

## Signing in

Your user name and password. `init` sets the owner's (asked on a
terminal; otherwise made and left in `<data>/owner.password`, and the site
offers to change it at your first sign-in). With `init --no-password` (the
installer) the new hub is open until it is claimed: init leaves
`<data>/unclaimed`, and while it is there anyone who opens the site is the
owner. Setting a password (Your account, or `passwd`) removes it for good;
from then on a new browser gets the sign-in page. A hub without it is never
open. A hub moved from 0.4 takes 0.4's
dashboard password. **Your account** (bottom of the sidebar) changes your
password and shows the address claude.ai's connector takes. Lost it? On the
hub's machine: `python -m rook.core2.hub --config hub.yaml passwd <name>`
sets a new one at once. Tokens are for agents and apps, never for signing
in. `doors: {http: {web: false}}` turns the site off.

## What is there

| Installed | You get |
|---|---|
| the hub alone | **Model providers** (under Agents); **Workers** (under Machines); **Tokens**, **Jobs**, **Journal**, **Vault** and **Settings** (under Hub) |
| + agents | the house agent's **chat dock** beside every page. A hub the installer made opens on its guided first run (`agent.setup`); after that, on your first visit it asks how you want to use the site; it builds pages for you and can set your start page. The **Agents** page (under Agents): each agent's engine and model, the default engine, the attention pass. With `trust` in the library, the **Trust map** too; with `speech`, the **Voice** page (your voice, who answers, what hears and speaks) |
| + workbench | **Tasks**, **Projects** and **Knowledge** pages; every page an agent makes is built under a task, so you can review it |

The site opens on your start page, else the hub's, else Workers. The
chat sits beside the page, on the right or the left (the arrows in its
head, or ask the agent), and can pop out into its own window (the arrow
beside them; closing that window puts it back). Ask the agent where
something is and it opens the page and Rookie points at the control. On a
phone, the chat is a sheet over the page; tap Rookie in the corner.

## The sidebar

Every page is in it, under its plugin's segment: Agents, Machines, Work
and Hub first, then any other a plugin names, then Pages for what agents
made. **Pinned** at the top holds the pages you pin (a page's ⋯ menu: Pin,
Unpin), in the order you pinned them; each stays in its segment too.
Click a segment's heading to open or fold it, and it stays that way. One
you have not touched is open when it holds one of the three pages you
open most; until you have opened three different pages, your start
page's segment is open, or every segment when you have no start page. A folded segment
still shows its pages, as one quiet line of links.

**Hide** (the ⋯ menu) takes a page off your sidebar only: its address
still opens it and everyone else still has it. **Hidden**, folded at the
bottom, lists what you hid, each with **Show**. To rename a page, move it
to another segment or group pages under a new one, ask the agent in the
dock; it changes only your sidebar. The sidebar is yours, and the agent
keeps it with you: it can pin what you keep opening, and hide, rename,
move, fold and put back (`page.arrange`, see the
[page plugin's manual](../../rook/core2/hub/doors/web/builtin/page/docs/manual.md)).

- **Workers**: every machine, online or not, its plugins and what it can't serve.
- **Model providers**: each installed `llm.complete` provider, whether it
  answers, its key, address, worker and System One/Two/Three models, and
  **Use this one** to send `llm.complete` to it ([Settings](settings.md#choosing-a-provider)).
- **Jobs**: every job, its last run, Run now and on/off ([Jobs](jobs.md)).
- **Tokens**: a token for an agent, with its ready `claude mcp add` line and
  claude.ai connector details (this is the agent installer); revoke one.
- **Journal**: what was done, by whom, and why ([The journal](journal.md)).
- **Vault**: the hub's secrets by name, never a value: add, replace, delete
  ([the vault's manual](../../rook/core2/hub/vault/docs/manual.md)).
- **Settings**: the hub (its band, and the relay machines join at), its
  people and agents, the plugin settings and your own ([Settings](settings.md)).
  Owners add and remove people and set their passwords there ([People](people.md)).

## Pages agents make

Ask the house agent in the dock for a page ("a board with the lights
downstairs and today's tasks") and it makes one. A page is one HTML file,
or a layout of ready-made components. Each page says why it exists and
keeps the history of every change under **About**. **Archive** (in the ⋯
menu of a page an agent made) takes it off the site for everyone without
losing it; **Restore** in the sidebar's Archived fold brings it back. A
page that came with a plugin is never archived from the menu, only hidden.

A page can never do more than the person viewing it: every call it makes
runs as you, through dispatch (rights, holds, journal), and only for
the caps the page declared. It runs sandboxed, with no network of its own,
and it can't answer holds.

## Building pages

How a page talks to the hub (`window.rook`), how plugins ship pages and
components, and the `page.*` caps agents use are in
`docs/castling-dashboard.md` and the [page plugin's manual](../../rook/core2/hub/doors/web/builtin/page/docs/manual.md).
The house agent's playbook is the agents package's `hub-pages` skill.
