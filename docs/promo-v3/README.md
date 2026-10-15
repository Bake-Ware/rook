# Promo v3: "Give your agents agency"

Sources for the 66-second Rook film on the landing page, plus the 15-second teaser, the vertical cuts and the link-preview card made from it. Everything here is generated: no stock footage, no samples.

| file | what it is |
|---|---|
| `film.html` | The picture. HTML/SVG overlays over a three.js world, driven by `render(t, frame)`. The beat map is 128 BPM, 142 beats. |
| `world3d.js` | The three.js world `film.html` draws into: toon-shaded devices, agent cores, beams, rings. |
| `scene3d.html` | Two pre-rendered 3D shots. `?mode=rook` is the rook, its door and the dolly through it. `?mode=shatter` is the glass pane (textured with `chat.png`) cracking into shards. |
| `render_3d.py` | Renders a frame range of `scene3d.html` into `frames/<mode>/f%04d.{png,jpg}`. |
| `render_film.py` | Renders `film.html`, either as stills or as an H.264 segment. |
| `score.py`, `synth.py` | The score. It's an original arrangement of *Korobeiniki* (Russian folk song, 1861, public domain), synthesised with numpy/scipy: chiptune, then music box, then orchestra, with a body-percussion layer. |
| `build.sh` | Full rebuild into `rook-agency.mp4`. |
| `cuts.sh` | From the film, makes the teaser (`rook-teaser-16x9.mp4`), the 1080×1920 versions (`rook-agency-vertical.mp4`, `rook-teaser-vertical.mp4`) and `og-agency.jpg`. |

## Rebuild

You need:
- node, for three.js
- python 3 with `playwright`, `numpy` and `scipy`
- ffmpeg

```sh
cd docs/promo-v3
./build.sh          # 3D frames, picture (3 parallel segments), score, mux at -14 LUFS
./cuts.sh           # teaser, vertical cuts, link card
```

Rendering uses software WebGL (SwiftShader), so it runs headless on any machine. It is slow, roughly an hour for the whole film on a few cores. To use a particular Chromium build, set `CHROMIUM=/path/to/chrome`.

`cuts.sh` draws its titles with Inter Display Black and DejaVu Sans Mono from the system font paths. Change `fontfile=` if yours live elsewhere.

To look at single moments while editing, render stills. Times are in seconds:

```sh
python3 render_film.py stills stills/ 7.9 15.8 32.6
```

Most of the picture needs the pre-rendered frames. Without them, the rook shots and the shatter render empty.

The earlier cut (v1, with a WebAudio soundtrack) lives in `../promo/`.
