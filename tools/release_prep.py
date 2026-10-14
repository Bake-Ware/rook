#!/usr/bin/env python3
"""Stamp a checkout for a release build (run by .github/workflows/release.yml).

    python tools/release_prep.py --tag v0.2.0 \
        [--telesthete-wheel-url URL --telesthete-wheel-sha256 HEX]

Before ``python -m build`` and ``docker build``, on a clean checkout of the tag:

1. Sets the package version from the tag: ``v0.2.0`` -> ``0.2.0`` in
   pyproject.toml and ``rook/__init__.py`` (``v0.3.0-rc1`` -> ``0.3.0rc1``).
2. Writes ``rook/_release.py`` with that version and the commit's build number
   and ``<build>.<adjective>.<noun>`` name, so a hub installed from the wheel
   stamps the worker bundles it builds (OTA needs a real build number), plus
   ``--relay-version``: the telesthete release whose relay goes with it.
3. With ``--telesthete-wheel-url``: replaces the ``telesthete @ git+https://…``
   pin with ``telesthete @ <wheel URL>#sha256=<hash>``. A git direct reference
   makes pip clone the repo, so installing the release wheel would need git;
   a hash-pinned wheel URL needs only HTTPS, and pip verifies the hash.

The build number is computed before anything is modified (a modified tree
would otherwise count as dirty). ``--print-telesthete-pin`` prints the pinned
telesthete commit and exits.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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


def release_module(version: str, build: int, commit: str, name: str, relay: str = "") -> str:
    if relay and not re.fullmatch(r"v\d[0-9A-Za-z.\-]*", relay):
        raise ValueError(f"--relay-version must be a telesthete tag like v0.3.0, not {relay!r}")
    src = (ROOT / "rook" / "_release.py").read_text(encoding="utf-8")
    values = {"VERSION": repr(version), "BUILD": str(build), "COMMIT": repr(commit),
              "NAME": repr(name), "RELAY": repr(relay)}
    for key, value in values.items():
        src, n = re.subn(rf"(?m)^{key}(\s*)=\s*[^#\n]*?(\s*#|$)", rf"{key}\g<1>= {value}\g<2>", src, count=1)
        if n != 1:
            raise ValueError(f"no {key} in rook/_release.py")
    return src


def git_build(root: Path = ROOT) -> tuple[int, str]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, text=True).strip()
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("tracked files are modified: release from a clean checkout of the tag")
    return int(git("rev-list", "--count", "HEAD")), git("rev-parse", "--short", "HEAD")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tag", help="release tag, e.g. v0.2.0")
    ap.add_argument("--telesthete-wheel-url", default="")
    ap.add_argument("--telesthete-wheel-sha256", default="")
    ap.add_argument("--relay-version", default="",
                    help="telesthete release tag of the relay this release ships (v0.3.0)")
    ap.add_argument("--print-telesthete-pin", action="store_true")
    a = ap.parse_args(argv)

    if a.print_telesthete_pin:
        pin = pinned_telesthete_rev(ROOT / "pyproject.toml")
        if not pin:
            print("no telesthete @ git+…@<sha> pin in pyproject.toml", file=sys.stderr)
            return 1
        print(pin)
        return 0
    if not a.tag:
        ap.error("--tag is required")
    if bool(a.telesthete_wheel_url) != bool(a.telesthete_wheel_sha256):
        ap.error("--telesthete-wheel-url and --telesthete-wheel-sha256 go together")

    version = version_from_tag(a.tag)
    build, commit = git_build()
    name = f"{build}.{build_name(commit)}"

    pyproject = ROOT / "pyproject.toml"
    text = set_project_version(pyproject.read_text(encoding="utf-8"), version)
    if a.telesthete_wheel_url:
        text = set_telesthete_requirement(text, a.telesthete_wheel_url, a.telesthete_wheel_sha256)
    init = ROOT / "rook" / "__init__.py"
    init_text = set_init_version(init.read_text(encoding="utf-8"), version)
    release = release_module(version, build, commit, name, a.relay_version)

    pyproject.write_text(text, encoding="utf-8")
    init.write_text(init_text, encoding="utf-8")
    (ROOT / "rook" / "_release.py").write_text(release, encoding="utf-8")
    print(f"rook {version} = build {name} (commit {commit})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
