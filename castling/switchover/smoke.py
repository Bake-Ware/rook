"""Smoke checks for the cutover: does the 0.5 hub serve what the move put in
it, and do its workers answer? Read-only calls only.

    ROOK_TOKEN=<an owner's 0.5 token> python castling/switchover/smoke.py --url https://rook05.example \\
        [--worker cachyrig ...] [--verify-json <data>/migrate/verify.json]

Bake, Oct 31 (castling-switchover-decisions, "Fly it"): cut over once the
artifacts survive and "the hub and bands work correctly". ``migrate
--verify`` checks the first; this checks the second from outside, through
the hub's HTTP door, the way a client sees it:

- the hub answers and lists its workers; each ``--worker`` is online and
  answers ``worker.ping``;
- the moved stores answer through their caps (rooms and huddles, huddles,
  jobs, change records, knowledge, tasks, projects, secret names); with
  ``--verify-json`` each must show at least as many items as verify moved
  (up to a page);
- a migrated huddle reads back through ``stream.read``.

Exit 0 when every check passes. Prints counts and names, never a value.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))       # run from a checkout

from castling.switchover.migrate import names  # noqa: E402

WHY = "Cutover smoke check: read-only."
PAGE = 200
# cap, args, the result's list key, verify kind whose "moved" it should reach. Record caps
# are named from this checkout's manifests (castling/switchover/migrate/names.py).
CHECKS = [
    ("stream.list", {"kind": "room", "limit": PAGE}, "streams", None),
    ("stream.list", {"kind": "huddle", "limit": PAGE}, "streams", None),
    (names.cap("huddle", "list"), {"limit": PAGE}, "records", None),
    (names.cap("jobs", "list"), {"limit": PAGE}, "records", "jobs (disabled, 0.4 definition kept)"),
    (names.cap("changes", "list"), {"limit": PAGE}, "records", "change records"),
    (names.cap("knowledge", "list"), {"limit": PAGE}, "records", "knowledge pages"),
    (names.cap("tasks", "list"), {"limit": PAGE}, "records", "tasks"),
    (names.cap("projects", "list"), {"limit": PAGE}, "records", "projects"),
    ("secret.list", {}, "secrets", "secrets (names + value hashes)"),
]


class Door:
    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return {"ok": False, "error": f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"}
        except (urllib.error.URLError, OSError, ValueError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def call(self, cap: str, args: dict, worker: str = "rook") -> dict:
        return self.request("POST", "/call", {"worker": worker, "cap": cap, "args": args, "why": WHY})


def _items(res: dict, key: str) -> list:
    """The listed items: record caps answer a bare list, others ``{key: [...]}``."""
    r = res.get("result", res)
    if isinstance(r, list):
        return r
    v = r.get(key) if isinstance(r, dict) else None
    return v if isinstance(v, list) else []


def run(door: Door, workers: list, moved: dict) -> list[tuple[bool, str]]:
    out = []
    ws = door.request("GET", "/workers")
    rows = ws.get("workers") if isinstance(ws.get("workers"), list) else []
    out.append(("error" not in ws, f"hub answers: {len(rows)} workers known"))
    for name in workers:
        row = next((w for w in rows if w.get("name") == name), None)
        online = bool(row and (row.get("online") or row.get("state") == "online"))
        ping = door.call("worker.ping", {}, worker=name)
        out.append((online and ping.get("ok") is True,
                    f"worker {name}: {'online' if online else 'NOT online'}, ping "
                    f"{'ok' if ping.get('ok') else ping.get('error', 'failed')}"))
    for cap, args, key, kind in CHECKS:
        r = door.call(cap, args)
        n = len(_items(r, key))
        if key == "records" and r.get("ok") is True:          # archived records list apart
            n += len(_items(door.call(cap, {**args, "archived": True}), key))
        want = min(moved.get(kind, 0), PAGE) if kind else 0
        ok = r.get("ok") is True and n >= want
        out.append((ok, f"{cap} {json.dumps(args)}: {n}" + (f" (verify moved {moved[kind]})" if kind in moved else "")
                    + ("" if r.get("ok") else f" {r.get('error')}")))
    huddles = _items(door.call("stream.list", {"kind": "huddle", "limit": 1}), "streams")
    if huddles:
        r = door.call("stream.read", {"stream": huddles[0]["id"], "limit": 5})
        out.append((r.get("ok") is True and bool(_items(r, "events")),
                    f"stream.read on huddle {huddles[0].get('title', '')[:40]!r}: {len(_items(r, 'events'))} events"))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only smoke checks against a 0.5 hub's HTTP door.")
    ap.add_argument("--url", required=True, help="the hub's HTTP door, e.g. https://rook05.example")
    ap.add_argument("--worker", action="append", default=[], help="a worker that must be online (repeatable)")
    ap.add_argument("--verify-json", help="migrate --verify --json output: lists must reach its moved counts")
    a = ap.parse_args(argv)
    token = os.environ.get("ROOK_TOKEN", "").strip()
    if not token:
        print("smoke: set ROOK_TOKEN to an owner's 0.5 token", file=sys.stderr)
        return 2
    moved = {}
    if a.verify_json:
        with open(a.verify_json, encoding="utf-8") as f:
            moved = {k["kind"]: k["moved"] for k in json.load(f).get("kinds", [])}
    results = run(Door(a.url, token), a.worker, moved)
    for ok, line in results:
        print(f"[{'ok' if ok else 'FAIL'}] {line}")
    return 0 if all(ok for ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
