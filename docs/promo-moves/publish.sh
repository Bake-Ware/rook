#!/bin/bash
# Copy finished clips into a Jellyfin movies library, one folder per clip with a poster frame.
#   ./publish.sh [/srv/media/movies]
set -e
cd "$(dirname "$0")"
LIB=${1:-/srv/media/movies}
YEAR=2026
for f in out/final/*.mp4; do
  base=$(basename "$f" .mp4)
  dir="$LIB/$base ($YEAR)"
  mkdir -p "$dir"
  cp "$f" "$dir/$base ($YEAR).mp4"
  dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$f")
  ffmpeg -v error -y -ss "$(echo "$dur * 0.42" | bc)" -i "$f" -frames:v 1 -vf scale=1280:-1 "$dir/backdrop.jpg"
  ffmpeg -v error -y -ss "$(echo "$dur * 0.42" | bc)" -i "$f" -frames:v 1 -vf "crop=ih*2/3:ih,scale=600:-1" "$dir/poster.jpg"
  chgrp -R media "$dir" 2>/dev/null || true; chmod -R g+rwX,o+rX "$dir"
  echo "published $dir"
done
