# Huddles

A **huddle** is a group thread where people and agents in roles work
something out: release notes with the writer and QA, a design question with
the lead and the PM. It is a durable stream: everything said stays readable.
All of it is caps on worker `rook`; put `huddle` in the hub's library (with
the agents package for the roles).

```
huddle.start title="Release notes for 0.5" roles=[writer, qa] project=rook
huddle.post huddle=<id> text="Draft is up; @qa can you check the install steps?"
huddle.read huddle=<id> after=0        → {events, next, closed}; pass next back
huddle.close huddle=<id>               (its creator or an owner)
```

- `roles` are agent names from the agents package. A post that names one
  (`@qa`), or `@all`, also goes to those agents (`agent.chat.send`), sent as
  you, so they act with no more than you may. The reply says who was told.
- `huddle.list`, `huddle.get` and `huddle.search` find huddles.

Not yet: question events (a question that waits for the person), which
come with `approval.ask` (castling C4). `huddle.post` and `huddle.read`
stand in for `stream.write` / `stream.read` on the hub, and become their
aliases once the hub serves those (castling-caps).

There are no chairs, pins or votes. Work lands by sign-off on its task and
[`github.merge`](../../github/docs/manual.md) (castling-workbench: sign-off
replaces merge votes).
