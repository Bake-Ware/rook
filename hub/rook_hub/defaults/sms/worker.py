"""sms: the text messages of the phone the Rook Android app runs on (brief AP3), shipped as a plugin
installed by default. sms.list and sms.send are 0.5's (origin/castling rook/plugins/sms): the same
names, arguments and replies.

Nothing here bounds a call (Bake, 17 November): sms.list gives every message in the box unless the
caller passes `limit` (0.5 cut at 30 by default, and capped `limit`, the text's length and the number's).
sms.send sends: nothing of ours stands in front of it.

The first call asks for the permission it needs (READ_SMS, SEND_SMS) through the app's bridge
(systems.bake.rook.core.worker.Bridge), as Android's own prompt; refused, the error says where to allow it.

Each text that comes in is also told to the hub as the event sms.received {from, body, ts}, 0.5's (brief
AP7). That is the app's own (Events.kt, SmsWatch), not this module's: it starts once READ_SMS is granted.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import re

from rook_worker import Cap

try:
    from java import cast, jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

BOXES = {"inbox": "content://sms/inbox", "sent": "content://sms/sent", "all": "content://sms"}
TYPES = {1: "inbox", 2: "sent", 3: "draft", 4: "outbox", 5: "failed", 6: "queued"}
COLUMNS = ["address", "body", "date", "type", "read"]
NUMBER = re.compile(r"\+?[0-9 ()./-]+\Z")


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


def _message(row):
    return {"from": row.get("address") or "", "body": row.get("body") or "",
            "ts": round(_num(row.get("date")) / 1000.0, 3), "box": TYPES.get(_num(row.get("type")), "?"),
            "read": bool(_num(row.get("read")))}


def _list(box, limit, since):
    _need("READ_SMS", "sms.list")
    where, args = ("date >= ?", [str(int(since * 1000))]) if since else (None, None)
    return {"messages": [_message(r) for r in _query(BOXES[box], COLUMNS, where, args, "date DESC", limit)]}


async def list_messages(args):
    _check(args, "sms.list", ("box", "limit", "since"))
    box = args.get("box", "inbox")
    if box not in BOXES:
        raise ValueError(f"box must be one of {', '.join(BOXES)}")
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a whole number, 1 or more; leave it out for every message")
    since = args.get("since", 0)
    if not isinstance(since, (int, float)) or isinstance(since, bool) or since < 0:
        raise ValueError("since must be epoch seconds, 0 or more")
    return await asyncio.to_thread(_list, box, limit, since)


def _send(to, text):
    _need("SEND_SMS", "sms.send")
    Manager = jclass("android.telephony.SmsManager")
    if int(jclass("android.os.Build$VERSION").SDK_INT) >= 31:
        manager = cast(Manager, _bridge().context().getSystemService(Manager))
    else:
        manager = Manager.getDefault()
    parts = manager.divideMessage(text)
    if parts.size() > 1:
        manager.sendMultipartTextMessage(to, None, parts, None, None)
    else:
        manager.sendTextMessage(to, None, text, None, None)
    return {"to": to, "parts": int(parts.size())}


async def send(args):
    _check(args, "sms.send", ("to", "text"), ("to", "text"))
    to, text = args["to"], args["text"]
    if not isinstance(to, str) or not NUMBER.match(to):
        raise ValueError("to must be a phone number: digits, with + ( ) . / - and spaces")
    if not isinstance(text, str) or not text:
        raise ValueError("text must be a non-empty string")
    return await asyncio.to_thread(_send, to, text)


CAPS = {} if jclass is None else {
    "sms.list": Cap(
        "The phone's texts, newest first: {messages: [{from, body, ts, box, read}]}; box is inbox, sent or all, "
        "since (epoch seconds) leaves out older ones, limit gives only that many. Each new text also comes as the event sms.received.",
        {"type": "object", "additionalProperties": False, "properties": {
            "box": {"type": "string", "enum": list(BOXES), "default": "inbox"},
            "limit": {"type": "integer", "minimum": 1},
            "since": {"type": "number", "minimum": 0, "default": 0, "description": "Epoch seconds."}}},
        list_messages),
    "sms.send": Cap(
        "Send a text from the phone to a phone number, as the phone's owner: {to, parts}; a long one goes as several parts.",
        {"type": "object", "additionalProperties": False, "required": ["to", "text"], "properties": {
            "to": {"type": "string", "pattern": "^" + NUMBER.pattern[:-2] + "$", "description": "The phone number."},
            "text": {"type": "string", "minLength": 1}}},
        send),
}
