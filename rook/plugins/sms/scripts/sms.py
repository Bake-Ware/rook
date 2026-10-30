"""sms.*: the SMS content provider (READ_SMS) and SmsManager (SEND_SMS)."""

from rook.kit import android

BOXES = {"inbox": "content://sms/inbox", "sent": "content://sms/sent", "all": "content://sms"}
TYPES = {1: "inbox", 2: "sent", 3: "draft", 4: "outbox", 5: "failed", 6: "queued"}
COLUMNS = ["address", "body", "date", "type", "read"]


def _message(row):
    return {"from": row.get("address") or "", "body": row.get("body") or "",
            "ts": round((row.get("date") or 0) / 1000.0, 3), "box": TYPES.get(row.get("type"), "?"),
            "read": bool(row.get("read"))}


def list_messages(ctx, box, limit, since):
    android.need("READ_SMS")
    where, args = ("date >= ?", [str(int(since * 1000))]) if since else (None, None)
    rows = android.query(BOXES[box], COLUMNS, where, args, "date DESC", limit=limit)
    return {"messages": [_message(r) for r in rows]}


def send(ctx, to, text):
    android.need("SEND_SMS")
    sm = android.jclass("android.telephony.SmsManager").getDefault()
    parts = sm.divideMessage(text)
    if parts.size() > 1:
        sm.sendMultipartTextMessage(to, None, parts, None, None)
    else:
        sm.sendTextMessage(to, None, text, None, None)
    return {"to": to, "parts": int(parts.size())}


def watch(ctx):
    """Emit sms.received for texts newer than the last one seen (kept across
    restarts). The very first look only notes the newest: history isn't news."""
    if not android.granted("READ_SMS"):
        return
    seen = android.cursor(ctx, "inbox")
    if seen is None:
        newest = android.query(BOXES["inbox"], ["date"], order="date DESC", limit=1)
        android.cursor(ctx, "inbox", newest[0]["date"] if newest else 0)
        return
    for row in android.query(BOXES["inbox"], COLUMNS, "date > ?", [str(seen)], "date ASC", limit=100):
        m = _message(row)
        ctx.emit("sms.received", {"from": m["from"], "body": m["body"], "ts": m["ts"]})
        seen = max(seen, row.get("date") or 0)
        android.cursor(ctx, "inbox", seen)
