---
description: Build, change and retire pages on the hub site for the person - ready-made components or your own sandboxed HTML.
when: the person wants to keep an eye on something, asks for a page or a dashboard, or a page has outlived its purpose; also on their first visit to the site.
---
# Hub pages

The hub site is your work surface with the person. You sit beside every page
in its chat dock. Messages from the dock start with a `[site]` line saying
which page they are looking at.

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
1. With the workbench (`page.site` says `workbench: true`): make a task for
   the build first and write the requirements from the conversation into it
   (`task.create {title, body, criteria}`), then pass its id as `task`. Without
   one, `page.create` makes the task itself.
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

**Retire a page.** `page.archive {page, reason}` takes it off the site
without losing anything; `page.restore` brings it back. When they say "when
the experiment ends, get rid of it", wire a trigger that calls
`page.archive` rather than remembering to.
