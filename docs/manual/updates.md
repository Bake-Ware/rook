---
id: updates
title: Updates and keeping Rook healthy
order: 21
features:
  - features/ota-self-update
  - features/post-update-health-gate
  - features/reliability
  - plugin/worker/selfupdate
  - settings/watchdog
since: 0.1.0
deprecated: false
---
# Updates and keeping Rook healthy

## Machines update themselves

When the hub has a newer worker version, it offers it to every machine that is
behind. Each machine checks that the update is signed by your hub and has not
been changed, tests it, installs it and restarts. You do not need to visit each
machine.

**Safety net.** After updating, a machine must prove it can still talk on the
band: it asks the hub a question and waits for the answer. If it cannot within
three minutes, it puts the previous version back and restarts into it, so an
update can never strand a machine where you cannot reach it. A version that
keeps crashing is also rolled back. A machine will not try a version it rolled
back again for a day (an hour if the problem was probably the network, not the
update). The Workers page and `rook_workers` show when a machine rolled back
and why.

If the hub itself goes down during an update, updating machines roll back and
try again later. That is the price of never losing a machine.

**Holding a machine back.** `worker.hold` keeps one machine on its current
version, for example while you test something. `worker.check` with `force`
makes one machine update now, useful for trying a version on one machine first.

Phones update through the app ([[manual-phones-and-tablets]]); the USB dongle
has its own firmware updates ([[manual-hardware]]).

## The hub keeps itself healthy

- **Agent connections are limited.** If too many agents are connected, the one
  that has been idle longest is disconnected (it reconnects by itself), so one
  misbehaving agent cannot lock everyone out.
- **The watchdog** checks the hub every minute: that agents can connect, that
  its services run, how many machines are online and how much memory is free.
  A second copy can run on another machine and check the hub from outside.
  Problems are sent to you once on Telegram (or through the hub's
  notifications), with an hourly reminder while they last and a message when
  they are fixed. Its settings are under **Watchdog** on the Settings page.

## Updating the hub

How to update the hub depends on how it was installed; see
[[manual-running-a-hub]]. After the hub updates, it may tell you that this
manual changed and offer to update the copy in your knowledge base
([[manual-help]]).
