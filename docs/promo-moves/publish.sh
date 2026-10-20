#!/bin/bash
# Publish the Rook films to a Jellyfin Shows library as one series, "Rook":
#   Season 01 = Rook Moves 1-7 (out/final), Season 00 (Specials) = the earlier films.
#   ./publish.sh [/srv/media/tv] [specials-dir]
# specials-dir holds the earlier films by these names (any that are missing are skipped):
#   rook-promo.mp4, rook-agency.mp4, rook-agency-remaster.mp4
set -e
cd "$(dirname "$0")"
LIB=${1:-/srv/media/tv}
SPEC=${2:-}
SHOW="$LIB/Rook"
mkdir -p "$SHOW/Season 01" "$SHOW/Season 00"

frame() {   # frame <video> <out.jpg> <vf> : a still from 42% of the way in
  local dur; dur=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$1")
  ffmpeg -v error -y -ss "$(echo "$dur * 0.42" | bc)" -i "$1" -frames:v 1 -vf "$3" "$2"
}
nfo() {     # nfo <file> <season> <episode> <title> <plot>
  cat > "$1" <<EOF
<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<episodedetails><title>$4</title><season>$2</season><episode>$3</episode><plot>$5</plot><aired>2026-10-20</aired></episodedetails>
EOF
}

cat > "$SHOW/tvshow.nfo" <<'EOF'
<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<tvshow><title>Rook</title><plot>Short films about Rook, the band of agents that runs on your own machines. Your machines, your move.</plot><year>2026</year><premiered>2026-10-20</premiered><genre>Animation</genre><studio>Bake-Ware</studio></tvshow>
EOF

first=""
for f in out/final/Rook\ Moves\ *.mp4; do
  base=$(basename "$f" .mp4)                    # "Rook Moves N - Title"
  n=${base#Rook Moves }; n=${n%% *}; title=${base#Rook Moves $n - }
  ep=$(printf 'S01E%02d' "$n")
  dst="$SHOW/Season 01/Rook - $ep - $title"
  cp "$f" "$dst.mp4"
  frame "$f" "$dst-thumb.jpg" "scale=1280:-1"
  nfo "$dst.nfo" 1 "$n" "$title" "Rook Moves, part $n."
  [ -z "$first" ] && first="$f"
  echo "published $dst.mp4"
done
if [ -n "$first" ]; then
  frame "$first" "$SHOW/poster.jpg" "crop=ih*2/3:ih,scale=680:-1"
  frame "$first" "$SHOW/fanart.jpg" "scale=1920:-1"
  cp "$SHOW/poster.jpg" "$SHOW/Season 01/poster.jpg" 2>/dev/null || true
fi

if [ -n "$SPEC" ]; then
  i=0
  for pair in "rook-promo.mp4|Rook, the first promo" "rook-agency.mp4|Give Your Agents Agency" "rook-agency-remaster.mp4|Give Your Agents Agency (Remaster)"; do
    src="$SPEC/${pair%%|*}"; title=${pair#*|}
    [ -f "$src" ] || continue
    i=$((i + 1)); ep=$(printf 'S00E%02d' "$i")
    dst="$SHOW/Season 00/Rook - $ep - $title"
    cp "$src" "$dst.mp4"
    frame "$src" "$dst-thumb.jpg" "scale=1280:-1"
    nfo "$dst.nfo" 0 "$i" "$title" "An earlier Rook film."
    echo "published $dst.mp4"
  done
fi
chgrp -R media "$SHOW" 2>/dev/null || true; chmod -R g+rwX,o+rX "$SHOW"
