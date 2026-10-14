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

1. Create a bot: with Telegram's @BotFather, or in Discord's developer portal.
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

## Deluge

On a machine that runs the Deluge torrent client, the `deluge.*` capabilities
list, add, pause, resume and remove torrents and fetch finished files over the
band.

## Hermes and Claude Code

Rook can drive a Hermes agent on a machine and search your Claude Code history
across machines; see [[manual-sessions]].
