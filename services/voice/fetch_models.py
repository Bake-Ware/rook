"""Download the speech model files the voice service loads, checked against models.json.

    python -m services.voice.fetch_models DIR [--optional] [--check]

Fetches every ``required`` file of ``models.json`` that ``DIR`` does not have yet
(``--optional`` adds the others, such as the speaker model), each over HTTPS into
``<name>.part`` beside it, checked against its SHA-256 and only then renamed into
place, so a cut download never leaves a file that looks complete. Files already
there are left alone; ``--check`` hashes them against the manifest instead and
downloads nothing. The hub (Intelligence sources > Voice, ``rook voice setup``)
uses the same functions. Standard library only: the hub imports it without the
voice extras.
"""
import hashlib
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

MANIFEST = Path(__file__).with_name("models.json")
CHUNK = 1 << 20
TIMEOUT_S = 60
SPARE = 64 << 20          # free space left over after the download


def manifest():
    return json.loads(MANIFEST.read_text())


def required(entries=None):
    """The files voice cannot start without, in manifest order."""
    entries = manifest() if entries is None else entries
    return [name for name, e in entries.items() if e.get("required")]


def missing(model_dir, names=None):
    names = required() if names is None else names
    return [name for name in names if not (Path(model_dir) / name).is_file()]


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def verify(model_dir, names=None):
    """``{name: problem}`` for the files that are there but do not match the manifest."""
    entries = manifest()
    bad = {}
    for name in required(entries) if names is None else names:
        path = Path(model_dir) / name
        if path.is_file() and sha256_of(path) != entries[name]["sha256"]:
            bad[name] = "does not match models.json (re-download: delete it and fetch again)"
    return bad


def _download(name, entry, model_dir, progress, opener):
    url = entry["url"]
    if not url.startswith("https://"):
        raise ValueError(f"{name}: refusing a non-https URL")
    final = Path(model_dir) / name
    part = final.with_name(name + ".part")
    h, done = hashlib.sha256(), 0
    total = int(entry.get("size") or 0)
    try:
        with opener(urllib.request.Request(url, headers={"User-Agent": "rook-voice"}),
                    timeout=TIMEOUT_S) as r, open(part, "wb") as out:
            total = int(r.headers.get("Content-Length") or total or 0)
            while True:
                block = r.read(CHUNK)
                if not block:
                    break
                out.write(block)
                h.update(block)
                done += len(block)
                if progress:
                    progress(name, done, total)
        if h.hexdigest() != entry["sha256"]:
            raise ValueError(f"{name}: downloaded file does not match models.json (sha256 {h.hexdigest()})")
        os.replace(part, final)
    finally:
        part.unlink(missing_ok=True)


def fetch(model_dir, names=None, progress=None, opener=urllib.request.urlopen):
    """Download the files of ``names`` (default: the required ones) ``model_dir`` lacks.
    ``progress(name, done_bytes, total_bytes)`` is called as they arrive. Returns the
    names downloaded; raises on the first failure (files already finished stay)."""
    entries = manifest()
    names = required(entries) if names is None else list(names)
    unknown = [n for n in names if n not in entries]
    if unknown:
        raise ValueError(f"not in models.json: {', '.join(unknown)}")
    Path(model_dir).mkdir(parents=True, exist_ok=True)
    todo = missing(model_dir, names)
    need = sum(int(entries[n].get("size") or 0) for n in todo) + SPARE
    free = shutil.disk_usage(model_dir).free
    if todo and free < need:
        raise OSError(f"not enough disk space in {model_dir}: {need // CHUNK} MB needed, "
                      f"{free // CHUNK} MB free")
    got = []
    for name in todo:
        _download(name, entries[name], model_dir, progress, opener)
        got.append(name)
    return got


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="python -m services.voice.fetch_models",
                                 description="Download the voice service's model files into DIR.")
    ap.add_argument("dir", help="the model folder (VOICE_MODEL_DIR)")
    ap.add_argument("--optional", action="store_true", help="also the optional files (speaker model)")
    ap.add_argument("--check", action="store_true", help="check the files there; download nothing")
    args = ap.parse_args(argv)
    entries = manifest()
    names = list(entries) if args.optional else required(entries)
    if args.check:
        gone, bad = missing(args.dir, names), verify(args.dir, [n for n in names
                                                             if n not in missing(args.dir, names)])
        for name in gone:
            print(f"missing  {name}")
        for name, why in bad.items():
            print(f"bad      {name}: {why}")
        if not gone and not bad:
            print(f"ok: {len(names)} model files in {args.dir}")
        return 1 if gone or bad else 0
    last = {}

    def show(name, done, total):
        step = done * 10 // total if total else done // (50 * CHUNK)     # every 10 %, else 50 MB
        if last.get(name) == step:
            return
        last[name] = step
        print(f"  {name}: {done // CHUNK} MB" + (f" of {total // CHUNK} MB" if total else ""), flush=True)

    got = fetch(args.dir, names, progress=show)
    print(f"downloaded {', '.join(got)}" if got else f"nothing to download: {args.dir} has them all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
