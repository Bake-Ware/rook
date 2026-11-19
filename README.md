# Rook

Rook core, rebuilt from scratch. `PLAN.md` is the locked plan and the only source of what gets built on this branch.

The earlier code (0.4 on `master`, the 0.5 rework on `castling`) is reference only. Nothing is carried forward from it.

## Layout

- `docs/telesthete-surface.md`: what the pinned Telesthete offers Rook, and where it disagrees with the plan.
- `docs/protocol.md`: the Rook protocol, v1 (W1).
- `protocol/`: the protocol library, one per language (U1). `vectors.json` is shared by every language; `python/` is the Python library.
- `hub/`: the hub, in Python.
- `core/python/`: the worker core written in Python (U1), and its start command (`rook_start.py`).
- `deploy/install/install.sh`: the installer: a hub and a worker as systemd user units, in one command.
- `tests/`: the tests for all of the above.

## Tests

Python 3.11 or newer. From a fresh clone:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

`requirements.txt` installs Telesthete from GitHub at the commit Rook pins (`docs/link-design.md`, section 8), so the install needs `git` and network access.

## Installing with the installer

On a Linux machine with systemd, curl and git, one command sets up a hub and a worker for this machine, each as a systemd user unit, and opens the hub's site:

```sh
curl -fsSL https://raw.githubusercontent.com/Bake-Ware/rook/house-agent/deploy/install/install.sh | bash
```

or `deploy/install/install.sh` from Rook's source, which installs that tree: a checkout's commit, or a copied folder as it is. Only the curl form downloads. It asks what you use for AI now (Claude Code is set up, signed in and tested; the others are coming soon), puts uv's Python 3.12 and Rook in `~/.rook`, starts the hub on port 8740 on every interface, joins this machine's worker with a join token, and opens the site. `--dir`, `--name` and `--http-port` let it run beside other installs; `--open` opens the site again; `--uninstall` stops it and removes its units and its folder, and nothing else. `--help` lists every option. What a curl install fetches is the `REF=` line near the top of the script: the `house-agent` branch for now.

## Starting a hub

From the clone, after the first two commands above:

```sh
PYTHONPATH=protocol/python:hub .venv/bin/python -m rook_hub --data ~/rook-hub --vault-key ~/rook-vault.key
```

- `--data` is the hub's data directory. It is made on the first start, readable by you only, and so is everything the hub writes in it. If an older start left anything there readable by other users, the hub makes it owner-only as it starts and says so in a `tightened:` line. What another user owns there it cannot change: it leaves it, says how many and the first path in a `could not tighten` line, and serves as usual.
- `--vault-key` is the vault key file. It must be outside the data directory. On the first start the hub makes a random key there, readable by you only, and reads it on every start after. Back it up apart from the data: if it is lost, every secret in the vault is lost with it. If the file is missing after the vault was keyed, the hub starts with the vault locked and makes no new key.
- `--host` and `--port` say where to listen: 127.0.0.1 and 8700 unless given.
- Everything is served at that one address: the site, the MCP door at `/mcp`, and for workers `POST /knock`, `POST /link` and `GET /band`, the WebSocket every worker's link runs over (Telesthete; `docs/link-design.md`). The hub opens no other port, UDP included. Behind HTTPS, the proxy passes `/band` upgrades through with the rest.

Open the address it prints. While the hub has no user, the site shows a setup page: choose a name and a password for the first user, typing the password twice, and you are signed in. After that the site shows the login page. The hub runs until you stop it with Ctrl-C.

Every hub also has a system owner, `system` (moniker `hub-system-owner`): not a person, it cannot log in, and no one can take its name. It owns the one built-in job, "Heartbeat roll-up", which every 12 hours rolls heartbeats older than 12 hours into one-minute buckets. The job is on the Jobs page, where it can be paused or deleted like any other; a deleted one is made again at the next start.

## Starting a worker

On the machine the worker is for, from a clone, after the first two commands under Tests, with the address the hub prints when it starts:

```sh
PYTHONPATH=protocol/python:core/python .venv/bin/python -m rook_worker http://127.0.0.1:8700/
```

- The hub's address is the only input. The worker keeps its keys, and the identity the hub gives it, in `~/.rook-worker`.
- On the first start it knocks under this machine's hostname and says it is waiting for approval. Approve it on the hub: on the Workers page, or by an agent. It then links, prints `Linked to … as <moniker>.` and serves until you stop it with Ctrl-C.
- Started again, it links at once as the same worker: no second knock, no second approval.
- To join with no one approving, add `--code TOKEN` with a join token from the hub (an agent mints one with `worker.join-token`, or `token.create` with `purpose: join`). It is approved at once, owned by whoever minted the token. A join token works once.
- If the hub cannot be reached, the address is not a hub, or the hub refuses the worker's key, it says so in one line and exits. Once approved, a hub that goes away is waited for: the worker says why it cannot link and links again when the hub is back.

## Talking to the house agent

Every hub has a house agent, "house" (named Rookie until you name it: setting `house_name` of plugin `agents`). It thinks with Claude Code on a worker, so the machine of one worker needs Claude Code installed and signed in (`claude` on its PATH, or its path set in that worker's config as `agents-claude-code.claude`). With one worker online that is all; with several, say which one with `setting.set {plugin: agents, key: worker, value: <moniker>}`.

- **Talk to it** from any agent at the door: `call worker=hub cap=agent.chat.send args={agent: house, text: "..."}` answers with its reply; `agent.chat.read` reads your conversation with it, which is kept for good and resumed on every message, across restarts.
- **What it can do on the band**: each turn reaches the door with a token minted for that turn, as you, and revoked when the turn ends.
- **Wake it on a schedule or an event** with a job whose call is `agent.chat.send`.
- **Stop a turn** by closing the stream its prompt names (`call` in the prompt event), as any call is stopped. A turn has no timeout.

## Backing up, restoring and moving a hub

A backup is one file holding everything the hub needs to come back as itself: users and their sessions, tokens, the vault's secrets, installed plugins, records and blob bytes, the journal, stream recordings, heartbeat history, and the hub's own key, so its workers are still accepted. It can be taken while the hub is running: the hub keeps serving, and the backup holds the data as it was at one moment.

```sh
PYTHONPATH=protocol/python:hub .venv/bin/python -m rook_hub backup --data ~/rook-hub --vault-key ~/rook-vault.key --to ~/rook-hub.backup
```

The backup is encrypted with a key made from the vault key. Without the vault key file nobody can read anything in it, and neither can you: the vault key is **not** in the backup. Keep the key file, and move it, apart from the backup.

While it runs, a backup keeps a plain working copy of the database in the data directory itself (`.backup.db`), never where the backup is written, and removes it when done; the data directory's disk needs free space for that copy, as big as `hub.db`. The backup file is written readable by you only. Its disk needs room for the backup, about the size of the data directory.

An agent can do the same through the hub's caps (worker `hub`): `backup.run {to}` writes a backup of the running hub to a new file on the hub's disk, `backup.list {folder}` lists the backups in a folder, and `backup.verify {path}` reads one through and proves it opens with this hub's vault key, writing nothing. Restoring stays a command, below: it writes a hub that is not running.

To restore, on this machine or another one, into a new or empty directory:

```sh
PYTHONPATH=protocol/python:hub .venv/bin/python -m rook_hub restore --from ~/rook-hub.backup --data ~/rook-hub --vault-key ~/rook-vault.key
```

Then start the hub on that directory with the start command above. A restore refuses, and writes nothing, when the directory already holds anything (so it never writes into a hub's data, running or not), when the key file is not the one the backup was made with, and when the file is not a whole, unchanged backup.

To move a hub to another machine: take a backup, copy it and the vault key file there, restore, and start the hub there. Its users, tokens, secrets, plugins, records and workers come with it.
