#!/usr/bin/env python3
"""Build band-worker.pyz — self-contained zipapp bundling rook.worker + telesthete.protocol.

Run from anywhere:
    python3 rook/remote/build_band_worker.py

Output: rook/remote/band-worker.pyz
"""

from __future__ import annotations

import datetime
import hashlib
import shutil
import subprocess
import tempfile
import zipapp
from pathlib import Path

HERE = Path(__file__).resolve().parent        # rook/remote/
REPO_ROOT = HERE.parent.parent               # /root/repos/rook
WORKER_SRC = REPO_ROOT / "rook" / "worker"
TELESTHETE_ROOT = REPO_ROOT.parent / "telesthete" / "telesthete"
OUTPUT = HERE / "band-worker.pyz"

_MAIN = """\
from rook.worker.cli import main
main()
"""


def _copy_pkg(src: Path, dst: Path) -> None:
    """Copy a Python package tree, skipping __pycache__ and compiled bytecode."""
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=str(REPO_ROOT), text=True,
        stderr=subprocess.DEVNULL).strip()


# -- memorable build names ---------------------------------------------------
# A build is identified to humans by "<build>.<adjective>.<noun>" — 111.stinky.goat
# reads back over a call in a way 111.f2ba9ac never did. The pair is derived from
# the commit hash, so it is deterministic: the same commit always earns the same
# name, and two builds of different commits never collide by accident. The exact
# commit is still stamped in COMMIT, so nothing is lost for tracing.
#
# Both lists are 128 long and every word is <= 8 characters: 256 // 128 == 2, so
# indexing a hash byte is unbiased, and the longest possible version string stays
# short enough for the dashboard's worker cards.

ADJECTIVES = [
    "stinky", "grumpy", "sleepy", "wobbly", "cranky", "dizzy", "fuzzy", "greasy",
    "jolly", "lanky", "mopey", "nifty", "plucky", "quirky", "rusty", "shabby",
    "snappy", "spicy", "sturdy", "tipsy", "wonky", "zesty", "brisk", "chunky",
    "clumsy", "crispy", "dapper", "drowsy", "feisty", "frisky", "gawky", "giddy",
    "glum", "gritty", "hasty", "humble", "jaunty", "jumpy", "lively", "loopy",
    "lumpy", "moody", "mushy", "nimble", "peppy", "perky", "prickly", "puffy",
    "rowdy", "salty", "sassy", "scruffy", "silly", "sloppy", "smug", "sneaky",
    "soggy", "spry", "squishy", "stubby", "sulky", "sunny", "swanky", "tangy",
    "testy", "thorny", "tidy", "twitchy", "uppity", "velvet", "wiry", "witty",
    "woolly", "zippy", "bumpy", "chilly", "creaky", "curly", "damp", "dusty",
    "eager", "flaky", "fluffy", "foggy", "frosty", "glossy", "gloomy", "hairy",
    "hollow", "husky", "icy", "itchy", "khaki", "leaky", "lofty", "murky",
    "mossy", "muddy", "nutty", "oily", "pesky", "pudgy", "quaint", "rugged",
    "rustic", "shaggy", "shiny", "sleek", "slick", "smoky", "snug", "sour",
    "sparse", "spotty", "steady", "sticky", "stormy", "sweaty", "tender", "toasty",
    "wispy", "yappy", "bouncy", "breezy", "burly", "chewy", "crusty", "dainty",
]

NOUNS = [
    "goat", "badger", "otter", "walrus", "ferret", "gopher", "moose", "newt",
    "ocelot", "panda", "quail", "raccoon", "shrew", "sloth", "toad", "vole",
    "weasel", "yak", "zebra", "beetle", "cactus", "kettle", "lantern", "muffin",
    "nugget", "pickle", "pretzel", "pudding", "rocket", "shovel", "sponge", "teapot",
    "thimble", "tractor", "trumpet", "turnip", "waffle", "walnut", "wagon", "anchor",
    "anvil", "bagel", "banjo", "barrel", "beacon", "bison", "blimp", "bobcat",
    "boulder", "bucket", "bugle", "bunny", "burrito", "camel", "candle", "canoe",
    "carrot", "chisel", "cobra", "comet", "compass", "cricket", "crumpet", "dagger",
    "dingo", "donkey", "dragon", "drum", "eagle", "falcon", "fennec", "fiddle",
    "finch", "gadget", "gecko", "gerbil", "gizmo", "gnome", "grouse", "hamster",
    "harbor", "heron", "hippo", "hornet", "iguana", "jackal", "kazoo", "kelp",
    "koala", "lemur", "lizard", "llama", "lobster", "locust", "magnet", "mammoth",
    "marmot", "meerkat", "mule", "narwhal", "oyster", "parrot", "peanut", "pelican",
    "penguin", "pigeon", "possum", "prawn", "puffin", "python", "rabbit", "radish",
    "raven", "robin", "salmon", "skunk", "snail", "sparrow", "spider", "squid",
    "stork", "tapir", "terrier", "tiger", "toucan", "turtle", "urchin", "wombat",
]

assert len(ADJECTIVES) == len(NOUNS) == 128, "word lists must stay 128 long"
assert len(set(ADJECTIVES)) == len(set(NOUNS)) == 128, "word lists must be unique"


def build_name(commit: str, dirty: bool = False) -> str:
    """Deterministic "<adjective>.<noun>" for a commit.

    A dirty tree takes ``dirty`` as its adjective — an unmistakable marker that
    the bundle does not match any commit. The noun still comes from the commit,
    so two dirty builds of different commits remain distinguishable.
    """
    h = hashlib.sha256(commit.encode()).digest()
    adjective = "dirty" if dirty else ADJECTIVES[h[0] % len(ADJECTIVES)]
    return f"{adjective}.{NOUNS[h[1] % len(NOUNS)]}"


def compute_build() -> tuple[int, str, str]:
    """Return (build, commit, version). ``build`` is git's commit count — a
    monotonic integer that the OTA self-update compares. ``version`` is the
    human-facing "<build>.<adjective>.<noun>". Falls back to (0, "", "0.dev")
    outside a git checkout, which disables auto-update for that bundle (0 is
    never greater than a published build)."""
    try:
        build = int(_git("rev-list", "--count", "HEAD"))
        commit = _git("rev-parse", "--short", "HEAD")
        # Only TRACKED changes can alter what goes into the bundle, so only they
        # make it dirty. Counting untracked files here tagged every build +dirty
        # over a stray backup file sitting in the clone — a warning that fires
        # constantly is one you learn to ignore, exactly when it starts mattering.
        dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
        version = f"{build}.{build_name(commit, dirty)}"
        if dirty:
            print("WARNING: building from a dirty tree (tracked files modified) "
                  "— version tagged as dirty")
        untracked = _git("status", "--porcelain", "--untracked-files=normal")
        stray = [ln[3:] for ln in untracked.splitlines() if ln.startswith("??")]
        if stray:
            print(f"note: {len(stray)} untracked file(s) in the tree, ignored for "
                  f"versioning: {', '.join(stray[:3])}"
                  + (" …" if len(stray) > 3 else ""))
        print(f"Build {build} is \"{version}\" (commit {commit})")
        return build, commit, version
    except Exception as e:
        print(f"WARNING: could not derive version from git ({e}); "
              "stamping BUILD=0 (auto-update disabled for this bundle)")
        return 0, "", "0.dev"


def _stamp_build_info(worker_dst: Path, build: int, commit: str,
                      version: str, built_at: str) -> None:
    """Overwrite the bundled _build_info.py with the real build stamp."""
    (worker_dst / "_build_info.py").write_text(
        '"""Generated at bundle time by build_band_worker.py — do not edit."""\n'
        "from __future__ import annotations\n\n"
        f"BUILD = {build}\n"
        f"VERSION = {version!r}\n"
        f"COMMIT = {commit!r}\n"
        f"BUILT_AT = {built_at!r}\n\n"
        "def as_dict() -> dict:\n"
        '    return {"version": VERSION, "build": BUILD, "commit": COMMIT, "built_at": BUILT_AT}\n',
        encoding="utf-8",
    )


def _write_manifest(build: int, commit: str, version: str, built_at: str) -> Path:
    """Compute the pyz hash and write a signed manifest next to it."""
    import hashlib
    h = hashlib.sha256()
    with open(OUTPUT, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    manifest = {
        "schema": 1,
        "build": build,
        "version": version,
        "commit": commit,
        "built_at": built_at,
        "filename": OUTPUT.name,
        "sha256": h.hexdigest(),
        "size": OUTPUT.stat().st_size,
    }
    # Optional self-contained download URL (signed) so an in-band push doesn't
    # depend on the target worker having ROOK_UPDATE_URL configured. The build
    # host sets ROOK_PUBLIC_BASE (e.g. https://rook.example.com).
    import os as _os
    base = _os.environ.get("ROOK_PUBLIC_BASE", "").strip().rstrip("/")
    if base:
        manifest["url"] = f"{base}/{OUTPUT.name}"
    import sys as _sys
    _sys.path.insert(0, str(REPO_ROOT))
    from rook.remote.update_keys import sign_manifest
    manifest = sign_manifest(manifest)
    import json
    manifest_path = OUTPUT.with_suffix(".json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    signed = "signed" if manifest.get("sig") else "UNSIGNED"
    print(f"Manifest: {manifest_path}  ({signed})")
    return manifest_path


def build() -> Path:
    if not WORKER_SRC.is_dir():
        raise FileNotFoundError(f"Worker source not found: {WORKER_SRC}")
    if not TELESTHETE_ROOT.is_dir():
        raise FileNotFoundError(
            f"Telesthete source not found: {TELESTHETE_ROOT}\n"
            "Expected at /root/repos/telesthete/telesthete"
        )

    build_num, commit, version = compute_build()
    built_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)

        (t / "__main__.py").write_text(_MAIN, encoding="utf-8")

        # rook.worker package
        rook_dst = t / "rook"
        rook_dst.mkdir()
        (rook_dst / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        _copy_pkg(WORKER_SRC, rook_dst / "worker")
        _stamp_build_info(rook_dst / "worker", build_num, commit, version, built_at)

        # telesthete.protocol package (only protocol/ is imported by rook.worker)
        tel_dst = t / "telesthete"
        tel_dst.mkdir()
        (tel_dst / "__init__.py").write_text("", encoding="utf-8")
        _copy_pkg(TELESTHETE_ROOT / "protocol", tel_dst / "protocol")

        zipapp.create_archive(
            str(t),
            target=str(OUTPUT),
            interpreter="/usr/bin/env python3",
        )

    size = OUTPUT.stat().st_size
    print(f"Built: {OUTPUT}  v{version}  ({size:,} bytes)")
    _write_manifest(build_num, commit, version, built_at)
    return OUTPUT


if __name__ == "__main__":
    build()
