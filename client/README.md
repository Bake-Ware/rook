# Rook HID-KVM source client

Turns this Linux box into the *control* side of a hardware KVM: it captures your
keyboard + trackball and streams them as USB HID to a Rook dongle plugged into
the target machine. Video is handled separately (e.g. a PCIe HDMI capture card);
this is input only.

Because the dongle injects real USB HID and reaches the network over its **own
WiFi radio**, the target sees a genuine keyboard/mouse and the whole path is
unaffected by the target's VPN, firewall, or lack of any installed software.

## Why this instead of a hardware KVM

A cheap hardware KVM emulates a 5-button USB boot mouse and mangles an
**Elecom HUGE** (8 buttons + hi-res/tilt scroll). Here we read raw evdev and
synthesize the exact 8-button HID report the firmware advertises, so every
button and axis passes through.

## Setup

```bash
pip install evdev pynacl
```

The client and dongle must share the band PSK (the same one in the firmware's
NVS / `/config`). The HID stream key is derived from it via
`HKDF-SHA256(psk, salt="telesthete-v1", info="hid-stream")` — a separate key
from the band hub traffic.

## Run

```bash
# root or 'input' group membership is required to read /dev/input
sudo ROOK_BAND_PSK="your-band-psk" python3 rook_hid_kvm.py --host 192.168.1.138
```

Find the dongle's IP from the LCD, or via the band: call `info.host`
(it now also reports `hid_stream_port`).

Options:
- `--host` / `--port` — dongle IP and HID stream port (default 7475)
- `--psk` — band PSK (or `ROOK_BAND_PSK`)
- `--kbd PATH` / `--mouse PATH` — pin specific `/dev/input/eventN` devices
  instead of auto-detect (repeatable)

## Use

**Double-tap RIGHT CTRL** to toggle forwarding:
- **ON** — devices are grabbed (`EVIOCGRAB`); this PC stops seeing them, every
  event drives the target.
- **OFF** — grab released; input is local again.

On every toggle an all-released report is sent so no key/button sticks down when
switching sides. Ctrl-C always ungrabs and releases before exiting.

## Notes / tuning

- The dongle is 2.4 GHz-only; expect single-digit-ms latency on a quiet network,
  occasional jitter under congestion. A quiet WiFi channel or AP-mode P2P helps
  if it ever bugs you.
- Mouse motion is coalesced per `SYN_REPORT` (one report per device sync) to keep
  packet rate sane without adding perceptible latency.
- Autorepeat (`value==2`) is dropped — the target host generates its own repeat.
- Stale/reordered UDP is dropped on the firmware side via a per-frame sequence
  number, so an old delta never lands after a newer one.
