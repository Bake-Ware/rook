---
id: workers
title: The Workers page
order: 6
features:
  - features/web-dashboard
  - plugin/worker/info
  - plugin/worker/shell
  - plugin/worker/file_ops
  - plugin/worker/proc
  - plugin/worker/log
  - plugin/worker/screenshot
  - plugin/worker/camera
  - plugin/worker/hid
  - plugin/worker/battery
  - plugin/worker/config
  - plugin/hub/info
  - plugin/hub/serves
  - settings/hub
since: 0.1.0
deprecated: false
---
# The Workers page

**Workers** is the dashboard's home page: every machine Rook can reach, live.

## Reading the list

- A green dot means the machine checked in recently. Workers check in about
  every 30 seconds; one that stays quiet for about 90 seconds is shown as stale.
- Each row shows the machine's name, band, Rook version and a small heartbeat
  graph. Phones, tablets and laptops show a battery level (a lightning bolt
  while charging; amber, then red, as it runs low).
- A row with a **via** tag is a device, such as a TV, that another worker
  answers for. Its status (`on · YouTube`) stands where a battery would. See
  [[manual-devices]].
- The tiles at the top count machines online and stale, and show which Rook
  versions are in use.
- **Group** (operating system, band, or none), **Sort**, and the filter box
  help on a big fleet. On a phone, **⇅** next to the filter shows them.
- Switch between **List**, **Grid** and **3D** views. In 3D, the hub is the
  rook and each machine a pawn; a quiet machine's pawn tips over.

## Doing things on a machine

Click a machine to expand it. Its capabilities are listed by group as chips.
Click one to open a form, fill in the arguments and run it. The answer appears
below: text output, a picture for screenshots, or a map link for a location.

The capabilities every desktop worker has:

- `shell.*`: run a command and get its output.
- `file.*`: read, write, list and search files.
- `info.*`: the machine's name, uptime and network reachability.
- `proc.*`: start a longer-running program and read its output as it goes.
- `log.*`: the machine's own record of who asked it to do what, and when.

Where the hardware allows:

- `screenshot.*`: a picture of the screen.
- `camera.*`: a still photo from a webcam.
- `hid.*`: type, press key combinations and move the mouse.
- `battery.*`: charge level.

**Custom commands.** With the `worker.*` capabilities you can add your own capability that runs a fixed command
with blanks to fill in, for example `cmd.restart-media` running
`systemctl restart {svc}`. Values typed into the blanks cannot break out of the
command. Custom commands stay on that machine across restarts.

## The machine menu (⋯)

- **Screenshot**, **Shell** and **Device info**: quick actions, where the
  machine supports them.
- **Rename…**: give the machine a new name; it restarts to apply it.
- **Edit description…**: a short note about what the machine is for (up to 280
  characters). Agents see it too.
- **Move to band…**: see [[manual-bands-and-keys]].
- **Deauth / ban…** and **Remove ban**: remove a machine from the band, or let
  it back.

## The hub itself

The hub appears as a worker called `rook`. Its capabilities run Rook's own
services: jobs, the knowledge base, chat rooms, settings and more. `hub.info`
reports the hub's version.

## Sites and services a machine hosts

The hub can keep a short list of the websites and services each machine hosts
(for example a media server's web page). Expanded machines show them as links,
and agents see them in the worker list. Agents and administrators set them with
the `serves.*` capabilities on the hub.

## Checking a machine from the machine itself

At the machine, `rook status` shows how its worker is doing and `rook doctor`
finds common problems and says how to fix them. See [[manual-worker-commands]].

## Worker settings

The `worker.*` capabilities (restart, reconfigure, plugin switches, updates)
belong to the worker's core and are always on. Per-machine settings are on the
Settings page under the worker's name, see [[manual-settings]].
