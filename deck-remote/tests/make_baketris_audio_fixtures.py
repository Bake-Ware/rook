"""Makes tests/fixtures/baketris_audio.json.gz: every sound Baketris synthesizes
(Sfx.java's seven skin sets and its shared sounds, Music.java's four songs),
rendered by Baketris's own Java (the synthesis code cut out of Sfx.java and
Music.java, from /home/bake/baketris, read only; the Android parts left behind)
and recorded as: length, sha256 of the int16 samples, and every 97th sample.
test_baketris_audio.py renders the same with the Python port and compares.

Run: python deck-remote/tests/make_baketris_audio_fixtures.py [--compare]
(needs a JDK; --compare also checks the Python port sample for sample, slowly)."""

import gzip
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path("/home/bake/baketris/src/systems/bake/baketris")
STEP = 97


def _cut(text: str, start: str, end: str) -> str:
    i = text.index(start)
    j = text.index(end, i)
    return text[i:j]


def java_source() -> str:
    sfx = (SRC / "Sfx.java").read_text()
    mus = (SRC / "Music.java").read_text()
    ctor = _cut(sfx, "int[] n = {523", "use(0);")
    ctor = re.sub(r"\b(\w+) = load\(\"(\w+)\", ", r'out(dir, "\2", ', ctor)
    synth = _cut(sfx, "static short[][] build(int s)", "private int load(")
    song = _cut(mus, "static final class Song", "private volatile AudioTrack")
    msynth = mus[mus.index("static double mtof"):mus.rindex("}")]
    return f"""
import java.io.*;
import java.nio.*;
import java.util.Random;

public class AudioGolden {{
    static final int RATE = 44100;
    static final int MOVE = 0, ROT = 1, HOLD = 2, LOCK = 3, HARD = 4, C1 = 5, C2 = 6, C3 = 7, C4 = 8, KABOOM = 9, N = 10;

    static void out(File dir, String name, short[] s) throws IOException {{
        ByteBuffer bb = ByteBuffer.allocate(s.length * 2).order(ByteOrder.LITTLE_ENDIAN);
        for (short v : s) bb.putShort(v);
        try (FileOutputStream o = new FileOutputStream(new File(dir, name + ".raw"))) {{ o.write(bb.array()); }}
    }}

    static void shared(File dir) throws IOException {{
        {ctor}
    }}

    {synth}

    static final class Mus {{
        static final int RATE = 24000;
        {song}
        {msynth}
    }}

    public static void main(String[] a) throws IOException {{
        File dir = new File(a[0]);
        shared(dir);
        for (int s = 0; s < 7; s++) {{
            short[][] w = build(s);
            for (int k = 0; k < N; k++) out(dir, "s" + s + "_" + k, w[k]);
        }}
        for (int i = 0; i < Mus.SONGS.length; i++) out(dir, "song" + i, Mus.render(Mus.SONGS[i]));
    }}
}}
"""


def run_java(tmp: Path) -> dict:
    (tmp / "AudioGolden.java").write_text(java_source())
    subprocess.run(["javac", "-d", str(tmp), str(tmp / "AudioGolden.java")], check=True)
    out = tmp / "out"
    out.mkdir()
    subprocess.run(["java", "-cp", str(tmp), "AudioGolden", str(out)], check=True)
    sounds = {}
    for f in sorted(out.glob("*.raw")):
        a = array("h")
        a.frombytes(f.read_bytes())
        if sys.byteorder != "little":
            a.byteswap()
        sounds[f.stem] = a
    return sounds


def record(a: array) -> dict:
    b = array("h", a)
    if sys.byteorder != "little":
        b.byteswap()
    return {"n": len(a), "sha256": hashlib.sha256(b.tobytes()).hexdigest(), "every": list(a[::STEP])}


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        sounds = run_java(Path(t))
    fx = {"step": STEP, "sounds": {k: record(v) for k, v in sounds.items()}}
    dest = HERE / "fixtures" / "baketris_audio.json.gz"
    with open(dest, "wb") as f, gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
        g.write(json.dumps(fx, separators=(",", ":")).encode())
    print(f"{len(sounds)} sounds -> {dest} ({dest.stat().st_size} bytes)")
    if "--compare" in sys.argv:
        sys.path.insert(0, str(HERE.parent))
        from rookmote.games import music, sfx
        py = dict(sfx.shared())
        for s in range(7):
            for k, w in enumerate(sfx.build(s)):
                py[f"s{s}_{k}"] = w
        for i, song in enumerate(music.SONGS):
            py[f"song{i}"] = music.render(song)
        for k in sorted(sounds):
            j, p = sounds[k], py[k]
            diff = max((abs(x - y) for x, y in zip(j, p)), default=0)
            off = sum(1 for x, y in zip(j, p) if x != y)
            print(f"{k:10s} n={len(j):8d}/{len(p):8d} max|d|={diff} differing={off}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
