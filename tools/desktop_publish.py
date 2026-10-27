#!/usr/bin/env python3
"""Publish the desktop app's unsigned bundles on a GitHub release.

The desktop workflow (.github/workflows/desktop.yml) builds the bundles on
every push to master and keeps them as run artifacts. This takes one run's
artifacts and attaches them to a release under the names the hub's update
check offers (``/updates/desktop.json``: release files starting
``rook-desktop`` ending ``-setup.exe``, ``.AppImage``, ``.deb`` or ``.dmg``),
and adds their hashes to the release's ``SHA256SUMS``. Only a green run of
a push to master is published:

    python tools/desktop_publish.py --run 123456789 --tag v0.4.0 [--dry-run]

``Rook_0.2.0_x64-setup.exe`` becomes ``rook-desktop-0.2.0-x64-setup.exe``.
With ``--prune``, other versions' ``rook-desktop-*`` files leave the release,
so hubs offer one version. Needs ``gh`` signed in with write access.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "Bake-Ware/rook"
ENDINGS = ("-setup.exe", ".AppImage", ".deb", ".dmg")
TAURI_NAME = re.compile(r"^Rook_(\d+(?:\.\d+)+)_(.+)$")


def app_version() -> str:
    conf = json.loads((ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    return conf["version"]


def release_name(name: str, version: str) -> str:
    """The published name of a Tauri bundle, ``rook-desktop-<version>-<rest>``."""
    m = TAURI_NAME.match(name)
    if not m or not name.endswith(ENDINGS):
        raise ValueError(f"not a desktop bundle: {name}")
    if m.group(1) != version:
        raise ValueError(f"{name} is version {m.group(1)}, the app is {version}")
    return f"rook-desktop-{version}-{m.group(2)}"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def merged_sums(old: str, new: dict[str, str]) -> str:
    """SHA256SUMS with every ``rook-desktop`` line replaced by ``new``."""
    keep = [line for line in old.splitlines()
            if line.strip() and not line.split()[-1].lstrip("*").startswith("rook-desktop")]
    keep += [f"{digest}  {name}" for name, digest in sorted(new.items())]
    return "\n".join(keep) + "\n"


def publishable(run: dict) -> str | None:
    """Why a workflow run must not be published, or None: only a finished,
    green desktop build of a push to master (what was reviewed and merged)."""
    if run.get("workflowName") != "desktop":
        return f"run is {run.get('workflowName')!r}, not the desktop workflow"
    if run.get("event") != "push" or run.get("headBranch") != "master":
        return f"run is a {run.get('event')} on {run.get('headBranch')}, not a push to master"
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        return f"run is {run.get('status')}/{run.get('conclusion')}, not a green build"
    return None


def gh(*args: str, capture: bool = False) -> str:
    out = subprocess.run(["gh", *args], check=True, text=True,
                         stdout=subprocess.PIPE if capture else None)
    return out.stdout if capture else ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--run", required=True, help="the desktop workflow run id whose artifacts to publish")
    ap.add_argument("--tag", required=True, help="the release to attach them to, e.g. v0.4.0")
    ap.add_argument("--prune", action="store_true", help="remove other versions' rook-desktop files")
    ap.add_argument("--dry-run", action="store_true", help="download and name, but upload nothing")
    a = ap.parse_args(argv)
    version = app_version()
    run = json.loads(gh("run", "view", a.run, "-R", REPO, "--json",
                        "workflowName,event,headBranch,status,conclusion,headSha", capture=True))
    why = publishable(run)
    if why:
        print(f"not publishing: {why}", file=sys.stderr)
        return 1
    print(f"desktop {version} from master {run['headSha'][:12]} (run {a.run})")
    with tempfile.TemporaryDirectory(prefix="rook-desktop-") as tmp:
        dl, out = Path(tmp, "artifacts"), Path(tmp, "publish")
        out.mkdir()
        gh("run", "download", a.run, "-R", REPO, "-D", str(dl))
        files = {}
        for f in sorted(dl.rglob("*")):
            if f.is_file() and f.name.endswith(ENDINGS):
                target = out / release_name(f.name, version)
                target.write_bytes(f.read_bytes())
                files[target.name] = sha256(target)
        if not any(n.endswith("-setup.exe") for n in files):
            print("no Windows installer in that run's artifacts", file=sys.stderr)
            return 1
        gh("release", "download", a.tag, "-R", REPO, "-p", "SHA256SUMS", "-D", tmp, "--clobber")
        sums = out / "SHA256SUMS"
        sums.write_text(merged_sums(Path(tmp, "SHA256SUMS").read_text(encoding="utf-8"), files),
                        encoding="utf-8")
        for name, digest in files.items():
            print(f"{digest}  {name}")
        if a.dry_run:
            print("dry run: nothing uploaded")
            return 0
        gh("release", "upload", a.tag, "-R", REPO, "--clobber", *(str(out / n) for n in files), str(sums))
        if a.prune:
            assets = json.loads(gh("release", "view", a.tag, "-R", REPO, "--json", "assets", capture=True))
            for asset in assets["assets"]:
                n = asset["name"]
                if n.startswith("rook-desktop") and n not in files:
                    gh("release", "delete-asset", a.tag, n, "-R", REPO, "--yes")
                    print(f"removed {n}")
    print(f"published desktop {version} on {a.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
