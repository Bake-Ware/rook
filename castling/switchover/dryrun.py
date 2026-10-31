"""The switchover's data rehearsal: the 0.5 migration's dry run against a
0.4 hub data copy that is already on this machine, never the live hub.

    python castling/switchover/dryrun.py --snapshot ~/rook-backups/hub-offload/<name>.tar --out DIR
    python castling/switchover/dryrun.py --from <copy of the 0.4 data folder> --out DIR

``--snapshot`` takes a hub snapshot tar as the 0.4 deploy scripts make them
(``<name>/data/db/...`` from ``sqlite3 .backup``, plus
``<name>/data/var-lib-rook-band-mcp-nodb.tar`` holding ``trust.json`` and
``vault.key``): only those files are unpacked, into a private temp folder
that is deleted at the end (it holds the vault key). A throwaway 0.5 hub
config is made beside it with ``init``; nothing is applied anywhere.

``DIR`` (made 0700, files 0600) gets:

- ``report.md`` / ``report.json``: the migration's own report. It names
  secrets, machines and people (never a secret's value): keep it private.
- ``summary.md``: counts only, with no names, safe to paste into a public
  doc (docs/castling-switchover.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from castling.switchover.migrate import migrate, render  # noqa: E402

INNER = re.compile(r"(^|/)data/[^/]*nodb\.tar$")      # the data folder minus databases
LOOSE = ("trust.json", "vault.key")                   # the loose files the migration reads
INIT_SECS = 60


def unpack(snapshot: Path, dest: Path) -> Path:
    """The snapshot's databases and loose files -> ``dest`` (a 0.4 copy)."""
    with tarfile.open(snapshot, "r:*") as outer:
        for m in outer:
            parts = m.name.split("/")
            db = next((i + 2 for i in range(len(parts) - 2) if parts[i:i + 2] == ["data", "db"]), None)
            if m.isfile() and db:
                _write(dest / "db", parts[db:], outer.extractfile(m))
            elif m.isfile() and INNER.search(m.name):
                with tarfile.open(fileobj=outer.extractfile(m), mode="r|*") as inner:
                    for im in inner:
                        if im.isfile() and len(im.name.split("/")) == 2 and im.name.split("/")[1] in LOOSE:
                            _write(dest, [im.name.split("/")[1]], inner.extractfile(im))
    if not (dest / "db").is_dir():
        raise SystemExit(f"{snapshot}: no data/db/ in it: not a 0.4 hub snapshot")
    return dest


def _write(root: Path, parts: list, src) -> None:
    if not parts or any(p in ("", ".", "..") for p in parts):
        return
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(src, out)


def throwaway_hub(folder: Path, owner: str, url: str) -> Path:
    """A 0.5 hub config made by ``init`` (its token and recovery key are
    discarded: this hub never runs). ``url`` is what the re-enroll checklist
    tells machines."""
    cfg = folder / "hub05" / "hub.yaml"
    cfg.parent.mkdir(mode=0o700)
    subprocess.run([sys.executable, "-m", "rook.core2.hub", "--config", str(cfg), "init", "--owner", owner,
                    "--relay", "127.0.0.1:9", "--bind", "127.0.0.1:9", "--url", url], cwd=REPO, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=INIT_SECS)
    return cfg


NOTE_KINDS = (("API key", "API keys to mint again"), ("link ", "chat links to make again"),
              ("chat space", "chat spaces to share again"), ("trust", "trust entries not carried over"),
              ("grant", "grant notes"))


def summary(r: dict, snapshot: str) -> str:
    """The report as counts, with no names of secrets, machines or people."""
    people: dict = {}
    for p in r["people"]:
        people[p["preset"]] = people.get(p["preset"], 0) + 1
    wb = r.get("workbench") or {}
    notes: dict = {}
    for n in r["notes"]:
        kind = next((label for key, label in NOTE_KINDS if key in n), "other notes")
        notes[kind] = notes.get(kind, 0) + 1
    j = r["journal"] or {}
    renamed = sum(w["name05"] != w["name"] for w in r["workers"])
    found = [k for k, v in r["found"].items() if v]
    lines = [
        f"- source: `{snapshot}`",
        f"- stores found: {len(found)} of {len(r['found'])} ({', '.join(found)})",
        f"- people: {len(r['people'])} ({', '.join(f'{v} {k}' for k, v in sorted(people.items())) or 'none'})",
        f"- sacred machines: {len(r['sacred'])}; project trust levels carried: {len(wb.get('trust') or ())}",
        "- workbench records: " + (f"not imported ({wb['not_imported']})" if wb.get("not_imported") else
                                   ", ".join(f"{k} {v}" for k, v in (wb.get("imported") or {}).items())),
        f"- vault: {len(r['secrets'])} secrets would be re-sealed (names in the private report only)",
        f"- house agent memory: {r['memory']['memory']} memories, {r['memory']['notepad']} notepad items; "
        f"not moved: {json.dumps(r['memory']['skipped'])}",
        f"- journal: {j.get('rows', 0)} calls archived read-only"
        + (f" ({_day(j['first'])} to {_day(j['last'])})" if j.get("rows") else ""),
        f"- machines to re-enroll: {len(r['workers'])} ({renamed} need a new name for 0.5)",
        "- for a person to look at: " + (", ".join(f"{v} {k}" for k, v in notes.items()) or "nothing"),
    ]
    return "\n".join(lines) + "\n"


def _day(ts) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d") if ts else "?"


def _private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--snapshot", type=Path, help="a 0.4 hub snapshot tar (hub-offload)")
    src.add_argument("--from", dest="copy", type=Path, help="a copy of the 0.4 hub's data folder")
    ap.add_argument("--out", type=Path, required=True, help="where the reports go (made private)")
    ap.add_argument("--owner", default="bake", help="the 0.5 owner's user name (as on 0.4)")
    ap.add_argument("--hub-url", default="https://HUB", help="the 0.5 hub's address, for the re-enroll lines")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(a.out, 0o700)
    with tempfile.TemporaryDirectory(prefix="rook-switchover-") as tmp:   # 0700; holds vault.key while it lives
        work = Path(tmp)
        copy = unpack(a.snapshot, work / "copy04") if a.snapshot else a.copy
        report = migrate(copy, throwaway_hub(work, a.owner, a.hub_url), apply=False)
    report["from"] = str(a.snapshot or a.copy)
    report["to"] = "(a throwaway 0.5 hub config, deleted)"
    _private(a.out / "report.json", json.dumps(report, indent=2) + "\n")
    _private(a.out / "report.md", render(report))
    text = summary(report, report["from"])
    _private(a.out / "summary.md", text)
    print(text, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
