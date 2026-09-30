"""`rook skill`: install or package the Rook agent skill.

    rook skill install [--harness claude|codex] [--dest DIR] [--hub URL] [--token T]
    rook skill package [-o FILE] [--hub URL] [--token T]
    rook skill path

Without --hub the skill bundled with this Rook install is used (generic).
With --hub it is fetched from ``<hub>/skill/rook.skill``; a token (--token or
ROOK_TOKEN) makes the hub include its site notes.
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

from ..band_mcp import skill as _skill

MAX_BYTES = 2 << 20


def harness_dir(harness: str) -> Path:
    """User-scope skills folder for ``harness``, including the ``rook/`` leaf."""
    if harness == "claude":
        base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        return base / "skills" / _skill.SKILL_NAME
    if harness == "codex":
        # Codex reads user skills from ~/.agents/skills/<name>/SKILL.md.
        return Path.home() / ".agents" / "skills" / _skill.SKILL_NAME
    raise ValueError(f"unknown harness {harness!r} (claude|codex)")


def unpack(data: bytes) -> dict[str, str]:
    """Read a .skill zip into {relative path: text}. Only Markdown files under
    the ``rook/`` root folder are accepted; anything else is refused."""
    out: dict[str, str] = {}
    prefix = _skill.SKILL_NAME + "/"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            rel = name[len(prefix):] if name.startswith(prefix) else None
            parts = Path(rel).parts if rel else ()
            if (not rel or rel.startswith("/") or ".." in parts or "\\" in rel
                    or not rel.endswith(".md")):
                raise ValueError(f"unexpected entry in skill archive: {name!r}")
            if info.file_size > MAX_BYTES:
                raise ValueError(f"skill entry too large: {name!r}")
            out[rel] = z.read(info).decode("utf-8")
    if "SKILL.md" not in out:
        raise ValueError("skill archive has no rook/SKILL.md")
    return out


def fetch(hub: str, token: str | None) -> dict[str, str]:
    url = hub.rstrip("/") + "/skill/rook.skill"
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 — operator-given URL
        data = resp.read(MAX_BYTES * 4 + 1)
    if len(data) > MAX_BYTES * 4:
        raise ValueError("skill archive too large")
    return unpack(data)


def _source(args) -> dict[str, str]:
    if args.hub:
        return fetch(args.hub, args.token or os.environ.get("ROOK_TOKEN"))
    return _skill.files()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rook skill", description="Install or package the Rook agent skill.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("install", "package"):
        p = sub.add_parser(name)
        p.add_argument("--hub", help="hub base URL, e.g. https://hub.example.com")
        p.add_argument("--token", help="bearer token (default: env ROOK_TOKEN); adds site notes")
        if name == "install":
            p.add_argument("--harness", choices=("claude", "codex"), default="claude")
            p.add_argument("--dest", help="target folder (the skill's own rook/ folder)")
        else:
            p.add_argument("-o", "--output", default="rook.skill")
    sub.add_parser("path", help="print where the bundled skill lives")
    args = ap.parse_args(argv)

    if args.cmd == "path":
        print(_skill.skill_dir())
        return 0
    try:
        file_map = _source(args)
    except Exception as e:  # noqa: BLE001 — CLI surface
        print(f"rook skill: {e}", file=sys.stderr)
        return 1
    if args.cmd == "package":
        Path(args.output).write_bytes(_skill.package(file_map))
        print(f"wrote {args.output} ({len(file_map)} files)")
        return 0
    dest = Path(args.dest).expanduser() if args.dest else harness_dir(args.harness)
    written = _skill.install(file_map, dest)
    site = " + site notes" if _skill.SITE_FILE in file_map else ""
    print(f"installed Rook skill{site} into {dest} ({len(written)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
