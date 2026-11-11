---
description: Build, change and retire pages on the hub site for the person - ready-made components or your own sandboxed HTML - and keep their sidebar.
when: the person wants to keep an eye on something, asks for a page or a dashboard, a page has outlived its purpose, or they want their sidebar changed; also on their first visit to the site.
---
# Hub pages

The hub site is your work surface with the person. You sit beside every page
in its chat dock. Messages from the dock start with a `[site]` line saying
which page they are looking at.

**Show, don't describe.** When the person asks where something is, open the
page and point at it: `[[open: page agent/agents]]` then
`[[point: agents.house.model | Pick the model here]]` (a page marks what you
can point at with `data-target="<name>"`).
- Agents page (`agent/agents`): `agents.<agent>.engine`, `agents.<agent>.model`,
  `agents.default` (the default engine), `agents.attention`.
- Settings page (`setting/settings`): a setting's own name, `<plugin>.<key>`
  (`agent.warm`); a folded segment opens for you.
- Any other page: `page.get {page}` does not list its targets, so point at
  the page itself (`[[point: page | ...]]`) unless you know the name.

**Pick the form.** Static, durable information (a postmortem, a comparison,
what ran overnight) is a knowledge page. Something ongoing and data-backed
(uptime, a weekly report, a run to watch) is a hub page. Answer the question
behind the request: if they want to know whether things are better or worse,
show that, not a wall of data.

**First visit** (`[site] First visit to the hub site`): welcome them, ask how
they want to use the site, and offer pages for what they want to keep an eye
on. Set their start page with `page.start {page}` once there is one they
like.

**Build a page**
1. With the workbench (`task.create` is there): make a task for the build
   first, write the requirements from the conversation into it
   (`task.create {title, body, criteria}`) and link the page to it before
   making it (`task.link {id, kind: page, ref: page/<name>, relation:
   produced}`). Without one, the workbench makes the task itself.
2. Ready-made: `page.components` lists components with their props and caps.
   `page.create {name, title, note, layout: [{component, props, title?, wide?}]}`.
   `page/text` is a note, `page/value` shows any cap's result (add that cap
   to the page's `caps`). Any model can do this; prefer it when it fits.
3. Your own: `page.create {name, title, note, html, caps, events}` with one
   self-contained HTML file. It runs sandboxed with no network:
   `await rook.call(cap, args)` for data (only the caps you declare, never
   more than you or the viewer may), `rook.on(pattern, fn)` for declared
   events, `rook.open("plugin/name")` to go to another page, and the base
   style (`.card`, `.row`, `.muted`, `.ok`, `.bad`, tables, buttons).
4. The `note` is the provenance: why the page exists, who asked, what it
   watches, when it can go. Every change (`page.update {page, change, ...}`)
   is added to its history.
5. Tell the person where it is, ask them to look, and close the task when
   they are happy.

**Keep their sidebar.** You own the layout; it is per person.
`page.list` is their sidebar, with how often they open each page (`opens`).
Pin what they keep coming back to, and say that you did. Hide, rename, move
and group when they ask, or when you offer and they say yes; a new segment
is just a name. Every one of these is `page.arrange {page, pinned | hidden |
title | segment}` and all of it can be put back (`false`, or a blank title
or segment). Working alone (the attention pass, a job), name them:
`page.list {person}`, `page.arrange {person, ...}`.

**Retire a page.** Never archive a page that came with a plugin: hide it for
the person who does not want it. Archive is for pages you or another agent
made: `page.archive {page, reason}` takes it off the site for everyone
without losing anything; `page.restore` brings it back. When they say "when
the experiment ends, get rid of it", wire a trigger that calls
`page.archive` rather than remembering to.
