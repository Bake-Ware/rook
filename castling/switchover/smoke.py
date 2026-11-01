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
- the hub has security off (``hub.status`` ``security``), so nothing in step 2
  waits on a person (castling-switchover-decisions, "Usable first, security later");
- every moved store reads back in full through its caps, paged to the end
  (active and archived records): knowledge, tasks, task comments, projects,
  concepts, huddles, jobs, change records, secret names, rooms and huddles
  as streams, and their text and status events; with ``--verify-json`` each
  count must reach what verify moved;
- a migrated huddle reads back through ``stream.read``.

Requests carry a User-Agent: Cloudflare's browser check (error 1010)
refuses Python-urllib's default one.

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
UA = "rook-cutover-smoke/1.0"
PAGE = 500          # the kit's largest record page
STREAM_PAGE = 200
EVENT_PAGE = 1000   # rook.streams READ_LIMIT
# A record store's list cap and the verify kind whose "moved" its full count must reach.
# Record caps are named from this checkout's manifests (castling/switchover/migrate/names.py).
RECORDS = [
    (names.cap("knowledge", "list"), "knowledge pages"),
    (names.cap("tasks", "list"), "tasks"),
    (names.cap("tasks", "list", "comment"), "task notes -> comments"),
    (names.cap("projects", "list"), "projects"),
    (names.cap("concepts", "list"), "concepts"),
    (names.cap("huddle", "list"), None),
    (names.cap("jobs", "list"), "jobs (disabled, 0.4 definition kept)"),
    (names.cap("changes", "list"), "change records"),
]
STREAMS = "chat rooms and huddles -> streams"
EVENTS = {"text": "chat messages -> text events", "status": "huddle posts and questions -> status events"}
SECRETS = "secrets (names + value hashes)"


class Door:
    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json", "User-Agent": UA})
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


def _records(door: Door, cap: str) -> tuple[int, str | None]:
    """Every record a list cap holds, active then archived, paged to the end."""
    n = 0
    for archived in (False, True):
        offset = 0
        while True:
            r = door.call(cap, {"limit": PAGE, "offset": offset, **({"archived": True} if archived else {})})
            if r.get("ok") is not True:
                return n, r.get("error", "failed")
            got = len(_items(r, "records"))
            n, offset = n + got, offset + got
            if got < PAGE:
                break
    return n, None


def _streams(door: Door, kind: str) -> tuple[list, str | None]:
    out, offset = [], 0
    while True:
        r = door.call("stream.list", {"kind": kind, "limit": STREAM_PAGE, "offset": offset})
        if r.get("ok") is not True:
            return out, r.get("error", "failed")
        rows = _items(r, "streams")
        out += rows
        nxt = (r.get("result") or {}).get("next") if isinstance(r.get("result"), dict) else None
        if not rows or nxt in (None, offset):
            return out, None
        offset = nxt


def _events(door: Door, stream: str, kind: str) -> tuple[int, str | None]:
    n, after = 0, 0
    while True:
        r = door.call("stream.read", {"stream": stream, "after": after, "limit": EVENT_PAGE, "types": [kind]})
        if r.get("ok") is not True:
            return n, r.get("error", "failed")
        evs = _items(r, "events")
        n += len(evs)
        nxt = (r.get("result") or {}).get("next", after)
        if not evs or nxt == after:
            return n, None
        after = nxt


def _line(what: str, n: int, err: str | None, moved: dict, kind: str | None) -> tuple[bool, str]:
    want = moved.get(kind, 0) if kind else 0
    ok = err is None and n >= want
    return ok, (f"{what}: {n}" + (f" (verify moved {moved[kind]})" if kind in moved else "")
                + (f" {err}" if err else ""))


def run(door: Door, workers: list, moved: dict) -> list[tuple[bool, str]]:
    out = []
    ws = door.request("GET", "/workers")
    rows = ws.get("workers") if isinstance(ws.get("workers"), list) else []
    out.append(("error" not in ws, f"hub answers: {len(rows)} workers known"))
    # Nothing in step 2 waits on a person: the hub must have security off (castling-switchover-decisions).
    st = door.call("hub.status", {})
    mode = (st.get("result") or {}).get("security") if st.get("ok") is True else None
    out.append((mode == "off", f"hub.security: {mode or st.get('error', 'unknown')}"
                + ("" if mode == "off" else " (step 2 needs security off: device codes and holds would wait on a person)")))
    for name in workers:
        row = next((w for w in rows if w.get("name") == name), None)
        online = bool(row and (row.get("online") or row.get("state") == "online"))
        ping = door.call("worker.ping", {}, worker=name)
        out.append((online and ping.get("ok") is True,
                    f"worker {name}: {'online' if online else 'NOT online'}, ping "
                    f"{'ok' if ping.get('ok') else ping.get('error', 'failed')}"))
    for cap, kind in RECORDS:
        n, err = _records(door, cap)
        out.append(_line(f"{cap} (all pages, active + archived)", n, err, moved, kind))
    r = door.call("secret.list", {})
    out.append(_line("secret.list", len(_items(r, "secrets")), None if r.get("ok") is True else r.get("error"),
                     moved, SECRETS))
    streams, errs = [], []
    for k in ("room", "huddle"):
        got, err = _streams(door, k)
        streams += got
        errs += [err] if err else []
        out.append((err is None, f"stream.list {k} (all pages): {len(got)}" + (f" {err}" if err else "")))
    out.append(_line("rooms + huddles as streams", len(streams), errs[0] if errs else None, moved, STREAMS))
    for etype, kind in EVENTS.items():
        n, err = 0, None
        for st in streams:
            got, e = _events(door, st["id"], etype)
            n += got
            err = err or e
        out.append(_line(f"{etype} events in those streams", n, err, moved, kind))
    huddles = [st for st in streams if st.get("kind") == "huddle"] or streams
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
