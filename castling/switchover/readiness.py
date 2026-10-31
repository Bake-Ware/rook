"""Which of the switchover's readiness gates (docs/castling-switchover.md,
"Readiness") the code in this checkout meets. Read-only; no network unless
``--prs`` (then ``gh`` lists the castling PRs still open).

    python castling/switchover/readiness.py [--prs]

Each gate is a file that has to exist or a ``TODO(castling:<owner>)`` that has
to be gone. Only the gates in CUTOVER hold the cutover (D5, castling-switchover-
decisions: "cutover held for gates R1-R5, R10 and R14; the rest can follow").
The rest, and the follow-ups a person ticks (soak, review findings, on-device
checks), are listed for the record and hold nothing. Exit 0 when every
cutover gate passes.
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
# | ("nocap", "name:cap") no plugin of that name declares it | ("record", "name:type") the plugin
# declares that record type | ("all", [checks]) every one passes. Gates say what Bake's decisions
# ask for, never "the 0.4 X exists on 0.5".
GATES = [
    ("R1", "the hub has a vault: {{secret:NAME}} and secret settings read <data>/vault.db", ("todo", "vault")),
    ("R2", "worker events reach the hub (the attention job, sms.received, claudecode rules)", ("todo", "events")),
    ("R3", "streams have a hub index (stream.list / stream.search over every stream)", ("todo", "streams")),
    # R4: castling-caps C2, "records only, with apply, test and rollback as a job the record links".
    ("R4", "change records are records (C2): a declared change record, no apply/revert runner",
     ("all", [("todo", "changes"), ("record", "change:change"), ("nocap", "change:apply"),
              ("nocap", "change:revert")])),
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
    ("R14", "agents land work on 0.5 without 0.4: task sign-off, then github.merge at the signed-off head",
     ("plugin", "github:merge")),
]
CUTOVER = {"R1", "R2", "R3", "R4", "R5", "R10", "R14"}
FOLLOW_UPS = [
    "soak numbers meet the bars in the plan (docs/castling-soak.md)",
    "every castling/review.md finding is fixed or accepted by Bake",
    "the watch app on 0.5 checked on the watch",
    "the phone app's 0.5 mode checked on bakephone",
    "the desktop app's \"Add this computer\" on one Linux and one Windows machine",
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


def _manifests() -> list:
    return [yaml.safe_load(f.read_text(encoding="utf-8")) or {} for f in (REPO / "rook/plugins").glob("*/plugin.yaml")]


def _one(kind: str, arg, found: dict) -> tuple:
    """One check -> (ok, why)."""
    if kind == "all":
        parts = [_one(k, a, found) for k, a in arg]
        return all(ok for ok, _ in parts), "; ".join(("" if ok else "NOT: ") + why for ok, why in parts)
    if kind in ("plugin", "nocap", "record"):
        name, _, item = arg.partition(":")
        mine = [m for m in _manifests() if m.get("plugin") == name]
        if kind == "record":
            return (any(item in (m.get("records") or {}) for m in mine),
                    f"plugin {name} declares record {item}")
        has = any(item in (m.get("caps") or {}) for m in mine)
        if kind == "nocap":
            return not has, f"plugin {name} has no cap {item}"
        return has, f"plugin {name} with cap {item} in rook/plugins/"
    if kind == "file":
        return (REPO / arg).exists(), arg
    if kind == "cap":
        path, _, cap = arg.partition(":")
        caps = (yaml.safe_load((REPO / path).read_text(encoding="utf-8")) or {}).get("caps") or {}
        return cap in caps, f"cap {cap} in {path}"
    if kind == "grep":
        folder, _, word = arg.partition(":")
        hits = [f for f in (REPO / folder).rglob("*") if f.is_file() and f.suffix in (".py", ".yaml")
                and word in f.read_text(encoding="utf-8", errors="replace")]
        return bool(hits), f"{word!r} under {folder}/"
    left = found.get(arg, [])
    return not left, f"{len(left)} TODO(castling:{arg})" + (f" e.g. {left[0]}" if left else "")


def check(found: dict) -> list:
    rows = []
    for gate, what, (kind, arg) in GATES:
        ok, why = _one(kind, arg, found)
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
        hold = "holds cutover" if gate in CUTOVER else "follow-up"
        print(f"[{'x' if ok else ' '}] {gate} [{hold}] {what}  ({why})")
    print("\nFollow-ups for a person to tick (they hold nothing):")
    print("\n".join(f"[ ] {p}" for p in FOLLOW_UPS))
    if a.prs:
        prs = open_prs()
        print(f"\nOpen PRs into castling: {len(prs)}")
        print("\n".join(f"  {p}" for p in prs))
    return 0 if all(ok for gate, ok, _, _ in rows if gate in CUTOVER) else 1


if __name__ == "__main__":
    sys.exit(main())
