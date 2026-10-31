"""call.*: the call log provider (READ_CALL_LOG) and ACTION_CALL (CALL_PHONE)."""

from urllib.parse import quote

from rook.kit import android

DIRECTIONS = {1: "incoming", 2: "outgoing", 3: "missed", 4: "voicemail", 5: "rejected", 6: "blocked"}


def list_calls(ctx, limit):
    android.need("READ_CALL_LOG")
    rows = android.query("content://call_log/calls", ["number", "name", "type", "duration", "date"],
                         order="date DESC", limit=limit)
    return {"calls": [{"number": r.get("number") or "", "name": r.get("name") or "",
                       "direction": DIRECTIONS.get(android.num(r.get("type")), "?"),
                       "duration_s": android.num(r.get("duration")),
                       "ts": round(android.num(r.get("date")) / 1000.0, 3)} for r in rows]}


def dial(ctx, to):
    android.need("CALL_PHONE")
    i = android.intent("android.intent.action.CALL", "tel:" + quote(to, safe="+"))
    app = android.handler(i)
    return {"to": to, **({"app": app} if app else {}), **android.start(i)}
