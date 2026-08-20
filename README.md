# Rook

**A self-updating mesh of worker agents you drive from a web dashboard, a terminal control panel, or via MCP — over an encrypted peer-to-peer band.**

![Rook dashboard](docs/img/dashboard.png)

## What is this?

Rook lets you **run things on all your machines from one place**. You install a tiny background program on each computer you want to reach — a home server, a Raspberry Pi, a gaming PC, a cloud box, a phone, even a small USB dongle — and they all quietly link up over an encrypted connection. From then on you get one live view of every machine and can tell any of them to do something — run a command, grab a screenshot or a webcam photo, restart a service, manage downloads, type on another computer, send a message — and get the answer back right away.

It's **end-to-end encrypted**, the machines **update themselves** (so you never patch each one by hand), and you can **remove a machine from the group with one click**.

## What it's for

- **One control panel for all your machines** — instead of juggling a dozen SSH sessions and browser tabs.
- **Reaching machines you normally can't** — behind a home router, on another network, or on the road — because they dial *out* to a shared meeting point instead of you dialing *in*.
- **Doing the boring stuff everywhere at once** — updates, restarts, and health checks across the whole fleet.
- **Letting an AI assistant help run your machines**, through the same safe controls you use.

## Good use cases

- **Homelab / self-hosting** — watch and control your Pi-hole, NAS, database, media box, etc. from one dashboard, and push an update to all of them at once.
- **Remote help** — hop onto a family member's or a remote office machine to run a fix or grab a screenshot, with nothing to set up on their end.
- **Downloads on the go** — check and manage torrents on your media server from your phone.
- **Keyboard/mouse over the network** — a cheap USB dongle plugged into a machine lets you type into it or send key combos remotely, even during boot/BIOS where normal remote tools can't reach.
- **A quick line to your machines** — ping a box (or the person sitting at it) and get a reply, right from the dashboard or terminal.
- **AI-run operations** — let an assistant list your machines and carry out tasks on them through a controlled interface.

---

## Table of contents

- [What is this?](#what-is-this) · [what it's for](#what-its-for) · [use cases](#good-use-cases)
- [The band](#the-band)
- [Capabilities](#capabilities)
- [Integrations](#integrations) — Deluge · PiKVM · hermes · Claude Code · microcontroller HID/serial
- [Control planes](#control-planes) — [dashboard](#web-dashboard) · [`rook band` TUI](#rook-band--terminal-control-panel) · [chat](#chat--messaging) · [MCP](#mcp)
- [OTA self-update](#ota-self-update)
- [Security](#security)
- [Install](#install)
- [Repository layout](#repository-layout)

---

## The band

A **band** is a group of workers that share one pre-shared key (PSK). Everything rides on [**telesthete**](https://github.com/Bake-Ware/telesthete), a small encrypted transport:

- **Membership by PSK.** `band_id = SHA256(PSK)[:16]` is the cleartext routing label; the AEAD key is derived from the PSK (ChaCha20-Poly1305). Knowing the PSK = being on the band.
- **Hub-and-spoke over a blind relay.** Workers connect out (UDP on the LAN, or WebSocket through a Cloudflare tunnel for anything remote) to a **hub** that relays band traffic by `band_id` — it holds no key and can't read the payloads.
- **Workers announce themselves** every ~30s with their name, version, plugins, and capabilities. The control plane keeps a live roster; a worker that goes quiet ages off in ~90s.
- **Stable identity.** Each worker persists a `worker_id` across restarts, so it keeps one row in the dashboard and can be durably addressed.

```
  browser  ─┐                             ┌─ worker: gateway   (shell · file · info)
  rook band ─┼─ control plane ─ hub ──────┼─ worker: media     (deluge · screenshot · hid)
  MCP       ─┘   (relay, no keys)         ├─ worker: db-host    ┐ Proxmox host, one worker
                                          ├─ worker: db-01      ┤ per LXC (db · cache · dns)
                                          └─ worker: kvm-dongle  ESP32 firmware (HID/KVM)
```

## Capabilities

A worker's abilities are **plugins** that register dot-namespaced **capabilities**. A plugin only loads where it can actually function (`available()` gating), so a worker never advertises a cap it can't fulfill — a headless box won't offer `screenshot.*`, a box without deluge won't offer `deluge.*`.

Built-in plugins:

| Namespace | What it does |
|---|---|
| `shell.*` | run commands, `which`, env |
| `file.*` | read / write / list / search (base64 for binaries) |
| `info.*` | host, uptime, ping |
| `screenshot.*` | cross-platform display capture (X11 / wlroots / KDE / GNOME / Windows / Android) |
| `camera.*` | grab a still photo from a webcam / capture device (`list` + `snap` by camera) |
| `hid.*` | type / key-combo / mouse on the local display |
| `chat.* · msg.*` | two-way chat and one-way desktop notifications |
| `worker.*` | `restart`, `reconfigure`, signed `apply`/`deauth`, `hold`, runtime `plugin.enable/disable` |
| `caps.describe` | introspect every cap's args (powers the call forms) |

**Custom command-caps.** Define your own cap that runs a shell command with parameter substitution — e.g. `cmd.deploy` → `systemctl restart {svc}` — persisted per-worker and re-registered on boot. Argument *values* are shell-escaped, so a caller can't break out of the template.

## Integrations

On top of the generic caps, Rook ships purpose-built integrations for specific apps and hardware. Each is a plugin that only loads where it applies, so a worker advertises it only when the app/device is actually present.

- **Deluge** — `deluge.*`: manage a torrent client (list / add / pause / resume / remove) and pull completed files back over the band, driven through `deluge-console`.
- **PiKVM** — `pikvm.*`: control a [PiKVM](https://pikvm.org) through its REST API — snapshot the captured screen, send keyboard/mouse, ATX power actions, or hit any `/api/*` endpoint as a passthrough.
- **hermes** — `hermes.*`: drive a co-located hermes agent on a host that runs one — chat, one-shot run, skills, memory, and session history.
- **Claude Code** — `claude-history.*`: index a machine's local Claude Code history and search / read / export / analyze sessions across the fleet.
- **Microcontroller as HID / serial** — the ESP32 T-Dongle-S3 firmware turns a cheap dongle into a remote input device: USB-HID (`kvm.*`) and Bluetooth-HID (`bthid.*`) keystrokes and consumer keys into a target machine, plus a serial passthrough (`serial.*`). It speaks telesthete over UDP directly — no host agent required.

## Control planes

Drive the same band three ways — they all read from the same roster and invoke the same caps.

### Web dashboard

A band-first control panel: live worker list, version-spread and heartbeat visualizations, click-to-expand capabilities, run any cap from a form, an in-browser shell, token/install/APK pages, and one-click **deauth/ban**. Fully responsive.

<p>
  <img src="docs/img/dashboard-mobile.png" alt="Rook dashboard on mobile" width="300">
</p>

### `rook band` — terminal control panel

A btop-inspired, zero-dependency curses TUI (pure stdlib). Framed panels: a worker list on the left, a live **detail pane** for the selected worker on the right — arrow into it to browse capabilities as a tree and call one — and a **chats** panel. Run caps, toggle plugins, define custom caps, message workers, deauth/ban.

![rook band TUI](docs/img/tui.png)

Install it (see [Install](#install)) — the installer pulls in `python3` if it's missing:

```sh
curl -fsSL https://<your-host>/install | bash -s -- cli
# then just: rook
```

### Chat & messaging

Two flavors, both worker-gated:

- **notify** — a one-way desktop toast (`notify-send`) plus an inbox on the target.
- **chat** — a proper two-way conversation. Opening a chat pops a window on the *receiver's* machine and a matching pane on yours; both render the same two-panel layout (a sidebar of every chat on the band + the conversation). Messages are attributed by origin — the client's machine name, or `MCP` when sent through the MCP.

![rook chat](docs/img/chat.png)

### MCP

Expose the band as MCP tools:

```
rook_workers()                       # live roster
rook_caps()                          # every capability seen on the band
rook_call(cap, args, worker_id?)     # invoke a capability, get the reply
```

## OTA self-update

The Python worker fleet updates itself with a **signed-manifest + in-band push** system:

- Every build stamps a monotonic build number and emits an **ed25519-signed** manifest (`{build, sha256, url, sig}`) next to the bundle.
- The controller watches each worker's announced build and pushes `worker.apply(manifest)` to any worker that's behind. Workers **verify signature + sha256 + a `--selftest`** before swapping (fail-closed), keep the previous bundle for **rollback**, and restart kill-safely (systemd / runit / `os.execv`).
- `worker.hold` pins a node; `worker.check(force=true)` drives canary rollouts. The **dongle** (ESP32 firmware) is excluded and has its own signed flash path.

Ship a build → commit → rebuild the signed bundle → the running push loop converges the whole fleet in minutes, no manual per-device steps.

## Security

- **Signed control.** `worker.apply` and `worker.deauth` act only on an ed25519-signed payload, so *being on the band is not enough* to update or evict a worker — only the controller's signing key can. Deauth parks a worker off-band (persisted, survives reboots) and the controller denylists it (hidden, no pushes, calls refused).
- **AEAD hardening.** The per-session nonce counter is seeded from a CSPRNG to prevent cross-peer / cross-restart nonce reuse under the shared band key.
- **Roadmap.** Per-worker identity (to evict *hostile* nodes, not just cooperative ones), PSK rotation tooling, and signed firmware OTA are the next hardening steps.

> The band PSK, signing key, and dashboard credentials live only on your hosts — never in the repo. The install commands here use `<your-host>` placeholders.

## Install

One installer, selectable target — a **worker** (a controlled node), the **`rook band` CLI** (the controller), or **both**:

```sh
# interactive — asks what to install
curl -fsSL https://<your-host>/install | bash

# unattended — pass the target (worker | cli | both)
curl -fsSL https://<your-host>/install | bash -s -- worker
curl -fsSL https://<your-host>/install | bash -s -- cli
curl -fsSL https://<your-host>/install | bash -s -- both
```

The **worker** installs as a background service and joins the band. The **CLI** installs the single `rook` command (pulling in `python3` via the system package manager if it's missing) — for a fully unattended CLI install set `ROOK_WEB_PASS` (and optionally `ROOK_WEB_USER`) so it doesn't prompt. Windows (PowerShell) and a native Android worker APK are served from the same host (`/worker.py`, `/apk`).

### Run your own hub

The **hub** is the band relay — a dumb `band_id` forwarder that holds no keys. Stand one up from the same host with a short wizard that populates the vars, installs a hardened systemd unit, and starts it:

```sh
# interactive — prompts for bind address, TTL, prune interval, log level, user
curl -fsSL https://<your-host>/hub | bash

# unattended — take defaults (override any var via the environment)
curl -fsSL https://<your-host>/hub | bash -s -- --yes
HUB_BIND=0.0.0.0:7474 curl -fsSL https://<your-host>/hub | bash -s -- --yes
```

It fetches a prebuilt binary for the host's architecture when one is available and otherwise builds from source with `cargo` — auto-installing `git`, a Rust toolchain, and a C linker through the system package manager as needed. Tune a running hub by editing `/etc/telesthete-hub.env` and `systemctl restart telesthete-hub`; point workers at it with `--hub <hub-host>:7474`.

## Repository layout

```
rook/
  worker/          band worker: core, transports (telesthete), plugins, OTA self-update
    plugins/       shell, file, info, screenshot, camera, hid, pikvm, deluge, chat, msg, …
  band_mcp/        band client + the MCP server (rook_workers/caps/call)
  remote/          installer / controller (dashboard API, OTA build + push, deauth)
  web/             the dashboard (index.html)
  cli/             band_tui.py — the `rook band` terminal control panel
firmware/          ESP32 T-Dongle-S3 firmware (telesthete over UDP, BLE/USB HID)
docs/img/          screenshots
```
