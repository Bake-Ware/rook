"""python -m castling.switchover.migrate --from <0.4 data copy> --to <0.5 hub.yaml> [--apply | --verify] [--json]

Copy the 0.4 hub's data folder first, each database with
``sqlite3 <db> ".backup <copy>"`` (plus ``trust.json`` and ``vault.key``).
Without ``--apply`` nothing is written: read the report, then run again with
``--apply`` while the 0.5 hub is stopped, then ``--verify``: it exits 1
unless every 0.4 artifact is moved or left on purpose (and archived).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import MigrateError, NotACopy, migrate, render, verify
from .convert import NAME


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m castling.switchover.migrate", description="Move a 0.4 hub's state to 0.5.")
    ap.add_argument("--from", dest="src", required=True, help="a copy of the 0.4 hub's data folder (never the live one)")
    ap.add_argument("--to", dest="config", required=True, help="the 0.5 hub's config (hub.yaml)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write (the 0.5 hub stopped); default: report only")
    mode.add_argument("--verify", action="store_true",
                      help="check the move: per kind moved / left on purpose / missing; exit 1 if anything is missing")
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
        if a.verify:
            report = verify.verify(a.src, a.config)
        else:
            report = migrate(a.src, a.config, apply=a.apply, rename=rename)
    except (MigrateError, NotACopy, ValueError, OSError) as e:
        print(f"migrate: {e}", file=sys.stderr)
        return 2
    if a.verify:
        print(json.dumps(report, indent=2) if a.json else verify.render(report))
        return 0 if report["ok"] else 1
    print(json.dumps(report, indent=2) if a.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
