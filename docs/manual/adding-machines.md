---
id: adding-machines
title: Adding machines
order: 3
features:
  - features/pairing-and-key-management
  - features/installers-served-by-the-hub
  - plugin/worker/enrollment
since: 0.1.0
deprecated: false
---
# Adding machines

Every machine you want Rook to reach needs a worker. The easiest way to add one
is with a **pairing code**: a short, temporary code that lets a new machine
join your band without you typing the band key.

## With a pairing code (recommended)

1. Open **Install a worker** in the dashboard (under Manage).
2. Choose what the machine may do under **What do you want Rook to do?**
3. Click **Get a pairing code**. The page shows the code, a countdown and
   ready-to-copy commands with the code filled in.
4. Run the command for the new machine:
   - **Linux or macOS**: paste the `curl ... | bash` line into a terminal. It
     installs a background service for your user and may ask for your
     password. It takes 1 to 3 minutes.
   - **Windows**: paste the PowerShell line. Windows asks for permission
     (UAC); installing takes 2 to 5 minutes. The window tells you when it is
     done or what went wrong, and keeps a log in
     `%USERPROFILE%\.rook-band-worker\install.log`.
   - **Android phones and tablets**: install the Rook app, see
     [[manual-phones-and-tablets]]. In Termux you can use the Android command
     instead.
5. The machine appears on the Workers page when it finishes.

A code lasts five minutes and keeps renewing while the page is open. One code
can add several machines. **Revoke code** cancels it at once. A code only lets a
machine join that one band; it never gives access to the dashboard or to your
agents' tools.

If an install takes longer than the code lasts, the installer asks for a fresh
code. Keep the page open and give it the new one.

## Signing in instead of a code

A terminal installer can also sign in with your Rook account (Google or a local
account) and fetch every band you may use:

```sh
curl -fsSL 'https://<your-hub>/worker?login=google' | bash
```

It shows a code to approve in the web app, then asks which band to join.

## What the installer sets up

The worker runs in the background and starts again after a reboot. It keeps its
settings and certificate in `~/.rook-band-worker/` on that machine; keep that
folder if you reinstall the system. Desktop installs also add the `rook`
command, the terminal dashboard ([[manual-terminal-and-commands]]).

Workers renew their certificates on their own (they last 30 days). If a worker
loses its connection to the hub, it keeps working for up to an hour on what it
already knows.

## Installing again or upgrading

Running the installer again is safe: it replaces the worker cleanly, and on a
failure the previous worker keeps running. Use this to move a machine to a
different hub, too.

## Removing a machine

On the Workers page open the machine's **⋯** menu and choose **deauth**. The
worker leaves the band and the hub ignores it from then on. To stop a machine
from ever joining with its old certificate, revoke the device under
**Account & access**. See [[manual-security]] for what removing can and cannot
do.
