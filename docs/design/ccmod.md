# Claude Code rules: triggers and actions set on the hub

Implemented as the hub plugin `ccmod` (`rook/hub/plugins/ccmod/`), the worker
plugin `ccmod` (`rook/worker/plugins/ccmod.py`), the hook command
`rook/worker/ccmod_hook.py` and the settings merge
`rook/worker/ccmod_settings.py`. Dashboard: **Manage > Claude Code**
(`rook/web/ccmod.js`, API `/ccmod/account-api`). User guide:
[docs/web/claude-code-rules.md](../web/claude-code-rules.md).

The operator writes rules once, on the hub: "when *this* happens in a Claude
Code session *there*, do *that*". Rook puts them into effect in every Claude
Code installation on the workers it pushes to, next to the person's own
settings, and runs the parts that need the band on the hub.

## 1. Rules

| Field | Notes |
|---|---|
| `id` | slug from the name when absent; unique |
| `name` | up to 80 characters |
| `enabled` | off rules stay on the hub and are not pushed |
| `trigger.event` | a Claude Code hook event, `Idle`, or a Rook event (below) |
| `trigger.matcher` | `PreToolUse`/`PostToolUse`: tool name pattern (`Bash`, `Edit\|Write`, `mcp__.*`) |
| `trigger.contains` | tool events: text in the tool input; `UserPromptSubmit`: in the prompt; `Notification`: in the message (case-insensitive) |
| `trigger.minutes` | `Idle`: 1 to 1440 |
| `scope` | `all`, `band` (band label), `worker` (name or id), `project` (a folder name anywhere in the session's path, or an absolute path prefix), `agent` (the session's `ROOK_AGENT`) |
| `actions` | 1 to 8, below |

**Events.** Claude Code: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `Notification`, `Stop`, `SubagentStop`, `PreCompact`,
`SessionEnd`. Worker: `Idle` (Claude finished its turn and nobody has typed
for N minutes; once per idle stretch). Rook: `rook.worker_offline`,
`rook.worker_online` (the hub sees a worker leave or come back; scoped to a
band or worker only).

**Actions.**

| Action | Runs | Events | Tier |
|---|---|---|---|
| `post` (room id, a new room's name, or a project huddle; message template) | hub | all | write |
| `notify` (message template; `notify.send`) | hub | all | write |
| `cap` (capability, worker `same` or a name, fixed JSON args) | hub | all | the capability's own, at least write |
| `inject` (instructions) | hook | SessionStart, UserPromptSubmit, PostToolUse, Stop, SubagentStop | write |
| `block` (message) | hook | PreToolUse, UserPromptSubmit | write |
| `statusline` (text) | hook | Claude Code events | write |

Templates take `{worker}`, `{band}`, `{event}`, `{tool}`, `{project}`,
`{cwd}`, `{session}`, `{agent}`, `{prompt}`, `{message}`, `{minutes}` (each
clipped to 300 characters). `inject` on `Stop` answers with
`decision: block`, which asks Claude to carry on once with the text;
Claude Code marks the follow-up stop `stop_hook_active` and the hook lets it
stop then.

## 2. Policy

* Every rule edit is attributed (`ccmod.history`), and so is every push
  (the push record keeps who pushed). `ccmod.set`, `ccmod.delete`,
  `ccmod.preview` and `ccmod.push` are `admin` and need a hub administrator
  (`require_hub_admin`, which holds in audit mode too): a rule's text reaches
  Claude inside people's sessions (`inject` is as good as running code there,
  since Claude runs Bash), the hub runs a rule's posts from its *current*
  rules with no push (so a post template with `{prompt}` would carry
  people's prompts to any room), a push writes people's settings files, and
  a preview shows parts of them (which can hold keys under `env`). The
  dashboard page is operator-only.
* A `cap` action's capability must be on `ccmod.cap_allowlist` (exact names
  or `prefix.*`; empty by default). Its tier is the highest any live worker
  declares for it, floored by the built-in table, and unknown capabilities
  count as `exec`. `exec`/`admin` capabilities also need `ccmod.allow_exec`
  (off by default). Both are checked when the rule is saved and again each
  time it runs.
* Nothing from a session reaches a capability: `args` are fixed when the rule
  is saved; templates fill only messages. The hook command itself is Rook's
  fixed script; a push cannot make it run anything else.
* Remote actions are decided on the hub. The worker reports an event and the
  rule ids its hook matched; the hub runs a rule only when the hook matched
  it *and* the hub's current version of the rule still matches what the hub
  can check itself: the event, the tool, the agent, a project given by name,
  the Idle minutes, the band and worker. Text (`contains`) and a project
  given as a path are the hook's call: the hook saw the whole prompt or tool
  input (the hub gets a 300-character excerpt, or none), and a path such as
  `~/src/app` or `C:\src\app` means the worker's home and the worker's kind
  of path. At most `ccmod.rate_per_minute` (default 6) runs per rule and
  worker a minute.
* Background work runs as the hub, never as whoever pushed: the pollers and
  the actions start in a clean context, and a `cap` action calls with the
  identity `system:ccmod:<rule id>`.
* The worker's `ccmod.apply`, `ccmod.remove` and `ccmod.events` are `admin`
  (a worker in `enforce-admin` mode wants a hub ticket for them). The events
  carry prompt and notification excerpts, and only the hub's own poller
  needs them.

## 3. Push and preview

`ccmod.push(worker?)` sends each online worker its config (`v: 1`, `rev`,
`sha`, the rules that apply after band and worker scoping, local actions
only plus a `forward` flag; where a rule posts or what it calls stays on the
hub). A worker without `ccmod.apply` in its announced caps (an older build,
or no Claude Code installation, Android) is skipped and listed as such; it
never receives the call, and a stray one would be an unknown cap. The hub
records each push (`rev`, `sha`, per-file action); a worker is *up to date*
while its pushed `sha` equals the current one.

`ccmod.preview(worker)` calls `ccmod.apply(config, dry_run=true)`: per
settings file the action (`create`, `update`, `remove`, `unchanged`,
`absent`, `error`), notes and a unified diff, plus the rules file's diff.

## 4. On the worker

Installations: `$CLAUDE_CONFIG_DIR` (else `~/.claude`) and the folders in
`ROOK_CCMOD_CLAUDE_DIRS` (path-separator list), those that exist. Files in
`<worker state>/ccmod/`: `rules.json` (what the hook reads),
`rook-ccmod-hook.py` (copied from the bundle; refreshed when the worker
updates), `managed.json` (per settings file, the containers Rook created and
the SHA-256 of what Rook last wrote), `originals/` (each settings file as it
was before Rook first went into it), `backups/` (the previous settings file
before each write, newest five per installation), `outbox/`, `status/`
(per-session status line text).

**The managed block** in `settings.json` is JSON, so its marker is in the
data: every hook Rook adds runs `rook-ccmod-hook.py`, in a group of its own
appended after the person's groups for that event:

```json
{"matcher": "Bash", "hooks": [{"type": "command", "timeout": 10,
  "command": "\"/usr/bin/python3\" -I -S \"/home/user/.rook-band-worker/ccmod/rook-ccmod-hook.py\" PreToolUse || exit 0"}]}
```

`|| exit 0` (the same in sh, bash and cmd.exe) keeps a missing script or
Python harmless. Python exits 2 when it cannot open its script, and Claude
Code reads exit status 2 as "block": without it, deleting the worker folder
would deny every hooked tool call, erase every prompt and keep a Stop hook
from ever letting Claude stop. The worker also never deletes the script
while any settings file may still run it: a removal that cannot change one
file (not valid JSON, say) keeps the script and lists the file under
`script_kept`; the rules file goes, and without it the hook does nothing. A file whose Rook
entries the person already took out by hand counts as done.

One entry per event; the matcher is the union of the rules' tool patterns
(none when a rule takes every tool). Idle rules add `SessionStart`,
`UserPromptSubmit`, `Stop` and `SessionEnd` so the worker can see activity.
A status line rule sets `statusLine` only when the file has none.

Rules the merge keeps (tests in `tests/test_ccmod.py`):

* nothing outside Rook's entries changes: other keys, their order, the
  person's hooks (also one they added to Rook's group), unknown fields;
* applying the same config again writes nothing; removing restores the file
  byte for byte when nobody changed it since Rook last wrote it (from
  `originals/`, so a hand-formatted file comes back as it was), and
  otherwise takes Rook's entries out and leaves the rest JSON-equal;
  containers the person wrote (an empty `"Stop": []`) stay; a settings file
  Rook created is deleted again;
* a file that is not valid JSON, or whose `hooks` is not an object, is
  reported and left alone; `disableAllHooks` is reported in the preview;
* indent (spaces or tab) and CRLF line endings are kept (while Rook's
  entries are in, the rest of the file is rewritten as JSON in that style);
  writes are atomic and keep the file's mode and owner (a new file gets its
  folder's owner), both set on the temp file's descriptor before the rename,
  so a root worker never chmods or chowns a path in someone's folder (where
  it could have been swapped for a symlink); a symlinked file is written
  through, except by a worker running as root when the link (the file or a
  folder above it) leads to a file owned by someone other than the owner of
  the link and its folder: that file is refused and reported, like one that
  is not valid JSON;
* the file is read again just before it is replaced, and planned again if
  Claude Code changed it meanwhile (a `/model` or `/permissions` change).

**The hook** (`ccmod_hook.py`, stdlib only, `python -I -S`) reads the event
from stdin and the rules file, applies `block`/`inject`/`statusline` and
prints Claude Code's reply. When a matched rule forwards, it hands the event
to the worker: a `{"op": "hook"}` frame on the session shim's owner-only
socket (`<state>/shim/run/worker.sock`, peer uid checked; the shim plugin
listens for it even when the shim itself is not installed), else a file in
`outbox/` the worker reads every second (Windows, or no socket). The
outbox holds 500 events at most (the hook drops more), and the worker
drops events older than 10 minutes unread, so a worker that was stopped
does not fire stale posts when it starts. The prompt and notification text
go along only for rules that forward. Hook input is read up to 8 MiB; past
that, the short fields are read from the head of the text and `contains`
looks in the raw text, so a huge Write still meets a "block Write
containing X" rule. It always exits 0, so a broken rules file or a missing
worker never affects the session.

**Events to the hub.** The worker queues forwarded events (500 at most,
numbered per worker boot) and serves them with `ccmod.events(after, wait,
boot)`, a long poll (25 s at most). The hub keeps one poll open per online
worker that holds forwarding rules (`ccmod.listen`, on by default). Where
it got to (boot and cursor) is kept per worker in the hub's store, so a hub
restart or a new poller after the worker flapped offline does not replay
the queue; it is recorded before the batch's actions run (an action is
lost rather than run twice if the hub stops halfway).

## 5. Compatibility

* No wire change: new caps only. Older workers are skipped by the push;
  older hubs never call `ccmod.*`.
* Hooks need a Claude Code with command hooks in `settings.json`; the Rook
  Claude Code mod (`integrations/claude-code`) is not needed and is
  untouched.
* `rook.web` gains a page; the dashboard proxies `/account/ccmod/api`
  (`ROOK_CCMOD_ADMIN_URL`).

## 6. Not done yet

* While Rook's entries are in, the settings file is JSON as Python writes
  it (compact arrays spread over lines, `\u` escapes written out); the
  original comes back on removal.
* No Windows socket: the hook uses the outbox (one-second delay).
* Agent identity is `ROOK_AGENT` in the session's environment; Rook-launched
  terminals do not set it yet.
* Hook reply formats follow Claude Code's documented hook output
  (`hookSpecificOutput.permissionDecision`, `additionalContext`,
  `decision: block`); a Claude Code that changes them needs a hook update.
