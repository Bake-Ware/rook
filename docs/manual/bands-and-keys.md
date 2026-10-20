---
id: bands-and-keys
title: Bands and band keys
order: 4
features:
  - features/readable-band-keys
  - features/bands-and-worker-moves
  - settings/core/keys-access
since: 0.1.0
deprecated: false
---
# Bands and band keys

A **band** is a private group of machines. Every machine in a band shares the
same secret key, and everything sent inside the band is encrypted with it. Most
homes need only one band. Make another one to keep a group of machines apart,
for example the computers at a relative's house.

## Band keys

A band key is five ordinary words joined with hyphens, such as
`maple-orbit-cactus-lantern-ripple` (that one is only an example). Words are
easier to read out and type than random characters.

- To make a new band, click **+ Join band** on the Workers page (just **+** on
  a phone), choose **New key**, write the key down somewhere safe, then click
  **Join**. Generating alone changes nothing.
- **Show** lets you check what you typed.
- Keys are case-sensitive and include the hyphens.

**Keep band keys secret.** Anyone who knows a band's key can join it and run
commands on its machines. Pairing codes ([[manual-adding-machines]]) exist so you
rarely need to handle the key itself.

## The Bands page

Open **Bands** in the sidebar to see every band you can use, how many of its
machines are online and your role. Pick a band to see its tabs:

- **Workers**: the band's machines. **Move…** moves one machine;
  **Move all…** moves every machine in the band to another band.
- **Members** (owners only): who may use the band, their role, and
  **Invite a person** (a single-use link that lasts seven days).
- **Key** (owners only): the key's fingerprint and version, **Pairing code**,
  **Rotate key…**, the **⋯** menu (**Revoke pairing code**, **Download
  configuration**), and the machines enrolled with their own certificates
  (each with **Revoke**).

As a band's owner you can also **Rename…** a band or **Delete…** it from the
**⋯** menu next to its name. The
default band cannot be deleted. Deleting stops new machines joining and
revokes its pairing codes and certificates, but cannot erase the key from
machines that already have it.

## Moving one machine

On the Workers page, open a machine's **⋯** menu and choose **Move to band…**
(or use **Move…** on the Bands page). Pick the destination, or
**＋ Create new band…** in the same dialog. You must own the band the machine
is in now; being a member of the destination is enough. Machines need a recent
worker to move; older ones say an update is needed.

## How moves and key changes work

Rook first records which machines it expects, waits until each has saved the
new settings, then checks that each one really answers on the new band. Only
then is the move finished (or, for a new key, the old key retired). Keep the
Bands page open while this runs. If you close it or the hub restarts, the move
is remembered: use **Resume** to continue. A move that has not started can be
cancelled; once it starts it must finish.

Every machine in the band must be online for **Move all…** and
**Rotate key…**. Bring missing machines online first, or remove them under
**Account & access**.

## Rotating, replacing or revoking a key

- **Rotate key…** (Key tab) is the everyday way to change a key: every
  enrolled machine gets the new key, and the old one is retired once all of
  them have checked in on it.
- **Replace key now…** and **Revoke key…** are under **Compromised key** on
  the same tab, for a key you think someone else has. Replacing switches to a
  new key at once and shows it one time; every machine must then be enrolled
  again. Revoking stops all new joins until a new key is set. Both cancel
  outstanding pairing codes, and the new key is never sent over the old band.

**Important:** changing a key cannot reach into a machine that still has the
old one. A machine you do not trust any more can keep using the old key until
it is switched off. For a lost or stolen device, change the key and re-pair
every machine you still trust.

## Related settings

The **Keys & access** group on the Settings page holds the hub's key-related
settings, such as where the enrollment database lives. See
[[manual-settings]].
