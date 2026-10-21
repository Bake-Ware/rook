#!/bin/bash
# Build Rook Moves clips end to end: timing -> voice lines -> picture -> soundtrack -> mp4.
#   VOICE_KEY=... ./make.sh 1 2 3        (env PY=python with playwright+aiohttp+numpy, DEST=folder for finished mp4s)
set -e
cd "$(dirname "$0")"
PY=${PY:-python3}
[ -n "$VOICE_KEY" ] || export VOICE_KEY=$(cat ~/.config/rook-moves/voice-key 2>/dev/null)
DEST=${DEST:-out}
[ -d node_modules/three ] || npm install --no-audit --no-fund
declare -A NAMES=([1]="Secretary" [2]="Build it, don't wait up" [3]="Team huddle" [4]="Security reporting" [5]="Research team" [6]="Comic, song, shed" [7]="Make and track the promo campaign")
for n in "$@"; do
  $PY render.py meta $n
  $PY voices.py $n
  $PY render.py meta $n                # again, now with the real voice durations
  $PY render.py video $n
  $PY score.py $n
  name="Rook Moves $n - ${NAMES[$n]}"
  mkdir -p "$DEST"
  ffmpeg -v error -y -i out/c${n}_video.mp4 -i out/c${n}_mix.wav -map 0:v -map 1:a -c:v copy \
    -af loudnorm=I=-16:TP=-1.5:LRA=11 -c:a aac -b:a 192k -ar 48000 -shortest -movflags +faststart "$DEST/$name.mp4"
  echo "built $DEST/$name.mp4"
done
