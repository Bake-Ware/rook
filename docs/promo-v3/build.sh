#!/bin/bash
# Rebuild the "Give your agents agency" film from source: 3D frame sequences,
# the HTML/WebGL picture, the synthesised score, then mux and loudness-normalise.
# Needs: node (for three.js), python3 with playwright, numpy, scipy; ffmpeg.
# Set CHROMIUM=/path/to/chromium to use a specific browser build.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
[ -d node_modules/three ] || npm install --no-audit --no-fund
# 1. pre-rendered 3D: the rook and its door (two shots), and the glass shatter (alpha PNG)
$PY render_3d.py rook frames/rook 232 451 1 jpg
$PY render_3d.py rook frames/rook 1835 1998 1 jpg
$PY render_3d.py shatter frames/shatter 169 276 1 png
# 2. the picture, 1998 frames at 30 fps, in three segments that can run in parallel
mkdir -p seg
$PY render_film.py video seg/a.mp4 0 918 &
$PY render_film.py video seg/b.mp4 918 1835 &
$PY render_film.py video seg/c.mp4 1835 1998 &
wait
printf "file 'a.mp4'\nfile 'b.mp4'\nfile 'c.mp4'\n" > seg/list.txt
# 3. the score
$PY score.py score.wav
# 4. mux, normalised to -14 LUFS
ffmpeg -hide_banner -loglevel error -y -f concat -safe 0 -i seg/list.txt -i score.wav \
  -map 0:v -map 1:a -c:v copy -af loudnorm=I=-14:TP=-1:LRA=11 -c:a aac -b:a 256k -ar 48000 \
  -shortest -movflags +faststart rook-agency.mp4
echo "built rook-agency.mp4 (teaser, vertical cut and link card: ./cuts.sh)"
