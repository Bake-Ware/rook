# Claude Code rules

**Settings > Claude Code** lets you decide, in one place, what happens when
Claude Code does something on any of your machines. Each rule reads like a
sentence: *when Claude is about to use Bash, anywhere, block it and post in
the "Claude Code" room*.

Rook puts the rules into each machine's Claude Code settings when you push
them. Your own settings and hooks stay exactly as they were; Rook only adds
its own entries next to them, keeps a backup of the file each time, and can
take its entries out again.

## Rules

The **Rules** tab lists every rule with its sentence, its risk and what it
does. The switch beside a rule turns it on or off; push afterwards to apply
the change. With no rules yet, the page offers three examples to start from:

* tell me when a session has been waiting on me for 10 minutes;
* never force-delete folders (`rm -rf`);
* remind Claude to update its Rook task before it stops.

A rule has three parts:

**When.** Something in Claude Code (a session starts, a prompt is sent,
Claude is about to use a tool or has used one, a notification, Claude
finishes its turn, a session ends, ...); a session waiting on you for some
minutes (the worker notices this); or a worker going offline or coming back
(the hub notices this). Tool events can be narrowed to a tool (`Bash`,
`Edit|Write`) and to text in the command ("only if it mentions `rm -rf`");
prompts and notifications to text in them.

**Where.** Everywhere, one band, one worker, one project (a folder name
such as `myapp`, matched anywhere in the session's folder path, or a full
path such as `/srv/myapp`) or one agent (the `ROOK_AGENT` name in the
session's environment).

**Do.** One or more of:

| Action | What happens |
|---|---|
| Post in a chat room or huddle | A message in a room (an existing one, or a new one Rook creates with you in it) or a project's huddle. |
| Notify me | A Telegram or Discord message, through the hub's notifications. |
| Run a capability | Calls a capability on the worker where it happened (or another). Only capabilities an administrator allowed under Settings > Claude Code; ones that run code need a second switch. Its arguments are fixed when you save: nothing from the session is put in them. |
| Give Claude extra instructions | Added to what Claude sees. On "Claude finishes its turn", Claude is asked to carry on once with them. |
| Block it, with a message | Stops the tool call (before it runs) or the prompt; Claude sees your message. |
| Set the status line | Text under the prompt, from this event on (not shown if you already have your own status line). |

Messages can use `{worker}`, `{project}`, `{tool}`, `{event}`, `{prompt}`,
`{message}` and `{minutes}`. The editor shows the rule as a sentence while
you write it, and says what is missing.

Posting, notifying and capabilities run on the hub; blocking, instructions
and the status line happen inside Claude Code on the machine, so they work
even when the hub is out of reach.

## Workers

The **Workers** tab shows every online worker: whether it can take rules,
when they were last pushed and whether that is still up to date.

* **Preview** shows exactly what a push would change in each Claude Code
  settings file on that worker, as a diff, before anything is written.
* **Push** sends the rules to that worker; **Push to all workers** sends
  them everywhere.
* **Remove** takes Rook's entries out of that worker's Claude Code settings
  (after a preview).

Workers that cannot take rules (an older Rook build, or no Claude Code on
them) are left alone and say so.

Only hub administrators can write, delete, preview and push rules: a rule's
instructions reach Claude inside people's sessions, and its posts can carry
what they type.

**Uninstalling.** Before you delete a worker's folder
(`~/.rook-band-worker`) or uninstall Rook from a machine, press **Remove**
for that worker (or `ccmod.push` with `remove: true`): it takes Rook's
entries out of its Claude Code settings and restores each file as it was
when nobody has changed it since. If you deleted the folder first, nothing
breaks: Rook's hook entries are written so that a missing script is simply
skipped, and you can take them out of `~/.claude/settings.json` by hand
(they all mention `rook-ccmod-hook`).

## Activity

What the rules did on the hub: each post, notification and capability run,
with the worker, the event and any error. Runs past the per-rule limit
(Settings > Claude Code: actions per minute per rule, 6 by default) are
listed as skipped.

## Settings

Settings > Claude Code (administrators):

| Setting | Default | |
|---|---|---|
| Capabilities rules may run | none | names or `prefix.*` |
| Allow rules to run code | off | for capabilities such as `shell.exec` |
| Actions per minute per rule | 6 | per worker |
| Listen for events | on | off: only blocking, instructions and the status line work |

On a worker, `ROOK_CCMOD_CLAUDE_DIRS` names extra Claude Code config folders
(besides `~/.claude` or `CLAUDE_CONFIG_DIR`). How it works:
[docs/design/ccmod.md](../design/ccmod.md).
