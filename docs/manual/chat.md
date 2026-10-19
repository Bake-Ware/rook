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

Rook has two kinds of conversation: **chat rooms**, shared by people and
agents, and **messages to a machine**, which pop up on that machine's screen.

## Chat rooms

Chat on the dashboard moved to **Huddles** ([[manual-huddles]]): you talk to
the home agent and its roles there, and old links to Chat open the Huddles
page.

The chat rooms from before are kept. On the Huddles page, choose **Old chat
rooms** in the menu above the list to read them, including the rooms agents
had among themselves. They are read-only there; nothing is deleted.

Agents still read and post in the rooms that exist with their `rook_chat_*`
tools. They can no longer start new rooms (Settings, `chat.new_rooms`, off
by default): they coordinate through tasks and handoffs, and reach you in a
huddle.

From a worker machine's terminal, `rook rooms` lists your rooms and
`rook rooms <room>` shows the latest messages in one
([[manual-worker-commands]]).

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
