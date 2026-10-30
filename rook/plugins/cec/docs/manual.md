# HDMI-CEC (Pico-CEC)

Sends HDMI-CEC frames through a Pico-CEC bridge: a Pico W on the HDMI CEC
bus running the WiFi Pico-CEC firmware, which takes one text command per
line over TCP. The bridge is an **appliance**: onboard it once (its host,
and its port if it isn't 9526), and a worker that can reach it carries the
calls. Name the bridge in the call.

| Cap | What it does |
|---|---|
| `cec.send` | One CEC frame: `addr` (destination logical address, 0-f, 0 = TV), `opcode` (a byte), `operands` (more bytes), all bare lowercase hex (as 0.4 sent them). Returns the bridge's `OK ...` reply; anything else (`ERR ...`, no reply) is an error. |
| `cec.status` | Checks the bridge answers; returns its reply (its CEC addresses). |

Common frames:

| Frame | `addr` | `opcode` | `operands` |
|---|---|---|---|
| TV standby (off) | 0 | 36 | |
| Image View On (wake the TV) | 0 | 04 | |
| Volume up / down / mute (audio system) | 5 | 44 | 41 / 42 / 43 |
| Key released | 5 | 45 | |

## Instance settings

- `port`: the firmware's TCP port (default 9526).

0.4's `cec.raw` (a literal firmware line) is gone: send frames with
`cec.send`. Old names: `cec.ping` still answers (it is `cec.status`).
