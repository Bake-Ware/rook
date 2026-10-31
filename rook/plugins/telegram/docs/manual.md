# Telegram

The band's Telegram bot. It is a channel of `notify.send` (people are reached
through notify, not by calling this plugin), and it turns what people write to
the bot into `telegram.message` events the house agent and jobs can act on.

## Set up

1. Make a bot with @BotFather and copy its token.
2. Put the token in the vault and set `telegram.token` to the vault name
   (`setting.set {plugin: telegram, key: token, value: telegram-bot}`).
   Store the bot token in the vault first: `secret.set {name: telegram-bot, value: ...}`.
3. Each person links their account: `telegram.link` gives a code; they send
   `/link CODE` to the bot in a direct message within 10 minutes. The bot
   answers "Linked". A typed chat id is never accepted: the code proves the
   account is theirs.

## Caps

| Cap | What it does | Effects |
|---|---|---|
| `telegram.send {to, text, title?}` | a direct message from the bot to a linked person (what notify uses) | - |
| `telegram.post {chat, text, reply_to?}` | a message to any chat or group the bot is in, which can reach people outside the band | `sends_as` (a hard stop: the person approves) |
| `telegram.link` | a one-time code to link your account | `persists` |
| `telegram.unlink {to?}` | forget a link (yours; owners: anyone's) | - |
| `telegram.status {to?}` | connected, bot name, last error, whether a person is linked | - |

## Inbound messages

Every message to the bot (or in a group it is in, when its privacy mode lets
it see them) becomes a `telegram.message` event: `text`, `chat` (answer with
`telegram.post`), `private`, `from`, `from_id`, `message` and, when the sender
linked their account, `person`. The event is marked `taints`: a session that
receives it is tainted, since the text comes from outside. `/link` messages are
handled by the plugin and are not events.

A linked person's direct message is also answered: the agents package hands
it to the house agent (its `chat_agent` setting) as that person and sends
the reply back through this bot.

## Settings

| Setting | Meaning |
|---|---|
| `token` | vault name of the bot token |
| `api` | Bot API base URL (default `https://api.telegram.org`) |
| `poll` | seconds one long poll waits (default 25) |
