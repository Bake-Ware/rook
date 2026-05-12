# Phase 2 — Firmware port to real Telesthete

Today's deliverable (MCP server against the live `v0.2.0` HTTP/WS-JSON API) is
deliberately scoped to *not* touch the firmware. The dongle at
`192.168.1.138` is running and the existing API meets the demo. The firmware
rework is the next concrete chunk of work and these are the notes I'd want
when starting it.

## Goal

Replace the URL-aliased pseudo-Telesthete (`/telesthete/stream` JSON-text
WebSocket; `/telesthete/drop/*` HTTP file routes) with the real Telesthete
wire protocol, per §12.1 of `telesthete/SPEC.md` — the WebSocket + Stream +
Control minimal-client profile, ~300 LOC target.

## Prerequisites the next session needs

1. **PlatformIO toolchain.** Not currently installed on this machine.
   - `pip install platformio` (pulls toolchain on first build)
   - First `pio run` will download the ESP32-S3 framework (~500 MB)

2. **`firmware/include/secrets.h`.** Gitignored; not present locally. Copy
   from `secrets.h.example`, fill in WiFi SSID + password matching the
   network the dongle is currently on (it's already associated at
   192.168.1.138 — same network).

3. **Decision: keep HTTP routes during transition, or all-in on Telesthete?**
   The MCP server today talks HTTP + JSON WebSocket. If we want zero-downtime
   migration, the firmware should ship both protocols in parallel for one
   release. If we're OK with a flag day, gut the HTTP routes and only ship
   Telesthete framed binary on the WebSocket endpoint.

## Crypto question (deferred, per user's "skip crypto for first run")

The spec mandates XChaCha20-Poly1305 (24-byte nonce). ESP-IDF's mbedTLS
ships plain ChaCha20-Poly1305 (12-byte nonce), **not** the X variant.

Options when crypto goes in:

- **libsodium port for ESP32** — there are existing Arduino/PlatformIO
  libraries (e.g. `RobTillaart/Crypto`, `ESPSodium`); audit one for
  correctness against the spec's HKDF-SHA256 + XChaCha20-Poly1305 with
  16-byte AAD-included tag.
- **Implement HChaCha20 manually** on top of mbedTLS's ChaCha20, which lets
  you derive an XChaCha20 nonce/key pair. Small wrapper, ~50 LOC of C.
- **Negotiate a local-trust profile per spec §3.4** — for LAN-only operation
  with a "telesthete-local" PSK. Same wire format, same AEAD, same code
  path, no security guarantees. The spec explicitly authorizes this and
  notes XChaCha20-Poly1305 of a 32-byte descriptor is ≤1 µs on a Cortex-A53,
  so there's no perf justification for a separate plaintext lane.

For first port: pick the simplest one that meets the spec on the wire even
if it's a vendored implementation, since the wire format is the
interoperability contract.

## Committed design decisions (2026-05-11)

### WiFi: permanent APSTA

`wifi_setup.cpp` becomes `WiFi.mode(WIFI_AP_STA)` with both `softAP()` and
`begin()` always called. Single radio means both interfaces share STA's
channel — accepted tradeoff. AP is always reachable as fallback; STA gives
LAN access at the assigned IP. No runtime WiFi-mode switching.

### Button → storage mode toggle

The board's physical button (GPIO 0) is unused today. Reassigning to toggle
SD card ownership between firmware-internal and USB Mass Storage.

| State | SD card owned by | HID/CDC | `/telesthete/drop` HTTP | LCD |
|-------|------------------|---------|--------------------------|-----|
| `internal` (default) | Firmware (`SD_MMC`) | ✓ | works | `Storage: Internal` |
| `msc` | Host (`USBMSC` block device) | ✓ | 503 `card_in_msc` | `Storage: USB` |

Transitions:

- `internal → msc`: `SD_MMC.end()` → init `USBMSC` with raw block backend.
- `msc → internal`: `USBMSC.end()` → 500 ms grace period → `SD_MMC.begin()`.

Button behavior: short press toggles. ~50 ms debounce. Brief LCD flash on
accept. HID and CDC USB endpoints stay live across both states (separate
endpoints from MSC).

Force-takeover policy on `msc → internal`: do not wait indefinitely for
host `MSC_STOP_UNIT`. Unflushed host writes are lost — acceptable for a
scratch volume.

File transfer marker protocol (`<<<ROOK_FILE:>>>` over CDC) must check
mode before writing; when `msc`, reject + log + drop the chunk rather
than buffer in RAM.

### New endpoints + MCP tools

Firmware:

```
GET  /mode                       → {"mode":"internal"|"msc"}
POST /mode {"mode":"msc"}        → {"mode":"msc"}            // switches
GET  /status                     → existing fields + storage_mode
```

MCP server (`server/rook_kvm/server.py`):

```python
@mcp.tool()
async def get_device_mode() -> str:
    """Return current SD-card ownership mode: 'internal' or 'msc'."""

@mcp.tool()
async def set_device_mode(mode: str) -> str:
    """Switch storage mode. `mode` is 'internal' or 'msc'.
    Caveat: in 'msc' mode the host PC owns the card; firmware file
    routes return 503 until switched back."""
```

`bridge.py` adds matching `get_mode()` / `set_mode(mode)` wrappers.

### Open: HID kill-switch (defer or bundle?)

Same mode-system shape suggests adding a `hid.enabled` toggle on the
same PR — safety control for when an LLM is exploring on the target and
runaway keystrokes would hurt. MCP: `get_hid_enabled()` / `set_hid_enabled(bool)`.
Decision pending — bundled or separate work item.

## Functional parity contract

The current firmware exposes these capabilities — the rework must keep them
working (over Telesthete Channel/Stream messages instead of HTTP/JSON):

| Capability | Today | After port |
|------------|-------|-----------|
| Device status (board info) | `GET /status` (HTTP) | Control msg `meta.describe` / `meta.status` |
| Type text via HID | `POST /type {text,delay_ms}` | Channel msg, skill `device.usb.hid.type` |
| Key combo via HID | `POST /key {modifiers,key}` | Channel msg, skill `device.usb.hid.key` |
| Read CDC serial buffer | `GET /serial` | Channel msg, skill `device.usb.cdc.read` |
| Write to CDC serial | `POST /serial {data}` | Channel msg, skill `device.usb.cdc.write` |
| Clear CDC buffer | `POST /serial/clear` | Channel msg, skill `device.usb.cdc.clear` |
| Real-time serial duplex | `WS /telesthete/stream` (JSON text) | Stream channel, both directions |
| File transfer marker | `<<<ROOK_FILE:...>>>` over CDC + WS notify | Keep marker protocol; firmware-side decode unchanged. Notify peer via Stream `file_ready` |
| List/get/delete TF files | `GET/DELETE /telesthete/drop/*` | Channel msg, skill `device.tf.list` / `device.tf.read` / `device.tf.delete` |
| Local LCD display | (firmware-internal) | Unchanged |

Drop channel (Telesthete spec 0x04) is future. Don't try to rebuild the
file-transfer marker protocol on top of Drop yet; keep it as is, send
notifications over Stream/Control.

## Skill advertisement

Peer announces its skill set on HELLO. Proposed for the dongle:

```
device.usb.hid.type
device.usb.hid.key
device.usb.cdc.read
device.usb.cdc.write
device.usb.cdc.clear
device.usb.cdc.stream     # real-time duplex
device.tf.list
device.tf.read
device.tf.delete
meta.describe
meta.status
```

`hostname` in HELLO can be `rookdongle-<friendly_name>` matching the user's
preferred convention (current device reports `rook-kvm` — should change to
something like `rookdongle-tainted_monkey` per the convention discussion).

## Module-level changes vs. today

- `firmware/src/ws.cpp` — biggest rewrite. Stop parsing JSON, parse Telesthete
  27-byte framed binary frames; dispatch by `channel_type` (Control/Stream)
  and `channel_id`. Build a small frame writer for outbound messages.
- `firmware/src/http_routes.cpp` — either delete (flag-day) or wrap the
  existing handlers behind Channel messages and keep HTTP as a compatibility
  shim for the transition. Note: the current `server.onNotFound()`
  dispatcher hack (DEVLOG gotcha #5) goes away with HTTP — Telesthete dispatch
  is by channel ID, no path-prefix matching.
- `firmware/src/serial_buf.cpp` — file-transfer state machine stays. Hook
  its `file_ready` notification path into a Stream-channel message instead
  of the current JSON WS push.
- New: `firmware/src/telesthete.cpp/h` — frame parse/build, AEAD glue (or
  no-op for first port).
- `firmware/platformio.ini` — add the chosen crypto library (`ESPSodium`
  candidate); leave `ArduinoJson` in for now since `meta.describe` payloads
  can stay JSON inside Stream `data`.

## Test path

- Smoke test on bench: cycle peer registration, send Stream HID, verify
  keystrokes land.
- Round-trip CDC serial via the MCP path same as today's `validate_dongle.py`,
  just calling Channel ops instead of HTTP.
- File transfer round-trip with a small ramdisk file (no Linux target
  needed; can pipe a known payload via `write_serial` skill into the marker
  protocol).
