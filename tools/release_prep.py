#!/usr/bin/env python3
"""Stamp a checkout for a release build (run by .github/workflows/release.yml).

    python tools/release_prep.py --tag v0.2.0 \
        [--telesthete-wheel-url URL --telesthete-wheel-sha256 HEX] \
        [--pin-relay v0.3.0 [--relay-dir DIR]]
    python tools/release_prep.py --pin-relay v0.3.0      # move the relay pin

Before ``python -m build`` and ``docker build``, on a clean checkout of the tag:

1. Sets the package version from the tag: ``v0.2.0`` -> ``0.2.0`` in
   pyproject.toml and ``rook/__init__.py`` (``v0.3.0-rc1`` -> ``0.3.0rc1``).
2. Writes ``rook/_release.py`` with that version and the commit's build number
   and ``<build>.<adjective>.<noun>`` name, so a hub installed from the wheel
   stamps the worker bundles it builds (OTA needs a real build number).
3. With ``--telesthete-wheel-url``: replaces the ``telesthete @ git+https://…``
   pin with ``telesthete @ <wheel URL>#sha256=<hash>``. A git direct reference
   makes pip clone the repo, so installing the release wheel would need git;
   a hash-pinned wheel URL needs only HTTPS, and pip verifies the hash.
4. With ``--release-json PATH``: writes the release's metadata (version,
   build, channel, the compat floors from rook/core/compat.py, the relay pin,
   the APK and firmware versions, and whether it is ``breaking``) for the
   hub's update service; see rook/hub/release_meta.py. A release is breaking
   when ``--breaking`` says so or it raises a floor over
   ``--previous-release-json`` (the last release's release.json).
5. With ``--pin-relay TAG``: records the telesthete release and the sha256 of
   each ``telesthete-hub-<arch>-linux.tar.gz`` in ``rook/_release.py`` (``RELAY``,
   ``RELAY_SHA256``) and the Dockerfile's default ``TELESTHETE_RELAY_VERSION``.
   The hashes come from the archives themselves: from ``--relay-dir`` (the
   release workflow downloads them there and verifies their build provenance
   first) or downloaded here, and must match each archive's published
   ``.sha256``. A hash already pinned for the same tag must not change.

With ``--tag`` it first runs the user-manual release gate
(``tools/manual_check.py --release``; ``--skip-manual-check`` overrides it)
and writes ``docs/manual/manifest.json`` for the build.

The build number is computed before anything is modified (a modified tree
would otherwise count as dirty). ``--print-telesthete-pin`` prints the pinned
telesthete commit and exits.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rook.hub import release_meta  # noqa: E402
from rook.remote.build_band_worker import build_name, pinned_telesthete_rev  # noqa: E402

# Public PEP 440 release versions we tag: N(.N)* with an optional pre-release
# (a/b/rc), post and dev segment. Tags may write the pre-release with a dash.
_PEP440 = re.compile(r"^\d+(\.\d+)*((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")
_GIT_PIN = re.compile(r'"telesthete\s*@\s*git\+[^"]+"')


def version_from_tag(tag: str) -> str:
    """``v0.2.0`` -> ``0.2.0``; ``v0.3.0-rc1`` -> ``0.3.0rc1``. Raises ValueError."""
    v = tag.strip()
    if v.startswith("refs/tags/"):
        v = v[len("refs/tags/"):]
    if not v.startswith("v"):
        raise ValueError(f"release tags look like v1.2.3, not {tag!r}")
    v = v[1:]
    v = re.sub(r"-(a|b|rc)(\d+)$", r"\1\2", v)
    if not _PEP440.match(v):
        raise ValueError(f"{tag!r} is not a PEP 440 release version (v1.2.3, v1.2.3rc1, …)")
    return v


def set_project_version(pyproject: str, version: str) -> str:
    out, n = re.subn(r'(?m)^version\s*=\s*"[^"]*"', f'version = "{version}"', pyproject, count=1)
    if n != 1:
        raise ValueError("no version = \"…\" line in pyproject.toml")
    return out


def set_init_version(init: str, version: str) -> str:
    out, n = re.subn(r'(?m)^__version__\s*=\s*"[^"]*"', f'__version__ = "{version}"', init, count=1)
    if n != 1:
        raise ValueError("no __version__ in rook/__init__.py")
    return out


def set_telesthete_requirement(pyproject: str, url: str, sha256: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("--telesthete-wheel-sha256 must be 64 lowercase hex digits")
    if not url.startswith("https://") or not url.endswith(".whl"):
        raise ValueError("--telesthete-wheel-url must be an https:// URL of a .whl")
    out, n = _GIT_PIN.subn(f'"telesthete @ {url}#sha256={sha256}"', pyproject)
    if n != 1:
        raise ValueError("expected exactly one telesthete @ git+… requirement in pyproject.toml")
    return out


def release_module(version: str, build: int, commit: str, name: str,
                   src: str | None = None) -> str:
    if src is None:
        src = (ROOT / "rook" / "_release.py").read_text(encoding="utf-8")
    values = {"VERSION": repr(version), "BUILD": str(build), "COMMIT": repr(commit),
              "NAME": repr(name)}
    for key, value in values.items():
        src, n = re.subn(rf"(?m)^{key}(\s*)=\s*[^#\n]*?(\s*#|$)", rf"{key}\g<1>= {value}\g<2>", src, count=1)
        if n != 1:
            raise ValueError(f"no {key} in rook/_release.py")
    return src


RELAY_REPO = "https://github.com/Bake-Ware/telesthete/releases/download"
RELAY_ARCHES = ("x86_64", "aarch64", "armv7")
REQUIRED_ARCHES = ("x86_64", "aarch64")       # armv7 is best effort upstream
_TAG = re.compile(r"v\d[0-9A-Za-z.\-]*")


def relay_asset(arch: str) -> str:
    return f"telesthete-hub-{arch}-linux.tar.gz"


def current_relay_pin(src: str) -> tuple[str, dict]:
    """(RELAY, RELAY_SHA256) as written in a rook/_release.py source."""
    tree = ast.parse(src)
    vals = {t.id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) for t in node.targets
            if isinstance(t, ast.Name) and t.id in ("RELAY", "RELAY_SHA256")}
    return vals.get("RELAY", ""), dict(vals.get("RELAY_SHA256", {}))


def set_relay_pin(src: str, tag: str, shas: dict) -> str:
    if not _TAG.fullmatch(tag):
        raise ValueError(f"--pin-relay takes a telesthete tag like v0.3.0, not {tag!r}")
    missing = [a for a in REQUIRED_ARCHES if a not in shas]
    if missing:
        raise ValueError(f"relay {tag} has no archive for {', '.join(missing)}")
    if any(not re.fullmatch(r"[0-9a-f]{64}", h) for h in shas.values()):
        raise ValueError("relay hashes must be 64 lowercase hex digits")
    old_tag, old_shas = current_relay_pin(src)
    if old_tag == tag:
        changed = {a for a in old_shas if a in shas and old_shas[a] != shas[a]}
        if changed:
            raise ValueError(f"relay {tag} archives changed since they were pinned "
                             f"({', '.join(sorted(changed))}): refusing to re-pin a tag")
    pinned = "{" + ", ".join(f"{a!r}: {shas[a]!r}" for a in RELAY_ARCHES if a in shas) + "}"
    src, n = re.subn(r'(?m)^RELAY\s*=\s*"[^"]*"', f'RELAY = "{tag}"', src, count=1)
    if n != 1:
        raise ValueError("no RELAY in rook/_release.py")
    src, n = re.subn(r"(?ms)^RELAY_SHA256\s*=\s*\{.*?\}(?=[ \t]*(#[^\n]*)?$)", f"RELAY_SHA256 = {pinned}", src, count=1)
    if n != 1:
        raise ValueError("no RELAY_SHA256 in rook/_release.py")
    return src


def set_dockerfile_relay(dockerfile: str, tag: str) -> str:
    out, n = re.subn(r"(?m)^ARG TELESTHETE_RELAY_VERSION=\S*", f"ARG TELESTHETE_RELAY_VERSION={tag}",
                     dockerfile, count=1)
    if n != 1:
        raise ValueError("no ARG TELESTHETE_RELAY_VERSION= in the Dockerfile")
    return out


def _fetch(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "rook-release-prep"}),
                                    timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def relay_hashes(tag: str, relay_dir: Path | None = None) -> dict:
    """sha256 of each relay archive of ``tag``, checked against its .sha256."""
    shas = {}
    for arch in RELAY_ARCHES:
        name = relay_asset(arch)
        if relay_dir is not None:
            path, sums = relay_dir / name, relay_dir / (name + ".sha256")
            if not path.is_file():
                continue
            data = path.read_bytes()
            published = sums.read_text(encoding="utf-8") if sums.is_file() else ""
        else:
            data = _fetch(f"{RELAY_REPO}/{tag}/{name}")
            if data is None:
                continue
            published = (_fetch(f"{RELAY_REPO}/{tag}/{name}.sha256") or b"").decode("utf-8", "replace")
        got = hashlib.sha256(data).hexdigest()
        want = published.split()[0].lower() if published.split() else ""
        if want != got:
            raise ValueError(f"{name}: sha256 {got} does not match its .sha256 ({want or 'missing'})")
        shas[arch] = got
    return shas


def release_json(release_src: str, root: Path | None = None, previous: dict | None = None,
                 breaking: bool = False, breaking_note: str = "") -> dict:
    """release.json for the tree whose rook/_release.py source is ``release_src``
    (stamped: a source checkout has no release to describe)."""
    tree = ast.parse(release_src)
    vals = {t.id: ast.literal_eval(node.value) for node in tree.body
            if isinstance(node, ast.Assign) for t in node.targets if isinstance(t, ast.Name)}
    if not vals.get("VERSION") or not vals.get("BUILD"):
        raise ValueError("rook/_release.py is not stamped: pass --tag too (or run it first)")
    version = vals["VERSION"]
    tag = "v" + version
    return release_meta.build_release_json(
        root or ROOT, tag=tag, version=version, build=int(vals["BUILD"]), name=vals.get("NAME", ""),
        commit=vals.get("COMMIT", ""), relay=vals.get("RELAY", ""), previous=previous,
        breaking=breaking, breaking_note=breaking_note)


def git_build(root: Path = ROOT) -> tuple[int, str]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked files are modified: release from a clean checkout of the tag")
    return int(git("rev-list", "--count", "HEAD")), git("rev-parse", "--short", "HEAD")


def check_manual(tag: str, skip: bool = False):
    """The user-manual release gate (tools/manual_check.py --release): every
    release ships a manual that covers every feature. Returns the bundle, for
    the manifest the release build carries (docs/manual/manifest.json)."""
    sys.path.insert(0, str(ROOT / "tools"))
    import manual_check
    from rook.hub.plugins.manual import bundle as manual_bundle
    problems = [] if skip else manual_check.check(release=tag)
    if problems:
        raise SystemExit("the user manual is out of date (CONTRIBUTING.md, 'The user manual'; "
                         "--skip-manual-check overrides):\n  - " + "\n  - ".join(problems))
    if skip:
        print("warning: the user-manual check was skipped (--skip-manual-check)", file=sys.stderr)
    return manual_bundle.load()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", help="release tag, e.g. v0.2.0")
    ap.add_argument("--telesthete-wheel-url", default="")
    ap.add_argument("--telesthete-wheel-sha256", default="")
    ap.add_argument("--pin-relay", metavar="TAG", default="",
                    help="pin the relay to this telesthete release (records its archives' sha256)")
    ap.add_argument("--relay-dir", type=Path, default=None,
                    help="hash the relay archives in this directory instead of downloading them")
    ap.add_argument("--print-telesthete-pin", action="store_true")
    ap.add_argument("--release-json", type=Path, default=None, metavar="PATH",
                    help="write the release metadata (release.json) here")
    ap.add_argument("--previous-release-json", type=Path, default=None, metavar="PATH",
                    help="the previous release's release.json: a raised compat floor "
                         "makes this release breaking")
    ap.add_argument("--breaking", action="store_true",
                    help="mark the release breaking (operators must approve it)")
    ap.add_argument("--breaking-note", default="", help="why it is breaking, in plain words")
    ap.add_argument("--skip-manual-check", action="store_true",
                    help="cut the release even if the user manual is out of date (emergencies only)")
    a = ap.parse_args(argv)

    if a.print_telesthete_pin:
        pin = pinned_telesthete_rev(ROOT / "pyproject.toml")
        if not pin:
            print("no telesthete @ git+…@<sha> pin in pyproject.toml", file=sys.stderr)
            return 1
        print(pin)
        return 0
    if not a.tag and not a.pin_relay and not a.release_json:
        ap.error("--tag, --pin-relay and/or --release-json is required")
    if (a.previous_release_json or a.breaking or a.breaking_note) and not a.release_json:
        ap.error("--previous-release-json/--breaking go with --release-json")
    if bool(a.telesthete_wheel_url) != bool(a.telesthete_wheel_sha256):
        ap.error("--telesthete-wheel-url and --telesthete-wheel-sha256 go together")
    if a.relay_dir and not a.pin_relay:
        ap.error("--relay-dir goes with --pin-relay")

    release_path = ROOT / "rook" / "_release.py"
    release = release_path.read_text(encoding="utf-8")
    writes: dict[Path, str] = {}
    if a.tag:
        version = version_from_tag(a.tag)
        manual = check_manual(a.tag, skip=a.skip_manual_check)
        build, commit = git_build()          # before anything changes the tree
        name = f"{build}.{build_name(commit)}"
    if a.pin_relay:
        shas = relay_hashes(a.pin_relay, a.relay_dir)
        release = set_relay_pin(release, a.pin_relay, shas)
        dockerfile = ROOT / "Dockerfile"
        writes[dockerfile] = set_dockerfile_relay(dockerfile.read_text(encoding="utf-8"), a.pin_relay)
        print(f"relay pinned to telesthete {a.pin_relay}: "
              + ", ".join(f"{k} {v[:12]}…" for k, v in shas.items()))
    if a.tag:
        pyproject = ROOT / "pyproject.toml"
        text = set_project_version(pyproject.read_text(encoding="utf-8"), version)
        if a.telesthete_wheel_url:
            text = set_telesthete_requirement(text, a.telesthete_wheel_url, a.telesthete_wheel_sha256)
        writes[pyproject] = text
        init = ROOT / "rook" / "__init__.py"
        writes[init] = set_init_version(init.read_text(encoding="utf-8"), version)
        release = release_module(version, build, commit, name, release)
        relay, relay_shas = current_relay_pin(release)
        if not relay or not all(a_ in relay_shas for a_ in REQUIRED_ARCHES):
            raise SystemExit("rook/_release.py pins no relay hashes: pass --pin-relay "
                             f"{relay or 'vX.Y.Z'} (a release must ship verified relay hashes)")
    writes[release_path] = release
    if a.tag and manual is not None:
        writes[manual.directory / "manifest.json"] = json.dumps(manual.manifest(), indent=1,
                                                                ensure_ascii=False) + "\n"
    for path, content in writes.items():
        path.write_text(content, encoding="utf-8")
    if a.tag:
        print(f"rook {version} = build {name} (commit {commit})")
    if a.release_json:
        prev = None
        if a.previous_release_json and a.previous_release_json.is_file():
            try:
                prev = release_meta.parse_release_json(
                    json.loads(a.previous_release_json.read_text(encoding="utf-8")))
            except ValueError as e:
                print(f"ignoring --previous-release-json: {e}", file=sys.stderr)
        meta = release_json(release, previous=prev, breaking=a.breaking,
                            breaking_note=a.breaking_note)
        a.release_json.parent.mkdir(parents=True, exist_ok=True)
        a.release_json.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        print(f"release.json: {meta['version']} ({meta['channel']}"
              + (", BREAKING: " + "; ".join(meta["breaking_reasons"]) if meta["breaking"] else "")
              + ")")
    return 0


if __name__ == "__main__":
    sys.exit(main())
