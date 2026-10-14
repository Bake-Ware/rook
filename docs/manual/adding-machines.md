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
can add several machines. **Revoke pairing code** (on the Bands page, Key tab)
cancels it at once. A code only lets a
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

## Installing without anyone at the keyboard

The installers show six numbered steps and end with "Done: <machine> joined
<band>"; the details go to `install.log` in `~/.rook-band-worker`. They only
ask questions when a person is at a terminal. For cloud-init, a script or a
management tool, add `-s -- --yes` after `bash` (on Windows, set
`$env:ROOK_YES = "1"` first) and nothing will prompt. `ROOK_WORKER_NAME` sets
the machine's name on the band (letters, digits, `.`, `_` and `-`), and
`ROOK_BAND` picks the band when an account sign-in offers several.

**Run as root** (cloud-init, `sudo`), the Linux installer stops and asks whose
worker this is, because a worker running as root would run every band command
(shell, files, terminals) as root:

- `ROOK_USER=<account>` installs it for that account instead: it runs as
  them, as a service that starts at boot. The account must exist already.
- `ROOK_ALLOW_ROOT=1` installs it as root anyway, for example in a container.

```sh
curl -fsSL 'https://<your-hub>/worker?band=CODE' | ROOK_USER=alice bash -s -- --yes
```

If a worker was installed for root on that machine before, `ROOK_ALLOW_ROOT=1`
updates it in place, and `ROOK_USER` stops and disables it once the account's
new worker is in, so the machine never runs two.

**On Windows**, run the command in an administrator PowerShell of the person
who uses the machine. It refuses to run as SYSTEM: the worker runs in that
person's session, which keyboard, mouse and screenshots need.

## What the installer sets up

The worker runs in the background and starts again after a reboot. It keeps its
settings and certificate in `~/.rook-band-worker/` on that machine; keep that
folder if you reinstall the system. Desktop installs also add the `rook`
command: the terminal dashboard ([[manual-terminal-and-commands]]), and
`rook status` and `rook doctor` to check on the worker from that machine
([[manual-worker-commands]]). Keyboard and mouse control is skipped on a Linux
machine with no screen (set `ROOK_HID=1` to set it up anyway). On Windows
without Python, the installer downloads it from python.org, checks it against
a known fingerprint and installs it for the current user.

Workers renew their certificates on their own (they last 30 days). If a worker
loses its connection to the hub, it keeps working for up to an hour on what it
already knows.

## Installing again or upgrading

Running the installer again is safe: it replaces the worker cleanly, and on a
failure the previous worker keeps running. Use this to move a machine to a
different hub, too.

## Removing a machine

On the Workers page open the machine's **⋯** menu and choose
**Deauth / ban…**. The worker leaves the band and the hub ignores it from then
on (**Remove ban** undoes it). To stop a machine from fetching its settings
with its certificate, **Revoke** it in the band's enrolled devices (Bands page,
Key tab, or **Account & access**). See [[manual-security]] for what removing can and cannot
do.
