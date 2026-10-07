# Sessions page

The dashboard's **Sessions** tab lists every agent session on every connected
worker: Claude Code, Codex, Hermes and shells, whether Rook started them or
someone started them in their own terminal. From here you start new ones,
watch and steer running ones, resume closed ones and stop the ones Rook
started. The contract behind it is docs/design/sessions.md (§3.1 record,
§3.3 view tiers, §3.6 hub routes).

The older **Classic view** (the Codex app-server Work view, [work.md](work.md))
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
streams it), `rook` (Rook started it) and `task …` (linked to a task).

- **Search** matches title, folder and session id. It filters at once and asks
  every worker again (each searches its newest 500 transcripts per agent).
- **Filters**: agent, host, **Live only**.
- The header counts live, idle and closed sessions; a host heading shows that
  host's live and idle counts from its heartbeat.
- **Notices**: a host that did not answer stays listed from the hub's last
  copy of its list, marked **stale**, with a line saying when that copy was
  read. Other per-host errors are listed the same way.

The list refreshes every 8 seconds while the tab is open and visible.

## Opening a session

Opening a session shows the best view it has:

1. **Terminal**: the session runs in a Rook terminal. You get the real
   terminal (xterm.js), the same one the hub fans out to every viewer: one
   viewer holds input (**Take control**, **Release**, **Hand off to…**,
   **Ctrl-C**), and only the holder's window size reaches the terminal. A
   Rook terminal the hub has no Work session for yet (one an agent opened, or
   one `/rook-move` created) is attached on first open.
2. **Live view**: the session was started elsewhere and the Rook mod mirrors
   it (Claude Code only). Prompts and messages from other sessions, the
   assistant's text as it streams, tool calls (name and clipped input) and
   their results, turn ends, and a state chip (working, idle, "waiting for
   approval on <host>" while a permission prompt is open). It long-polls the
   mirror, so new events appear within a second.
3. **Transcript**: no terminal and no mirror (Codex, or a machine without the
   mod). The tail of the session's log, a few seconds behind; the last 40
   messages, with a note of how many earlier ones are not shown.

Tool output, in the live view and the transcript, is shown in read-only
terminal panes, so colours and progress bars render as they did on the host.
The panes take no input and ignore clipboard writes (OSC 52) and hyperlinks
(OSC 8) in that output; an emulator is created only while its pane is near
the visible part of the log. Assistant and user text is shown as plain text.
The log keeps its newest 400 entries.

On a phone the open session replaces the list; **← All sessions** goes back.

## Actions

- **Send** (live and idle sessions with a way in): the text goes to the
  session as if you typed it. The result says how it arrived: "Delivered as a
  new turn" (the agent's inbox), "Waiting for approval on <host>" (Claude Code
  there holds messages from other sessions until someone accepts them), or
  "Typed into the Rook terminal". Enter sends; Shift+Enter adds a line.
- **Resume in a Rook terminal** (closed Claude and Codex sessions, on hosts
  that run Rook terminals): reopens the conversation in a Rook terminal and
  opens it.
- **Stop** (sessions Rook started): ends the terminal on the host and revokes
  the session's MCP token. A live session started outside Rook cannot be
  stopped from here: the page says to run `/rook-move` in that Claude Code to
  hand it to Rook, or to end it on its host.
- **Link to task**: a task id (`t_…`) or slug, stored on the hub. Empty
  unlinks.
- **New session**: host, harness (only those the host reports installed),
  folder (absolute; recent folders on that host are suggested), model,
  persona, title, an optional task to link, and **Give it a Rook MCP token
  scoped to this session** (see [worklog.md](worklog.md) for the token's
  scope and lifetime). The session opens in its terminal as soon as the host
  has started it.

## Hub routes

All are on the dashboard and need an operator login.

| Route | Purpose |
|---|---|
| `GET /account/work/sessions` | the merged list (sessions.md §3.6) |
| `POST /account/work/session/<op>` | `mirror`, `follow`, `send`, `stop`, `resume`, `new`, `attach`, `link` (sessions.md §3.6) |
| `/account/work/term/<work session>` | the terminal websocket ([worklog.md](worklog.md)) |

Text that crosses the hub from a session (mirror events, transcript pages) is
masked for known vault values before it reaches the browser.

## Verification

- `pytest tests/test_sessions_page.py`: the hub routes (auth, Origin, CSRF,
  relays and their fallbacks for older workers, the long-poll cap, launch,
  resume, attach, stop, task links).
- `python tests/browser_sessions.py [--screenshots DIR]`: the page in
  Chromium against a real terminals plugin and scripted `sessions.*` caps:
  list, filters, stale host, live view with tool output, transcript, send
  (held, turn, keys), link, new session, two viewers and hand-off, stop,
  resume, classic link, phone layout.
