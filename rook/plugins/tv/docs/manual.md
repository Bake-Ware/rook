# LG webOS TVs

Drives LG webOS TVs over their own control API (SSAP, the "second screen"
websocket on port 3000, or 3001 with TLS on newer firmware). A TV is an
**appliance**: it runs no Rook worker. Onboard it once (the hub finds it by
SSDP, `urn:lge-com:service:webos-second-screen:1`) and pair it; a worker on
the TV's network carries its calls. Name the TV in the call; the caps don't
take a host.

| Cap | What it does |
|---|---|
| `tv.status` | On/off, power state, app in front, volume, mute, sound output, model, Wake-on-LAN switch and MACs. Never fails on a TV that is off: `on: false, reachable: false`. |
| `tv.app.list` | Installed apps `{id, title}`. |
| `tv.inputs` | External inputs (HDMI...). |
| `tv.app.launch` | An app by id or by name as people say it (`youtube`, `disney plus`), with launch `params` (a YouTube video: `{"contentTarget": "https://www.youtube.com/tv?v=<id>"}`). |
| `tv.set_input` | Switch to an input by id or label (`HDMI 1`). |
| `tv.notification.post` | A toast on the screen (`title: text`). |
| `tv.sound.set` | `level` 0-100, or `step` (+3, -2), and/or `mute`. Says so when the sound goes to a soundbar the TV's volume may not reach. |
| `tv.media.play` / `pause` / `stop` | The current app's playback. |
| `tv.media.seek` | The remote's skip: forward when `by` is blank or positive, back when negative (webOS can't seek to a position). |
| `tv.hid.key` | A remote button: home, back (or escape), up, down, left, right, enter (or ok), menu, exit, info, red/green/yellow/blue, play, pause, stop, rewind, fastforward, volumeup, volumedown, mute, channelup, channeldown, 0-9. No modifiers. |
| `tv.hid.type` | Text into the field open on the TV (a search box). `replace` clears it first, `enter` submits, `app` types only if that app is in front. |
| `tv.app.open_url` | An http(s) URL in the TV's browser. |
| `tv.screen.capture` | What the TV shows, as a JPEG (960x540 on a CX), fetched from the TV itself. The remote's pointer isn't in it; protected video may be black. |
| `tv.screen.off` / `tv.screen.on` | Just the panel; sound keeps playing. |
| `tv.power.off` | Off. Already off is not an error. `force` changes nothing (a TV has one way off). |
| `tv.power.on` | Wake-on-LAN, then waits up to `wait` s (0-60) for it to answer. Needs a MAC, the TV's "Turn on via Wi-Fi" setting, and the delegate on the TV's network. |
| `tv.pair` | Onboarding (see below). |

The TV also answers the shared device caps: `notification.post`,
`app.launch`, `sound.set`, `media.play|pause|stop|seek`, `hid.key`,
`hid.type`, `screen.on`, `screen.capture` and `power.off` sent to it reach the caps above.

## Pairing

The first time, someone has to accept an "allow this device?" prompt on the
TV with its remote. `tv.pair` (no args) asks the TV to show it and waits up
to `timeout` s (10-110). `tv.pair key=<key>` checks a key paired elsewhere
instead. Either way it returns `{"paired": true, "settings": {"client_key":
...}}`: onboarding keeps that in the vault as the instance's `client_key`,
and every later call presents it. The key is a credential, so the hub treats
`tv.pair` as revealing a secret.

## Instance settings

- `client_key`: the pairing key (vault). Without it every cap answers "not
  paired: run tv.pair".
- `mac`: MAC address(es) for Wake-on-LAN, comma-separated. Optional:
  `tv.status` learns the TV's wired and Wi-Fi MACs while it is on and keeps
  them on the delegate; `tv.power.on` also asks the delegate's ARP table.

The 0.4 names (`tv.apps`, `tv.button`, `tv.power_on`, ...) are gone (0.5 is
a clean cut). Also gone: `tv.mute`
(`tv.sound.set mute=`), `tv.media` (one cap per action), `webos.list` and
`webos.discover` (the hub lists and discovers appliances), the `webos.tvs`
worker setting (TVs are onboarded instances), `tv.screen {on}` (two caps)
and `tv.sleep` (schedule `tv.power.off` with a job instead).
