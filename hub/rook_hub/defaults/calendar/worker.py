"""calendar: the calendars on the phone the Rook Android app runs on (brief AP3), shipped as a plugin
installed by default. calendar.list, calendar.create, calendar.update and calendar.delete are 0.5's
(origin/castling rook/plugins/calendar): the same names, arguments and replies.

Instances is the store every calendar app syncs into (Google Calendar, Outlook when it syncs calendars,
Samsung, Exchange, CalDAV); a recurring event comes back as one row per occurrence. Times without an
offset are phone-local (the phone's zone, read from Java: Python's own is UTC there); all-day events are
stored at UTC midnight and reported as dates.

Nothing here bounds a call (Bake, 17 November). calendar.list gives every event in its window unless the
caller passes `limit`; 0.5 cut at 20 by default, capped the window at 366 days and a query at 5000 rows,
and cut titles, places and descriptions to a length; none of that is kept. Its defaults that define a
call rather than cut one are kept: a list's window is the next 24 hours, an event lasts an hour, and a
new event goes to the phone's primary writable calendar. 0.5 also asked the phone's owner to turn on
"let the assistant act for me" before any change: a gate, not kept.

The first call asks for the calendar permissions (READ_CALENDAR, and WRITE_CALENDAR for a change) through
the app's bridge (systems.bake.rook.core.worker.Bridge), as Android's own prompt; refused, the error says
where to allow them.

Off the app it announces nothing: CAPS is empty wherever Chaquopy's `java` module does not import,
which is everywhere but the app."""

import asyncio
import re
import time
from datetime import date, datetime, timedelta, timezone

from rook_worker import Cap

try:
    from java import jclass     # Chaquopy's: only in the Rook Android app
except ImportError:
    jclass = None

EVENTS = "content://com.android.calendar/events"
CALENDARS = "content://com.android.calendar/calendars"
CAN_WRITE = 500                     # Calendars.CAL_ACCESS_CONTRIBUTOR
DAY_MS = 24 * 3600 * 1000
HOUR_MS = 3600 * 1000
COLUMNS = ["title", "begin", "end", "allDay", "eventLocation", "calendar_displayName",
           "account_name", "account_type", "event_id", "selfAttendeeStatus"]


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


def _need(cap, *permissions):
    """Ask for the permissions `cap` needs, in one prompt; raise with what to do if any is refused."""
    refused = _bridge().askAll(["android.permission." + p for p in permissions], cap)
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


def offset_ms(epoch_ms):
    """The phone's UTC offset at `epoch_ms`."""
    return int(jclass("java.util.TimeZone").getDefault().getOffset(int(epoch_ms)))


def parse_time(value, default_ms, offset=None):
    """Epoch ms from None or "" (`default_ms`), "now", epoch seconds or ms, or ISO 8601. ISO without an
    offset is phone-local time."""
    offset = offset or offset_ms
    if value is None or (isinstance(value, str) and not value.strip()):
        return int(default_ms)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value:
            raise ValueError("not a time: NaN")
        return int(value * 1000) if abs(value) < 1e11 else int(value)   # seconds or ms
    if not isinstance(value, str):
        raise ValueError(f"not a time: {value!r} (use ISO 8601 like 2026-10-05T14:00, or epoch seconds)")
    s = value.strip()
    if s.lower() == "now":
        return int(time.time() * 1000)
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return parse_time(float(s), default_ms, offset)
    try:
        moment = datetime.fromisoformat(s[:-1] + "+00:00" if s[-1:] in "Zz" else s)
    except ValueError:
        raise ValueError(f"not a time: {value!r} (use ISO 8601 like 2026-10-05T14:00, or epoch seconds)") from None
    if moment.tzinfo is not None:
        return int(moment.timestamp() * 1000)
    naive = int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return naive - offset(naive - offset(naive))   # the second pass settles DST edges


def iso_local(epoch_ms, offset=None):
    zone = timezone(timedelta(milliseconds=(offset or offset_ms)(epoch_ms)))
    return datetime.fromtimestamp(epoch_ms / 1000, zone).isoformat(timespec="minutes")


def _utc_date(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).date()


def local_date(epoch_ms, offset=None):
    return _utc_date(epoch_ms + (offset or offset_ms)(epoch_ms))


def all_day_dates(begin, end):
    """An all-day row's dates as (first, last + 1): UTC midnights, end exclusive."""
    first = _utc_date(begin)
    return (first, first + timedelta(days=1)) if end <= begin else (first, _utc_date(end - 1) + timedelta(days=1))


def select_rows(rows, begin, finish, limit, offset=None):
    """Rows of a widened query that belong to the local window [begin, finish), sorted by local start (an
    all-day event at local midnight of its first date, before timed events starting then); the first
    `limit` when given. All-day rows are kept when their dates overlap the window's local dates: Android
    compares their UTC midnights with local instants, a day off either side of UTC."""
    offset = offset or offset_ms
    first_day, last_day = local_date(begin, offset), local_date(finish - 1, offset)
    keep = []
    for row in rows:
        b, e = _num(row.get("begin")), _num(row.get("end"))
        if _num(row.get("allDay")):
            d0, d_end = all_day_dates(b, e)
            if not (d0 <= last_day and d_end > first_day):
                continue
            key = parse_time(d0.isoformat(), 0, offset)
        else:
            if not (b < finish and (e > begin or b >= begin)):   # overlaps; a zero-length one when it starts inside
                continue
            key = b
        keep.append((key, 0 if _num(row.get("allDay")) else 1, str(row.get("title") or ""), row))
    keep.sort(key=lambda k: k[:3])
    return [k[3] for k in (keep if limit is None else keep[:limit])]


def event_row(row, offset=None):
    all_day = bool(_num(row.get("allDay")))
    begin, end = _num(row.get("begin")), _num(row.get("end"))
    if all_day:   # the end is an exclusive midnight: a one-day event ends the same date
        start_s, end_s = _utc_date(begin).isoformat(), _utc_date(max(begin, end - 1)).isoformat()
    else:
        start_s, end_s = iso_local(begin, offset), iso_local(end, offset)
    out = {"title": row.get("title") or "(no title)", "start": start_s, "end": end_s, "all_day": all_day,
           "start_ms": begin, "end_ms": end, "location": row.get("eventLocation") or "",
           "calendar": row.get("calendar_displayName") or "", "account": row.get("account_name") or "",
           "account_type": row.get("account_type") or "", "event_id": _num(row.get("event_id"))}
    if _num(row.get("selfAttendeeStatus")) == 2:
        out["declined"] = True
    return out


def _list(start, end, limit):
    _need("calendar.list", "READ_CALENDAR")
    begin = parse_time(start, int(time.time() * 1000))
    finish = parse_time(end, begin + DAY_MS)
    if finish <= begin:
        raise ValueError("end must be after start")
    # Widened a day each side: all-day rows sit at UTC midnight, the window is local.
    rows = _query(f"content://com.android.calendar/instances/when/{begin - DAY_MS}/{finish + DAY_MS}",
                  COLUMNS, "visible = 1", None, "begin ASC, allDay DESC, title ASC")
    return {"start": iso_local(begin), "end": iso_local(finish), "timezone_offset_min": offset_ms(begin) // 60000,
            "events": [event_row(r) for r in select_rows(rows, begin, finish, limit)]}


def _calendars():
    rows = _query(CALENDARS, ["_id", "calendar_displayName", "account_name", "isPrimary", "calendar_access_level",
                              "visible"])
    return [{"id": _num(r["_id"]), "name": r.get("calendar_displayName") or "", "account": r.get("account_name") or "",
             "primary": _num(r.get("isPrimary")) == 1, "access": _num(r.get("calendar_access_level")),
             "visible": _num(r.get("visible")) == 1} for r in rows]


def _values(title=None, location=None, description=None, times=None, calendar_id=None):
    """ContentValues for an insert or update; `times` is (begin_ms, end_ms, all_day)."""
    J = jclass
    values = J("android.content.ContentValues")()
    if calendar_id is not None:
        values.put("calendar_id", J("java.lang.Long")(calendar_id))
    for key, value in (("title", title), ("eventLocation", location), ("description", description)):
        if value is not None:
            values.put(key, str(value))
    if times is not None:
        begin, finish, all_day = times
        zone = str(J("java.util.TimeZone").getDefault().getID())
        if all_day:   # stored at UTC midnight, covering every local date touched
            off = offset_ms(begin)
            begin = ((begin + off) // DAY_MS) * DAY_MS
            finish = max(begin + DAY_MS, ((finish + off + DAY_MS - 1) // DAY_MS) * DAY_MS)
            zone = "UTC"
        values.put("dtstart", J("java.lang.Long")(begin))
        values.put("dtend", J("java.lang.Long")(finish))
        values.put("allDay", J("java.lang.Integer")(1 if all_day else 0))
        values.put("eventTimezone", zone)
    return values


def _times(start, end, all_day, was=None):
    """(begin, end, all_day) from the args. `was` is an event's (begin, end): what is not given keeps its
    time, and a moved start keeps the length."""
    if was is None:
        begin = parse_time(start, int(time.time() * 1000))
        finish = parse_time(end, begin + HOUR_MS)
    else:
        b0, e0 = was[0], was[1] or was[0] + HOUR_MS
        begin = parse_time(start, b0)
        finish = parse_time(end, begin + (e0 - b0))
    if finish <= begin:
        raise ValueError("end must be after start")
    return begin, finish, bool(all_day)


def _event(event_id):
    rows = _query(f"{EVENTS}/{int(event_id)}", ["dtstart", "dtend", "title", "allDay"])
    if not rows:
        raise ValueError(f"no event {event_id}")
    return rows[0]


def _resolver():
    return _bridge().context().getContentResolver()


def _create(title, start, end, all_day, location, description, calendar):
    _need("calendar.create", "READ_CALENDAR", "WRITE_CALENDAR")
    calendars = [c for c in _calendars() if c["access"] >= CAN_WRITE]
    want = calendar.strip().lower()
    if want:
        pick = [c for c in calendars if want in (c["name"].lower(), c["account"].lower())]
    else:
        pick = [c for c in calendars if c["primary"] and c["visible"]] or [c for c in calendars if c["visible"]]
    if not pick:
        names = ", ".join(sorted({c["name"] for c in calendars})) or "none"
        raise ValueError(f"no calendar to write to{' called ' + calendar if want else ''} (writable: {names})")
    begin, finish, day = _times(start, end, all_day)
    values = _values(title, location or None, description or None, (begin, finish, day), pick[0]["id"])
    uri = _resolver().insert(jclass("android.net.Uri").parse(EVENTS), values)
    if uri is None:
        raise ValueError("the calendar refused the event")
    return {"event_id": int(jclass("android.content.ContentUris").parseId(uri)), "calendar": pick[0]["name"],
            "start": iso_local(int(values.getAsLong("dtstart"))), "end": iso_local(int(values.getAsLong("dtend")))}


def _update(event_id, title, start, end, all_day, location, description):
    _need("calendar.update", "READ_CALENDAR", "WRITE_CALENDAR")
    was = _event(event_id)
    b0, e0 = _num(was.get("dtstart")), _num(was.get("dtend")) or None
    times = None
    if start is not None or end is not None or all_day is not None:
        day = bool(_num(was.get("allDay"))) if all_day is None else all_day
        times = _times(start, end, day, (b0, e0))
    values = _values(title, location, description, times)
    if values.size() == 0:
        raise ValueError("nothing to change")
    if _resolver().update(jclass("android.net.Uri").parse(f"{EVENTS}/{int(event_id)}"), values, None, None) <= 0:
        raise ValueError("the calendar refused the change")
    return {"event_id": int(event_id), "title": title or was.get("title") or ""}


def _delete(event_id):
    _need("calendar.delete", "READ_CALENDAR", "WRITE_CALENDAR")
    was = _event(event_id)
    if _resolver().delete(jclass("android.net.Uri").parse(f"{EVENTS}/{int(event_id)}"), None, None) <= 0:
        raise ValueError("the calendar refused the delete")
    return {"event_id": int(event_id), "title": was.get("title") or ""}


def _text(args, name, empty=True):
    value = args.get(name)
    if value is not None and (not isinstance(value, str) or (not empty and not value)):
        raise ValueError(f"{name} must be a{'' if empty else ' non-empty'} string")
    return value


def _flag(args, name):
    value = args.get(name)
    if value is not None and not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _time(args, name):
    value = args.get(name)
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int, float))):
        raise ValueError(f"{name} must be ISO 8601, epoch seconds or \"now\"")
    return value


def _event_id(args):
    value = args.get("event_id")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("event_id must be a whole number from calendar.list")
    return value


async def list_events(args):
    _check(args, "calendar.list", ("start", "end", "limit"))
    limit = args.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("limit must be a whole number, 1 or more; leave it out for every event in the window")
    return await asyncio.to_thread(_list, _time(args, "start"), _time(args, "end"), limit)


async def create(args):
    _check(args, "calendar.create", ("title", "start", "end", "all_day", "location", "description", "calendar"),
           ("title", "start"))
    return await asyncio.to_thread(
        _create, _text(args, "title", empty=False), _time(args, "start"), _time(args, "end"),
        bool(_flag(args, "all_day")), _text(args, "location") or "", _text(args, "description") or "",
        _text(args, "calendar") or "")


async def update(args):
    _check(args, "calendar.update", ("event_id", "title", "start", "end", "all_day", "location", "description"),
           ("event_id",))
    return await asyncio.to_thread(
        _update, _event_id(args), _text(args, "title", empty=False), _time(args, "start"), _time(args, "end"),
        _flag(args, "all_day"), _text(args, "location"), _text(args, "description"))


async def delete(args):
    _check(args, "calendar.delete", ("event_id",), ("event_id",))
    return await asyncio.to_thread(_delete, _event_id(args))


TIME = {"type": ["string", "number"], "description": "ISO 8601 (no offset: phone-local), epoch seconds or \"now\"."}
CAPS = {} if jclass is None else {
    "calendar.list": Cap(
        "The phone's calendar events in a window (start to end, blank: the next 24 hours), soonest first, ongoing ones "
        "included: {start, end, timezone_offset_min, events: [{title, start, end, all_day, location, calendar, account, "
        "account_type, start_ms, end_ms, event_id, declined?}]}; limit gives only that many.",
        {"type": "object", "additionalProperties": False, "properties": {
            "start": TIME, "end": TIME, "limit": {"type": "integer", "minimum": 1}}},
        list_events),
    "calendar.create": Cap(
        "Add an event to a calendar on the phone: {event_id, calendar, start, end}; calendar names one by name or "
        "account (blank: the primary one the phone may write to), end defaults to an hour after start.",
        {"type": "object", "additionalProperties": False, "required": ["title", "start"], "properties": {
            "title": {"type": "string", "minLength": 1}, "start": TIME, "end": TIME,
            "all_day": {"type": "boolean", "default": False}, "location": {"type": "string", "default": ""},
            "description": {"type": "string", "default": ""}, "calendar": {"type": "string", "default": ""}}},
        create),
    "calendar.update": Cap(
        "Change a phone calendar event by event_id (from calendar.list), only the fields given; moving start alone "
        "keeps its length: {event_id, title}.",
        {"type": "object", "additionalProperties": False, "required": ["event_id"], "properties": {
            "event_id": {"type": "integer", "minimum": 1}, "title": {"type": "string", "minLength": 1},
            "start": TIME, "end": TIME, "all_day": {"type": "boolean"}, "location": {"type": "string"},
            "description": {"type": "string"}}},
        update),
    "calendar.delete": Cap(
        "Delete a phone calendar event by event_id (from calendar.list), every occurrence of it: {event_id, title}.",
        {"type": "object", "additionalProperties": False, "required": ["event_id"], "properties": {
            "event_id": {"type": "integer", "minimum": 1}}},
        delete),
}
