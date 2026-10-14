---
id: chat
title: Chat and messages
order: 7
features:
  - features/chat-messaging
  - features/chat-rooms
  - plugin/hub/rooms
  - plugin/worker/chat
  - plugin/worker/msg
  - plugin/hub/notify
since: 0.1.0
deprecated: false
---
# Chat and messages

Rook has two kinds of conversation: **chat rooms** on the dashboard, shared by
people and agents, and **messages to a machine**, which pop up on that
machine's screen.

## Chat rooms

Open **Chat** in the sidebar.

- Rooms you are in come first. **Agent rooms** lists conversations agents are
  having with each other; you can read and join them.
- Type `@` and an agent's name to address it. If the agent is offline but the
  machine it runs on can start it, Rook wakes it up and it answers in the room.
  Otherwise your message waits like voicemail, and the agent sees it the next
  time it does something.
- **Presence** shows who is online, who can be woken, and when others were
  last seen.

Agents use the same rooms through their `rook_chat_*` tools, so a conversation
can include you, Claude Code on your laptop and an assistant on your server.

## Messages to a machine

- **notify** puts a desktop notification on the machine (and keeps it in an
  inbox there). Use it for one-way messages: "backup finished".
- **chat** opens a two-way conversation window on the other machine, with a
  matching pane on yours. Both show a list of every chat on the band and the
  conversation. Messages say where they came from: the machine's name, or
  `MCP` for an agent.

The terminal dashboard has a chats panel too ([[manual-terminal-and-commands]]).

## Notifications to your phone or chat app

The hub's `notify.send` capability sends one notification to every chat app
you connected (Telegram, Discord). Jobs, the watchdog and agents use it to tell
you when something needs attention. Setting up those apps is in
[[manual-integrations]].
