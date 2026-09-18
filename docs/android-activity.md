# Chat, Activity and Decisions (0.4.5 / 9)

The 0.4.4 top bar is unchanged. Compact Chat / Activity / Decisions tabs sit
below it. Chat contains conversation bubbles and images. System notices, tool
updates and errors appear in the Activity timeline, grouped by connection and
turn, with timestamps, details and durations. Unseen Activity/Decisions counts
clear on viewing the tab; unseen failures/errors make the badge red.

Protocol-2 hello always sends `activity: true`; `thinking: true` still follows
Show thinking. The optional event formats are in `CONTRACT-activity.md` and
`CONTRACT-decision-event.md`. Unknown event types/phases and answer IDs are
ignored. Sequence IDs deduplicate activity within a connection; connection IDs
prevent reused turn numbers matching an earlier socket's messages.

The strip above the input shows the current phase and a monotonic elapsed timer.
Tool-wait heartbeats reset its silence timer. With activity support, 15 seconds
without an event shows an amber warning and 45 seconds shows red Stalled. Done
hides the strip. An old server that sends no activity never triggers these
warnings; existing state events supply the fallback display.

A decision is optional. A message has no marker without a matching event.
A tiny 8dp corner triangle marks the matching assistant bubble (accent for ok,
grey otherwise). If there is no assistant bubble, its user bubble carries the
marker; a later assistant reply takes over the record. Tap to animate expansion
or collapse of engine status, answers and confidence bars, elapsed time,
model/adapter and a shadow badge. RecyclerView stable message IDs and a
connection/turn index retain expansion across scrolling and rebinding.

The Decisions tab lists the same records, newest last. Tapping a card selects
Chat and expands/scrolls to its matching message. Missing chat messages are
reported explicitly. With Show thinking off, it displays the settings explainer
and chat markers/details are hidden. The current Activity owns chat/history as
before; messages/events missed while it is paused are not replayed.

Unit coverage includes parsing, grouping, duplicate sequences, old-server
fallback, stall boundaries and heartbeats, engine status, unread failures,
early/late attachment, user fallback, expansion persistence, and card jumps.
Owner screenshots are optional; missing capture permission is not an OTA gate.
