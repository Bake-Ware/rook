# Sessions page

The dashboard's **Sessions** tab lists every agent session on every connected
worker: Claude Code, Codex, Hermes and shells, whether Rook started them or
someone started them in their own terminal, plus the console rooms agents
open (`rook_console_open`), which run in Rook terminals on workers that have
them. From here you start new ones, watch and steer running ones, resume
closed ones and stop the ones Rook started. The contract behind it is
docs/design/sessions.md (§3.1 record, §3.3 view tiers, §3.6 hub routes).

Words: **Sessions** are agent and terminal sessions (this page); **Work** is
tasks (the Work page, `rook_task`). A session can be linked to the task it
works on.

The older **Classic view** (the Codex app-server sessions view, [work.md](work.md))
is one link away in the page header until the Sessions page covers its
features. Each browser remembers which of the two it was on
(`localStorage` key `rook.work.view`). `ROOK_WORK_V2=0` on the dashboard serves
only the classic view.

## The list

Sessions are grouped by host, then by project folder. Within a project, live
sessions come first, then idle ones (live, waiting for input), then closed
ones, newest first; hosts and projects with live sessions sort first. Each row
shows the title, agent, state, age and message count, plus chips:
`terminal` (it runs in a Rook terminal), `mirror` (the Rook Claude Code mod
streams it), `rook` (Rook started it), `console room` (an agent's console
room: its output is also archived and searchable with `rook_console_search`)
and `task …` (linked to a task).

- **Search** matches title, folder and session id. It filters at once and asks
  every worker again (each searches its newest 500 transcripts per agent).
- **Filters**: agent, host, **Live only**.
- The header counts live, idle and closed sessions; a host heading shows that
  host's live and idle counts from its heartbeat.
- **Activity**: the header's **Activity** button opens a panel over the
  page (it never pushes the page around) with host warnings and the results
  of what you did here: a host that did not answer (it stays listed from the
  hub's last copy, marked **stale**), a host answering again, send results
  ("Waiting for approval on …", "Delivered as a new turn", "Typed into the
  Rook terminal"), task links, stop and resume results, a terminal's process
  exiting, and read errors in live views. A badge counts entries you have not
  seen, red when one is a warning or error. A repeat of the newest entry
  bumps its count instead of adding a row; the panel keeps the last 200.
- **maybe running**: a Claude or Codex session that no process on its host is
  known to hold, but that may still be running there: its transcript changed
  in the last 2 minutes, or a Claude Code in its folder has a PID marker
  Rook cannot read (a full disk can leave `~/.claude/sessions/<pid>.json`
  empty) and this is that folder's newest session. It counts as live, and
  is never offered for resume (see Actions).

The page shows the hub's last copy of every host's list at once, then asks
each host on its own and replaces that host's rows as it answers; the label at
the end of the filters says "Updating…" (its tooltip names the hosts it is
waiting for) and then "Updated <time>". A slow host
never holds up the others. The list refreshes every 8 seconds while the tab
is open and visible.

## Opening a session

Opening a session shows the best view it has:

1. **Terminal**: the session runs in a Rook terminal. You get the real
   terminal (xterm.js), the same one the hub fans out to every viewer: one
   viewer holds input (**Take control**, **Release**, **Hand off to…**,
   **Ctrl-C**), and only the holder's window size reaches the terminal. A
   Rook terminal the hub has no Work session for yet (one an agent opened, or
   one `/rook-move` created) is attached on first open.
2. **Live view**: the session was started elsewhere and the Rook mod mirrors
   it (Claude Code only). It long-polls the mirror, so new events appear
   within a second; a state chip above it says working, idle or "waiting for
   approval on <host>".
3. **Transcript**: no terminal and no mirror (Codex, or a machine without the
   mod). It opens at the last 40 messages (one request: the host sends just
   those, each clipped to 4,000 characters, with a line saying how many
   earlier ones are not shown) and then follows only new messages, a few
   seconds behind. Hosts from before this asked for the whole log in pages
   and take one request more to reach the end.

The live view and the transcript are drawn in one read-only terminal
(xterm.js), the way the agent's own terminal shows them: `> ` and bold for
prompts (and "message from another session" above one that came from
another session), `● ` for the assistant's text (streamed text appears in
place), `● Name(argument)` for a tool call with its output indented under
`  ⎿  ` (dimmed, the first 6 lines, with "… +N lines"), a dim rule at the end
of each turn, and a status line under the last block ("✻ Working…",
"✻ Waiting for approval on <host>", "· idle"). A Claude Code live view
starts with a box naming the version, folder, model and inbox setting.
Codex sessions use Codex's marks (`› ` and `• `). The terminal keeps 10,000
lines and stays at the bottom unless you scroll up to read. It takes no
input; colours in tool output survive, and every other control sequence in
session text is dropped, so a session can neither write to your clipboard
(OSC 52) nor show links (OSC 8).

On a phone the open session replaces the list; **← All sessions** goes back.

## Actions

- **Send** (live and idle sessions with a way in): the text goes to the
  session as if you typed it. The result says how it arrived: "Delivered as a
  new turn" (the agent's inbox), "Waiting for approval on <host>" (Claude Code
  there holds messages from other sessions until someone accepts them), or
  "Typed into the Rook terminal". Enter sends; Shift+Enter adds a line.
- **Resume in a Rook terminal** (closed Claude and Codex sessions, on hosts
  that run Rook terminals): reopens the conversation in a Rook terminal and
  opens it. Never offered for a session that is **maybe running**: two Claude
  Codes on one transcript corrupt it. The page says why instead, and the host
  refuses such a resume too (`work.stream.open` without `force`).
- **Stop** (sessions Rook started): ends the terminal on the host and revokes
  the session's MCP token. A live session started outside Rook cannot be
  stopped from here: the page says to run `/rook-move` in that Claude Code to
  hand it to Rook, or to end it on its host.
- **Link to task**: a task id (`t_…`) or slug, stored on the hub and added to
  the task's links (kind `session`). Empty unlinks. Linking does not claim
  the task.
- **New session**: host, harness (only those the host reports installed),
  folder (absolute; recent folders on that host are suggested), model,
  persona, title, an optional task, and **Give it a Rook MCP token
  scoped to this session** (see [worklog.md](worklog.md) for the token's
  scope and lifetime). The session opens in its terminal as soon as the host
  has started it.

## Sessions and tasks

A session started for a task (the **Task** field of New session) claims that
task for you, the way `rook_task(action="claim")` would, and links the
session to it (the task's links show `session <host id>/<harness>/<terminal>`).
A claim that fails (an unknown task, tasks turned off on the hub) does not
stop the session; it starts unlinked from the task's side.

When a linked session ends (Stop, or its process exits), the task gets a note
saying so and asking for a handoff, and its claimants get a `session_ended`
hygiene finding (on the deck, and on an agent's next reply). Nothing closes
the task or releases the claim: whoever did the work leaves the handoff or
sets the state. The page shows the same reminder on a closed session that is
linked to a task. The note is sent with your next request to the dashboard
(the hub notices an ended terminal without one), so it may lag by up to half
a minute.

Agents get the same: `rook_console_open(task_id=…)` and
`rook_call("work.stream.open", args={…, "task": …})` claim the task for the
calling agent and link the room or terminal, and the hub notes the end on the
task when the process exits.

## Hub routes

All are on the dashboard and need an operator login.

| Route | Purpose |
|---|---|
| `GET /account/work/sessions` | the merged list (sessions.md §3.6); `cached=1` answers from the hub's last copy without asking any host, `worker=` asks one host |
| `POST /account/work/session/<op>` | `mirror`, `follow` (with `tail`), `send`, `stop`, `resume`, `new`, `attach`, `link` (sessions.md §3.6) |
| `GET /account/work/assets/session_screen.js` | the read-only terminal of the live view and the transcript |
| `/account/work/term/<work session>` | the terminal websocket ([worklog.md](worklog.md)) |

Text that crosses the hub from a session (mirror events, transcript pages) is
masked for known vault values before it reaches the browser.

## Verification

- `pytest tests/test_sessions_fixes.py`: the cached list, `follow(tail=)`
  and the hub's fallback for older hosts, the cached transcript scans behind
  a fast `sessions.list`, the 2-minute rule and unreadable PID markers.
- `pytest tests/test_sessions_page.py`: the hub routes (auth, Origin, CSRF,
  relays and their fallbacks for older workers, the long-poll cap, launch,
  resume, attach, stop, task links, the claim on launch and the note on end).
- `pytest tests/test_sessions_consistency.py`: console rooms on Rook
  terminals and the `proc.*` fallback, command terminals on the worker, and
  task claims, links and end notes from the MCP side.
- `python tests/browser_sessions.py [--screenshots DIR]`: the page in
  Chromium against a real terminals plugin and scripted `sessions.*` caps:
  the list as hosts answer (a slow host, then the cached copy on reload),
  filters, stale host, the live view and transcripts as read-only terminals
  (their text, styles, no links or clipboard writes), a 400-message
  transcript opened at its tail in one request, a "maybe running" session
  without Resume, send (held, turn, keys), link, new session, two viewers and
  hand-off, stop, resume, classic link, phone layout.
