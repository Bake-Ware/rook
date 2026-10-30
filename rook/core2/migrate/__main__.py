"""python -m rook.core2.migrate --from <0.4 data copy> --to <0.5 hub.yaml> [--apply] [--json]

Copy the 0.4 hub's data folder first, each database with
``sqlite3 <db> ".backup <copy>"`` (plus ``trust.json`` and ``vault.key``).
Without ``--apply`` nothing is written: read the report, then run again with
``--apply`` while the 0.5 hub is stopped.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import MigrateError, NotACopy, migrate, render
from .convert import NAME


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m rook.core2.migrate", description="Move a 0.4 hub's state to 0.5.")
    ap.add_argument("--from", dest="src", required=True, help="a copy of the 0.4 hub's data folder (never the live one)")
    ap.add_argument("--to", dest="config", required=True, help="the 0.5 hub's config (hub.yaml)")
    ap.add_argument("--apply", action="store_true", help="write (the 0.5 hub stopped); default: report only")
    ap.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                    help="a 0.4 person (id or derived name) -> their 0.5 user name; repeatable")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    a = ap.parse_args(argv)
    rename = {}
    for item in a.rename:
        old, _, new = item.partition("=")
        if not old or not NAME.fullmatch(new):
            print(f"--rename {item}: OLD=NEW, NEW a user name (lowercase letters, digits, - and _)", file=sys.stderr)
            return 2
        rename[old] = new
    try:
        report = migrate(a.src, a.config, apply=a.apply, rename=rename)
    except (MigrateError, NotACopy) as e:
        print(f"migrate: {e}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2) if a.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
