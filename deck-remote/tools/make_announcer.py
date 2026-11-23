"""Makes Baketris's announcer clips: rookmote/games/announcer/*.ogg. Run on a desk
machine, never on the Deck (it has no TTS); the clips are committed.

The voice: Piper (https://github.com/OHF-Voice/piper1-gpl), its en_US "joe" medium
voice from rhasspy/piper-voices (the models MIT; joe's dataset CC0, per its
MODEL_CARD). Plain words, said by Piper, then made deep and heavy with ffmpeg:
rubberband pitched down 6 semitones (tempo kept a little slow), a low-end lift,
a tanh soft clip for grit, compression, a short slap-back "arena" echo and a
limiter. Original audio: nothing from any game.

    uv venv -p 3.12 /tmp/deck/ttsvenv && . /tmp/deck/ttsvenv/bin/activate
    uv pip install piper-tts==1.8.0
    python deck-remote/tools/make_announcer.py            # from the repository root

It prints each clip's length, peak and loudness (the check that it is speech, not
silence or clipping, since the result can't be listened to here)."""

from __future__ import annotations

import json
import subprocess
import sys
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "rookmote" / "games" / "announcer"
CACHE = Path.home() / ".cache" / "rookmote-tts"
VOICE = "en/en_US/joe/medium/en_US-joe-medium"
BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

# name -> what it says (plain words; the game picks the clip)
CLIPS = {
    "single": "Single!",
    "double": "Double!",
    "triple": "Triple!",
    "baketris": "Bake! Tris!",
    "tspin": "T-spin!",
    "combo": "Combo!",
    "megacombo": "Mega combo!",
    "backtoback": "Back to back!",
    "allclear": "All clear!",
    "blast": "Chain blast!",
}

FILTER = ("rubberband=pitch=0.707:tempo=0.94:formant=preserved,"
          "highpass=f=55,bass=g=7:f=120:w=0.8,equalizer=f=2500:t=q:w=1.2:g=3,"
          "volume=2.2,asoftclip=type=tanh,"
          "acompressor=threshold=-18dB:ratio=4:attack=5:release=120:makeup=4,"
          "aecho=0.85:0.55:55|110|170:0.32|0.2|0.1,"
          "apad=pad_dur=0.15,alimiter=limit=0.89,aresample=44100")


def fetch(name: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    p = CACHE / Path(name).name
    if not p.exists():                       # curl: it retries (the model is 60 MB)
        subprocess.run(["curl", "-sL", "--retry", "3", "-o", str(p), BASE + name], check=True)
    return p


def measure(path: Path) -> dict:
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(path), "-af", "volumedetect,ebur128", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    out = {}
    for line in r.splitlines():
        if "max_volume" in line:
            out["peak_db"] = float(line.split(":")[1].split()[0])
        if "mean_volume" in line:
            out["mean_db"] = float(line.split(":")[1].split()[0])
        if line.strip().startswith("I:") and "LUFS" in line:
            out["lufs"] = float(line.split(":")[1].split()[0])
    d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True).stdout.strip()
    out["seconds"] = round(float(d), 2)
    return out


def main() -> int:
    from piper import PiperVoice, SynthesisConfig
    model = fetch(VOICE + ".onnx")
    fetch(VOICE + ".onnx.json")
    card = fetch(VOICE.rsplit("/", 1)[0] + "/MODEL_CARD")
    print(card.read_text().split("## Training")[0].strip())
    voice = PiperVoice.load(str(model))
    cfg = SynthesisConfig(length_scale=1.25, noise_scale=0.5, noise_w_scale=0.6)
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = CACHE / "raw"
    tmp.mkdir(exist_ok=True)
    report = {}
    for name, text in CLIPS.items():
        raw = tmp / f"{name}.wav"
        with wave.open(str(raw), "wb") as w:
            voice.synthesize_wav(text, w, syn_config=cfg)
        dst = OUT / f"{name}.ogg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw), "-af", FILTER, "-ac", "1",
                        "-c:a", "libvorbis", "-q:a", "4", str(dst)], check=True)
        m = measure(dst)
        report[name] = m
        print(f"{name:11s} {text!r:18s} {m}")
        if m["seconds"] < 0.3 or m.get("mean_db", -99) < -40 or m.get("peak_db", 0) > -0.3:
            print(f"  !! {name} looks wrong (too short, silent or clipped)", file=sys.stderr)
            return 1
    (OUT / "README").write_text(
        "Baketris's announcer clips, made by deck-remote/tools/make_announcer.py with Piper's en_US joe\n"
        "medium voice (rhasspy/piper-voices, MIT; dataset CC0), pitched down and processed with ffmpeg.\n"
        "Original audio. Measurements when made:\n" + json.dumps(report, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
