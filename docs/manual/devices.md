---
id: devices
title: TVs and other devices
order: 18
features:
  - features/device-workers-tvs-and-other-devices
  - plugin/worker/webos
  - settings/webos
  - plugin/hub/show
  - settings/show
since: 0.1.0
deprecated: false
---
# TVs and other devices

Some things on your network can't run a Rook worker: a smart TV, and later
light switches and thermostats. Rook can still put them on your band. A
worker on the same network becomes their **delegate**: it talks to each
device and shows it on the band as a worker of its own, with its own name,
its own capabilities and its live state.

So the living-room TV appears on the Workers page as **living-room**, with a
TV icon, a small status such as `on · YouTube · vol 12`, and a **via**
tag naming the worker that answers for it. Agents call it by name, for
example "put YouTube on the living-room TV", without knowing which machine
is in the middle.

## What you see

- **The status** next to the name is the device's live state. For a TV:
  `on` or `off`, the app or input showing, and the volume.
- **via** names the delegate. If that worker goes offline, its devices
  leave the list with it, because nothing is answering for them any more.
- A TV that is switched off stays on the list as `off`. Its delegate is
  still there and can switch it back on.
- Devices are updated with their delegate, so they don't appear on the
  Updates page. They have no settings of their own, and you can't deauth
  one. To remove a device, take it out of the delegate's settings; to cut
  off all of them, deauth the delegate.

## LG webOS TVs

LG TVs that run webOS (most LG TVs since 2014) can be device workers.
Any always-on worker on the TV's network can be the delegate, for example
a small server or a container. A laptop is a poor choice, because its
devices disappear whenever it sleeps.

To add your TVs:

1. On the TV, turn on **Settings > General > Devices > TV Management > LG
   Connect Apps** (the name varies a little by model). To switch the TV on
   from Rook, also turn on **Turn on via Wi-Fi** (or **Mobile TV On**).
2. Open the delegate's **Settings** page, find **Webos > TVs**, and enter
   each TV as `name=address`, separated by commas, for example
   `living-room=192.0.2.10, office-tv=192.0.2.11`. The name is what the TV
   is called on your band. To find your TVs' addresses, run `webos.discover`
   on the delegate.
3. Save. Each TV shows up on the Workers page within a minute, as
   `not paired`.
4. Pair each TV: stand at the TV, run `tv.pair` on it (from the TV's row,
   or ask an agent), and accept the **allow this device?** prompt with the
   remote. The status changes to `on`. You only do this once per TV.

A TV only answers while it is on (or in its network standby mode). Rook
learns the TV's network address for Wake-on-LAN while it is on, so after
that `tv.power_on` can switch it on, if the TV allows it.

### What you can do with a TV

- `tv.status`: on or off, the app or input showing, volume and mute.
- `tv.toast`: show a short message on the screen.
- `tv.launch`: open an app by name, such as `YouTube` or `Netflix`
  (`tv.apps` lists them). It can also open a particular YouTube video.
- `tv.input`: switch to an input such as `HDMI 1` (`tv.inputs` lists them).
- `tv.volume`, `tv.mute`: set or step the volume, mute and unmute.
- `tv.media`: play, pause, stop, rewind and fast forward in the current app.
- `tv.button`: press a remote button, such as `HOME`, `BACK` or the arrows.
- `tv.open_url`: open a web page in the TV's browser.
- `tv.screen`: turn only the screen off (sound keeps playing) and back on.
- `tv.power_off`, `tv.power_on`: switch the TV off, or on with Wake-on-LAN.

Everything except the status, app and input lists counts as a physical
action, and `tv.pair` is an admin action, so your permission rules and
approvals apply to them as to any other capability.

### When something is wrong

- **not paired**: run `tv.pair` while someone is at the TV. If the prompt
  never appears, check that **LG Connect Apps** is on.
- **off** while the TV is on: the delegate can't reach it. Check the
  address, and that both are on the same network.
- `tv.power_on` does nothing: the TV's **Turn on via Wi-Fi** setting is
  off, or the TV went fully to sleep. Some TVs only wake from a short
  standby.

On the delegate itself, `webos.list` shows every configured TV and why it
is not reachable. More about delegates and devices: [[manual-workers]].

## Showing you things on a screen

When a picture, a diagram, a web page or an app answers better than words,
Rook can say a short line and put the thing itself on a screen near you, in
one go. Ask the house agent ("show me the floor plan"), or have an agent call
`show.present` on the hub.

- **Where it goes**: the screens listed in **Settings > Show > Screens to show
  on**, in order; otherwise your phone (the house agent's phone setting);
  otherwise the only phone that is online. A TV is used only when you name it
  ("on the office TV") or list it there. With several phones and nothing
  saying which is yours, Rook doesn't guess: it keeps the link instead.
- **What it shows**: a web page or an app opens as it is. A picture, a
  diagram, a list or a knowledge page becomes a small page of its own at
  `https://<your hub>/show/...`, which any browser can open without signing
  in. Only someone with the link can open it, and the link stops working
  after an hour (**Settings > Show > Show pages last**, at most a day).
- **When no screen is free**: Rook still says the line and gives you the
  link. When nothing can speak, it still shows the page.

Showing on a device uses that device's own permissions: a person can only
show things on screens they may use, and the house agent follows the trust
map, as for any other call.

