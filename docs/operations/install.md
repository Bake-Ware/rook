# Installing a hub

Three ways to run a hub, from least to most hands-on:

1. **One-command installer** (`scripts/install-hub.sh`): a hub as systemd services on a
   Linux machine. Recommended.
2. **Docker**: `ghcr.io/bake-ware/rook` with `compose.yaml`.
3. **From source**: a clone, a virtualenv and `scripts/local-hub.sh` in the foreground.
   For development; see the [README](../../README.md#c-from-source).

All three run the same three processes: the relay (`telesthete-hub`, UDP 7474), the
dashboard (`rook-dashboard`, TCP 7005) and the MCP server (`rook-mcp`, TCP 8765), with all
state in one data directory (`ROOK_DATA_DIR`).

*Roadmap:* a desktop app, a single binary that is a worker and can also install a hub on
the machine it runs on, using this installer's steps.

## The one-command installer

```sh
curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh | bash
```

Options go after `bash -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh \
  | bash -s -- --user --version v0.2.0 --yes
```

| Option | Default | Meaning |
|---|---|---|
| `--system` | when root or `sudo` is available | System services, a dedicated `rook` user, `/opt/rook`, data in `/var/lib/rook` |
| `--user` | otherwise | Per-user services (`systemctl --user`), everything under `~/.local/share/rook-hub`; no root needed once the prerequisites are installed |
| `--version TAG` | latest release | Rook release to install, e.g. `v0.2.0` |
| `--relay-version TAG` | the release's | telesthete release for the relay (the Rook release records the one it was tested with) |
| `--data-dir DIR` | see above | Hub state: keys, databases, logs |
| `--bind ADDR` | `0.0.0.0` | Address all three listen on. `127.0.0.1` keeps the hub to this machine |
| `--public-host HOST` | this machine's LAN IP | The address browsers and workers use; written into installer links |
| `--dashboard-port`, `--mcp-port`, `--relay-port` | 7005, 8765, 7474 | |
| `--yes`, `-y` | | Don't ask for confirmation (it never asks without a terminal) |
| `--uninstall` | | Stop and remove the services and the program; keep the data |
| `--purge` | | With `--uninstall`: delete the data directory too (band keys, history) |
| `--no-systemd` | | Start the processes in the background instead (containers, CI); see below |
| `--relay-bin PATH` | | Install this `telesthete-hub` instead of downloading one |
| `--package SPEC` | | pip requirement for Rook instead of the release wheel (a wheel file or URL, a source directory) |
| `--git-ref REF` | | Install Rook from git at `REF` (installs `git`) |
| `--python PATH` | first Python 3.11+ found | Interpreter for the virtualenv |

`ROOK_REPO`, `TELESTHETE_REPO` (`owner/name`) and `ROOK_GITHUB` (base URL) point it at a
fork or a mirror.

### What it does

1. Detects the distribution (apt, dnf, pacman or zypper) and architecture (x86_64,
   aarch64; armv7 best effort), and installs what is missing of `python3` (3.11+) with
   `venv`, `curl` and `ca-certificates`. It does not install `git` or build tools unless
   it has to fall back to building from source.
2. Creates the `rook` system user (`--system`), the program directory and the data
   directory (mode 700).
3. Creates a virtualenv and installs Rook from the release wheel
   (`https://github.com/Bake-Ware/rook/releases/download/<tag>/rook-<version>-py3-none-any.whl`).
   The wheel is checked against the release's `SHA256SUMS`, and it pins its transport,
   `telesthete`, as a wheel on the same release with its sha256, so pip needs nothing
   but HTTPS. Only when the repository has no release at all does it install from
   `master` with `git` instead (and says so); if a release exists but can't be
   downloaded, it stops rather than install something else.
4. Downloads the relay, `telesthete-hub-<arch>-linux.tar.gz` from the telesthete release
   Rook pins (`RELAY` in `rook/_release.py`), and checks it against the sha256 Rook
   pins for that archive (`RELAY_SHA256`), not against a checksum file published next
   to it; only a version the pin doesn't cover falls back to the release's `.sha256`,
   with a warning. With no
   prebuilt for this machine, it builds the relay with an existing `cargo`, or stops and
   explains how to get one (`--relay-bin` takes a binary you built).
5. Writes `hub.env` (settings) and `secrets.env` (`ROOK_BAND_PSK`, `ROOK_WEB_PASS`,
   `ROOK_MCP_STATIC_TOKEN`) into the data directory, mode 600, owned by the service user.
   Secrets are generated once and never rewritten.
6. Writes and starts the systemd units, waits until the dashboard and MCP server answer
   their health checks (90 s each), and prints the dashboard URL, the password and the
   next step: open the URL, sign in (any username), **Install a worker**, **Get a
   pairing code**.

### Files

| | `--system` | `--user` |
|---|---|---|
| Program | `/opt/rook/venv`, `/opt/rook/bin/telesthete-hub` | `~/.local/share/rook-hub/venv`, `…/bin/telesthete-hub` |
| Data | `/var/lib/rook` | `~/.local/share/rook-hub/data` |
| Settings | `/var/lib/rook/hub.env` | `…/data/hub.env` |
| Secrets | `/var/lib/rook/secrets.env` | `…/data/secrets.env` |
| Units | `/etc/systemd/system/rook-{relay,dashboard,mcp}.service` | `~/.config/systemd/user/rook-*.service` |
| Install record | `/opt/rook/install.env` | `~/.local/share/rook-hub/install.env` |

Change a setting by editing `hub.env` and restarting the services
(`sudo systemctl restart rook-dashboard rook-mcp`, or `systemctl --user …`). Every
variable is listed in [settings-reference.md](settings-reference.md). For a public hub
behind HTTPS set `ROOK_DOMAIN`, `ROOK_HUB_PUBLIC`, `ROOK_MCP_PUBLIC_URL` and
`ROOK_ALLOWED_HOSTS` there as the [README](../../README.md#going-beyond-localhost)
describes.

### Service layout

Today the hub is three units: `rook-relay`, `rook-dashboard` (After/Wants the relay) and
`rook-mcp`. When the dashboard and MCP server merge into one process (`rook hub serve`),
the installer finds that entry point in the installed Rook and writes `rook-relay` and
`rook-hub` instead, removing the old two; nothing else changes. The layout in use is in
the install record (`LAYOUT=`).

System units run as `rook` with `NoNewPrivileges`, `PrivateTmp` and `ProtectSystem=full`;
the relay, which holds no keys, is sandboxed further (`ProtectSystem=strict`, no
capabilities, no home directories).

### Upgrading

Run the same command again. It installs the newest release (or `--version`) over the
existing virtualenv, updates the relay if the release names a newer one, rewrites the
units and restarts the services. `hub.env` is kept as you left it; only options you pass
on that run (`--bind`, ports, `--public-host`) change their keys. `secrets.env` is never
touched.

### Uninstalling

```sh
curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/master/scripts/install-hub.sh | bash -s -- --uninstall
```

stops and disables the units, deletes them, the virtualenv and the relay, and keeps the
data directory: reinstalling picks up the same band keys, password and history. Add
`--purge` to delete the data as well (and, for `--system`, the `rook` user); it asks
first, and without a terminal it needs `--yes`. It only deletes a directory that holds
the hub's `hub.env` or `secrets.env` (or is empty), never `/` or your home. `--user`
uninstalls never use `sudo`. Workers you enrolled keep trying to reach the hub until
you deauthorize or uninstall them.

### Without systemd (containers, CI)

`--no-systemd` runs the same commands as background processes: logs in
`<data>/logs/<service>.log`, pid files in `<data>/run/`. Nothing restarts them after a
crash or a reboot; re-run the installer to start them again. As root with `--system`,
they run as the `rook` user. CI uses this mode (`.github/workflows/installer.yml`).

### Troubleshooting

- **"Rook needs Python 3.11 or newer"**: Debian 11 and Ubuntu 22.04 ship 3.9/3.10. Use
  Debian 12+, Ubuntu 24.04+, Fedora or Arch, install a newer Python and pass
  `--python /path/to/python3.12`, or use Docker.
- **"no systemd user session"** with `--user`: `su` and `sudo -u` sessions have no user
  manager. Log in as that user directly (SSH or console), or use `--system`.
- **User services stop when you log out**: run `sudo loginctl enable-linger $USER` once
  (the installer reminds you).
- **A service "did not become healthy"**: the installer prints its last log lines. More:
  `sudo journalctl -u rook-dashboard -n 100` (or `journalctl --user -u …`). A port already
  in use is the usual cause: pick others with `--dashboard-port` / `--mcp-port` /
  `--relay-port`.
- **Other machines can't open the dashboard**: allow TCP 7005 and 8765 (and UDP 7474 for
  workers started by hand with `--hub <ip>:7474`) through the firewall, e.g.
  `sudo ufw allow 7005,8765/tcp`. Check the printed URL uses an address they can reach;
  fix it with `--public-host`.
- **"this machine has a public address"**: with the default `--bind 0.0.0.0` on a
  machine with a non-private IP (a VPS), the dashboard and MCP server are reachable from
  the internet over plain HTTP. Firewall them, or use `--bind 127.0.0.1` behind an HTTPS
  proxy as the README's "Workers anywhere" describes.
- **Downloads fail behind a proxy**: the installer uses `curl` and `pip`, which honour
  `HTTPS_PROXY`; with `sudo`, pass it through (`sudo -E`) or run as root.
- **No prebuilt relay** for your architecture: install Rust (`https://rustup.rs`) and a C
  compiler and re-run, or build `telesthete-hub` elsewhere and pass `--relay-bin`.
- **Starting over**: `--uninstall --purge`, then install again. Enrolled workers then
  need new pairing codes.

## Docker

`compose.yaml` runs `ghcr.io/bake-ware/rook:${ROOK_VERSION:-latest}` (linux/amd64 and
linux/arm64) as three services sharing the `rook-data` volume; see the
[README](../../README.md#b-docker) for the `.env` it needs. The dashboard reaches the
MCP server's account APIs at `http://mcp:8765` (the `ROOK_*_ADMIN_URL` variables in
`compose.yaml`). The image contains the prebuilt relay pinned in `rook/_release.py`,
checked against its pinned sha256, and records the version in the image label
`io.github.bake-ware.telesthete.relay`. Base images are pinned by digest
(`PYTHON_IMAGE`, `RUST_IMAGE` build args).

Building it yourself:

```sh
docker compose -f compose.yaml -f compose.build.yaml up -d --build
docker build --build-arg TELESTHETE_RELAY_VERSION=v0.4.0 -t rook-hub .     # another relay (checked against its .sha256)
docker build --build-arg RELAY_SOURCE=source -t rook-hub .                 # compile the relay
```

`RELAY_SOURCE=source` compiles `telesthete-hub` at `TELESTHETE_REV` (the commit
`pyproject.toml` pins) instead of downloading it: minutes, not seconds, but it works
before a relay release exists or for an unreleased relay change.

## Releases and versions

A release is a `vX.Y.Z` tag; `.github/workflows/release.yml` does the rest:

- the **package version** comes from the tag (`v0.2.0` → `0.2.0`, `v0.3.0-rc1` →
  `0.3.0rc1`, a prerelease): the wheel, the GitHub Release and the Docker tag;
- the **build name** (`<build>.<adjective>.<noun>`, from `git rev-list --count` and the
  commit) is unchanged and still drives OTA; the wheel records it in `rook/_release.py`,
  so worker bundles built by a hub installed from a wheel carry the release's build
  number and update like bundles built from a clone.

The workflow:

1. verifies the pinned relay release (telesthete's build provenance on every archive,
   `gh attestation verify`) and records or checks its archives' sha256 in
   `rook/_release.py`;
2. builds the sdist and wheel (plus the pinned telesthete wheel) and publishes them with
   `SHA256SUMS` as a GitHub **prerelease**, with build provenance attestations;
3. pushes `ghcr.io/bake-ware/rook:<version>` for amd64 and arm64 (attested), and runs
   the installer against the published release;
4. only when both passed, and only for a final version, marks the release latest (what
   the installer and `releases/latest` resolve) and tags the image `:latest`.

The relay is pinned, not "latest": to move to a new telesthete release, cut that first,
then `python tools/release_prep.py --pin-relay vX.Y.Z` (records `RELAY`,
`RELAY_SHA256` and the Dockerfile default) and commit. Verify any download with
`gh attestation verify <file> -R Bake-Ware/rook`.

```sh
git tag -a v0.2.0 -m "Rook 0.2.0" && git push origin v0.2.0
```

**After the first release:** GitHub creates the `ghcr.io/bake-ware/rook` package
private on its first push. Make it public once (github.com/orgs/Bake-Ware/packages →
rook → Package settings → Change visibility) so `docker compose pull` works without a
login.
