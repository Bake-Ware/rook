# Roku

Drives Roku players and Roku TVs over Roku's External Control Protocol
(ECP): plain HTTP on port 8060, **no pairing**. A Roku is an **appliance**:
it runs no Rook worker. Onboard it once (the hub finds it by SSDP,
`roku:ecp`) with `appliance.add plugin=roku`; a worker on the Roku's
network carries its calls. Name the Roku in the call; the caps don't take a
host.

| Cap | What it does |
|---|---|
| `roku.status` | Power mode, on/off, channel in front, player state, device name, model, whether it is a TV. Never fails on a Roku that doesn't answer: `reachable: false`. |
| `roku.app.list` | Installed channels `{id, title}`. |
| `roku.app.launch` | A channel by id or by name as people say it (`youtube`, `prime video`), with deep-link `params` (`contentId`, `mediaType`). |
| `roku.hid.key` | A remote button: home, back (or escape), up, down, left, right, select (or ok, enter), info, play, rewind, fastforward, replay, search, backspace, volumeup, volumedown, mute, channelup, channeldown; or any ECP key name (`InputHDMI2`, `FindRemote`). No modifiers. |
| `roku.hid.type` | Text into the field open on the Roku (a search box), one character at a time; `enter` presses Enter after it. |
| `roku.set_input` | A Roku TV's input: `tuner`, `hdmi1`-`hdmi4`, `av1`. |
| `roku.sound.set` | A Roku TV's volume by `step` (+3, -2), and/or `mute` (a toggle). ECP has no volume level and doesn't report mute, so `level` is refused. |
| `roku.media.play` / `roku.media.pause` | The channel's playback; nothing when it is already in that state. |
| `roku.media.seek` | The remote's skip: forward when `by` is blank or positive, back when negative. |
| `roku.power.on` / `roku.power.off` | A Roku TV on (waits up to `wait` s) or to standby. A player has no power. |

The Roku also answers the shared device caps: `app.launch`, `hid.key`,
`hid.type`, `sound.set`, `media.play|pause|seek` and `power.off` sent to it
reach the caps above.

ECP has no screenshot, notification or web browser, so a Roku has no
`screen.capture`, `notification.post` or `app.open_url`.

## Setup on the Roku

- **Control by mobile apps:** newer Roku OS answers ECP with 403 unless
  Settings > System > Advanced system settings > Control by mobile apps >
  Network access is Default or Permissive. The caps say so when it isn't.
- **Fast TV start** (Roku TVs, Settings > System > Power): without it the TV
  drops off the network in standby, and `roku.power.on` can't reach it.

## Instance settings

- `port`: ECP's port, 8060 (no need to change it).
