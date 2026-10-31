# Phone notifications

| Cap | What it does |
|---|---|
| `notification.post` | A notification, or (with `id`) a decision card the person answers |
| `notification.update` | Show what came of a card: done, failed or gone |
| `notification.list` | Other apps' notifications, newest first |
| `notification.get` | One notification and the actions it offers |
| `notification.dismiss` | Dismiss one |
| `notification.act` | Run one of its actions (a reply sends as the phone's owner) |

## Posting

A plain notification is `title` and `text`. `channel` picks how it sounds:
blank is "Rook messages"; urgent rings like an alarm (through Do Not Disturb
when the phone allows it), family and approvals pop up, digest is quiet and
jobs silent.

With `id`, the post is a decision card: `text` is its one line, `body` up to
six short lines shown under it (`For: ...`, `Who: ...`, `I want to: ...`,
`Risk: ...`), `explain` what More details shows, `actions` its buttons (yes,
no, choose, explain, ack; default yes, no, explain, and ack for
`kind: alert`), `options` a question's choices (with the choose action).
Posting the same id again updates the card without ringing again.

## Events

- `notification-android.card`: what the person did with a card
  (`type` decision with `decision` approve, deny or choose and the `choice`
  tapped, verbatim; acked; dismissed; seen; opened). Checked every 10
  seconds; the app keeps them while the worker is down.
- `notification-android.received`: another app posted a notification
  (`key`, `package`, `title`, `text`). Checked every 30 seconds. Its words are
  whoever sent it, so it marks the session tainted, as `notification.list`
  and `notification.get` do.

Neither event announces what happened before the plugin first ran.

## Permissions

Reading needs Rook's notification access (Rook > Settings > Permissions).
`notification.act` speaks for the phone's owner, so the hub asks the person
first, and the phone's own switch must be on: Rook > Settings > App > "Let
the assistant act for me on this phone".
