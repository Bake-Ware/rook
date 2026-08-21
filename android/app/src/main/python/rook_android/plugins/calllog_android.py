"""calllog.* — recent calls (Chaquopy java bridge, READ_CALL_LOG)."""

from __future__ import annotations

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context, jclass, has_permission

_READ_CALL_LOG = "android.permission.READ_CALL_LOG"
_DIR = {1: "incoming", 2: "outgoing", 3: "missed", 4: "voicemail", 5: "rejected", 6: "blocked"}


class AndroidCallLogPlugin(Plugin):
    NAMESPACE = "calllog"

    @capability("list")
    def _list(self, limit: int = 30) -> dict:
        """Recent calls, newest first: number, name, direction, duration_s, ts."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        if not has_permission(_READ_CALL_LOG):
            return {"ok": False, "error": "READ_CALL_LOG not granted (grant it in the app)"}
        try:
            Uri = jclass("android.net.Uri")
            uri = Uri.parse("content://call_log/calls")
            cr = ctx.getContentResolver()
            n = max(1, min(int(limit), 500))
            cursor = cr.query(uri, None, None, None, "date DESC")
            if cursor is None:
                return {"ok": False, "error": "call_log query returned no cursor"}
            out = []
            try:
                iNum = cursor.getColumnIndex("number")
                iName = cursor.getColumnIndex("name")
                iType = cursor.getColumnIndex("type")
                iDur = cursor.getColumnIndex("duration")
                iDate = cursor.getColumnIndex("date")
                while cursor.moveToNext():
                    if len(out) >= n:
                        break
                    out.append({
                        "number": cursor.getString(iNum) if iNum >= 0 else None,
                        "name": cursor.getString(iName) if iName >= 0 else None,
                        "direction": _DIR.get(cursor.getInt(iType) if iType >= 0 else 0, "?"),
                        "duration_s": cursor.getInt(iDur) if iDur >= 0 else None,
                        "ts": round((cursor.getLong(iDate) / 1000.0), 3) if iDate >= 0 else None,
                    })
            finally:
                cursor.close()
            return {"ok": True, "count": len(out), "calls": out}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidCallLogPlugin
