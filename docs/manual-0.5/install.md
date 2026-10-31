# Install and first run

Rook 0.5 runs from a checkout of the `castling` branch: there are no 0.5
release packages yet. You need Python 3.11 or newer and git. A first band
is one machine running three things: a **relay** (carries the band's
encrypted traffic), the **hub**, and a **worker**. Add more machines later
([Adding machines](enrollment.md)).

## The quick way: the sidecar installer (Linux)

`deploy/castling-sidecar/install.sh` sets up a relay, a hub and a worker on
this machine under `~/.rook-castling`, as systemd user units, without
touching anything else (it was written to run beside a live 0.4 install).
It needs `uv`, `git` and `curl`.

```sh
deploy/castling-sidecar/install.sh                 # install from origin/castling
deploy/castling-sidecar/install.sh --upgrade       # new code, same data and keys
~/.rook-castling/src/deploy/castling-sidecar/check.sh   # smoke test
```

Your sign-in token is in `~/.rook-castling/secrets/owner.token` and your
recovery key in `~/.rook-castling/secrets/recovery-key.txt`: keep a copy
somewhere safe ([Backups](#backups)). The full guide, with ports, stop/start and uninstall,
is `docs/castling-sidecar.md`.

## By hand

From the top of the checkout (this makes `hub/` and `worker/` folders there):

```sh
pip install -e '.[manifest]'          # the hub validates plugin manifests with jsonschema

# 1. A relay. The small one from the tests listens on 127.0.0.1 only, so it
#    serves this machine's workers; for other machines run Telesthete's
#    relay, telesthete-hub (prebuilt binaries: the Telesthete README).
python tests/e2e/relay.py 7474 &

# 2. The hub: init writes hub/hub.yaml (with a new band key), makes the
#    hub's keys and prints your sign-in token and recovery key, once.
python -m rook.core2.hub --config hub/hub.yaml init --owner me --relay 127.0.0.1:7474
python -m rook.core2.hub --config hub/hub.yaml &

# 3. A worker on this machine, signed in as you, with shell and files.
mkdir -p worker && echo "plugins: [$PWD/rook/plugins/shell, $PWD/rook/plugins/file]" > worker/worker.yaml
python -m rook.core2.worker --config worker/worker.yaml enroll http://127.0.0.1:8740 --name laptop
python -m rook.core2.worker --config worker/worker.yaml &
```

`enroll` shows a short code; approve it as the owner (see
[Adding machines](enrollment.md)). Then open `http://127.0.0.1:8740/` and
paste the token `init` printed ([The hub site](dashboard.md)).

`init` options: `--owner` (your user name, lowercase), `--relay host:port`
or `--ws-url` (the band's relay), `--url https://...` (where other devices
reach this hub, for enrollment) and `--bind` (the HTTP door, default
`127.0.0.1:8740`).

## What to install on the hub

The hub loads every plugin folder in its **library** (`library:` in
`hub.yaml`) at start. Point it at the plugins you want, for example
symlinks into `rook/plugins/`:

- `agents` and `agents-claude-code` or `agents-codex`: the house agent and the
  other ready-made agents ([Agents](agents.md)).
- `tasks`, `projects`, `knowledge`, `concepts`, `whiteboard`: the workbench
  ([The workbench](workbench.md)).
- `notify`, `telegram`, `discord`: reaching people.
- `security`, `security-ml`: the gates ([Security plugins](security.md)).

The library also holds worker plugins: the hub pushes them, signed, to the
workers you choose ([Plugins](plugins.md)).

## Before it leaves this machine

The doors speak plain HTTP. On a LAN your token and the band key would
cross the network in the clear, so put TLS in front (a reverse proxy) before
enrolling another machine or opening the port; `enroll` refuses a plain
`http://` hub off this machine unless you pass `--insecure`. Behind TLS,
set `doors: {http: {web: {secure_cookie: true}}}`.

## Backups

Everything the hub knows is in its `data:` folder (keys, journal, records,
tokens, settings). The hub backs itself up: set a target and install the
nightly job ([Backup](../../rook/core2/hub/backup/docs/manual.md)):

    settings:
      backup: { target: "worker:janitor", keep: 7 }   # or dir:/mnt/usb, rclone:remote:path

Backups are sealed under the recovery key, made once with the owner: only
it restores one (`restore` on a new machine), so save it now.
`python -m rook.core2.hub --config hub/hub.yaml recovery-key` shows it
again while the hub lives.
