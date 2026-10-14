---
id: getting-started
title: Getting started
order: 2
features:
  - features/installers-served-by-the-hub
  - cli/hub
  - cli/hub/serve
since: 0.1.0
deprecated: false
---
# Getting started

This page walks you through your first hub, your first worker and your first
command. It takes about ten minutes.

## What you need

- A Linux computer to be the hub: a small home server, a Raspberry Pi 4 or a
  cheap cloud machine all work. It should stay switched on.
- A recent system (Debian 12, Ubuntu 24.04, Fedora or Arch). The installer adds
  the few things it needs, such as Python.

## Install the hub

On the hub machine, run this one command in a terminal:

```sh
curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh | bash
```

It asks once before changing anything, then installs Rook and the relay (a
small program that passes band messages along) as background services that
start again after a reboot. When it is done it prints the dashboard address,
for example `http://192.0.2.20:7005`, the password and the next steps in plain
words.

- Run the same command again later to upgrade; your keys and settings are kept.
- No administrator rights? Add `-s -- --user` after `bash` to install it just
  for your user.
- Prefer Docker, or want to run it from the source code? See
  [[manual-running-a-hub]].

## The first start

On its first start the hub:

- creates a band key, a dashboard password and an agent token, and keeps them
  in a file only the hub can read (`secrets.env` in its data folder);
- builds the worker installer in the background (about a minute).

Open the printed address from any computer on your network and sign in with
the printed password (a hub with no operator user name set asks for the
password alone).

## Add your first worker

While only the hub is on your band, the Workers page shows a card called
**Add your first worker**:

1. Under **What do you want Rook to do?**, tick what you want this machine to
   be able to do. You can change this later on the Plugins page.
2. Click **Get a pairing code**. A short code and ready-made install commands
   appear.
3. Copy the command for the other machine's system (Linux and macOS, Windows,
   or Android) and run it there.
4. Within a minute or two the machine appears on the Workers page.

[[manual-adding-machines]] explains pairing codes and the other ways to install.

## Run your first command

Click the new worker to expand it, find `shell.exec` among its capabilities and
run it with `{"cmd": "uname -a"}`. The answer appears under the form.

## Where things run

The hub is two background services: the relay and `rook-hub`, which runs the
dashboard and the agent connections in one program on one port (7005).
[[manual-running-a-hub]] has the details.

## If something goes wrong

- **Other machines can't open the dashboard.** Allow port 7005 through the
  hub's firewall (for example `sudo ufw allow 7005/tcp`).
- **The worker does not appear.** Workers check in every 30 seconds; wait a
  minute. Check that the other machine can reach the hub's address.
- **"Building, retry in a minute".** The hub is still building the worker
  installer on its first start. Try again shortly.
- **The pairing code expired.** Codes last five minutes. Get a fresh one; the
  installers ask for a new code if the old one ran out while they worked.
