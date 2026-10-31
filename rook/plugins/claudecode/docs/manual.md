# Claude Code rules

Write a rule once on the hub; it takes effect in Claude Code on every worker
where you install the rules.

## A rule

`claudecode.create {name, event, actions, matcher?, contains?, scope?, target?}`

- **event**: a Claude Code hook event (`PreToolUse`, `UserPromptSubmit`,
  `Stop`, ...). **matcher** narrows tool events to tools (`Bash`,
  `Edit|Write`, `mcp__.*`); **contains** to text in the tool input, prompt or
  notification.
- **scope**: `all`, `project` (a folder name anywhere in the session's path,
  or an absolute path prefix, in **target**) or `agent` (the session's
  `ROOK_AGENT`).
- **actions** (up to 8): `block` (deny the tool call or the prompt, on
  PreToolUse and UserPromptSubmit), `inject` (add instructions; on Stop, ask
  Claude to carry on once), `statusline` (the status line text for that
  session). Texts take `{tool}`, `{project}`, `{cwd}`, `{session}`,
  `{agent}`, `{prompt}`, `{message}`, `{event}`, `{worker}`, `{band}`.

Writing rules is admin: their text reaches Claude inside people's sessions.
`claudecode.list`, `get`, `history`, `update`, `archive`, `delete` work like
any record.

## Put them into effect

`claudecode.install` on a worker (`dry_run: true` first shows each file's
diff). Rook adds its own hook entries to every Claude Code installation there
(`~/.claude/settings.json`, `$CLAUDE_CONFIG_DIR`, folders in
`ROOK_CCMOD_CLAUDE_DIRS`) next to yours, never replacing them, and its status
line only when you have none. Each write keeps a backup. Run install again
after changing rules. `claudecode.remove` takes Rook's part out; a file
nobody touched since comes back byte for byte. `claudecode.status` shows
which installations carry Rook's hooks.

## Changed from 0.4

Rules are records (`ccmod.set` is create/update). The hub doesn't push: you
install per worker, which is also how a rule reaches only some workers. Not
yet carried: remote actions (post to a room, notify, run a cap) and the Idle
trigger; they come back as jobs on Claude Code events once worker events
reach the hub.
