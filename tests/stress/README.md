# Stress rounds (brief 48)

Each round starts its own hub and workers from this clone in a new directory under `/tmp`, on ports the hub picks, and removes them when it ends (`--keep` keeps the directory; `--log FILE` says where the JSON lines go, `/tmp/rook-stress-<round>.jsonl` unless given). Run from the clone root, after the README's first two commands. pytest does not collect anything here. Results are in `docs/stress.md`.

- Round 1, call flood: `.venv/bin/python tests/stress/flood.py`
- Round 2, terminal storm: `.venv/bin/python tests/stress/terminals.py`
- Round 3, stream volume: `.venv/bin/python tests/stress/streams.py`
- Round 4, large results: `.venv/bin/python tests/stress/results.py`
- Round 5, churn: `.venv/bin/python tests/stress/churn.py`
- Round 6, hub restart under load: `.venv/bin/python tests/stress/restart.py`
- Round 7, long haul: `.venv/bin/python tests/stress/longhaul.py` (`--minutes N`, 60 unless given)
- Round 8, journal and records at size: `.venv/bin/python tests/stress/size.py`
- Memory's read path at size (brief MEM5): `.venv/bin/python tests/stress/memory.py 500 5000 50000`, and `--hub N` for a whole hub; under the AMD micro's limits with `systemd-run --user --scope -p CPUQuota=12.5% -p MemoryMax=600M -p MemorySwapMax=0` in front (the figures are in the pull request)

`rig.py` is what they share; `plugin/` is round 8's own plugin, installed only on that round's hub.
