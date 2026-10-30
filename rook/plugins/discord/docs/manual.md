# Discord

The band's Discord bot. It is a channel of `notify.send` (people are reached
through notify, not by calling this plugin), and it turns what people write to
the bot (over the Gateway websocket) into `discord.message` events the house agent and jobs can act on.

## Set up

1. In the Discord developer portal, make an application with a bot, tick the
   **Message Content** intent, copy the bot token and invite the bot to your server.
2. Put the token in the vault and set `discord.token` to the vault name
   (`setting.set {plugin: discord, key: token, value: discord-bot}`).
   Until the hub has its vault, the hub reads the value from the environment:
   `ROOK_SECRET_DISCORD_BOT` for the name `discord-bot`.
3. Each person links their account: `discord.link` gives a code; they send
   `!link CODE` to the bot in a direct message within 10 minutes. The bot
   answers "Linked". A typed chat id is never accepted: the code proves the
   account is theirs.

## Caps

| Cap | What it does | Effects |
|---|---|---|
| `discord.send {to, text, title?}` | a direct message from the bot to a linked person (what notify uses) | - |
| `discord.post {chat, text, reply_to?}` | a message to any channel the bot can write in, which can reach people outside the band | `sends_as` (a hard stop: the person approves) |
| `discord.link` | a one-time code to link your account | `persists` |
| `discord.unlink {to?}` | forget a link (yours; owners: anyone's) | - |
| `discord.status {to?}` | connected, bot name, last error, whether a person is linked | - |

## Inbound messages

Every message to the bot (or in a channel it can read) becomes a `discord.message` event: `text`, `chat` (answer with
`discord.post`), `private`, `guild` (the server), `from`, `from_id`, `message` and, when the sender
linked their account, `person`. The event is marked `taints`: a session that
receives it is tainted, since the text comes from outside. `!link` messages are
handled by the plugin and are not events.

## Settings

| Setting | Meaning |
|---|---|
| `token` | vault name of the bot token |
| `api` | REST API base URL (default `https://discord.com/api/v10`) |
