#!/bin/sh
# Embedding and recall under limits like Oracle's free VM shapes, on this machine (x86), via a user
# systemd scope. "micro": 1/8 of one core, 600 MB for the memory process (the 1 GB AMD micro, leaving
# room for a hub of about 75 MB and the system). "arm": 2 cores, 12 GB (a stand-in: the real one is Arm).
# Every search option is measured: bge-small, bge-base, the static tables, keyword and hybrid.
# Usage: [SHAPES="micro arm"] tools/vm-bench.sh [PYTHON [MODEL...]] > results.jsonl
PY=${1:-python3}
shift 2>/dev/null
MODELS=${*:-bge-small bge-base blink-8M potion-base-8M keyword hybrid}
SHAPES=${SHAPES:-micro arm}
cd "$(dirname "$0")/.."
for shape in $SHAPES; do
  case $shape in
    micro) quota=12.5%; mem=600M; threads=1 ;;
    arm) quota=200%; mem=12G; threads=2 ;;
  esac
  for model in $MODELS; do
    started=$(date +%s)
    out=$(systemd-run --user --scope --quiet -p CPUQuota=$quota -p MemoryMax=$mem -p MemorySwapMax=0 \
      env OPENBLAS_NUM_THREADS=$threads OMP_NUM_THREADS=$threads \
      "$PY" -m rook_memory bench $model --sizes 500 5000 50000 --folder "${TMPDIR:-/tmp}/rook-memory-bench" 2>&1)
    code=$?
    line=$(printf '%s\n' "$out" | grep '^{' | tail -1)
    if [ -n "$line" ]; then
      printf '{"shape": "%s", "exit": %s, "wall_s": %s, "result": %s}\n' $shape $code $(( $(date +%s) - started )) "$line"
    else
      printf '{"shape": "%s", "model": "%s", "exit": %s, "wall_s": %s, "error": %s}\n' $shape $model $code \
        $(( $(date +%s) - started )) "$(printf '%s' "$out" | tail -3 | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')"
    fi
  done
done
