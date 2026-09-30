# Persona: one agent persona across every harness

Status: wave 3 of the beta refactor (`rook-beta-persona-plugin`). Implemented
as the hub plugin `persona` (`rook/hub/plugins/persona/`) and the worker
plugin `persona` (`rook/worker/plugins/persona.py`).

The goal: "if you're using Rook, always use this persona." An operator
describes the persona once, on the hub, and every agent that works through
Rook gets it, whatever harness it runs in: Claude Code, Codex, Hermes, the
voice assistant or any MCP client. The interface stays consistent without
copying text into each tool by hand.

Rook ships no persona text. The repository holds only neutral examples
(tests); an operator's persona lives in the hub's data directory.

## 1. Profiles

A profile is a named document:

| Field | Type | Limit | Rendered |
|---|---|---|---|
| `id` | slug `[a-z0-9][a-z0-9_-]*` | 48 | no (the key) |
| `name` | text | 60 | "You are {name}" and the heading |
| `owner` | text | 60 | ", working for {owner}" |
| `voice` | text | 600 | "Voice and tone: …" |
| `rules`, `do`, `dont` | lists of text | 20 × 300 | bulleted under Rules / Do / Don't |
| `formatting` | text | 600 | "Formatting: …" |
| `addenda` | `{family: text}` | 12 × 1200 | only the one for the harness being rendered |
| `description` | text | 200 | never (operator note) |

A profile needs at least a name, voice, rules, formatting or an addendum.
List fields accept a newline-separated string (leading `-`/`*` stripped).

**Families** name an agent harness: `claude-code`, `codex`, `hermes`,
`voice`, `mcp` (any other lowercase name is accepted too). Aliases fold in
the names Rook uses elsewhere: `claude` (work launches, User-Agent) becomes
`claude-code`.

**Rendering** (`model.render`) produces short Markdown:

```
## Persona: Example
You are Example, working for the operator.
Voice and tone: Calm and direct.
Rules:
- Say what you verified.
Do:
- Lead with the answer.
Don't:
- Pad replies.
Formatting: Short paragraphs.
<addendum for this family, if any>
```

## 2. Scopes and resolution

An assignment maps a scope to a profile:

| Scope | Target | Who it covers |
|---|---|---|
| `user` | a dashboard account id, or an API token's `agent_id` or label | that caller |
| `family` | a family name | agents of that harness |
| `band` | a band id | callers on that band (when the caller says which band) |
| `default` | none | everyone |

The most specific assignment wins, whole: **user > family > band > default**.
Profiles don't merge field by field. The addendum inside the winning profile
still depends on the harness it is rendered for, so one profile can serve
every family with small per-harness differences.

MCP tokens are not linked to dashboard accounts, so for MCP callers the user
scope matches the token (`agent_id` or label); the voice service's
per-account preferences use the account id.

## 3. Storage, versions and history

`<hub state>/plugins/persona/persona.db` (plugin migrations,
`migrations/001_schema.sql`):

- `persona_profiles(id, rev, doc, updated, actor)`: the current document.
  Every save that changes something raises `rev` by one; saving an identical
  document is a no-op. `expect_rev` refuses a stale edit.
- `persona_assignments(scope, target, profile, updated, actor)`.
- `persona_history(kind, ref, rev, doc, ts, actor, note)`: every profile save
  and delete (with the full document) and every assignment change, attributed
  to the caller (`agent:…`, `human:<username>`). Deleting a profile keeps its
  history; a profile that is still assigned cannot be deleted.

## 4. Caps on worker `rook`

None has a dedicated MCP tool (`tools/list` is unchanged); they are one
`rook_call(worker="rook", …)` away.

| Cap | Risk | Does |
|---|---|---|
| `persona.get(id?, family?, user?, band?, harness?)` | read | A profile, or the one that applies to the caller, with `source` and rendered `text`. |
| `persona.render(harness?, profile?, user?, band?)` | read | Just `{text, profile, rev, sha}` for a harness. Band-callable: workers fetch it. |
| `persona.list()` | read | Profiles and assignments. |
| `persona.history(id? \| scope+target)` | read | Attributed changes, newest first. |
| `persona.set(profile, note?, dry_run?, expect_rev?)` | admin | Create or replace a profile. `dry_run` validates and previews without the admin gate. |
| `persona.assign(scope, profile?, target?, note?)` | admin | Assign (empty `profile` removes). |
| `persona.delete(id, note?)` | admin | Delete an unassigned profile. |

Admin caps call `rook.hub.authz.require_hub_admin`: band owners, operator
tokens and in-process hub code only, whatever the policy mode. Over the band
only the read caps pass (the hub's band ceiling, plugins.md 10.4).

Persona text is not a secret, so `persona.render` answering unauthenticated
band callers is acceptable; as with `settings.worker_secret`, a worker
trusts whichever band peer answers its request (the band key is the trust
boundary until device-signed calls land, permissions.md).

## 5. Delivery channels

### 5.1 MCP `initialize` instructions

`rook/band_mcp/persona_connect.py` wraps the low-level server's
`create_initialization_options`. FastMCP calls it once per session, inside
the task started for the request that opens the session, so a context
variable set around the session manager's `handle_request` carries that
request's headers. From them:

- the bearer token gives the user-scope ids (`TokenStore.principal_for`);
- the family comes from an explicit `X-Rook-Client` header, else the
  User-Agent (`claude…` → claude-code, `codex…`, `hermes…`), else `mcp`.

`guidance.compose_instructions(server_slot, persona)` appends the rendered
persona after the operator's `server` guidance slot, separated by a blank
line.

**Token budget.** With nothing assigned the instructions are exactly the
`server` slot (1,148 characters today; `tests/test_persona.py` asserts the
equality, and `tools/token_budget.py` reports the same number before and
after this change). With a persona, the persona section is capped at
`model.MCP_BUDGET` = 1,200 characters, cut at a line boundary with a pointer
to `persona.get` for the rest. Instructions are paid once per connect, not
per tool listing, and `tools/list` is untouched. Any failure (no hub node,
store error) leaves the instructions unchanged.

### 5.2 Harness files: `persona.apply` (worker)

`persona.apply(harness, path?, content?, profile?, remove?, dry_run?)`
(risk `write`) writes one managed block:

```
<!-- rook:persona:begin profile=steady rev=3 sha=4edc65a3c5b3 (managed by Rook; edits here are replaced) -->
...rendered persona for the harness...
<!-- rook:persona:end -->
```

| harness | default file |
|---|---|
| `claude-code` | `$CLAUDE_CONFIG_DIR/CLAUDE.md`, else `~/.claude/CLAUDE.md` (user level) |
| `codex` | `$CODEX_HOME/AGENTS.md`, else `~/.codex/AGENTS.md` |
| `hermes` | `$HERMES_HOME/SOUL.md`, else `~/.hermes/SOUL.md` |

`path` may name a project file instead: absolute, with the file name
`CLAUDE.md`, `AGENTS.md` or `SOUL.md` (so the cap cannot be pointed at
arbitrary files).

Rules the implementation keeps (and `tests/test_persona.py` checks on sample
files):

- **Never touch content outside the markers.** Replacing a block splices
  exactly the marker span; everything before and after is kept byte for byte,
  wherever the user moved the block.
- **Idempotent.** Applying the same text again reports `unchanged` and does
  not write.
- **Round trip.** Adding a block appends a blank line and the block;
  `remove=true` takes both out again, so apply then remove restores a
  newline-terminated file exactly. (A file without a final newline gets one.)
  A file that held only the block is deleted on removal.
- **Refuse, don't guess.** A missing, repeated or out-of-order marker pair is
  an error; the file is left alone.
- **Line endings and links.** CRLF files stay CRLF; a symlinked
  `CLAUDE.md` is written through to its target (the link stays); the file's
  mode is kept; writes are atomic (temp file + rename).
- `dry_run=true` returns the action (`create`, `insert`, `update`,
  `unchanged`, `remove`, `absent`) and a unified diff without writing.

Without `content`, the worker asks the hub for `persona.render` for that
harness (optionally a named `profile`). `persona.status` reports, per
harness file, whether a block is present and its `profile`/`rev`/`sha`, so
an operator can see which machines carry an old revision.

### 5.3 Work launches

`work.stream.open` (rook/worker/plugins/terminals.py) fetches
`persona.render` for the harness before it spawns an agent: the session's
`persona` field names a profile, else the scoped resolution for the family
applies. `terminals.persona_args` turns the text into argv:

| harness | argv |
|---|---|
| claude | `--append-system-prompt <text>` |
| codex | `-c developer_instructions="<text>"` |
| hermes | none (Hermes reads SOUL.md: use `persona.apply`) |

The text is also written to a private file exported as `ROOK_PERSONA_FILE`
(removed with the terminal) for wrapper scripts; `ROOK_PERSONA` still carries
the profile id. The fetch waits at most 3 s; a hub that never answers (no
persona plugin) is not asked again for 10 minutes, so launches don't keep
paying the timeout. `persona.apply` always asks.

A launched agent that also gets a Rook MCP connection sees the persona in
its MCP instructions too; the duplication is a few hundred characters and
keeps launches without MCP consistent.

### 5.4 Skill site overlay

`skill.site_notes` (rook/band_mcp/skill.py) appends a short "Persona"
section, from the **default** assignment only, to the operator's site notes
(`ROOK_SKILL_SITE_PAGE` / `ROOK_SKILL_SITE_FILE`). It is served as
`references/site.md` to token holders, like the rest of the overlay.
Per-user and per-family personas stay out of the downloadable skill.

### 5.5 Voice service

The voice service names itself from `ROOK_VOICE_ASSISTANT_NAME` and
`ROOK_VOICE_OWNER` (services/voice/providers.py). They are now also voice
service settings:

| Setting | Env | Scope |
|---|---|---|
| `voice.assistant_name` | `ROOK_VOICE_ASSISTANT_NAME` | hub, overridable per user |
| `voice.owner` | `ROOK_VOICE_OWNER` | hub, overridable per user |

**Mapping.** `settings.fetch("voice")` asks every hub plugin with a
`settings_fetch_extra(namespace, values, users)` hook to fill blanks. The
persona plugin fills:

- `values.assistant_name` ← `name`, `values.owner` ← `owner` of the persona
  resolved for family `voice` (family > band > default), when the stored
  voice settings leave them blank;
- `users[<account id>].assistant_name` / `.owner` from a persona assigned at
  `user:<account id>`, when that user has no explicit voice value.

Precedence for the voice service: its own environment > an explicit
`voice.*` setting > the persona > the built-in default ("Rook", neutral
owner). The voice service is being moved to `settings.fetch` separately; it
needs no persona-specific code, only these two keys.

## 6. Settings UI

Settings > **Persona** (operator account only) lists the assignments (add or
remove), edits a profile field by field (per-family addenda included),
previews the rendered text (a dry run), saves with the revision it loaded
(`expect_rev`), deletes unassigned profiles and shows the history. It uses
the Settings account API (`/settings/account-api`, `view=persona`; POST
actions `persona_save`, `persona_assign`, `persona_delete`), which the
dashboard already proxies.

## 7. Compatibility

- No wire change: build-167 workers don't have `persona.apply` and never ask
  for `persona.render`. Newer workers asking an older hub get no answer and
  launch without a persona.
- Hubs without the plugin (or with `ROOK_HUB_PLUGINS=0`) serve the same MCP
  instructions and skill as before.
- `settings.fetch("voice")` gains two keys; a voice service that ignores them
  is unaffected.

## 8. Open questions

- Per-band resolution for MCP callers: a token spans bands, so MCP
  instructions resolve without a band. `persona.get(band=…)` supports it.
- Linking API tokens to dashboard accounts would let one user assignment
  cover both a person's tokens and their voice preferences.
- Pushing `persona.apply` to many workers at once (a hub-side fan-out) is
  left out: it would run worker writes under the hub's identity. Agents or
  the operator call it per worker.
