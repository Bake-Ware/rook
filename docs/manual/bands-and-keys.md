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

- To make a new band, open the **Join with key** button on the Workers page,
  choose **generate five-word key**, write the key down somewhere safe, then
  click **add**. Generating alone changes nothing.
- **show key** lets you check what you typed.
- Keys are case-sensitive and include the hyphens.

**Keep band keys secret.** Anyone who knows a band's key can join it and run
commands on its machines. Pairing codes ([[manual-adding-machines]]) exist so you
rarely need to handle the key itself.

## The Bands page

Open **Bands** in the sidebar to see every band you can use. As a band's owner
you can:

- **Rename** a band or **delete** it (the main band cannot be deleted; deleting
  hides the band and stops new machines joining it, but cannot erase the key
  from machines that already have it).
- **Migrate all** moves every machine in the band to another band.
- **Migrate PSK** gives the band a new key and moves every machine onto it,
  without re-installing anything.

## Moving one machine

On the Workers page, open a machine's **⋯** menu and choose **Move to band…**.
Pick the destination, or **Create new band…** in the same dialog. You must own
the band the machine is in now; being a member of the destination is enough.
Machines need a recent worker to move; older ones say an update is needed.

## How moves and key changes work

Rook first records which machines it expects, waits until each has saved the
new settings, then checks that each one really answers on the new band. Only
then is the move finished (or, for a new key, the old key retired). Keep the
Bands page open while this runs. If you close it or the hub restarts, the move
is remembered: use **Resume** to continue. A move that has not started can be
cancelled; once it starts it must finish.

Every machine in the band must be online for **Migrate all** and
**Migrate PSK**. Bring missing machines online first, or remove them under
**Account & access**.

## Replacing or revoking a key

Under **Tokens** (API tokens) a band owner can also **Replace PSK** (type a new
key or generate one) or **Revoke band** (stop all new joins until a new key is
set). Both cancel outstanding pairing codes, and the hub moves to the new key
within a couple of seconds. The new key is never sent over the old band:
re-pair your trusted machines afterwards.

**Important:** changing a key cannot reach into a machine that still has the
old one. A machine you do not trust any more can keep using the old key until
it is switched off. For a lost or stolen device, change the key and re-pair
every machine you still trust.

## Related settings

The **Keys & access** group on the Settings page holds the hub's key-related
settings, such as where the enrollment database lives. See
[[manual-settings]].
