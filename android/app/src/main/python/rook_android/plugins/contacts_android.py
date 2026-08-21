"""contacts.* — search the address book (Chaquopy java bridge, READ_CONTACTS)."""

from __future__ import annotations

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context, jclass, has_permission

_READ_CONTACTS = "android.permission.READ_CONTACTS"


class AndroidContactsPlugin(Plugin):
    NAMESPACE = "contacts"

    @capability("search")
    def _search(self, query: str = "", limit: int = 40) -> dict:
        """Contacts whose name/number matches ``query`` (empty = first N)."""
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        if not has_permission(_READ_CONTACTS):
            return {"ok": False, "error": "READ_CONTACTS not granted (grant it in the app)"}
        try:
            Uri = jclass("android.net.Uri")
            uri = Uri.parse("content://com.android.contacts/data/phones")
            cr = ctx.getContentResolver()
            n = max(1, min(int(limit), 500))
            q = str(query or "").strip()
            sel, args = None, None
            if q:
                sel = "display_name LIKE ? OR data1 LIKE ?"
                args = [f"%{q}%", f"%{q}%"]
            cursor = cr.query(uri, None, sel, args, f"display_name ASC LIMIT {n}")
            if cursor is None:
                return {"ok": False, "error": "contacts query returned no cursor"}
            out, seen = [], set()
            try:
                iName = cursor.getColumnIndex("display_name")
                iNum = cursor.getColumnIndex("data1")
                while cursor.moveToNext():
                    name = cursor.getString(iName) if iName >= 0 else None
                    num = cursor.getString(iNum) if iNum >= 0 else None
                    key = (name, num)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({"name": name, "number": num})
            finally:
                cursor.close()
            return {"ok": True, "count": len(out), "contacts": out}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


PLUGIN = AndroidContactsPlugin
