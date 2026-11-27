"""call: the call log of the phone the Rook Android app runs on, and placing calls from it (brief AP3),
shipped as a plugin installed by default. call.list and call.dial are 0.5's (origin/castling
rook/plugins/call): the same names, arguments and replies.

Nothing here bounds a call (Bake, 17 November): call.list gives every call in the log unless the caller
passes `limit` (0.5 cut at 30 by default, and capped `limit` and the number's length). call.dial places
the call at once: nothing of ours stands in front of it.

The first call asks for the permission it needs (READ_CALL_LOG, CALL_PHONE) through the app's bridge
(systems.bake.rook.core.worker.Bridge), as Android's own prompt; refused, the error says where to allow
it. Placing a call opens the phone's call screen, which Android lets an app in the background do only
once it may display over other apps: the bridge asks for that too, the first time.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import re
from urllib.parse import quote

from rook_worker import Cap

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

DIRECTIONS = {1: "incoming", 2: "outgoing", 3: "missed", 4: "voicemail", 5: "rejected", 6: "blocked"}
NUMBER = re.compile(r"\+?[0-9 ()./*#-]+\Z")


def _check(args, cap, allowed, required=()):
    """Refuse an argument the cap does not take, so it is never silently ignored (C3)."""
    extra = sorted(set(args) - set(allowed))
    if extra:
        raise ValueError(f"{cap} takes no argument {', '.join(extra)}; it takes {', '.join(allowed) or 'none'}")
    for name in required:
        if args.get(name) is None:
            raise ValueError(f"{cap} needs {name}")


def _bridge():
    return jclass("systems.bake.rook.core.worker.Bridge")


def _need(permission, cap):
    refused = _bridge().ask("android.permission." + permission, cap)
    if refused is not None:
        raise ValueError(str(refused))


def _query(uri, columns, where=None, args=None, order=None, limit=None):
    """Rows from a content provider, each column as str, int, float or None; `limit` stops after that many."""
    cursor = _bridge().context().getContentResolver().query(
        jclass("android.net.Uri").parse(uri), columns, where, args, order)
    if cursor is None:
        raise ValueError(f"{uri}: the phone gave no answer")
    rows = []
    try:
        names = [str(n) for n in cursor.getColumnNames()]
        while (limit is None or len(rows) < limit) and cursor.moveToNext():
            rows.append({n: _value(cursor, i) for i, n in enumerate(names)})
    finally:
        cursor.close()
    return rows


def _value(cursor, i):
    kind = int(cursor.getType(i))       # Cursor.FIELD_TYPE_NULL, INTEGER, FLOAT, STRING, BLOB
    return {1: lambda: int(cursor.getLong(i)), 2: lambda: float(cursor.getDouble(i)),
            3: lambda: str(cursor.getString(i))}.get(kind, lambda: None)()


def _num(value):
    return int(float(value)) if value not in (None, "") else 0


def _list(limit):
    _need("READ_CALL_LOG", "call.list")
    rows = _query("content://call_log/calls", ["number", "name", "type", "duration", "date"], order="date DESC",
                  limit=limit)
    return {"calls": [{"number": r.get("number") or "", "name": r.get("name") or "",
                       "direction": DIRECTIONS.get(_num(r.get("type")), "?"), "duration_s": _num(r.get("duration")),
                       "ts": round(_num(r.get("date")) / 1000.0, 3)} for r in rows]}


async def list_calls(args):
    _check(args, "call.list", ("limit",))
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a whole number, 1 or more; leave it out for every call")
    return await asyncio.to_thread(_list, limit)


def _dial(to):
    _need("CALL_PHONE", "call.dial")
    intent = jclass("android.content.Intent")("android.intent.action.CALL",
                                              jclass("android.net.Uri").parse("tel:" + quote(to, safe="+")))
    out = {"to": to}
    try:
        found = _bridge().context().getPackageManager().resolveActivity(intent, 0)
        app = str(found.activityInfo.packageName) if found is not None else None
    except Exception:
        app = None
    if app and app != "android":
        out["app"] = app
    refused = _bridge().start(intent, "call.dial")
    if refused is not None:
        raise ValueError(str(refused))
    return out


async def dial(args):
    _check(args, "call.dial", ("to",), ("to",))
    to = args["to"]
    if not isinstance(to, str) or not NUMBER.match(to):
        raise ValueError("to must be a phone number: digits, with + ( ) . / * # - and spaces")
    return await asyncio.to_thread(_dial, to)


CAPS = {} if jclass is None else {
    "call.list": Cap(
        "The phone's recent calls, newest first: {calls: [{number, name, direction, duration_s, ts}]}; direction is "
        "incoming, outgoing, missed, voicemail, rejected or blocked; limit gives only that many.",
        {"type": "object", "additionalProperties": False, "properties": {"limit": {"type": "integer", "minimum": 1}}},
        list_calls),
    "call.dial": Cap(
        "Call a phone number now from the phone, as its owner (the phone's speaker and microphone carry the call): {to, app}.",
        {"type": "object", "additionalProperties": False, "required": ["to"], "properties": {
            "to": {"type": "string", "pattern": "^" + NUMBER.pattern[:-2] + "$", "description": "The phone number."}}},
        dial),
}
