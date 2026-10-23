# Starting a project through conversation, and borrowed state (B3)

Phase 4 of the house agent vision (Rook knowledge page `house-agent-vision`,
sections 9 and 10), on top of the front door (docs/design/front-door.md) and
the trust map (docs/design/trust.md). Built 2026-10-23 (overnight track B,
phase B3). User manual: `docs/manual/onboarding.md`.

Code: `rook/hub/plugins/home/onboard.py` (threads, intake, plan, setup,
borrow/restore, sweep), wired into `rook/hub/plugins/home/front.py` (router
tools, the hub-written reply lines, `run_action`'s borrow context, Yes/No on
restore and review asks), `rook/hub/trust.py` (`resource:` selector,
`baseline`, lease fields, `borrowing`) and `rook/hub/approvals.py`
(`Approvals.trust_ruling`). Tables: `onboard_threads`, `onboard_projects`,
`onboard_offers` in `<home data>/front.db` (migration `002_onboard.sql`).

## 1. Brainstorm

A conversation is one thread, keyed by its room, else `<source>:<asker>`
(`Onboarding.key`), current while touched in the last 12 hours. So voice turns
through `home.ask`, which carry no history, still share a thread.

While the person thinks out loud the router answers and calls `note` with the
gist of each new point. Notes go in the thread and in the house agent's
workspace, `<home data>/workspace/<thread or slug>/notes.md`. Nothing else is
created. The router's prompt carries the thread's stage, notes and answers so
far (`Onboarding.prompt_text`).

## 2. The turn

When the talk turns to building, the router calls `offer_project(title)`.
The hub, not the model, writes the reply's line:
"Sounds like this wants to be a project. Want me to set it up?"
(`OFFER_LINE`). A second offer in the same thread is refused. A message that
matches `BUILD_CUE` ("let's write some code", "let's build it", ...) adds a
hint to the prompt while the thread is still brainstorming and not offered.
`project_intake answer=decline` ends it: the thread is `declined` and
never offered again.

## 3. Intake: asked, never guessed

`project_intake(answer, words, ...)` records one answer. `words` must be in
the person's message (`heard`: case and spacing aside), so the model cannot
fill in an answer they did not give. The hub picks the next question and puts
it in the reply word for word (`QUESTIONS`, in this order):

- where: "Where do you want it to live: a new folder or repo, or an existing
  one, and on which machine?" (`machine` must be a worker on the band, not the
  hub; `kind` folder|repo; `new`; `path` absolute or under `~`; `repo`)
- protection: "How do you feel about deployments for it: a toy experiment
  agents can just ship, guarded so they ask you first, or sacred so nothing
  changes without you?" (`level` toy|guarded|sacred; toy is ship-it)
- touches: "What will it touch besides its own folder: any shared machines,
  GPUs, services or data?" (`touches [{host, resource, what}]` or
  `nothing=true`; a resource is matched to the host's baseline by id, title or
  the words that matter)

Also `start` (yes to the offer, or "set up a project for X" without one),
`title`, `review` (`review_days`, the exception's expiry and the first check-in;
default `home.onboard_review_days`), `cancel`. Stages: brainstorm, offered,
intake, readback, done, declined, cancelled.

## 4. Conflicts and scoped exceptions

When everything is answered the hub builds the plan (`Onboarding.plan`) and
reads it back ("Here's the plan for <title>: ... Set it up?"). A host the
project touches, or lives on, whose trust entry is stricter than the
project's level is a conflict:

- touched resources that have a **baseline** become one exception proposal per
  host: `id <slug>-<host>`, `applies_to [<host entry>]`,
  `targets [resource:<baseline id>, ...]`, kinds change+admin, tier go,
  `expires` at the review date, `project <slug>`. Read back as "kaiju's
  sacred, but <title> may stop and restore <resource> until <date> (an
  exception you accept on the Trust page); anything else on kaiju still comes
  to you."
- touched resources without a baseline stay with the person each time ("I
  don't know how to stop and restore X on kaiju yet ...").
- a new folder on such a host needs the person's yes once (an ask at setup).

A `resource:<id>` target matches only when the ruling's context carries that
resource. Only one thing sets it: `FrontDoor.run_action`, for an action
carrying `borrow` (a baseline's own recorded stop or restore call), sets
`trust.borrowing` = `{resource, cap, worker, principal}` inside the call's
task. `Approvals.trust_ruling` passes the resource to the ruling only when
the call it rules on has the same cap, worker (name or id) and principal (the
house agent's `home:<name>`). Any other call on the host is ruled as before:
sacred.

## 5. Setup

`project_setup(words)` on their yes to the plan (stage readback only):

1. **Folder or repo** (new only): `shell.exec` on the machine, `sh -c` with
   `SETUP_SCRIPT`: `mkdir -p`, `git init -q` for a repo, a README if none,
   prints the path. Ruled like any house agent action (`Onboarding._act`):
   `go` runs, otherwise a one-line ask.
2. **Wiki page** "<title>: notes from the brainstorm": the notes and the intake
   answers in their words (`knowledge.write`, in process as the house agent).
3. **Project record** under the concept `home.onboard_concept` (blank:
   `house-projects`, created on first use), linking the page, with the level
   in their words, where it lives, the team and what it touches.
4. **Trust entry** `id <slug>`, kind project, targets `project:<slug>` (+
   `repo:` for an `owner/name` repo), description with their words.
   guarded and sacred apply at once as the house agent's change (it only
   tightens; a notice on the Trust page). **ship-it is a proposal**: it
   loosens, and only the person loosens the map, on the Trust page.
5. **Exceptions**: always proposals, for the same reason.
6. **Starter team** (`TEAMS`): toy: lead; guarded: lead, qa, critic (or
   skeptic); sacred: lead, qa, security, critic. Only roles that exist are
   picked; recorded on the project.
7. The project goes in `onboard_projects` for check-ins; the thread is `done`;
   `setup.json` in its workspace has the plan and what happened.

The reply reads back what was set, from the steps: "Set up <title>: folder
..., project <name>, wiki page ..., trust entry <slug>: ship-it waits for your
tap on Trust (tp_...), exception ... waits for your tap on Trust, team: ...".
Failures are listed as "Not done: ...". `home.onboard.setup` is journaled.

## 6. Borrowed state

`baseline` (trust.json, op `set_baseline`): a host's normal state per
resource `{id, entry, title, normal, stop {worker, cap, args}, restore {...},
check?}`. The house agent, a person or `system` may record one; the house
agent may not change the stop/restore calls of a resource an exception names
(that is the person's). Router tool `record_baseline`.

- `borrow(resource, project?, words)`: the baseline's exact stop call as the
  house agent. With an active exception naming the resource the ruling is
  `go` and it runs; otherwise it is an action ask, and the person's Yes runs
  it. Either way the lease is recorded (`add_lease` as the house agent):
  `{id, resource (host entry), baseline, project, what ("<title> stopped"),
  why, words, restore (readable), restore_call, exception?, until?,
  authorized_by}`. An open lease on the same baseline refuses a second borrow.
  A run under an exception lands in the digest citing it.
- `restore(what, words)` ("bring Strata back"): finds the open lease by
  baseline, title, project or id, runs `restore_call`, closes the lease
  (`state restored`). Without an active exception it is a `restore` ask.
- `ledger(host?)`: what is borrowed and each host's normal state, with a
  `spoken` line ("Borrowed right now: ..." / "Nothing is borrowed ...").
  The trust summary agents get lists "Borrowed now: ...".

## 7. The sweep

Every `home.onboard_sweep_minutes` (default 10; 0 off) while the front door is
on, `Onboarding.sweep` looks at each open lease:

- its exception ended or expired: "The <project> exception has ended. Want me
  to bring <resource> back up?"
- its project is done, archived, closed or cancelled: "<project> ran to
  completion. Want me to bring <resource> back up?"
- no project update and no lease activity for `home.onboard_quiet_days`
  (default 14): "<project> has been quiet N days. Close the <host> exception
  and restore <resource>?" (Yes also ends the exception.)

Each is a `restore` ask (one line, Yes / No / Explain, in the relayed
approvals list, so the phone card and `rook_approvals` see it), made once per
lease and reason (`onboard_offers`), and never while another open ask already
offers to restore that lease. A Yes to a restore ask whose lease is already
closed does nothing ("Already restored; nothing to do."). A Yes runs with the
person's decision, so a trust hold on the call (exception gone) is approved
by it.

Projects whose review date passed get a `review` ask: "<title> has been
<level> for N days. Still a toy?" (guarded: "Still guarded, not core?";
sacred: "Still sacred?"). Yes keeps it and asks again after
`onboard_review_days`. No tightens the project's entry one level (a ship-it
proposal never accepted becomes a guarded entry) and ends its exceptions;
borrowed state stays until the person says restore.

## 8. Caps, MCP, dashboard

- `home.ledger` (read) `{host}`; `home.projects` (read): onboarded projects
  and threads; `home.restore` (write) `{what, words, relayed_by}`: a verified
  person or a client they enabled to relay; runs the restore with their
  decision.
- `rook_house` actions `ledger` (data `{host}`), `restore` (data `{what}`,
  words, relayed_by), `projects`.
- Decisions page: **Borrowed now** (open leases) after the asks, and a
  collapsed **New projects** list (threads, stage, the plan read back, what
  setup did and which proposals to accept on Trust). Trust page: the ledger
  shows project, the person's words and until; a collapsed **Normal state**
  list shows the baseline.
- A dashboard Yes to any ask that runs an action (action and restore asks)
  needs a sign-in that is not a band secret, as before.
- Settings: `home.onboard_review_days`, `home.onboard_quiet_days`,
  `home.onboard_concept`, `home.onboard_sweep_minutes`.

## 9. Not built / limits

- The project's own folder or instances on a sacred host are not covered by
  the exception: there is no path selector, so such changes ask each time.
  Only the declared baseline stop/restore calls go.
- No GitHub repository is created: a "repo" is a local `git init`. An
  existing `owner/name` repo is recorded and added to the trust entry.
- The house agent does not discover a host's stop/restore commands; a
  baseline must be recorded (by the person, `system`, or the house agent with
  exact calls) before a resource can be borrowed under an exception.
- The starter team is recorded on the project; no roles are created and no
  code location is added for sessions.
- "Quiet" is the project record's last update (and the lease's start), not
  commits or session activity.
