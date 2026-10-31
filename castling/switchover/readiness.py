"""Which of the switchover's readiness gates (docs/castling-switchover.md,
"Readiness") the code in this checkout meets. Read-only; no network unless
``--prs`` (then ``gh`` lists the castling PRs still open).

    python castling/switchover/readiness.py [--prs]

Each gate is a file that has to exist or a ``TODO(castling:<owner>)`` that has
to be gone. Gates a person decides (soak numbers, the review's findings, the
on-device checks) are listed for the record, never ticked here. Exit 0 when
every code gate passes.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
TODO = re.compile(r"TODO\(castling:([a-z0-9_-]+)\)")
SCAN = ("rook", "android", "desktop", "services", "deploy")

# (gate id, what must be true, check): ("file", path) exists | ("todo", owner) has no markers
# left | ("cap", "manifest.yaml:name") the manifest declares that cap | ("grep", "folder:word")
# a file under the folder mentions the word | ("plugin", "name:cap") a plugin in rook/plugins declares it
GATES = [
    ("R1", "the hub has a vault: {{secret:NAME}} and secret settings read <data>/vault.db", ("todo", "vault")),
    ("R2", "worker events reach the hub (attention, Feed, sms.received, claudecode remote rules)", ("todo", "events")),
    ("R3", "streams have a hub index (sessions list, history importers, desktop Sessions tab)", ("todo", "streams")),
    ("R4", "change records exist (plugin push and risky changes go through them)", ("todo", "changes")),
    ("R5", "off-site hub backup, sealed with the recovery key", ("todo", "backup")),
    ("R6", "a 0.5 release format: workers and apps update without a person at each machine",
     ("cap", "rook/core2/builtin/worker.yaml:update")),
    ("R7", "speech on the hub (the voice assistant)", ("file", "rook/plugins/speech/plugin.yaml")),
    ("R8", "the phone app runs 0.5", ("file", "android/app/src/main/java/systems/bake/rook/Castling.kt")),
    ("R9", "the desktop app runs 0.5", ("file", "desktop/src-tauri/src/hub05.rs")),
    ("R10", "the desktop app updates on 0.5", ("todo", "desktop-updates")),
    ("R11", "the soak results are written up", ("file", "docs/castling-soak.md")),
    ("R12", "the hardening review's report is in", ("file", "castling/review.md")),
    ("R13", "the MCP door speaks OAuth (claude.ai web and mobile connectors can't send a bare bearer)",
     ("grep", "rook/core2/hub/doors:oauth")),
    ("R14", "huddles with a merge vote (how agents land PRs and change requests today)",
     ("plugin", "huddle:vote")),
]
PERSON = [
    "soak numbers meet the bars in the plan (docs/castling-soak.md)",
    "every castling/review.md finding is fixed or accepted by Bake",
    "the watch app on 0.5 checked on the watch",
    "the phone app's 0.5 mode checked on bakephone",
    "a Windows machine runs the 0.5 worker for a week",
]


def todos() -> dict:
    out: dict = {}
    for top in SCAN:
        for path in (REPO / top).rglob("*"):
            if path.is_file() and path.suffix in (".py", ".kt", ".rs", ".yaml", ".js", ".sh", ".md"):
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                for owner in TODO.findall(text):
                    out.setdefault(owner, []).append(str(path.relative_to(REPO)))
    return out


def check(found: dict) -> list:
    rows = []
    for gate, what, (kind, arg) in GATES:
        if kind == "file":
            ok, why = (REPO / arg).exists(), arg
        elif kind == "cap":
            path, _, cap = arg.partition(":")
            caps = (yaml.safe_load((REPO / path).read_text(encoding="utf-8")) or {}).get("caps") or {}
            ok, why = cap in caps, f"cap {cap} in {path}"
        elif kind == "plugin":
            name, _, cap = arg.partition(":")
            ok = any((m.get("plugin") == name and cap in (m.get("caps") or {}))
                     for m in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                               for f in (REPO / "rook/plugins").glob("*/plugin.yaml")))
            why = f"plugin {name} with cap {cap} in rook/plugins/"
        elif kind == "grep":
            folder, _, word = arg.partition(":")
            hits = [f for f in (REPO / folder).rglob("*") if f.is_file() and f.suffix in (".py", ".yaml")
                    and word in f.read_text(encoding="utf-8", errors="replace")]
            ok, why = bool(hits), f"{word!r} under {folder}/"
        else:
            left = found.get(arg, [])
            ok, why = not left, f"{len(left)} TODO(castling:{arg})" + (f" e.g. {left[0]}" if left else "")
        rows.append((gate, ok, what, why))
    return rows


def open_prs() -> list:
    out = subprocess.run(["gh", "pr", "list", "--base", "castling", "--state", "open", "--limit", "100",
                          "--json", "number,title"], cwd=REPO, capture_output=True, text=True, timeout=60)
    if out.returncode:
        return [f"(gh failed: {out.stderr.strip()[:200]})"]
    return [f"#{p['number']} {p['title']}" for p in json.loads(out.stdout)]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Switchover readiness gates against this checkout.")
    ap.add_argument("--prs", action="store_true", help="also list open PRs into castling (gh)")
    a = ap.parse_args(argv)
    rows = check(todos())
    for gate, ok, what, why in rows:
        print(f"[{'x' if ok else ' '}] {gate} {what}  ({why})")
    print("\nFor a person to tick:")
    print("\n".join(f"[ ] {p}" for p in PERSON))
    if a.prs:
        prs = open_prs()
        print(f"\nOpen PRs into castling: {len(prs)}")
        print("\n".join(f"  {p}" for p in prs))
    return 0 if all(ok for _, ok, _, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
