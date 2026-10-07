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
| Live terminals | `rook/worker/plugins/terminals.py` (`work.stream.*`), `rook/remote/term_hub.py`, `rook/web/worklog.js` | PTY on the worker, byte ring with cursor long-poll, hub fan-out, xterm.js in the browser, one input holder. Linux/macOS only (`has('pty')`). Docs: `docs/web/worklog.md`. |
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
| Home agent | `rook/hub/plugins/home/`, Manage > Home agent (`home.js`) | Hub LLM reachable as `@home` in chat rooms and `home.ask`. Config page only, no chat on the page. |

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
  "worker_id": "…", "worker": "cachyrig",
  "agent": "claude", "native_id": "634b5e51-…",
  "title": "Voice replies", "cwd": "/home/bake/rook",
  "state": "live" | "idle" | "closed",   // live: a process holds it; idle: live, waiting for input; closed: no process
  "origin": "rook" | "external",        // started by Rook (a Rook terminal) or anywhere else
  "updated": 1791400000, "messages": 412,
  "view": {                              // what a viewer can attach to, best first
    "terminal": "t_ab12…" | null,        // raw PTY bytes (work.stream), Rook-started only
    "mirror": true | false,              // live event stream from the Claude Code mod
    "transcript": true                   // transcript tail (always, for claude/codex)
  },
  "input": "pty" | "inbox" | "none",     // how send() reaches it; inbox notes hold/accept below
  "inbox_policy": "accept" | "hold" | "unknown",
  "links": {"task": "t_…", "claim": "…", "work_session": "…", "console_room": "…", "chat_room": "…"},
  "resumable": true                      // closed and the agent supports resume
}
```

The **worker** is the source of truth for its own sessions (it sees the
processes, transcripts and terminals). The **hub** keeps a cache of the
latest catalog per worker plus the links only it knows (task, chat room,
work session), and serves the merged list.

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
   Rook. Exists today on Linux/macOS; Windows needs ConPTY (workstream D).
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
The mod writes only where the worker's state dir already exists. The spool
folder is owner-only: 0700 with 0600 chunks on POSIX, an owner-only ACL
(`icacls /inheritance:r`) on Windows. Secrets: the mod has no access to the
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
schema is filed under the namespace. The mirror plugin has neither.

### 3.5 Worker caps (target)

All on the existing `terminals` plugin unless noted; old caps stay as thin
aliases for at least one release.

- `sessions.list(limit, offset, query, live_only)` → `{ok, harnesses, items: [record…], total, next_offset}`; supersedes `work.sessions` (kept as alias).
- `sessions.mirror(agent, native_id, cursor, wait)` (above; its own plugin, `session_mirror`).
- `sessions.follow(agent, native_id, offset, version)` → transcript tail for either agent (wraps `claude-history.follow`, adds Codex).
- `sessions.send(agent, native_id, text, command_id)` → routes to inbox or PTY; returns `{ok, delivery: "turn"|"held"|"keys", note}`.
- `sessions.stop(agent, native_id)`.
- `work.stream.*` unchanged except `work.stream.open(handoff_pid=…)` (§3.3, Take over); `work.stream.open(resume=…)` becomes the only resume path (`*-history.resume` delegates to it where `work.stream` exists).

### 3.6 Hub

- `sessions` store: latest catalog per worker (refreshed when a viewer has
  the page open, and from the heartbeat summary `hb.work.sessions` =
  `{live, idle}` counts), plus links.
- One viewer socket per session: `/account/work/session/<key>` speaks the
  worklog terminal protocol for tier 1 and a JSON event protocol for tiers 2
  and 3 (`{type: "event", event}` frames, `{type: "send", text}` from the
  browser).
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
Code uses there); Codex control socket path on Windows.

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
