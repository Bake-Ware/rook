"""python -m rook.kit.manifest validate <folder> | diff <old> <new>"""

from __future__ import annotations

import argparse
import sys

from . import check_bump, diff, file_hashes, load, trust_class
from .load import ManifestError, check


def _validate(folder) -> int:
    data, issues = check(folder)
    for i in issues:
        print(i)
    errors = sum(i.level == "error" for i in issues)
    if errors:
        print(f"{errors} error(s)")
        return 1
    try:
        cls, n_files = trust_class(folder), len(file_hashes(folder))
    except (OSError, ValueError) as e:
        print(f"error: {e}")
        return 1
    m = load(folder)
    print(f"ok: {m.plugin} {m.version}, {len(m.caps)} cap(s), {n_files} file(s), trust class {cls}")
    return 0


def _diff(old, new) -> int:
    try:
        a, b = load(old), load(new)
    except ManifestError as e:
        print(f"error: {e}")
        return 1
    for c in diff(a, b):
        print(f"{c.kind}: {c.path}: {c.message}")
    issues = check_bump(a, b)
    for i in issues:
        print(i)
    return 1 if issues else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.kit.manifest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="validate a plugin folder").add_argument("folder")
    d = sub.add_parser("diff", help="breaking changes between two versions, and the semver check")
    d.add_argument("old")
    d.add_argument("new")
    a = ap.parse_args(argv)
    return _validate(a.folder) if a.cmd == "validate" else _diff(a.old, a.new)


if __name__ == "__main__":
    sys.exit(main())
