---
id: running-a-hub
title: Running a hub (administrators)
order: 24
features:
  - features/run-your-own-hub
  - cli/hub/release
  - cli/hub/deploy
  - cli/hub/rollback
  - cli/hub/status
  - cli/hub/history
  - cli/hub/disarm
  - cli/hub/prune
  - cli/hub/units
since: 0.1.0
deprecated: false
---
# Running a hub (administrators)

This page is for whoever looks after the hub machine.

## Ways to run it

- **Quickstart script**: `scripts/local-hub.sh` runs the relay and the hub in
  the foreground ([[manual-getting-started]]).
- **Docker**: `docker compose up -d` with the repository's `compose.yaml` and a
  `.env` file (copy `.env.example`). State lives in the `rook-data` volume.
- **A service**: run `rook hub serve` (or `rook-hub`) under systemd. The hub
  serves the dashboard and agent connections on one port (7005 by default);
  the agent connection is also offered alone on port 8765 for older setups.

The hub keeps everything (band keys, accounts, tokens, the call journal, chat,
secrets, the knowledge base) in one folder, `ROOK_DATA_DIR`. Back it up and
keep it private.

## Reaching the hub from outside

Workers away from home connect through the hub's web address over HTTPS, so
put the hub behind a reverse proxy or tunnel with TLS and set **Public
dashboard host** on the Settings page. The README's "Going beyond localhost"
section has the details.

## Your own relay

The relay forwards encrypted band traffic and holds no keys. To run one on
another machine:

```sh
curl -fsSL https://<your-hub>/hub | bash
```

It asks a few questions, installs a service and starts it. Tune it in
`/etc/telesthete-hub.env`.

## Signed hub releases

`rook hub` builds, deploys and rolls back signed hub releases on a server:

- `rook hub release build` and `rook hub release verify`: package a version and
  sign it; check a signature.
- `rook hub deploy`: install a signed release and switch the services to it.
  If the new version does not come up healthy, it switches back by itself.
- `rook hub disarm`: cancel that automatic switch-back once you are happy.
- `rook hub rollback`: go back to the previous release.
- `rook hub status` and `rook hub history`: what is running and what happened.
- `rook hub prune`: delete old releases, keeping the ones in use.
- `rook hub units`: print example systemd service files.

The full runbook is docs/operations/hub-deploy.md.

## After an update

When a new hub version ships a changed manual, the dashboard asks whether to
update the knowledge base copy ([[manual-help]]).
