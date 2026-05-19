# PiKVM rook worker

Deploys the rook worker on a PiKVM (Arch Linux ARM, armv7l) so it joins the
Telesthete band and exposes the `pikvm.*` capabilities defined in
`rook/worker/plugins/pikvm.py`. After it's running, Claude can drive the
PiKVM through `mcp.bakeforge.com` like any other rook worker.

## Capabilities

| cap                  | what it does                                        |
| -------------------- | --------------------------------------------------- |
| `pikvm.snap`         | JPEG screenshot, base64'd. `preview=True` default   |
| `pikvm.type`         | Type a string via PiKVM HID                         |
| `pikvm.key`          | Key combo (modifiers + key) via PiKVM HID           |
| `pikvm.mouse.move`   | Absolute mouse position (PiKVM units -32768..32767) |
| `pikvm.mouse.click`  | Press+release a mouse button                        |
| `pikvm.power`        | ATX action: on / off / off_hard / reset / reset_hard|
| `pikvm.power.status` | ATX state (LEDs, busy, enabled)                     |
| `pikvm.api.get`      | GET any `/api/*` endpoint, decodes JSON/text/binary |
| `pikvm.api.post`     | POST any `/api/*` endpoint                          |

## Deploy

Below assumes PiKVM at `192.168.1.166` and `root:root` SSH credentials.
The PiKVM rootfs is read-only by default; `rw` and `ro` are PiKVM-provided
shortcuts that remount it.

### 1. Install Python deps

```
ssh root@192.168.1.166
rw
pacman -Sy --noconfirm python-pip python-cffi libsodium
# Add swap so pip-building pynacl doesn't OOM on the 389MB-RAM Pi.
fallocate -l 768M /var/swapfile && chmod 600 /var/swapfile
mkswap /var/swapfile && swapon /var/swapfile
mkdir -p /opt/rook-worker
python3 -m venv /opt/rook-worker/venv
/opt/rook-worker/venv/bin/pip install pynacl
```

### 2. Push the source

From your dev host (where this repo lives):

```
cd /path/to/this/repo
tar c rook -C ../.. telesthete | \
  ssh root@192.168.1.166 'mkdir -p /opt/rook-worker/src && tar x -C /opt/rook-worker/src'
```

(`telesthete` here means the Python package from the `Bake-Ware/telesthete`
repo — the worker imports `telesthete.protocol.crypto` and `.framing`.)

### 3. Drop the unit + env

```
scp pikvm/rook-worker.service root@192.168.1.166:/etc/systemd/system/
scp pikvm/rook-worker.env.example root@192.168.1.166:/etc/rook-worker.env
ssh root@192.168.1.166 'chmod 600 /etc/rook-worker.env'
```

Edit `/etc/rook-worker.env` on the PiKVM to set `ROOK_BAND_PSK` and
`PIKVM_PASS` — the file in this repo is a template only.

### 4. Start

```
ssh root@192.168.1.166 'systemctl daemon-reload && systemctl enable --now rook-worker'
ro
```

### 5. Verify on the band

Call `rook_workers` through `mcp.bakeforge.com` — `pikvm-rack1` (or your
configured `ROOK_WORKER_NAME`) should appear with the cap list above.

## Notes

- **HID needs a USB-OTG cable** to the target host. `pikvm.api.get path=/api/hid`
  reports `keyboard.online` and `mouse.online`. If both are `false`, the OTG
  cable is missing, charge-only, or the host isn't enumerating the device —
  API calls will return 200 but keystrokes won't land.
- **Snapshots default to preview** (256x144, ~5KB). Full-res 1280x720 is
  ~80KB which fragments into ~110 UDP packets; band reliability degrades
  past ~50 fragments under loss. Pass `preview: False` to force full-res.
- **Rootfs RO**: any change to `/opt/rook-worker/` or `/etc/` needs `rw`
  first. Run `ro` after to restore the protective mode (PiKVM's standard).

## Files in this folder

- `rook-worker.service` — systemd unit. References `/opt/rook-worker/venv`
  and `/etc/rook-worker.env`.
- `rook-worker.env.example` — env template. Copy to `/etc/rook-worker.env`
  and fill in real secrets.
