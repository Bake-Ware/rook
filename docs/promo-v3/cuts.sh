#!/bin/bash
set -e
cd "$(dirname "$0")"
SRC=${1:-rook-agency.mp4}
B=0.46875
# teaser: box -> shatter -> door -> hook/title (beats 8-40 = 3.75s-18.75s), then the end card (beats 133-140)
T1S=3.75; T1E=18.75; T2S=$(python3 -c "print(133*$B)"); T2E=$(python3 -c "print(140.5*$B)")
ffmpeg -hide_banner -loglevel error -y -i $SRC -filter_complex "\
[0:v]trim=$T1S:$T1E,setpts=PTS-STARTPTS,fade=t=in:st=0:d=0.3[v1];\
[0:v]trim=$T2S:$T2E,setpts=PTS-STARTPTS,fade=t=out:st=$(python3 -c "print($T2E-$T2S-0.6)"):d=0.6[v2];\
[0:a]atrim=$T1S:$T1E,asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3[a1];\
[0:a]atrim=$T2S:$T2E,asetpts=PTS-STARTPTS,afade=t=out:st=$(python3 -c "print($T2E-$T2S-0.8)"):d=0.8[a2];\
[v1][v2]xfade=transition=fade:duration=0.25:offset=$(python3 -c "print($T1E-$T1S-0.25)")[v];\
[a1][a2]acrossfade=d=0.25[a]" -map "[v]" -map "[a]" -c:v libx264 -preset slow -crf 19 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart rook-teaser-16x9.mp4
# vertical 9:16: full frame centred over a blurred, zoomed copy, with a title band above and the URL below
mkvert() {
  in=$1; out=$2
  ffmpeg -hide_banner -loglevel error -y -i $in -filter_complex "\
[0:v]split[a][b];[a]scale=-2:1920,crop=1080:1920,boxblur=28:2,eq=brightness=-0.18[bg];\
[b]scale=1080:-2[fg];[bg][fg]overlay=0:(H-h)/2-40[c];\
[c]drawtext=fontfile=/usr/share/fonts/opentype/inter/InterDisplay-Black.otf:text='GIVE YOUR AGENTS':fontsize=92:fontcolor=0xe2e3d9:x=(w-tw)/2:y=330,\
drawtext=fontfile=/usr/share/fonts/opentype/inter/InterDisplay-Black.otf:text='AGENCY.':fontsize=150:fontcolor=0xd8ad6d:x=(w-tw)/2:y=440,\
drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf:text='github.com/Bake-Ware/rook':fontsize=44:fontcolor=0xf0c98c:x=(w-tw)/2:y=1500,\
drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf:text='open source · self-hosted · end-to-end encrypted':fontsize=28:fontcolor=0x92998c:x=(w-tw)/2:y=1570[v]" \
  -map "[v]" -map 0:a -c:v libx264 -preset slow -crf 20 -pix_fmt yuv420p -c:a copy -movflags +faststart $out
}
mkvert $SRC rook-agency-vertical.mp4
mkvert rook-teaser-16x9.mp4 rook-teaser-vertical.mp4
# link preview card 1200x630 from the title frame
ffmpeg -hide_banner -loglevel error -y -ss 16.6 -i $SRC -frames:v 1 -vf "scale=1200:-2,crop=1200:630:0:(ih-630)/2" -q:v 3 og-agency.jpg
