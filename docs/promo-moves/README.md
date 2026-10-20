# Rook Moves

Seven short films of real jobs Rook does for a person, from the storyboard doc
"Rook Moves Storyboards". Models are built in Blender; animation and rendering are
three.js in headless Chromium; voice lines come from Rook's voice server; the
soundtrack is synthesised with numpy.

| # | Clip | Tagline |
|---|------|---------|
| 1 | Secretary | The best secretary you've never had. |
| 2 | Build it, paint it, don't wait up | Make ideas, not time. |
| 3 | Team huddle | Focus on the results, not the work. |
| 4 | Security reporting (concept, demo data) | Vigilance that's active. |
| 5 | Research team | Explore. More. |
| 6 | Comic, song, shed | Superpowers, for real life. |
| 7 | Make and track the campaign | Share your self. |

## Files

- `models.py`: Blender script that builds the scene kit and exports `assets/*.glb`
  (rook, agent rook, devices, the folio, figure, props). Materials are named; the
  kit swaps them for its toon palette and draws ink on the edges.
- `kit.js`: the scene kit (Board, pulses that move like a rook, panes, chips,
  subtitles, captions, the opening sting and the end card).
- `clips/c1.js` … `c7.js`: one timeline each. Every frame is a pure function of time.
- `index.html?clip=N`: plays a clip; `render(t)` sets the frame for `t` seconds.
- `director.py` serves the review site: `/` lists every clip with its render and notes for Claude
  (`clips/cN.notes.json`); `index.html?clip=N&director=1` is the director: scrub a clip, fly a free camera, add or
  delete camera keys, save them to `clips/cN.keys.json` (used instead of the clip's own keys).
- Scale: everything is real size in metres; a Board square is 1 m; pieces are tokens (`kit.piece`).
- `render.py`: `meta`, `stills` and `video` modes (playwright + aiohttp).
- `voices.py`: speaks the lines with the voice server (`VOICE_URL`, `VOICE_KEY`).
- `score.py`: underscore, sound cues and voices, ducked under the voice.
- `make.sh N…`: the whole chain for each clip; `publish.sh` puts them in a Jellyfin
  Shows library as the series "Rook" (Season 01 = these clips, Specials = the earlier films).

## Build

```sh
npm install
python -m venv .venv && .venv/bin/pip install playwright aiohttp numpy
.venv/bin/python -m playwright install chromium
GL=gpu PY=.venv/bin/python DEST=out/final ./make.sh 1 2 3 4 5 6 7
./publish.sh /srv/media/tv specials/   # specials/ holds the earlier films, optional
```

`GL=gpu` uses ANGLE on the GPU (about 14 frames a second on an RTX 2070); without
it SwiftShader renders on the CPU. Panes show demo data only.
