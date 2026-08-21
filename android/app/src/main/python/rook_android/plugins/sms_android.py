"""sms.* — read the SMS inbox and send texts (Chaquopy java bridge).

Reads via the SMS content provider (READ_SMS) and sends via SmsManager
(SEND_SMS). Both are dangerous runtime permissions granted from the app.
Sideloaded, so no Play SMS-policy wall — but Android still gates on the grant.
"""

from __future__ import annotations

import time

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context, jclass, has_permission

_READ_SMS = "android.permission.READ_SMS"
_SEND_SMS = "android.permission.SEND_SMS"

_TYPE = {1: "inbox", 2: "sent", 3: "draft", 4: "outbox", 5: "failed", 6: "queued"}


def _list(box: str, limit: int, since_ts: float) -> dict:
    ctx = app_context()
    if ctx is None:
        return {"ok": False, "error": "not an Android host"}
    if not has_permission(_READ_SMS):
        return {"ok": False, "error": "READ_SMS not granted (grant it in the app)"}
    try:
        Uri = jclass("android.net.Uri")
        base = {"inbox": "content://sms/inbox", "sent": "content://sms/sent",
                "all": "content://sms"}.get(box, "content://sms/inbox")
        uri = Uri.parse(base)
        cr = ctx.getContentResolver()
        n = max(1, min(int(limit), 500))
        cursor = cr.query(uri, None, None, None, f"date DESC LIMIT {n}")
        if cursor is None:
            return {"ok": False, "error": "sms query returned no cursor"}
        out = []
        try:
            iA = cursor.getColumnIndex("address")
            iB = cursor.getColumnIndex("body")
            iD = cursor.getColumnIndex("date")
            iT = cursor.getColumnIndex("type")
            iR = cursor.getColumnIndex("read")
            while cursor.moveToNext():
                ts = (cursor.getLong(iD) / 1000.0) if iD >= 0 else 0.0
                if since_ts and ts < since_ts:
                    continue
                out.append({
                    "from": cursor.getString(iA) if iA >= 0 else None,
                    "body": cursor.getString(iB) if iB >= 0 else None,
                    "ts": round(ts, 3),
                    "box": _TYPE.get(cursor.getInt(iT) if iT >= 0 else 0, "?"),
                    "read": bool(cursor.getInt(iR)) if iR >= 0 else None,
                })
        finally:
            cursor.close()
        return {"ok": True, "count": len(out), "messages": out}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


class AndroidSmsPlugin(Plugin):
    NAMESPACE = "sms"

    @capability("list")
    def _list(self, box: str = "inbox", limit: int = 30, since_ts: float = 0.0) -> dict:
        """Recent SMS. ``box`` = inbox|sent|all; newest first."""
        return _list(str(box), int(limit), float(since_ts or 0))

    @capability("send")
    def _send(self, to: str, text: str) -> dict:
        """Send an SMS to ``to``. Long messages are split into parts."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        if not has_permission(_SEND_SMS):
            return {"ok": False, "error": "SEND_SMS not granted (grant it in the app)"}
        to = str(to or "").strip()
        text = str(text or "")
        if not to or not text:
            return {"ok": False, "error": "both 'to' and 'text' required"}
        try:
            SmsManager = jclass("android.telephony.SmsManager")
            sm = SmsManager.getDefault()
            parts = sm.divideMessage(text)
            if parts.size() > 1:
                sm.sendMultipartTextMessage(to, None, parts, None, None)
            else:
                sm.sendTextMessage(to, None, text, None, None)
            return {"ok": True, "to": to, "parts": parts.size(), "ts": time.time()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidSmsPlugin
