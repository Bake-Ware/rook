#!/bin/bash
# Render the Blender remaster of "Give your agents agency": 1998 frames at 1920x1080,
# the synthesised score from ../promo-v3, then encode, mux and loudness-normalise.
# Needs: Blender 5.2+ (BLENDER=/path/to/blender), ffmpeg, python3 with numpy + scipy (a venv is made if missing).
# JOBS parallel Blender processes, THREADS each (Freestyle is single threaded, so a few jobs beat one).
set -e
cd "$(dirname "$0")"
BLENDER=${BLENDER:-blender}
JOBS=${JOBS:-3}
THREADS=${THREADS:-4}
OUT=${OUT:-rook-agency-remaster.mp4}
mkdir -p frames logs
# 1. frames (interleaved across jobs; finished frames are skipped, so a rerun resumes)
for j in $(seq 0 $((JOBS - 1))); do
  THREADS=$THREADS "$BLENDER" -b --factory-startup --python-exit-code 1 -P film.py -- frames frames $j 1998 $JOBS > logs/job$j.txt 2>&1 &
done
wait
n=$(ls frames/f*.png | wc -l)
[ "$n" -eq 1998 ] || { echo "only $n of 1998 frames rendered, see logs/"; exit 1; }
# 2. the score (same as promo-v3)
PY=python3
$PY -c 'import numpy, scipy' 2>/dev/null || { [ -d .venv ] || python3 -m venv .venv; .venv/bin/pip -q install numpy scipy; PY=.venv/bin/python; }
$PY ../promo-v3/score.py score.wav
# 3. encode + mux, normalised to -14 LUFS
ffmpeg -hide_banner -loglevel error -y -framerate 30 -i frames/f%04d.png -i score.wav \
  -map 0:v -map 1:a -c:v libx264 -preset slow -crf 16 -pix_fmt yuv420p \
  -af loudnorm=I=-14:TP=-1:LRA=11 -c:a aac -b:a 256k -ar 48000 -shortest -movflags +faststart "$OUT"
echo "built $OUT"
