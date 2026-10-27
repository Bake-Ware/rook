---
id: integrations
title: Telegram, Discord and other apps
order: 19
features:
  - features/integrations
  - plugin/hub/telegram
  - plugin/hub/discord
  - settings/telegram
  - settings/discord
  - plugin/worker/deluge
since: 0.1.0
deprecated: false
---
# Telegram, Discord and other apps

## Telegram and Discord

Connect Rook to a Telegram chat or a Discord channel to get notifications
there, talk to your chat rooms from your phone, and send a few commands. Both
are off until you set them up.

1. Create a bot: with Telegram's @BotFather, or in Discord's developer portal
   (there, also turn on the **Message Content** intent, or the bot hears only
   direct messages and mentions). Give the hub a bot of its own: a bot that
   another program already listens with won't work reliably.
2. On the Settings page, open **Telegram** or **Discord** and enter the bot
   token (stored write-only) and the chat or channel to use.
3. Choose which Rook chat rooms to connect. Messages flow both ways: Rook
   messages appear as `sender: text`, and replies come back into the room.
4. Switch the plugin on (Settings or Plugins page).

You can map chat-app user names to Rook names, so `@alice` in Telegram mentions
the right person or agent in Rook. When more than 20 messages arrive at once,
only the last 20 are sent, with a note saying how many were skipped.

Notifications from jobs, the watchdog and agents go to every connected app
([[manual-chat]]).

### Linking your account

The bot only listens to people it knows. Each person links their own
Telegram or Discord account once:

1. Ask the hub for a code: on the house agent or any agent, "give me a
   Telegram link code" (the tool is `users.link_code`). An owner can make one
   for someone else, naming them by account id, name, user name or email;
   a name that matches no account is refused.
2. Send `/link CODE` to the bot (on Discord, `!link CODE`), best as a direct
   message, within 10 minutes. The code works once. On Discord you can only
   message a bot you share a server with; in a server channel, write
   `@Bot !link CODE`.

After that, what you send acts as you: your access decides what the bot may
do for you, the same as on the dashboard. Write to the bot directly and the
house agent answers. Messages from accounts nobody linked are ignored.
`users.links` shows what you have linked and `users.unlink` forgets it.

**Your own bot.** The hub's bot is used by default. To use your own, put its
token in the vault (as `user.<your id>.<name>`) and tell the hub its name with
`users.chat_bot`. Your messages and notifications then go through your bot.

**Shared servers and groups.** An owner can make a household Discord server
or a family Telegram group a hub resource (`users.space`) and give people
access to it like a machine (a `space` grant). There the bot answers only
commands, replies to it and mentions of it, from people with that access.

## Deluge

On a machine that runs the Deluge torrent client, the `deluge.*` capabilities
list, add, pause, resume and remove torrents and fetch finished files over the
band.

## Hermes and Claude Code

Rook can drive a Hermes agent on a machine and search your Claude Code history
across machines; see [[manual-sessions]].
