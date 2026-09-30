# Chat integrations: Telegram and Discord

Rook can connect its persistent chat rooms to a Telegram chat or a Discord
channel, send notifications there, and take a small set of commands from it.
Each platform is a hub plugin: `telegram` (`rook/hub/plugins/telegram.py`)
and `discord` (`rook/hub/plugins/discord.py`). Both are built on
`rook/hub/integrations.py`. They run on the hub as worker `rook`, and both
are off by default.

Neither plugin needs a platform library. Both talk to the platform's HTTP API
(and, for Discord, the Gateway websocket) through `aiohttp`, which Rook
already depends on. If `aiohttp` can't be imported, the plugin's
`available()` returns false and it doesn't load.

## What they do

| Feature | Telegram | Discord |
|---|---|---|
| Receive | Bot API long polling (`getUpdates`, 25 s) | Gateway websocket: identify, heartbeat, reconnect |
| Send | `sendMessage`, plain text (no parse mode) | `POST /channels/{id}/messages`, `allowed_mentions` restricted |
| Command prefix | `/` (also `/cmd@yourbot`) | `!` |
| Message limit | 4096 characters, split on paragraph, line or sentence | 2000 characters, split the same way |
| Principal | `integration:telegram` | `integration:discord` |

### Room bridge

The bridge relays messages both ways between each room in `rooms` and the
configured `chat_id`. It uses the same `chat.db` as the MCP chat tools
(`rook_chat_*`) and the dashboard chat.

- **Rook to platform.** The bridge checks the rooms every 2 seconds.
  Each message is sent as `sender: text`. When more than one room is bridged,
  the room title goes in front: `[room] sender: text`. A new bridge starts
  from the current end of the room, so history is not replayed. Its position
  in each room is saved in the plugin's data directory
  (`<hub state>/plugins/<platform>/bridge.json`), so a restart doesn't
  resend messages or skip them. When more than 20 new messages arrive in one
  room between checks, only the last 20 are sent, after a line saying how
  many were skipped.
- **Platform to Rook.** A message from the configured chat is posted into
  the first bridged room as sender `telegram:<username>` or
  `discord:<username>`. A reply to a relayed message goes to that message's
  room instead.
- **Loop prevention.**
  - A message whose sender starts with `<platform>:` is never relayed back
    to that platform. A Telegram message can still reach Discord when both
    plugins bridge the same room.
  - Messages from bots are ignored on the way in, and so are this bot's own
    messages.
  - Notifications and command replies are never written into a room.
- **Mentions.** The `mentions` setting maps a platform handle (Telegram
  username, Discord user id) to a Rook identity, and it is used in both
  directions:
  - Inbound, `@alice` (or Discord's `<@id>`) becomes a mention of the mapped
    identity. That routes the message to them and invites them to the room,
    as described in `rook/band_mcp/chat_rooms.py`. Writing `@<identity>` for
    an existing participant also works.
  - Outbound, a message that mentions `user:operator` shows `@alice` on
    Telegram, or a real `<@id>` ping on Discord.
  - Discord messages are sent with `allowed_mentions: {parse: []}`, so room
    text can never ping `@everyone`, a role, or an unmapped user.
- **Rate limits.** Outbound messages share a token bucket (`rate_out`,
  default 20 per minute). A platform `429` is retried after its
  `retry_after`, up to 3 attempts. Inbound messages are limited per user
  (`rate_in`, default 10 per minute), and extra messages are dropped.

### Notifications

| Cap (worker `rook`) | Risk | What it does |
|---|---|---|
| `notify.send(text, channel="all")` | write | Posts to every running integration, or only to `telegram` or `discord`. `ok` is true if at least one of them accepted it. |
| `notify.channels()` | read | Lists the integrations that are running. |
| `telegram.send(text, chat=None)` / `discord.send(...)` | write | Posts to the configured chat. `chat` can only name that same chat. |
| `telegram.status()` / `discord.status()` | read | Reports whether the bot is connected, whether a token and chat are set (as booleans only), the bridged room count, counters and the last error. |

From an agent:

```
rook_call(worker="rook", cap="notify.send", args={"text": "backup finished"})
```

A hub plugin can call `self.dependency("notify")` if it declares
`DEPENDS = ("notify",)`. It can also look the plugin up through its bound
node.

**Watchdog.** When `ROOK_WATCHDOG_VIA_HUB=1` is set,
`rook/band_mcp/watchdog.py` sends alerts through `notify.send`. It uses the
MCP endpoint and static token that its probe already uses. If the hub can't
deliver an alert (the hub is down, or no integration is running), the
watchdog falls back to its direct Telegram settings
(`ROOK_WATCHDOG_TELEGRAM_TOKEN` / `_CHAT`), so a dead hub still sends
alerts.

### Commands

Commands are accepted only from the configured chat. If `command_users` is
set, only those platform user ids can use them. The platform user must type
the command explicitly, and the set is fixed:

| Command | Authorized as | Result |
|---|---|---|
| `help` | none | Lists the enabled commands. |
| `workers` | `band.workers` on `rook` (read) | Names of the live workers. |
| `rooms` | `chat.read` on `rook` (read) | The bridged rooms. |
| `call <worker> <cap> [json]` | the cap on the target worker | Runs the cap and replies with the result, truncated to 1500 characters. The cap must match one of the `allowed_caps` globs, and the policy must allow it. |

`commands` defaults to `help`, `workers` and `rooms`. To use `call`, add it
to `commands` and list the caps it may run in `allowed_caps`.

## Permissions

Every command, and the bridge's own chat reads and writes, runs under the
integration's principal, `integration:telegram` or `integration:discord`.
The hub authorizer (`rook/hub/authz.py`) evaluates each call and journals it.
Command calls carry the display identity `integration:<platform>/user:<id>`,
so the journal shows which chat user asked.

The shipped policy (`DEFAULT_POLICY` in `rook/hub/policy.py`) gives
integrations these defaults:

```json
"role:integration": {"read": "allow", "write": "allow", "exec": "deny", "admin": "deny"},
"integration:*":    {"read": "allow", "write": "allow", "exec": "deny", "admin": "deny"}
```

**Integrations fail closed.** An integration runs a command only when the
policy decision is `allow`. That includes `off` mode, but only for read and
write caps. A `would_deny` is refused like a `deny`, so the policy's default
`audit` mode doesn't give a chat user exec. You can check any decision with:

```
rook_call(worker="rook", cap="policy.explain",
          args={"principal": "integration:telegram", "cap": "shell.exec", "worker": "worker-a"})
```

### Example: exec on named workers (not enabled)

To let the Telegram integration run exec-tier caps on two lab machines and
nowhere else, add this rule to `rules` in the policy document (`policy.set`,
or the hub's `policy.json`). Also add the caps to the plugin's
`allowed_caps`:

```json
{"id": "telegram-exec-lab", "who": "integration:telegram",
 "allow": "tier:exec", "on": ["worker-a", "worker-b"]}
```

The most specific rule wins. This one names exact workers, so it beats the
`integration:*` exec default on those two workers, and everywhere else exec
stays denied. The same rule is `EXAMPLE_EXEC_RULE` in
`rook/hub/integrations.py`, and `tests/test_integrations.py` uses it. Admin
stays denied either way. To keep an integration from reading sensitive caps,
also add a deny rule for `tag:sensitive`, as shown in
`docs/design/permissions.md` §3.

## Settings

Both plugins use the same settings schema, and every setting except the
token can be set in the hub's `hub_plugin_settings.json` under the plugin's
namespace. Settings are read when the hub starts, so restart it to apply
changes. The token is read from the vault each time it is used.

| Setting | Type | Env | Default | Notes |
|---|---|---|---|---|
| `enabled` | bool | `ROOK_TELEGRAM` / `ROOK_DISCORD` | false | Loads the plugin. |
| `token` | secret | `ROOK_TELEGRAM_TOKEN` / `ROOK_DISCORD_TOKEN` | none | Vault key `plugin.telegram.token` / `plugin.discord.token`. |
| `chat_id` | str | `ROOK_TELEGRAM_CHAT` / `ROOK_DISCORD_CHAT` | empty | The Telegram chat id or Discord channel id. |
| `rooms` | list | | `[]` | Rook room ids to bridge. |
| `commands` | list | | `["help","workers","rooms"]` | A subset of `help`, `workers`, `rooms`, `call`. |
| `allowed_caps` | list | | `[]` | Globs that `call` may run. |
| `command_users` | list | | `[]` | Platform user ids allowed to use commands. Empty means anyone in the chat. |
| `mentions` | dict | | `{}` | `{handle or user id: rook identity}`. |
| `rate_out` / `rate_in` | int | | 20 / 10 | Messages per minute. |
| `api_base` | str | `ROOK_TELEGRAM_API` / `ROOK_DISCORD_API` | official API | Change it only for a proxy or a test server. |

Example `hub_plugin_settings.json`:

```json
{"telegram": {"enabled": true, "chat_id": "-1001234567890",
              "rooms": ["3f2a9c0d1e4b5a67"],
              "mentions": {"alice": "user:operator"}},
 "discord":  {"enabled": true, "chat_id": "123456789012345678",
              "commands": ["help", "workers", "call"], "allowed_caps": ["hub.info"]}}
```

Store the token with `rook_secret(action="set", name="plugin.telegram.token",
...)` or on the dashboard's Secrets page.

**Secrets.**
- The token is never logged, returned or put in a reply.
- Errors, command replies and status text are passed through a mask that
  removes the configured token and anything shaped like a Telegram or
  Discord token.
- `status` reports only `token_set: true/false`.
- The Telegram API puts the token in the request URL, so don't point
  `api_base` at a server you don't trust.

## Platform setup

- **Telegram.**
  1. Create a bot with @BotFather.
  2. Add it to the group. For bridging, turn off privacy mode so it can see
     ordinary messages.
  3. Get the chat id. For example, send a message and read the `chat.id`
     from `getUpdates`, or use a helper bot.
  4. Group chat ids are negative.
- **Discord.**
  1. Create an application and a bot.
  2. Turn on the **Message Content** intent.
  3. Invite the bot with the permissions to view the channel, send
     messages and read message history.
  4. `chat_id` is the channel id (turn on Developer Mode and use Copy ID).

## Limits and non-goals

- One chat per plugin. Every bridged room goes to that chat.
- The Discord client reconnects with a fresh identify after any disconnect.
  It does not resume, so events sent while it is disconnected are lost. Room
  messages are not affected, because the bridge's saved positions cover
  them.
- Chat users are not linked to Rook accounts. The integration's own policy
  always applies. On-behalf-of chains (`docs/design/permissions.md` §1.1)
  are a later step.
- There is no free-form agent: the command set is fixed. A chat message is
  never run as an instruction, only relayed.

## Tests

`tests/test_integrations.py` runs each plugin against a fake Telegram Bot API
server, and a fake Discord REST and Gateway server, both built with
`aiohttp.test_utils`. The tests use no real network. They cover:

- the bridge in both directions, with attribution
- loop prevention
- mention mapping
- reply routing
- burst summaries
- the command surface and the policy denial of exec
- the example exec rule, checked with `policy.explain`
- rate limits and `429` retry
- secret masking in logs, replies and status
- `notify.send`
- the watchdog's hub path and its fallback
