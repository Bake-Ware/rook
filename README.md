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
