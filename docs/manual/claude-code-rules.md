---
id: claude-code-rules
title: Claude Code rules
order: 9
features:
  - features/claude-code-rules
  - plugin/hub/ccmod
  - plugin/worker/ccmod
  - settings/ccmod
since: 0.1.0
deprecated: false
---
# Claude Code rules

**Settings > Claude Code** lets you decide, in one place, what happens when
Claude Code does something on any of your machines. Each rule reads like a
sentence: *when Claude is about to use Bash, anywhere, block it and post in
the "Claude Code" room*.

When you push the rules, Rook adds them to the Claude Code settings on each
machine. Your own settings and hooks stay as they were: Rook adds its own
entries beside them, keeps a backup of the file each time, and can take its
entries out again.

Only hub administrators can write, delete, preview and push rules. A rule's
instructions reach Claude inside people's sessions, and its posts can carry
what they type.

## Rules

The **Rules** tab lists every rule with its sentence, its risk and what it
does. The switch beside a rule turns it on or off; push afterwards to apply
the change. With no rules yet, the page offers three examples to start from:

- tell me when a session has been waiting on me for 10 minutes;
- never force-delete folders (`rm -rf`);
- remind Claude to update its Rook task before it stops.

A rule has three parts.

**When.** Something in Claude Code: a session starts, a prompt is sent,
Claude is about to use a tool or has used one, a notification, Claude
finishes its turn, a subagent finishes, the conversation is about to be
compacted, a session ends. Or a session that has been waiting on you for
some minutes (the worker notices this), or a worker going offline or coming
back (the hub notices this). Tool events can be narrowed to a tool (`Bash`,
`Edit|Write`) and to text in what the tool is given ("only if it mentions
`rm -rf`"); prompts and notifications to text in them.

**Where.** Everywhere, one band, one worker, one project or one agent. A
project is a folder name such as `myapp`, matched anywhere in the session's
folder path, or a full path such as `/srv/myapp` or `~/src/app` on that
machine. An agent is the `ROOK_AGENT` name in the session's environment.

**Do.** One or more of these:

- **Post in a chat room or huddle**: a message in an existing room, in a new
  room Rook creates with you in it, or in a project's huddle ([[manual-chat]],
  [[manual-work]]).
- **Notify me**: a Telegram or Discord message through the hub's
  notifications ([[manual-integrations]]).
- **Run a capability** on the machine where it happened, or on another one.
  Only capabilities an administrator allowed under Settings > Claude Code;
  ones that run code need a second switch. The capability's arguments are
  fixed when you save the rule: nothing from the session goes into them.
- **Give Claude extra instructions**: added to what Claude sees. On "Claude
  finishes its turn", Claude is asked to carry on once with them.
- **Block it, with a message**: stops the tool call before it runs, or the
  prompt; Claude sees your message.
- **Set the status line**: text under the prompt from this event on. It is
  not shown if you already have your own status line.

Messages can use `{worker}`, `{band}`, `{project}`, `{cwd}`, `{tool}`,
`{event}`, `{session}`, `{agent}`, `{prompt}`, `{message}` and `{minutes}`.
The editor shows the rule as a sentence while you write it, and says what is
missing.

Posting, notifying and capabilities run on the hub. Blocking, instructions
and the status line happen inside Claude Code on the machine, so they work
even when the hub is out of reach.

## Workers

The **Workers** tab shows every online worker: whether it can take rules,
when they were last pushed, by whom, and whether that is still up to date.

- **Preview** (in the worker's ⋯ menu) shows exactly what a push would change in each Claude Code
  settings file on that worker, as a diff, before anything is written.
- **Push** sends the rules to that worker; **Push all** sends them
  everywhere.
- **Remove** (in the worker's ⋯ menu) takes Rook's entries out of that worker's Claude Code settings,
  after a preview. A settings file nobody changed since Rook last wrote it
  comes back exactly as it was before Rook went in.

Workers that cannot take rules (an older Rook build, a phone, or no Claude
Code on them) are left alone and say so. A settings file that is not valid
JSON is never rewritten: the push lists it as an error for you to fix by
hand. So is a file a worker running as root would reach through a link to
another user's file.

## Activity

What the rules did on the hub: each post, notification and capability run,
with the worker, the event and any error. Runs past the per-rule limit are
listed as skipped. Each event runs its rules once: restarting the hub does
not run them again.

## Settings

Settings > Claude Code (administrators):

- **Capabilities rules may run** (`ccmod.cap_allowlist`): capability names
  or `prefix.*`. Empty by default, so rules cannot run capabilities.
- **Allow rules to run code** (`ccmod.allow_exec`): off by default; needed
  for capabilities such as `shell.exec`.
- **Actions per minute per rule** (`ccmod.rate_per_minute`): 6 by default,
  per worker.
- **Listen for events** (`ccmod.listen`): on by default. Off: only blocking,
  instructions and the status line work.

On a worker, `ROOK_CCMOD_CLAUDE_DIRS` names extra Claude Code config folders
besides `~/.claude` (or `CLAUDE_CONFIG_DIR`).

## Uninstalling

Before you delete a worker's folder (`~/.rook-band-worker`) or uninstall
Rook from a machine, press **Remove** for that worker. If you deleted the
folder first, nothing breaks: Rook's hook entries are written so that a
missing script is simply skipped. You can then take them out of
`~/.claude/settings.json` by hand; they all mention `rook-ccmod-hook`.

For agents: the same rules are the `ccmod.*` capabilities on worker `rook`
(`ccmod.list`, `ccmod.status`, `ccmod.set`, `ccmod.push`, ...); see
[[manual-agents-and-mcp]].
