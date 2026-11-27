"""contact: the address book of the phone the Rook Android app runs on (brief AP3), shipped as a plugin
installed by default. contact.search is 0.5's (origin/castling rook/plugins/contact): the same name,
arguments and reply.

Nothing here bounds a call (Bake, 17 November): contact.search gives every match unless the caller passes
`limit` (0.5 cut at 40 by default, and capped `limit` and the query's length).

The first call asks for READ_CONTACTS through the app's bridge (systems.bake.rook.core.worker.Bridge), as
Android's own prompt; refused, the error says where to allow it.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio

from rook_worker import Cap

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

PHONES = "content://com.android.contacts/data/phones"


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


def _search(query, limit):
    _need("READ_CONTACTS", "contact.search")
    q = query.strip()
    where, args = ("display_name LIKE ? OR data1 LIKE ?", [f"%{q}%", f"%{q}%"]) if q else (None, None)
    out, seen = [], set()
    for row in _query(PHONES, ["display_name", "data1"], where, args, "display_name ASC"):
        key = (row.get("display_name") or "", row.get("data1") or "")
        if key not in seen:
            seen.add(key)
            out.append({"name": key[0], "number": key[1]})
            if limit is not None and len(out) >= limit:
                break
    return {"contacts": out}


async def search(args):
    _check(args, "contact.search", ("query", "limit"))
    query = args.get("query", "")
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a whole number, 1 or more; leave it out for every match")
    return await asyncio.to_thread(_search, query, limit)


CAPS = {} if jclass is None else {
    "contact.search": Cap(
        "The phone's contacts whose name or number contains query (blank: all, by name): {contacts: [{name, number}]}, "
        "one row per number; limit gives only that many.",
        {"type": "object", "additionalProperties": False, "properties": {
            "query": {"type": "string", "default": ""}, "limit": {"type": "integer", "minimum": 1}}},
        search),
}
