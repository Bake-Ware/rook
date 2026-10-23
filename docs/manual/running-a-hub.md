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

- **The one-command installer** (recommended): `scripts/install-hub.sh`
  installs the latest release as two systemd services, `rook-relay` and
  `rook-hub`, checks every download against its published checksum, and keeps
  the hub's settings in `hub.env` and its keys in `secrets.env`. Run it again
  to upgrade; `--uninstall` removes it and keeps the data (`--purge` deletes
  that too). It also puts the release's Android app on the hub, so phones
  can install it from `/apk` from the first start (`--no-apk` to skip). `--user` installs without root. When it finishes it prints, in
  plain words, the dashboard address and password, the next steps, the address
  and token for AI agents, and how to check on, upgrade or remove the hub. All
  options and troubleshooting: docs/operations/install.md.
- **Docker**: download `compose.yaml` and `.env.example` (as `.env`), fill in
  the band key, password and agent token, and run `docker compose up -d`. It
  runs the published image as two containers, the hub and the relay, with
  state in the `rook-data` volume.
- **From source**, for development: a clone, a virtualenv and
  `scripts/local-hub.sh` in the foreground (the README's "From source").

Whichever you choose, the hub is one program, `rook-hub` (`rook hub serve`),
serving the dashboard, `/mcp` for agents and `/band` for workers on one port
(7005), next to the relay (UDP 7474). For setups made before that, it also
serves agents alone on port 8765.

The hub keeps everything (band keys, accounts, tokens, the call journal, chat,
secrets, the knowledge base) in one data folder (`/var/lib/rook` for the
installer's system install, the `rook-data` volume for Docker). Back it up and
keep it private.

## Reaching the hub from outside

Workers away from home connect through the hub's web address over HTTPS, so
put the hub behind a reverse proxy or tunnel with TLS and set **Public
dashboard host** on the Settings page (or `ROOK_DOMAIN` in `hub.env`). If the
installer warns that the machine has a public address, firewall port 7005 or
install with `--bind 127.0.0.1` behind the HTTPS proxy. The README's "Going beyond localhost"
section has the details.

## Your own relay

The relay forwards encrypted band traffic and holds no keys. To run one on
another machine:

```sh
curl -fsSL https://<your-hub>/hub | bash
```

It asks a few questions, installs a service and starts it. Tune it in
`/etc/telesthete-hub.env`.

## Signed hub releases (advanced)

For a hub deployed from a git checkout rather than the installer, `rook hub`
builds, deploys and rolls back signed hub releases:

- `rook hub release build` and `rook hub release verify`: package a version and
  sign it; check a signature. A release also carries what the hub serves
  besides its code: the worker bundle (built from the same version, or the one
  your workers run now) and the Android app and beta app the hub serves now.
- `rook hub deploy`: install a signed release and switch the services to it.
  If the new version does not come up healthy, it switches back by itself.
  It refuses a release whose app the approved hashes (`ROOK_PUBLIC_APK_SHA256`,
  `ROOK_BETA_APK_SHA256`) would not serve, keeps serving the worker bundle and
  app when a release has none, and after the switch checks that `/apk` and
  `/band-worker.pyz` still answer, switching back if not.
- `rook hub disarm`: cancel that automatic switch-back once you are happy.
- `rook hub rollback`: go back to the previous release.
- `rook hub status` and `rook hub history`: what is running and what happened.
- `rook hub prune`: delete old releases, keeping the ones in use.
- `rook hub units`: print example systemd service files.

The full runbook is docs/operations/hub-deploy.md.

## After an update

When a new hub version ships a changed manual, the dashboard asks whether to
update the knowledge base copy ([[manual-help]]).
