# Rook

Rook core, rebuilt from scratch. `PLAN.md` is the locked plan and the only source of what gets built on this branch.

The earlier code (0.4 on `master`, the 0.5 rework on `castling`) is reference only. Nothing is carried forward from it.

## Layout

- `docs/telesthete-surface.md`: what the pinned Telesthete offers Rook, and where it disagrees with the plan.
- `docs/protocol.md`: the Rook protocol, v1 (W1).
- `protocol/`: the protocol library, one per language (U1). `vectors.json` is shared by every language; `python/` is the Python library.
- `hub/`: the hub, in Python.
- `core/python/`: the worker core written in Python (U1). Only the minimal worker so far.
- `tests/`: the tests for all of the above.

## Tests

Python 3.11 or newer. From a fresh clone:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest
```

## Starting a hub

From the clone, after the first two commands above:

```sh
PYTHONPATH=protocol/python:hub .venv/bin/python -m rook_hub --data ~/rook-hub --vault-key ~/rook-vault.key
```

- `--data` is the hub's data directory. It is made on the first start.
- `--vault-key` is the vault key file. It must be outside the data directory. On the first start the hub makes a random key there, readable by you only, and reads it on every start after. Back it up apart from the data: if it is lost, every secret in the vault is lost with it. If the file is missing after the vault was keyed, the hub starts with the vault locked and makes no new key.
- `--host` and `--port` say where to listen: 127.0.0.1 and 8700 unless given.

Open the address it prints. While the hub has no user, the site shows a setup page: choose a name and a password for the first user, and you are signed in. After that the site shows the login page. The hub runs until you stop it with Ctrl-C.

## Backing up, restoring and moving a hub

A backup is one file holding everything the hub needs to come back as itself: users and their sessions, tokens, the vault's secrets, installed plugins, records and blob bytes, the journal, stream recordings, heartbeat history, and the hub's own key, so its workers are still accepted. It can be taken while the hub is running: the hub keeps serving, and the backup holds the data as it was at one moment.

```sh
PYTHONPATH=protocol/python:hub .venv/bin/python -m rook_hub backup --data ~/rook-hub --vault-key ~/rook-vault.key --to ~/rook-hub.backup
```

The backup is encrypted with a key made from the vault key. Without the vault key file nobody can read anything in it, and neither can you: the vault key is **not** in the backup. Keep the key file, and move it, apart from the backup.

To restore, on this machine or another one, into a new or empty directory:

```sh
PYTHONPATH=protocol/python:hub .venv/bin/python -m rook_hub restore --from ~/rook-hub.backup --data ~/rook-hub --vault-key ~/rook-vault.key
```

Then start the hub on that directory with the start command above. A restore refuses, and writes nothing, when the directory already holds anything (so it never writes into a hub's data, running or not), when the key file is not the one the backup was made with, and when the file is not a whole, unchanged backup.

To move a hub to another machine: take a backup, copy it and the vault key file there, restore, and start the hub there. Its users, tokens, secrets, plugins, records and workers come with it.
