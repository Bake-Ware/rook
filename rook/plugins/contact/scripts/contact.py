"""contact.search: the contacts provider's phone rows (READ_CONTACTS)."""

from rook.kit import android

PHONES = "content://com.android.contacts/data/phones"


def search(ctx, query, limit):
    android.need("READ_CONTACTS")
    q = query.strip()
    where, args = ("display_name LIKE ? OR data1 LIKE ?", [f"%{q}%", f"%{q}%"]) if q else (None, None)
    out, seen = [], set()
    for r in android.query(PHONES, ["display_name", "data1"], where, args, "display_name ASC"):
        key = (r.get("display_name") or "", r.get("data1") or "")
        if key not in seen:
            seen.add(key)
            out.append({"name": key[0], "number": key[1]})
            if len(out) >= limit:
                break
    return {"contacts": out}
