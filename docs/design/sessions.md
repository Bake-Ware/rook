# Sessions: one place to start, watch, steer and resume agent work

Status: master plan, October 2026. Work lands as PRs to the `beta` branch;
each workstream below is one PR (or a short series). This document is the
contract between them: if a PR needs to change a shape defined here, it
changes this document in the same PR. All six workstreams (§4, A-F) have
landed on `beta`; none is promoted yet. Workstream G (the session shim,
§4) follows them.

Words (workstream F): **Sessions** means agent and terminal sessions
everywhere (dashboard Sessions page, the mod's Sessions tab, console rooms,
`sessions.*` and `work.stream.*` caps); **Work** means tasks (dashboard Work
page, the mod's Deck tab, `rook_task`). Cap, route and tool names that say
`work` for sessions (`work.stream.*`, `/account/work/*`, `ROOK_WORK_SESSION`,
the hub's "Work session" record) keep their names.

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
| Console rooms | `rook/band_mcp/console_rooms.py`, `console_pump.py` | Named, archived, searchable "terminal as chat room", ANSI stripped. Pumped from `work.stream.read` (the process is a Rook terminal, on the Sessions page) where the worker's terminals take commands, else from `proc.read` (§3.7). |
| Codex app-server work | `work.*` (`rook/worker/plugins/work.py`, `work_runtime.py`) | Web-initiated Codex sessions driven through the app-server protocol (the classic Work view). |
| Web | Dashboard **Sessions** tab (`sessions.js`, the Sessions page; `work.js` keeps the classic view behind a link), **Work** tab (tasks) | One list of sessions, a separate task board. Docs: `docs/web/sessions.md`. |
| Claude Code mod | `integrations/claude-code/` | Pane with Bands, Sessions (`sessions.list`, falling back to `claude-history`), Deck (open tasks and handoffs: Work), Settings. |
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
  "resumable": true,                     // closed and the agent supports resume
  "possibly_live": "recent_write" | "unreadable_marker"  // only when the state is a guess (below)
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
  it; `closed` otherwise. Two guesses keep a session that may still run from
  looking closed (and resumable): a Claude or Codex transcript modified in
  the last 120 s (`possibly_live: "recent_write"`), and, for each live
  `claude` process whose PID marker is empty or unparsable (a full disk
  leaves it at 0 bytes; a `claude -c` has no id on its command line either),
  the newest transcript in that process's folder (its cwd, from `/proc` or
  `lsof`; the project directory is the cwd with every non-alphanumeric
  character turned into `-`) that no other evidence accounts for
  (`possibly_live: "unreadable_marker"`). Such a record is `live` (or
  `idle` when its last turn ended), `input: "none"`, `resumable: false`,
  and `sessions.send` refuses it with the reason. Activity detection: Linux `/proc`; macOS `ps` plus
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
- **links**: the worker only knows what its Rook terminal was opened with:
  `work_session` (the hub session it was launched for), `task` and
  `console_room` (§3.7); the hub adds the rest (and its own `task` link wins).
- Extra fields that may appear: `activity` (`working`/`ready`/`pending`),
  `pid` (live only), `model`, `local` (true: a Rook terminal the session
  shim runs in someone's own terminal, §4 G; it is sized by that terminal).

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
| link | attach to a task | env var at launch only | built: the hub's `link` route (§3.6) and automatic on launch-for-task (§3.7) |

### 3.3 Streaming a session Rook did not start

Rook cannot read the raw bytes of a terminal it does not own, and Bake does
not want a wrapper. So there are three tiers, and the page always shows the
best one available:

1. **Terminal** (raw bytes, full control). Sessions started or resumed by
   Rook. Linux/macOS (PTY) and Windows 10 1809+ (ConPTY, workstream D).
   Where the session shim is installed (§4 G, opt-in per host), also every
   interactive `claude` or `codex` started the ordinary way in a terminal
   there (Linux/macOS).
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
  Speed: the history caps cache each transcript's metadata and activity by
  file version (inode, size, mtime) and read an append-only Claude log on
  from where the last scan stopped (`claude_history.scan_session`), so a
  list reads only what changed; the Claude and Codex pulls run in parallel,
  and the plugin reads the newest 50 of each once at start. (Before this, every
  list re-read each of the newest 50 transcripts per agent whole, twice,
  and a host with about 340 long sessions took longer than the hub's 15 s.)
- `sessions.mirror(agent, native_id, cursor, wait, max_events)` (above; its own plugin, `session_mirror`).
- `sessions.shim.install(agents?, shells?)`, `sessions.shim.uninstall()`,
  `sessions.shim.status()` (§4 G; their own plugin, `session_shim`, Linux
  and macOS, no heartbeat).
- `sessions.follow(agent, native_id, offset=0, version="", tail=0)` → the
  `claude-history.follow` / `codex-history.follow` reply (`unchanged`, or
  `messages`, `version`, `replace_from`, …) plus `agent`, `native_id`.
  `tail=N` (1-200) starts at the last N messages instead of `offset`: one
  page of up to 64,000 characters with each message clipped to 4,000
  (`clipped` = characters left out), and `tail_from` = its first index;
  follow on with `offset`/`version` as usual. Messages may carry `kind:
  "tool_result"` (a user record that only returns tool output; `error` when
  one failed). Workers before `tail` refuse the argument ("bad args"); the
  hub then asks again without it. Shells and Hermes have no transcript
  (error). Risk read, sensitive.
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
  (claude only), `work.stream.open(handoff_pid=…)` (§3.3, Take over),
  `work.stream.open(resume=…)` refusing a session that may still run (its
  transcript changed in the last 120 s, or a `claude` with an unreadable
  marker probably holds it, §3.1) unless `force=true` (the handoff path is
  unchanged, and `force` never overrides process evidence), a
  `session` field in terminal info, and ConPTY on Windows (workstream D:
  the same caps, so resume and `sessions.send` keys work there too). `*-history.resume`
  delegates to `work.stream.open(harness=agent, resume=id, cwd=…)` where
  the worker has `work.stream.open`, and returns `terminal` (the terminal
  id) instead of `handle`; `*-history.resumed` entries carry `terminal` or
  `handle`. Workers without `work.stream` keep the `proc.*` path.
  Workstream F adds `work.stream.open(argv=[…] | cmd="…", env={…},
  task="<id or slug>", room="<console room id>")`: `argv`/`cmd` run a
  command instead of the login shell (harness `shell` only, no `resume`;
  `PAGER`/`GIT_PAGER` set to `cat`), and `task`/`room` are recorded on the
  terminal (`task`, `room`, `cmd` in terminal info; `ROOK_TASK` in its
  environment). Workers with these arguments announce `hb.work.commands = 1`;
  the hub sends them to no other worker.
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
  | `cached` | off | `1`: answer at once from the hub's last catalog of each worker, asking none |

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
      "stale": false,              // true: the last read of this worker failed (served from the cache)
      "fetched": 1791400000.0,     // when that catalog was read (null if never)
      "cached": false,             // true in a cached=1 reply
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
  `stale`, when that worker fails. The Sessions page first asks with
  `cached=1` (at once, no worker asked; a worker never read yet has no rows
  and `fetched: null`), renders that, then asks each worker with `worker=`
  in parallel and replaces that worker's rows as it answers, so one slow
  worker never holds up the page.
- Heartbeat summary `hb.sessions` = `{live, idle}` counts (§3.5), shown as
  `workers[].counts`.
- **Per-session routes** (built, workstream C): `POST
  /account/work/session/<op>` in `rook/remote/work_web.py`. Operator auth
  (401/403), the dashboard `Origin` (403 otherwise) and the CSRF token in the
  JSON body (403), like the terminal socket; `Cache-Control: no-store`. Every
  request names the session as `worker` (worker id), `agent` and `native_id`.
  Replies are `{ok: true, …}` or `{ok: false, error}` with 400 (bad request,
  unknown host, a cap the worker lacks), 404 (unknown op), 429 (too many live
  views) or 502 (the worker failed or refused; its message). Tier 1 needs no
  new socket: a record with `view.terminal` opens through the existing
  `/account/work/term/<work_session>` socket (`attach` makes the Work session
  when there is none). Tiers 2 and 3 are JSON long-polls rather than a socket,
  so a lost reply costs one request and the cursor stays with the browser.

  | `op` | Body (besides `csrf`, `worker`, `agent`, `native_id`) | Reply |
  |---|---|---|
  | `mirror` | `cursor` (0), `wait` (0-20 s), `max_events` (≤500) | `{events, cursor, done, exists}` from `sessions.mirror`, masked. At most 32 waiting calls hub-wide (429 beyond) |
  | `follow` | `offset`, `version`, `tail?` (0-200) | `sessions.follow` page (`unchanged`, `version`, `replace_from`, `messages`, `truncated`, `next_offset`, `next_content_offset`, `total_messages`, `activity`, `active`, `tail_from`), masked; `tail` retried without it for workers that refuse it; older workers through `<agent>-history.follow` (no `tail`). claude/codex only |
  | `send` | `text` (1-24,000), `command_id?` | `{delivery: turn/held/keys, note, detail?, terminal?, native_id?}` from `sessions.send`; older workers through `<agent>-history.send` (`delivery: turn`) |
  | `stop` | | `{stopped, terminal?, handle?, exit_code?, note?}` from `sessions.stop`; a hub Work session on that terminal is marked ended and its MCP token revoked |
  | `resume` | `id` (command id, 8-100 chars), `cwd?`, `title?`, `mcp?`, `cols?`, `rows?` | `{session, terminal, title}`: a Work session (the same id history discovery uses for that conversation) resumed with `work.stream.open(resume=native_id)`. claude/codex, hosts with `work.stream.*` only; idempotent per `id`, and a session already running returns its terminal |
  | `new` | `id`, `harness`, `cwd`, `model?`, `title?`, `persona?`, `task?`, `mcp?`, `cols?`, `rows?` (no `agent`/`native_id`) | `{session, terminal, title}`: the Work socket's `launch` (same validation, token minting and Work session), awaited. `harness` must be one the host reports; `cwd` absolute (POSIX or `X:\`) |
  | `attach` | `terminal` | `{session}`: a Work session for a running Rook terminal the hub had none for (checked with `work.stream.list`) |
  | `link` | `task` (`t_…` or slug; `""` unlinks), `work_session?` | `{links: {task}, note?}`. Stored on the hub by catalog key (and on the Work session when given); the merged list returns it as `links.task`. Also added to the task's links (kind `session`, relation `touched`; `note` says why not when that fails). Linking does not claim |
- The classic Work view's non-PTY **Resume on host** accepts a `terminal`
  result from `*-history.resume` and attaches the session's terminal.
- MCP: no new tools. Agents use `rook_call` on these caps; `rook_console_*`
  keeps its names, arguments and replies (one optional argument, `task_id`,
  and an extra reply field, `terminal`; §3.7).

### 3.7 Console rooms and task links (workstream F)

**Console rooms on Rook terminals.** `rook_console_open` starts its process
with `work.stream.open(harness="shell", argv|cmd, cwd, env, title=<task
title>, room=<room id>, task=<linked task>)` when the worker has
`work.stream.open/read/write` and `hb.work.commands >= 1`; the process is then
a Rook terminal, streamable on the Sessions page, listed by `sessions.list`
(agent `shell`, native id the terminal id) with `links.console_room` and
`links.task`. It is always a tty there, whatever `pty` says. Otherwise (older
workers, or `too many live terminals`) the room runs on `proc.*` as before.
Each room records its `transport` (`term` or `proc`; rooms in an older
`console.db` are `proc`), and the tools route by it:

| Tool | `proc` | `term` |
|---|---|---|
| open | `proc.start` → `handle` | `work.stream.open` → `handle` = terminal id, reply also has `terminal` |
| pump | `proc.read(handle, cursor)` → `chunk`, `next_cursor` | `work.stream.read(id, cursor)` → decoded bytes (UTF-8, incremental), `next`; "no such terminal" closes the room |
| write | `proc.write(data, newline)` | `work.stream.write(data + "\r" if newline)`; reply gains `handle` |
| signal | `proc.signal(sig)` | `work.stream.signal(sig)` |
| close kill | `proc.close` | `work.stream.close` |

The archive is unchanged: sanitized text, secret masking, freeze on exit,
FTS. In a terminal the process's own echo of typed input also lands in the
room (beside the `$ text` row the hub writes), masked like any output.
`proc.*` stays for non-terminal jobs and as this fallback.

**Launch for a task.** A session started for a task claims it for the
session's identity and links the session; nothing closes the task.

| Started by | Claimed as | Link on the task |
|---|---|---|
| Sessions page **New session** with `task` | the signed-in operator (`human:<account id>`), through the knowledge service's account API; `provider_session` = Work session id | kind `session`, ref `<worker_id>/<harness>/<terminal id>` |
| `rook_console_open(task_id=…)` | the calling agent (fails the call if the task cannot be claimed) | kind `console`, ref the room id |
| `rook_call("work.stream.open", args={…, "task": …})` | the calling agent (a failure is `_task_error` on the reply; the terminal still runs) | kind `session`, ref as above |

Without `task_id`, a console room still links to the caller's claimed task
(as before) and passes it to the terminal as `task`.

**Ending.** When a linked session ends on its own, the task gets a `note`
event ("… ended: …") and its claimants a `session_ended` hygiene finding
asking for a handoff (docs/design/hygiene.md); task state and claims are
untouched. Who notices:

- console rooms: the pump, when the process exits or the worker loses it
  (`HygieneEngine.on_linked_session_end("console", room, …)`);
- agent terminals: the pump watches them (`term_watch` in `console.db`, one
  `work.stream.list` per worker every 30 s; dropped after 7 days if the worker
  never answers);
- Sessions-page terminals: the hub marks the Work session
  `task_end_pending` when the terminal ends, and posts the note (a task
  `note` with `data.session_end` = the link ref, which raises the finding)
  with the operator's next dashboard request (at most every 30 s; at once on
  **Stop**). A closed session linked to a task shows the same reminder on
  the page.

## 4. Workstreams

Each is one PR to `beta` unless it says otherwise. Shared shapes are §3.

**A. Catalog and verbs (worker + hub).** *Done.* `sessions.list/follow/send/stop`,
the record of §3.1 (state, origin, view, input, inbox policy, links),
activity detection on Windows and macOS (process list, Claude PID markers),
resume through `work.stream.open` everywhere, hub cache and merged list API
for the page. Tests: unit, plus the existing `test_work_terminals.py` and
`test_work_sessions.py` stay green.

**B. Mirror (Claude Code mod + worker).** *Done.* Mod hooks write the spool of
§3.4; worker `sessions.mirror`; mod `/rook-move`; the mod's Sessions tab
reads `sessions.list` (falls back to `claude-history.pull` on older
workers). Mod version bump. Tests: mod tests (`claude plugin test`), worker
unit tests for tailing, rotation and cleanup.

**C. Sessions page.** *Done:* `rook/web/sessions.js`, the routes of §3.6,
docs in `docs/web/sessions.md`; the mod's Sessions tab sends through
`sessions.send` (mod 0.3.2). One list of every session across workers, grouped by
host and project (live first, then idle, then closed), with: **New session**
(harness, host, folder, model, persona, optional task), **Open** (terminal,
or live view rendering mirror/transcript events as ANSI in one read-only
xterm.js per open session, styled like the agent's TUI:
`rook/web/session_screen.js`), **Send** box (shows "waiting
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

**E. Home agent chat.** *Done.* A chat panel on Manage > Home agent: talks to the
home agent in a two-person room through the existing chat store (so the
conversation also shows in Chat), streaming the reply if the plugin can,
with the room's history. Tests: unit and a browser test.

**F. Consistency pass (after A-C).** Console rooms run on `work.stream`
instead of `proc.read` pumping (`proc.*` stays for non-terminal jobs);
launch-for-task claims the task and ending a session offers a handoff;
vocabulary: the dashboard tab, the mod tab and the docs all say
**Sessions** for sessions and **Work** for tasks. *Done:* §3.7 (console
rooms on Rook terminals with the `proc.*` fallback, launch-for-task claims
and end notes from the page, `rook_console_open(task_id=)` and
`work.stream.open(task=)`), the `session` link kind, and the vocabulary
pass (mod 0.3.3: the Deck tab's help and headings say tasks and Work).
Tests: `tests/test_sessions_consistency.py`, `tests/test_sessions_page.py`.

**G. Session shim: tier 1 for sessions started the ordinary way.** Bake: "I
want them to run in xterm always", with Claude Code and Codex working
normally on any machine and no special startup. Rook cannot attach to a
running process (`ptrace_scope=1`), so the program has to start inside a
terminal Rook can read. A `claude`/`codex` shim on PATH does that without
anyone typing anything different. *Built* (Linux and macOS):

- **Who owns the program.** The shim does, not the worker. It runs the real
  binary under a PTY it opens itself (like `script(1)`), relays the
  person's terminal to it, and streams the output to the worker, which
  registers it as a **local terminal**: an ordinary entry in the terminals
  plugin, so `work.stream.read/write/signal/close/list`, `sessions.list`,
  `sessions.send` (keys) and `sessions.stop` all work on it unchanged, and
  the Sessions page opens it in the live xterm with input. A worker-owned
  PTY was the first idea; it was rejected because a worker restart (every
  OTA update, and systemd stopping the unit's whole cgroup) would kill every
  session typed into a terminal, and because the person's keystrokes would
  then cross the worker's event loop. With the shim owning the PTY a worker
  restart costs nothing: the shim keeps relaying, reconnects every 3 s, and
  registers again (a new terminal id, with its last 256 KiB of output
  replayed into the new ring). Local keystrokes never touch the worker.
- **Files** (`rook/worker/shim.py`, all under `<worker state>/shim/`):
  `bin/claude`, `bin/codex` (POSIX `sh`, one per agent found on the host, or
  named), `bin/.rook-shim-dir` (marker), `client.py` (the relay,
  `rook/worker/shim_client.py`, stdlib only, copied out of the worker bundle
  at install and refreshed at every worker start), `env.sh` / `env.fish`
  (put `bin/` first on PATH), `installed.json` (agents, the rc files
  touched, the Python used), `run/worker.sock`.
- **Install** (`sessions.shim.install(agents?, shells?)`, risk write,
  opt-in, **off by default**; nothing installs it implicitly, so a work
  laptop never gets it unless someone runs the cap there). Shells: those with
  a config on the host plus the login shell, or `shells=[bash|zsh|fish]`.
  bash and zsh get one marked block appended to `~/.bashrc` (plus
  `~/.bash_profile` on macOS when it exists) or `${ZDOTDIR:-~}/.zshrc`:
  `# >>> rook session shim >>>` … `[ -r …/env.sh ] && . …/env.sh` … `# <<<
  rook session shim <<<`. At the end of the file, so it comes after the
  file's own PATH edits; re-installing replaces it; a dotfile manager's
  symlink is followed, not replaced. fish gets its own file,
  `~/.config/fish/conf.d/rook-shim.fish`, which also re-asserts the order at
  the first prompt (conf.d runs before `config.fish`). Agents: those found
  on the worker's PATH or the usual per-user folders (`~/.local/bin`, npm,
  bun, volta, Homebrew); a shim for a program that is not installed would
  make `command -v` lie, so it is only written when found or named.
- **Uninstall** (`sessions.shim.uninstall`): removes exactly the marked
  blocks (and the blank line install put before one), the fish file and the
  folder; the rc files are byte-for-byte what they were (a file install
  found without a final newline keeps the one it gained). Running sessions
  keep running. Open shells keep their PATH until restarted (`hash -r` /
  `rehash`). `sessions.shim.status` reports agents, shims, real binaries,
  rc blocks present, whether the socket listens and how many local
  terminals run.
- **Fall-through rules.** The script finds the real binary by walking PATH
  and skipping its own folder and any folder with a `.rook-shim-dir` marker
  (so it can never run itself; the worker's own `_binary`/`_claude_bin` use
  the same rule, `shim.which_real`). It `exec`s the real binary unchanged,
  without starting Python, when: `ROOK_SHIM` is `0`/`off`/`no`/`false`;
  `ROOK_WORK_TERMINAL` is set (already in a Rook terminal); stdin, stdout or
  stderr is not a terminal (pipes, redirects, scripts, agents' tool calls);
  the socket does not exist (no worker, or uninstalled); or the worker's
  Python or the client is missing. The client then also falls through for
  non-interactive invocations (claude: `-p`/`--print`, `--output-format`,
  `--input-format`, `--bg`, `-v`, `-h` and the subcommands `auth`, `doctor`,
  `install`, `mcp`, `plugin(s)`, `update`/`upgrade`, `setup-token`, `logs`,
  `rm`, `stop`/`kill`, …; codex: `-V`, `-h` and `exec`/`e`, `review`,
  `login`, `logout`, `mcp`, `app-server`, `completion`, `apply`, …), when
  the connect takes over 80 ms or the answer over 300 ms, when the worker
  refuses, or when the PTY cannot be set up: in every case before the
  person's terminal is touched. Sessions with no real binary print
  `<name>: command not found` and exit 127. A shim not at the front of PATH
  is simply never run.
- **Local link.** An `AF_UNIX` stream socket at `<state>/shim/run/
  worker.sock`, folder 0700, socket 0600, listening only while the shim is
  installed; each connection's peer uid must be the worker's (`SO_PEERCRED`
  on Linux, `LOCAL_PEERCRED` on macOS). No network exposure. Frames are 1
  byte kind + 4 bytes length + payload: `J` JSON control, `O` output (shim
  to worker), `I` input (worker to shim), 1 MiB at most. Shim to worker:
  `hello {v, agent, argv, cwd, cols, rows, shim_pid, term, pid?, reattach?}`,
  `started {pid}`, `size {cols, rows}`, `exit {code, signal}`. Worker to
  shim: `welcome {ok, id | error}`, `signal {sig}`, `hangup`, `kill`. The
  shim drops the link (and reconnects later) if more than 2 MiB of output
  waits for the worker, so a stuck worker never slows the person's terminal.
- **Terminal behaviour.** The program gets the person's termios and window
  size, their whole environment plus `ROOK_WORK_TERMINAL=<terminal id>`
  (so `/rook-move` knows it is already a Rook terminal, and a nested `claude`
  falls through), SIGPIPE/SIGXFSZ back at their defaults, and argv[0] the
  real binary's path (as `exec` of a resolved path gives; a shell would pass
  the bare name). The person's terminal is raw while it runs and restored
  after. SIGWINCH resizes the PTY; the person's window owns the size, so
  `work.stream.resize` on a local terminal answers its size with `fixed:
  true`, the hub drops viewers' resizes and tells viewers `fixed`, and
  `TermView` renders that grid for every viewer, the holder too. Ctrl-C and
  friends are bytes to the program as before. Ctrl-Z: when the program stops
  itself, the shim restores the terminal and stops too, so the shell's job
  control works; on `fg` it re-enters raw mode and continues the program.
  A closed window (SIGHUP, or EOF/EIO on the terminal) hangs up the program,
  as before. Exit: the shim exits with the program's status, and when a
  signal (INT, TERM, HUP, KILL, PIPE, ALRM, USR1/2) killed it, the shim
  kills itself with the same signal. From the page, `work.stream.signal`
  signals the PTY's foreground process group, and `work.stream.close` /
  `sessions.stop` send `hangup` then `kill` (1.5 s, 3 s); the person's
  terminal then says "[rook] This session was stopped from the Sessions
  page." A worker stopping does **not** end local terminals.
- **Catalog.** A local terminal has the agent as its harness, the program's
  pid (so `agent_activity.session_under` links it to the Claude or Codex
  session id once the agent reports one, as for any Rook terminal), `resume`
  from `--resume`/`-r` (claude) or `resume <id>` (codex), and `local: true`
  in `work.stream.list` and in its §3.1 record (`origin: "rook"`, `view.
  terminal` set, `input: "pty"` unless the inbox is preferred). Local
  terminals do not count toward the 8 live Rook terminals; at most 32 run at
  once (the 33rd falls through).
- **Latency.** Falling through costs one `sh` start and a PATH walk with
  shell built-ins (about 1 ms). Attaching starts the worker's Python with
  `-I -S` (about 30 ms) and one local round trip; after that the relay is a
  `select` loop in the shim, and keystrokes go straight to the PTY.
- **Windows** (follow-up, not built): the same split works with a `.cmd`
  shim earlier on PATH than npm's, a named pipe with an owner-only DACL
  (`\\.\pipe\rook-shim-<sid>`) instead of the socket, and the shim running
  the program on a ConPTY of its own (`rook/worker/conpty.py` already has
  spawn, read, write, resize and the kill-on-close job). The relay loop needs
  threads instead of `select`, and console raw mode through
  `SetConsoleMode` (`ENABLE_VIRTUAL_TERMINAL_INPUT`). PowerShell profiles
  would get the marked block. Until then the plugin does not load on
  Windows and nothing changes there.
- Tests: `tests/test_session_shim.py` (scratch `HOME` and
  `ROOK_WORKER_HOME`): which invocations attach, install/uninstall
  round-trip of bash/zsh (symlinked)/fish config, PATH order, `which_real`,
  the script's fall-through without a terminal and with no real binary, peer
  uid, and end to end under a PTY with a fake `claude`: passthrough both
  ways (person and page), resize (SIGWINCH reaches the program, the worker
  learns the size, viewers' resizes are refused), exit code, signal and stop
  from the page, fall-through with no worker, with a stale socket, with
  `ROOK_SHIM=0`, on refusal and for another uid, and a session surviving a
  worker restart.

Order: A first (contract), B, C, D and E in parallel against §3, then F, then G.

## 5. Rules for every PR

- Base branch `beta`. Never commit `android/rook.properties`, the private
  wake model, secrets, real host names, user names or home paths.
- Tests never run with the real `HOME`: use a scratch `HOME` and
  `ROOK_UPDATE_KEY` pointing at a scratch path.
- No deploys, no worker updates, no changes to live hosts from a
  workstream; promotion from `beta` is a separate, reviewed step.
- Update `docs/` with the behavior (this file for contracts, user docs for
  what people see).
