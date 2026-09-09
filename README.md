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

### Readable band keys

The permanent PSK is five random lowercase words separated by
hyphens. In the dashboard, open **+ → generate five-word key** to create a new
key, save it for your devices, then click **add** to join that band. **Show key**
lets you read an existing entry while typing. Generation alone changes nothing.

Enter the same key, including hyphens, in Android Settings or a worker's `--psk`
option. Existing PSKs remain valid and case-sensitive; they are never converted
automatically. Replacing a PSK changes its transport routing ID and requires
workers to be reconfigured; the site's persistent band record stays the same.

Each word is independently selected using the OS random generator from a
bundled 7,776-word dictionary ([EFF attribution](rook/remote/psk_words.LICENSE.md)).
Five words give approximately 64.6 bits of entropy, versus 192 bits for the old
generator. This is a manual-entry convenience, not additional authentication:
the current transport still derives its routing ID and encryption key from the
PSK. Google configuration fetching and device certificates protect enrollment
and HTTPS configuration reads; authenticated peer transport is still pending.

### Pairing and key management

Open **Tokens**, select a band, and click **Start pairing**. Its six-character
lowercase alphanumeric code expires after five minutes and rolls while the page
is open. It can provision multiple devices during that window. **Revoke code**
invalidates it immediately, including refreshes from other open tabs. Expired
codes do not disconnect installed workers: the worker keeps the permanent PSK.

The Tokens page supplies ready-to-copy commands, for example:

```sh
curl -fsSL 'https://rook.bakeforge.com/worker?band=jd4ps9' | bash
```

`jd4ps9` is an example, not a working code. A worker needs no interactive login
when supplied with a valid code. Missing, expired, and revoked codes are rejected;
the bare `/worker` URL no longer returns credentials. A valid code grants the
selected band's configuration only, not dashboard or MCP administrator access.
All worker capabilities within that band retain their existing PSK trust model.

**Replace PSK** accepts a new key or generates five words; **Revoke band**
disables enrollment until a replacement PSK is assigned. Either invalidates
outstanding pairing codes. Both controllers reload active credentials every two
seconds and leave the old band. They never send the replacement over the old
band. Re-enroll trusted workers with a fresh code or configure them locally.
Revocation cannot erase credentials on remote devices: peers retaining the old
PSK can still communicate and accept commands on the old band until reconfigured
or taken offline. Device-level revocation needs additional transport enforcement.

The installer and MCP services must share `ROOK_ENROLLMENT_DB` and
`ROOK_SETUP_PATH` with read/write access. By default the database is
`data/enrollment.db`, beside `data/setup.json`. Existing setup/env bands are
imported; the database becomes authoritative for rotations and revocations, so
stale service environments cannot reintroduce retired PSKs. Back up the database
with the site's configuration. Do not delete it to reset pairing codes.

Redemption attempts are limited across processes to five per source address and
20 globally per minute, shared by `/worker` and `POST /enroll`. Forwarded address
headers are not trusted; deployments behind one proxy share its per-address
budget. Application access logs omit query strings. Configure reverse proxies,
tunnels, and analytics to omit the `band` query value as well. Secret responses
use `Cache-Control: no-store`. Legacy APK downloads require dashboard login.
A generic APK can be served publicly only when its exact SHA-256 matches the
server configuration `ROOK_PUBLIC_APK_SHA256`; mismatches fail closed.

### Google, accounts and enrolled workers

Open `/account` to sign in with Google or a local account. Owners manage band
members, invitations, pairing and device revocation there. Connecting accounts
requires proof of both logins; matching email addresses do not merge accounts.
The Google picture is the default avatar, with custom-photo and initials options.

A terminal installer can use a Google or local browser login:

```sh
curl -fsSL 'https://rook.bakeforge.com/worker?login=google' | bash
```

It displays a code to approve in the web app and fetches all authorized band
configurations. Choose the active band. With a pairing code, no account login is
required. Keep the pairing page open and use a fresh code: it must still be valid
when dependency installation finishes and the worker enrolls.

The worker generates its private device key locally and stores its certificate
and configurations in `~/.rook-band-worker/enrollment.json` with mode `0600`.
It starts with `--enrolled`, checks for configuration updates every 30 seconds,
and reconnects when its active band changes. Certificates last 30 days and renew
within seven days of expiry. Known authorization denials stop the worker; network
outages permit at most a one-hour cached lease. Revoking a certificate cannot
stop a hostile device from using a PSK it already knows on the legacy transport.
The compatibility download is separate from the fleet's signed OTA manifest.

Android Settings offers Google login, saved-band selection and pairing fallback.
The generic APK contains no default band PSK. Google web login was tested live;
physical Android sign-in and certificate-backed Android device storage are still
pending. See [deployment status](docs/DEPLOYMENT-enrollment.md).

### Preconfigure a dongle build

Create the usual private `firmware/include/secrets.h` for Wi-Fi/admin settings.
Then fetch band settings with the current pairing code:

```sh
python3 firmware/scripts/dongle-band-config.py \
  --server https://rook.bakeforge.com \
  --udp-hub YOUR-UDP-HUB:7474
```

The helper prompts for the code and writes a private, git-ignored
`firmware/include/band_secrets.h`. Build/flash with PlatformIO normally. The
resulting firmware contains the permanent PSK and must be kept private. The UDP
hub is explicit because the dongle cannot use the worker's WebSocket endpoint.
Saved NVS values override build defaults on an already provisioned dongle; update
its band/hub settings through the local configuration interface when reflashing.

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

### Bands and worker moves

Open **Bands** from the dashboard or visit `/account/bands`. The page lists
bands your account can access. Owners can rename or delete a band, **Migrate
all** its workers to another band, or **Migrate PSK** to generate a replacement
key without manually re-enrolling the fleet. The configured primary band cannot
be deleted. Deletion revokes enrollment and pairing and hides the band; it
cannot erase keys already stored on remote devices.

Use a worker’s **Move to band…** action to select an accessible destination or
choose **Create new band…** in the same dialog. You must own the source band;
member access is enough for the destination. Both bands must use the same hub.
Moves require updated workers advertising `worker.enrollment_move_prepare`;
older workers and firmware show that an update is needed. Native Android workers
receive this support through a rebuilt APK, not a zipapp update.

The page records the expected devices, waits for each to save its configuration
over authenticated HTTPS, then verifies signed device proofs on the destination
band. Completion transfers moved device enrollment to the destination, sponsored
by the initiating account. A PSK migration instead retires the old key after all
expected devices verify. **Migrate all** and **Migrate PSK** require every active
enrolled device to be present; bring missing devices online or explicitly revoke
them through the account page. An empty band’s PSK can be replaced immediately.

Keep the Bands page open while migrating. Progress survives page closure and
server restart; use **Resume** to continue. A prepared migration can be canceled.
Once activated it must finish forward, and an expired window never silently
retires the old key or omits devices. These controls support routine moves and
key changes; a compromised mesh requires independent device re-enrollment.

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

For the `worker` or `both` targets, supply `?band=CODE` on the installer URL or
enter the current code when prompted. Unattended worker installs require the
code in the URL (or `ROOK_JOIN_CODE` in the installer's environment). Direct
Windows worker installation uses
`iex (irm "https://<your-host>/worker?band=CODE&os=windows")`.

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

### Running tests

Keep the [Telesthete checkout](https://github.com/Bake-Ware/telesthete) at
`../telesthete`; `tests/conftest.py` imports its protocol package, matching the
worker and Android bundle inputs. With Python 3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/pytest -q
```
