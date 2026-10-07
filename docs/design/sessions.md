# Sessions: one place to start, watch, steer and resume agent work

Status: master plan, October 2026. Work lands as PRs to the `beta` branch;
each workstream below is one PR (or a short series). This document is the
contract between them: if a PR needs to change a shape defined here, it
changes this document in the same PR.

## 1. What Bake asked for

1. **Start** a new interactive session (Claude Code, Codex, Hermes, a shell)
   on any worker from the web, as a real terminal.
2. **Stream running sessions**, including ones started the ordinary way in
   someone's own terminal, and **restart closed ones**.
3. **The Sessions page is where they all live.** One list, one place.
4. **Claude Code and Codex just work normally on any machine.** No wrapper
   command, no special way of starting them. The Claude Code mod may help.
5. **Consistency as a feature**: the session pieces built over time in many
   places should behave as one system, with one vocabulary.
6. A **chat with the home agent** on its web page.

## 2. What exists today

| Piece | Where | What it does |
|---|---|---|
| Live terminals | `rook/worker/plugins/terminals.py` (`work.stream.*`), `rook/remote/term_hub.py`, `rook/web/worklog.js` | PTY on the worker, byte ring with cursor long-poll, hub fan-out, xterm.js in the browser, one input holder. Linux/macOS PTYs; Windows 10 1809+ ConPTY (`rook/worker/conpty.py`). Placement `has('pty')`. Docs: `docs/web/worklog.md`. |
| Session catalog | `work.sessions` (terminals.py) | Live terminals plus Claude/Codex history on one host. |
| History | `claude-history.*`, `codex-history.*` | List, search, read, `follow` (transcript tail by version), `transcript`/`export` (`rook.transcript/1`), `resume` (through `proc.start`), `send`. |
| Session inbox | `rook/worker/session_messages.py`, `codex_input.py` | Claude: the peer messaging socket (arrives as a user turn; `crossSessionInbound` decides hold/accept). Codex: app-server control socket, or typing into Konsole over D-Bus. |
| Activity | `rook/worker/agent_activity.py` | Which history sessions have a live process. Linux `/proc` only; other platforms report nothing. |
| Processes | `proc.*` | Pipe or PTY processes with a handle; no ring, no hub fan-out. |
| Console rooms | `rook/band_mcp/console_rooms.py` | Named, archived, searchable "terminal as chat room", pumped from `proc.read`, ANSI stripped. |
| Codex app-server work | `work.*` (`rook/worker/plugins/work.py`, `work_runtime.py`) | Web-initiated Codex sessions driven through the app-server protocol (the classic Work view). |
| Web | Dashboard **Sessions** tab (`work.js`: worklog or classic), **Work** tab (tasks) | Two views of sessions, a separate task board. |
| Claude Code mod | `integrations/claude-code/` | Pane with Bands, Sessions (calls `claude-history` directly), Deck, Settings. |
| Tasks and handoffs | `rook_task`, `rook_handoff_*`, journal | Work tracking; `ROOK_WORK_SESSION` env links a Rook-launched terminal to its hub session. |
| Home agent | `rook/hub/plugins/home/`, Manage > Home agent (`home.js`) | Hub LLM reachable as `@home` in chat rooms and `home.ask`. Config page with a chat panel (workstream E, polls the shared 1:1 room). |

The pieces work; they disagree. A Claude session has a session id, maybe a
terminal id, maybe a proc handle, maybe a console room, maybe a hub work
session, maybe a task claim, and nothing links them. "Send a message" means
four different things depending on where you are.

## 3. The model

### 3.1 One session record

A **session** is one agent conversation (or one shell) on one host. Its
identity is `(worker_id, agent, native_id)`:

- `agent`: `claude`, `codex`, `hermes`, `shell`.
- `native_id`: the agent's own id (Claude session id, Codex thread/rollout
  id). A shell, or an agent before it has reported its id, uses the
  terminal id, and the record is re-keyed when the native id appears.

Every surface (web, mod, MCP, CLI) reads the same record:

```jsonc
{
  "key": "w_3f2a…/claude/634b5e51-…",   // worker_id/agent/native_id
  "worker_id": "…", "worker": "workstation",
  "agent": "claude", "native_id": "634b5e51-…",
  "title": "Voice replies", "cwd": "/srv/rook",
  "state": "live" | "idle" | "closed",   // live: a process holds it; idle: live, waiting for input; closed: no process
  "origin": "rook" | "external",        // started by Rook (a Rook terminal) or anywhere else
  "updated": 1791400000, "messages": 412,
  "view": {                              // what a viewer can attach to, best first
    "terminal": "t_ab12…" | null,        // raw PTY bytes (work.stream), Rook-started only
    "mirror": true | false,              // live event stream from the Claude Code mod
    "transcript": true                   // transcript tail (always, for claude/codex)
  },
  "input": "pty" | "inbox" | "none",     // how send() reaches it; inbox notes hold/accept below
  "inbox_policy": "accept" | "hold" | "refuse" | "unknown",
  "links": {"task": "t_…", "claim": "…", "work_session": "…", "console_room": "…", "chat_room": "…"},
  "resumable": true                      // closed and the agent supports resume
}
```

The **worker** is the source of truth for its own sessions (it sees the
processes, transcripts and terminals). The **hub** keeps a cache of the
latest catalog per worker plus the links only it knows (task, chat room,
work session), and serves the merged list.

How the worker fills the record (as built, `rook/worker/plugins/sessions.py`):

- **state**: `live` while a process holds the session (Claude PID marker,
  exact resume argument, open transcript, or a running Rook terminal);
  `idle` when Claude's marker says `status: idle`, or, without a marker, the
  transcript's last turn ended (`activity: ready`) and no Rook terminal runs
  it; `closed` otherwise. Activity detection: Linux `/proc`; macOS `ps` plus
  `lsof` for open transcripts; Windows the process snapshot (no command
  lines there, so only Claude's markers count, checked against the process
  creation FILETIME Claude records as `procStart`). Codex sessions on
  Windows are not detected as live yet.
- **origin**: `rook` while the worker still has the Rook terminal (running,
  or up to 15 minutes after it ended); `external` otherwise, including a
  closed session that once ran in a Rook terminal.
- **native_id** of a Rook terminal: the `resume` id, else the session the
  terminal's process (or its child) holds once the agent reports it, else
  the terminal id. The key changes when the native id appears.
- **input**: a reachable inbox is `session_messages.messageable`: Claude's
  Unix socket on Linux/macOS or its named pipe on Windows, Codex's control
  socket or Konsole. `inbox` when the session has one and the policy
  is `accept` or `unknown`; else `pty` when a Rook terminal runs it; else
  `inbox` when the policy is `hold` (the message will wait for approval);
  else `none`. `refuse` never routes to the inbox.
- **inbox_policy** (Claude): the mod's live `inbound` from the latest
  `session.start` in the mirror spool, else `crossSessionInbound` from managed settings,
  else the user's `~/.claude/settings.json`. Its `default` holds while
  permissions are bypassed: `--dangerously-skip-permissions` or
  `--permission-mode bypassPermissions` on the process command line, or
  `permissions.defaultMode: bypassPermissions` in managed, project local,
  project or user settings (first that sets it). Without a command line
  (Windows) a default the settings do not decide is `unknown`. Codex:
  `accept` when it has an inbox, else `unknown`; shells and Hermes
  `unknown`.
- **view.mirror**: the session has a spool (§3.4; read through
  `rook.worker.session_mirror.spools()` / `chunks()`, the same helpers
  `sessions.mirror` uses). **view.transcript**: claude/codex with a known native id.
- **links**: the worker only knows `work_session` (the hub session a Rook
  terminal was launched for); the hub adds the rest.
- Extra fields that may appear: `activity` (`working`/`ready`/`pending`),
  `pid` (live only), `model`.

### 3.2 One set of verbs

| Verb | Meaning | Today | Target |
|---|---|---|---|
| list | the catalog | `work.sessions`, `*-history.pull`, `rook_console_list` | `sessions.list` (worker), hub merged list |
| view | attach a live viewer | worklog socket (terminal), mod polls `follow` | terminal if any, else mirror, else transcript; one hub socket |
| send | put text in front of the agent as the user | `claude-history.send`, `codex_input`, `work.stream.write` | `sessions.send`: inbox for agents that have one (a real user turn), PTY keystrokes otherwise |
| new | start an interactive session | `work.stream.open` | unchanged, from the Sessions page and the mod |
| resume | reopen a closed session | `*-history.resume` (proc) or `work.stream.open(resume=)` | always `work.stream.open(resume=)` so it is streamable |
| stop | end it | `work.stream.close`, `proc.close` | `sessions.stop` |
| export | transcript pages | `work.export` (`rook.transcript/1`) | unchanged |
| link | attach to a task | env var at launch only | `sessions.link` (hub) and automatic on launch-for-task |

### 3.3 Streaming a session Rook did not start

Rook cannot read the raw bytes of a terminal it does not own, and Bake does
not want a wrapper. So there are three tiers, and the page always shows the
best one available:

1. **Terminal** (raw bytes, full control). Sessions started or resumed by
   Rook. Linux/macOS (PTY) and Windows 10 1809+ (ConPTY, workstream D).
2. **Mirror** (live events, near real time). A Claude Code session with the
   Rook mod installed writes its own events (prompt, streamed assistant
   text, tool calls and results, turn end, session start/end) to a spool
   file on its host. The worker tails the spool and serves it. This is how a
   session started normally in Konsole, Windows Terminal or VS Code becomes
   watchable live, with no change to how it was started.
3. **Transcript** (message-level, a second or two behind). Any Claude or
   Codex session, from the JSONL the agent writes anyway (`follow`). The
   fallback for Codex and for machines without the mod.

Input to an external session goes through its inbox (Claude peer messaging,
Codex control socket). Where the inbox holds messages for approval
(`crossSessionInbound` = hold, the work laptops), the page says the message
is waiting for approval on that machine.

**Take over.** The mod adds `/rook-move` to Claude Code: it resumes the same
conversation in a Rook terminal on the same host and exits the local Claude
Code, so from then on the session is tier 1. Two processes must never hold
one session, so the mod calls `work.stream.open(harness="claude",
resume=<id>, cwd=<cwd>, handoff_pid=<its own pid>)`: the worker returns the
terminal at once and starts the harness only after that pid has exited (it
gives up after 2 minutes), then the mod runs `/exit`. The pid comes from
Claude Code's own PID marker (`~/.claude/sessions/<pid>.json`); without one
the mod does not move and says to `/exit` and resume from the Sessions page. The Sessions page cannot move a
live external session by itself (only the person at that terminal can end
it); it shows the hint "run /rook-move there" on such sessions, and offers
**Resume in a Rook terminal** once the session is closed.

### 3.4 Mirror spool contract

Path: `<rook worker state dir>/mirror/<agent>/<native_id>.jsonl` where the
state dir is `~/.rook-band-worker` (Windows `%USERPROFILE%\.rook-band-worker`),
overridable by `ROOK_WORKER_HOME`. One JSON object per line:

```jsonc
{"v": 1, "seq": 17, "ts": 1791400000.123, "type": "assistant.delta", "text": "…"}
```

| `type` | Fields |
|---|---|
| `session.start` | `cwd`, `title?`, `model?`, `pid` (null when Claude Code recorded none), `version` (Claude Code), `inbound` (accept/hold/refuse/default) |
| `prompt` | `text`, `from` (`person` or `peer`), `origin` (Claude Code's own word for where it came from: `composer`, `bridge`, `peer`, …) |
| `assistant.delta` | `text` (streamed; the pieces of one flush, about 250 ms, arrive as one event) |
| `assistant.done` | `text` (the full message, so a late viewer needs no deltas) |
| `tool.call` | `id`, `name`, `input` (clipped to 2,000 chars) |
| `tool.result` | `id`, `ok`, `text` (clipped to 4,000 chars) |
| `turn.end` | `stop_reason?` (`answer`, `aborted`, `refusal`, `error`) |
| `state` | `state` (`working`, `idle`, `waiting` (on a permission prompt)) |
| `session.end` | `reason?` |

Only the main conversation is mirrored; a subagent's work shows in its
tool result. Readers ignore fields they do not know.

**Chunks.** The mod API has no append, so the spool is cut into chunks and
the mod rewrites the newest chunk whole on each flush (at most one write per
250 ms, never awaited by a hook): `<native_id>.jsonl`, then
`<native_id>.1.jsonl`, `<native_id>.2.jsonl`, … each up to 256 KiB. A chunk
is never written again once the next exists. Past 64 chunks (16 MiB) the mod
empties the oldest (it cannot delete); readers skip empty chunks. A reader
counts only whole lines (ending in a newline) and skips a line that does not
parse, so a read that catches a rewrite half done just returns fewer events.

Rules: `seq` increases by one per line across the chunks (a reload of the
mod, or a `/resume` of the same session in a new process, reads the last
`seq` and carries on); the worker deletes spools of closed sessions after 7
days (a spool with no `session.end` whose process may still run is kept).
The mod writes only where the worker's state dir already exists, and only
while its `mirror` option ("Mirror this session to Rook", default on) is on;
off, it writes nothing and creates no folder. The spool folder is owner-only:
0700 with 0600 chunks on POSIX. On Windows, `icacls <state>\mirror
/inheritance:r /grant:r *<user SID>:(OI)(CI)F *S-1-5-18:(OI)(CI)F`: the
person's own SID (from `whoami /user`, so a domain account is never
ambiguous; `DOMAIN\user` if that fails) plus SYSTEM. The band worker
installs as a logon scheduled task running as the person (elevated), and the
older `rook/remote/worker.py` installer can run it as an NSSM service under
LocalSystem, so both must be able to read the spool. If the person cannot be
named, the folder keeps its inherited ACL. Secrets: the mod has no access to the
vault, so it masks nothing itself; the hub masks known vault values in every
reply that crosses it, `sessions.mirror` included. Tool inputs and results are
clipped, never expanded.

Worker cap: `sessions.mirror(agent, native_id, cursor=0, wait=0, max_events=500)`
returns `{ok, events: [...], cursor, done, exists}`, long-polling like
`work.stream.read` (wait up to 25 s). `cursor` is the last `seq` the caller
has (0 for everything); pass the returned `cursor` back. `done` is true when
the last event is `session.end`, or when the process of the last
`session.start` is gone (POSIX only). `exists` is false when there is no
spool. `rook.worker.session_mirror.spools()` lists the spools on the host,
for the catalog's `view.mirror`.

The mirror plugin (`rook/worker/plugins/session_mirror.py`) shares the
`sessions` namespace with the catalog plugin. The loader registers caps by
their full name and refuses only a duplicate cap, so both load side by side.
Two things key on the namespace and would collide: `host.plugin("sessions")`
(a `DEPENDS` lookup) finds the first one loaded, and a heartbeat or settings
schema is filed under the namespace. The mirror plugin has neither; the
catalog plugin's heartbeat (`hb.sessions`, §3.5) is the only one there.

### 3.5 Worker caps

`sessions.list/follow/send/stop` live in their own plugin,
`rook/worker/plugins/sessions.py` (namespace `sessions`), because they work
wherever there is agent history, inboxes or process evidence, while the
`terminals` plugin needs a PTY (or ConPTY on Windows 10 1809+). `sessions.mirror` is in `session_mirror.py` (same
namespace). Old caps stay as thin aliases for at least one release.

- `sessions.list(limit=20, offset=0, query="", live_only=false)` →
  `{ok, harnesses, items: [record…], total, next_offset}`. Live and idle
  first, then closed, newest first within each. `query` matches title, cwd
  and native id (scans the newest 500 transcripts per agent); `live_only`
  drops closed ones. `limit` up to 200. Supersedes `work.sessions`, which
  keeps its old shape for the worklog page and older hubs. Risk read.
- `sessions.mirror(agent, native_id, cursor, wait, max_events)` (above; its own plugin, `session_mirror`).
- `sessions.follow(agent, native_id, offset=0, version="")` → the
  `claude-history.follow` / `codex-history.follow` reply (`unchanged`, or
  `messages`, `version`, `replace_from`, …) plus `agent`, `native_id`.
  Shells and Hermes have no transcript (error). Risk read, sensitive.
- `sessions.send(agent, native_id, text, command_id="")` → `{ok, delivery,
  note, native_id}`, routed by the record's `input` (§3.1): `inbox` calls
  `<agent>-history.send` (session_messages / codex_input; `command_id`
  makes a retry safe, default a fresh id) and returns `delivery: "turn"`,
  or `"held"` when the policy is `hold` (plus `detail`, the inbox's own
  delivery word); `pty` writes `text` + `\r` to the Rook terminal
  (`work.stream.write`; multi-line text to an agent is wrapped in bracketed
  paste) and returns `delivery: "keys"` with `terminal`. Errors (`ok:
  false`): empty or over 24,000 characters, policy `refuse`, live with no
  reachable input, closed. Risk exec.
- `sessions.stop(agent, native_id)` → closes the Rook terminal
  (`{stopped: "terminal", terminal, exit_code}`), or the `proc.*` process an
  older resume started (`{stopped: "process", handle}`). A live session
  started outside Rook is refused (end it on its host, or `/rook-move`); a
  closed one returns `{ok: true, stopped: null}`. Risk exec, destructive.
- `work.stream.*` unchanged except `work.stream.open(remote_control=label)`
  (claude only), `work.stream.open(handoff_pid=…)` (§3.3, Take over), a
  `session` field in terminal info, and ConPTY on Windows (workstream D:
  the same caps, so resume and `sessions.send` keys work there too). `*-history.resume`
  delegates to `work.stream.open(harness=agent, resume=id, cwd=…)` where
  the worker has `work.stream.open`, and returns `terminal` (the terminal
  id) instead of `handle`; `*-history.resumed` entries carry `terminal` or
  `handle`. Workers without `work.stream` keep the `proc.*` path.
- Heartbeat: `hb.sessions = {live, idle}`, recounted every 2 minutes from
  process evidence and terminals only (no transcript reads) and after every
  `sessions.list`. It sits under `sessions`, not `work`, so a worker
  without the `terminals` plugin (no PTY/ConPTY) reports it too.

### 3.6 Hub

- **Merged list** (built): `GET /account/work/sessions` in
  `rook/remote/work_web.py`, operator auth like the other `/account/work/*`
  routes (401 signed out, 403 for non-admins), `Cache-Control: no-store`.

  | Param | Default | Meaning |
  |---|---|---|
  | `query` | `""` | passed to each worker (title, cwd, native id), max 200 chars |
  | `live_only` | off | `1`/`true`/`yes`/`on`: only `live` and `idle` |
  | `limit` | 50 | sessions asked of each worker, 1-200 (400 if not a number) |
  | `worker` | all | one worker, by id or name |

  ```jsonc
  {
    "sessions": [record…],      // §3.1, sorted live/idle first then closed, newest first;
                                // each stamped with worker_id, worker (name) and
                                // key = "<worker_id>/<agent>/<native_id>";
                                // links.work_session = the operator's Work session id
                                // for it when the hub has one (the id the worklog
                                // view, its ws ops and /account/work/term/<id> use)
    "workers": [{
      "worker_id": "…", "name": "…", "band": "…",
      "source": "sessions.list" | "work.sessions" | "history",  // how it was read
      "count": 12, "total": 159,   // returned / the worker's total
      "stale": false,              // true: served from the cache, the worker did not answer
      "fetched": 1791400000.0,     // when that catalog was read (null if never)
      "harnesses": ["shell", "claude"],  // what New session may offer there
      "counts": {"live": 1, "idle": 2}   // hb.sessions, or null
    }],
    "errors": [{"worker_id": "…", "worker": "…", "error": "…"}],
    "generated": 1791400000.0
  }
  ```

  The hub fans out to every connected worker in parallel (15 s each). Older
  workers are read through `work.sessions`, else `claude-history.pull` /
  `codex-history.pull`, and turned into records on the hub (no mirror,
  `inbox_policy: "unknown"`); the hub applies `query`/`live_only` to those.
  Workers with none of these caps are left out. The latest unfiltered
  catalog per worker is cached in memory and served, filtered and marked
  `stale`, when that worker fails.
- Heartbeat summary `hb.sessions` = `{live, idle}` counts (§3.5), shown as
  `workers[].counts`.
- One viewer socket per session: `/account/work/session/<key>` speaks the
  worklog terminal protocol for tier 1 and a JSON event protocol for tiers 2
  and 3 (`{type: "event", event}` frames, `{type: "send", text}` from the
  browser). Not built yet (workstream C); until then a record with
  `view.terminal` and `links.work_session` opens through the existing
  `/account/work/term/<work_session>` socket.
- The classic Work view's non-PTY **Resume on host** accepts a `terminal`
  result from `*-history.resume` and attaches the session's terminal.
- MCP: no new tools. Agents use `rook_call` on these caps; `rook_console_*`
  keeps working.

## 4. Workstreams

Each is one PR to `beta` unless it says otherwise. Shared shapes are §3.

**A. Catalog and verbs (worker + hub).** `sessions.list/follow/send/stop`,
the record of §3.1 (state, origin, view, input, inbox policy, links),
activity detection on Windows and macOS (process list, Claude PID markers),
resume through `work.stream.open` everywhere, hub cache and merged list API
for the page. Tests: unit, plus the existing `test_work_terminals.py` and
`test_work_sessions.py` stay green.

**B. Mirror (Claude Code mod + worker).** Mod hooks write the spool of
§3.4; worker `sessions.mirror`; mod `/rook-move`; the mod's Sessions tab
reads `sessions.list` (falls back to `claude-history.pull` on older
workers). Mod version bump. Tests: mod tests (`claude plugin test`), worker
unit tests for tailing, rotation and cleanup.

**C. Sessions page.** One list of every session across workers, grouped by
host and project (live first, then idle, then closed), with: **New session**
(harness, host, folder, model, persona, optional task), **Open** (terminal,
or live view rendering mirror/transcript events, read-only xterm for tool
output like the AI Workbench's output panes), **Send** box (shows "waiting
for approval on <host>" for held inboxes), **Resume** for closed ones,
**Stop**, **Link to task**. Replaces the classic/worklog toggle with one
view; the classic Codex app-server view stays reachable until its features
are covered. Tests: browser tests in `tests/browser_*.py`.

**D. Windows terminals.** ConPTY behind `work.stream.*` on Windows
(pywinpty or ctypes ConPTY), so new and resumed sessions stream from
Windows workers; Claude inbox on Windows (named pipe if that is what Claude
Code uses there); Codex control socket path on Windows. *Done:* pure-ctypes
ConPTY with a kill-on-close Job Object, PowerShell as the shell, Claude's
named-pipe inbox (`\\.\pipe\LOCAL\cc-msg-<hex>`, FILETIME `procStart`,
owner/DACL checks, `GetNamedPipeServerProcessId`). Codex has no control
socket on Windows (its control endpoint is a Unix socket Python cannot reach
there), so Codex input on Windows goes through a Rook terminal. Details and
the platform table: `docs/web/worklog.md`.

**E. Home agent chat.** A chat panel on Manage > Home agent: talks to the
home agent in a two-person room through the existing chat store (so the
conversation also shows in Chat), streaming the reply if the plugin can,
with the room's history. Tests: unit and a browser test.

**F. Consistency pass (after A-C).** Console rooms run on `work.stream`
instead of `proc.read` pumping (`proc.*` stays for non-terminal jobs);
launch-for-task claims the task and ending a session offers a handoff;
vocabulary: the dashboard tab, the mod tab and the docs all say
**Sessions** for sessions and **Work** for tasks.

Order: A first (contract), B, C, D and E in parallel against §3, then F.

## 5. Rules for every PR

- Base branch `beta`. Never commit `android/rook.properties`, the private
  wake model, secrets, real host names, user names or home paths.
- Tests never run with the real `HOME`: use a scratch `HOME` and
  `ROOK_UPDATE_KEY` pointing at a scratch path.
- No deploys, no worker updates, no changes to live hosts from a
  workstream; promotion from `beta` is a separate, reviewed step.
- Update `docs/` with the behavior (this file for contracts, user docs for
  what people see).
