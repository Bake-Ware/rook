# Castling 0.5 hub site

The hub's HTTP door serves the hub site on `/`: a shell that lists pages and
opens one at a time. Pages come from plugins (`pages:` in a manifest) and from
agents (`page.create`). Design: knowledge pages castling-dashboard and
castling-attention. Code: `rook/core2/hub/doors/web/`.

## What you get with each package

| Installed | The site |
|---|---|
| core | Workers and Approvals in the sidebar; Journal and Settings under More. Every page opens by its address (`#/plugin/name`). |
| + agents | The house agent's chat dock beside every page (the `page.dock` setting names the agent; blank turns it off). On your first visit it asks how you want to use the site. It builds pages for you and can set your start page. |
| + workbench | Tasks, Projects and Knowledge pages; the `task/list` component; every page an agent makes is built under a task linked to the page. |
| both | All of the above. |

The sidebar shows pages whose manifest entry has `nav:` and every page an
agent made; the rest are under More. The site opens on your start page
(`page.start`), else the hub's own, else Workers.

## Sign in

Open `http://127.0.0.1:8740/` and sign in with your user name and password
(`rook/core2/hub/passwords.py`: `init` sets the owner's, `passwd <name>` sets
or resets anyone's, the Account page `/account` changes your own; a hub moved
from 0.4 takes 0.4's dashboard password). Tokens are for agents and apps; the
desktop app may still post a person's own token as `{token}`, an agent's never
signs in. Behind TLS, set `doors: {http: {web: {secure_cookie: true}}}`;
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
rook.open("approval/approvals");                              // go to another page
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

## Components

The canned tier: small cap-backed scripts a package ships, which any agent
(a weak local model too) arranges into a page.

```yaml
components:
  list: { title: Tasks, file: components/list.js, doc: "Open tasks, by project.", caps: [task.list],
          props: { project: { type: str } } }
```

```js
// components/list.js
rook.component(async (el, props) => {
  const tasks = await rook.call("task.list", { where: { project: props.project } });
  el.innerHTML = tasks.map((t) => `<p>${rook.esc(t.title)}</p>`).join("");
});
```

`page.components` lists them. A layout page may call what its components
declare (as they are now) plus its own `caps`. Core ships `page/text`,
`page/value` (any cap's result: put that cap in the page's `caps`),
`hub/workers`, `approval/holds` and `journal/recent`.

## Pages an agent makes

```
page.create {name, title, note, html | layout, caps?, events?, section?, task?}
page.update {page, change, title?, html? | layout?, note?, caps?, events?, section?}
page.archive {page, reason?}    page.restore {page}    page.get {page}    page.list {archived?}
page.components    page.site    page.start {page?}
```

`layout` is `[{component, props?, title?, wide?}, ...]`, checked against each
component's props. With the workbench, `page.create` links the page to
`task` (relation `produced`), or makes that task (`Page: <title>`, in
progress, assigned to the person to review); `page.update` comments every
change on it. A refusal there comes back as `task_error` and never undoes the
page. The house agent's playbook is the agents package's `hub-pages` skill.

Every page action is a cap, so a trigger can call it: a plugin hook
`on: [{event: lab.experiment.ended, call: page.archive, args: {page: page/run}}]`
(with `page.archive` in its `uses`) retires a page when its experiment ends.

They live in `<data>/pages/<name>/` (`page.html`, `meta.json`, `NOTE.md`).
`NOTE.md` says why the page exists and keeps the history of every change; the
shell shows it under About. Such a page never does more than any agent or
person who wrote it, and it can't answer holds. Archiving takes a page off the
site without losing anything; Restore (in the sidebar) brings it back. The
Archive button sits in every page's bar.

## The dock

With the agents package the shell talks to the dock agent through
`agent.chat.send`/`agent.chat.read`, the only caps beyond `page.*` it may
send, and only with that agent. Those calls run in the person's own session,
so the agent works with the person's rights. Each message carries a first line
`[site] The person is on the page <id> (<title>).`; the dock hides it. The
first message of a new conversation is the site's first-visit note. On a phone
the dock is a sheet behind the Chat button.
