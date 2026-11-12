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
