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
command. It takes about ten minutes, most of it waiting for software to build.

## What you need

- A Linux computer to be the hub (a small home server, a Raspberry Pi 4 or a
  cheap cloud machine all work). It should stay switched on.
- Python 3.11 or newer, and `git`.
- The relay, a small program that passes band messages along. The quickstart
  builds it for you with Rust.

The full step-by-step commands are in the README's Quickstart. In short:

```sh
git clone https://github.com/Bake-Ware/rook
cd rook
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
scripts/local-hub.sh
```

To reach the hub from other machines in your home, start it with
`BIND=0.0.0.0 scripts/local-hub.sh` instead.

## The first start

On its first start the hub:

- creates a band key, a dashboard password and an agent token, and saves them
  in `rook-data/quickstart.env` (only you can read that file);
- prints the dashboard address and password;
- builds the worker installer in the background (about a minute).

Open the printed address in your browser and sign in with the password.

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

## Running the hub as a service

`rook hub serve` (also installed as `rook-hub`) runs the whole hub, dashboard
and agent connections, in one program. `scripts/local-hub.sh` starts it for
you together with the relay. Administrators who want a permanent setup, Docker
or signed hub releases should read [[manual-running-a-hub]].

## If something goes wrong

- **The worker does not appear.** Workers check in every 30 seconds; wait a
  minute. Check that the other machine can reach the hub's address.
- **"Building, retry in a minute".** The hub is still building the worker
  installer on its first start. Try again shortly.
- **The pairing code expired.** Codes last five minutes. Get a fresh one; the
  installers ask for a new code if the old one ran out while they worked.
