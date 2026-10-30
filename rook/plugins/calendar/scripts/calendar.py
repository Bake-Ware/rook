"""calendar.*: CalendarContract on the phone (READ_CALENDAR, WRITE_CALENDAR).

Instances is the store every calendar app syncs into (Google Calendar,
Outlook when it syncs calendars, Samsung, Exchange, CalDAV); recurring events
come back as one row per occurrence. Times without an offset are phone-local;
all-day events are stored at UTC midnight and reported as dates.
"""

import re
import time
from datetime import date, datetime, timedelta, timezone

from rook.kit import android
from rook.kit.ctx import KitError

EVENTS = "content://com.android.calendar/events"
CALENDARS = "content://com.android.calendar/calendars"
CAN_WRITE = 500                     # Calendars.CAL_ACCESS_CONTRIBUTOR
DAY_MS = 24 * 3600 * 1000
HOUR_MS = 3600 * 1000
MAX_SPAN_MS = 366 * DAY_MS
MAX_ROWS = 5000                     # one widened instances query, before filtering
COLUMNS = ["title", "begin", "end", "allDay", "eventLocation", "calendar_displayName",
           "account_name", "account_type", "event_id", "selfAttendeeStatus"]


def offset_ms(epoch_ms: int) -> int:
    """The phone's UTC offset at ``epoch_ms`` (Python's own zone on Android is UTC)."""
    return int(android.jclass("java.util.TimeZone").getDefault().getOffset(int(epoch_ms)))


def parse_time(value, default_ms: int, offset=offset_ms) -> int:
    """Epoch ms from None/"" (``default_ms``), "now", epoch seconds or ms, or ISO 8601.
    ISO without an offset is phone-local time. Raises KitError."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return int(default_ms)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value != value:
            raise KitError("not a time: NaN")
        return int(value * 1000) if abs(value) < 1e11 else int(value)   # seconds vs ms
    s = str(value).strip()
    if s.lower() == "now":
        return int(time.time() * 1000)
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return parse_time(float(s), default_ms, offset)
    try:
        dt = datetime.fromisoformat(s[:-1] + "+00:00" if s[-1:] in "Zz" else s)
    except ValueError:
        raise KitError(f"not a time: {value!r} (use ISO 8601 like 2026-10-05T14:00, or epoch seconds)") from None
    if dt.tzinfo is not None:
        return int(dt.timestamp() * 1000)
    naive = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    return naive - offset(naive - offset(naive))   # the second pass settles DST edges


def iso_local(epoch_ms: int, offset=offset_ms) -> str:
    tz = timezone(timedelta(milliseconds=offset(epoch_ms)))
    return datetime.fromtimestamp(epoch_ms / 1000, tz).isoformat(timespec="minutes")


def _utc_date(epoch_ms: int) -> date:
    return datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).date()


def local_date(epoch_ms: int, offset=offset_ms) -> date:
    return _utc_date(epoch_ms + offset(epoch_ms))


def all_day_dates(begin: int, end: int) -> tuple[date, date]:
    """An all-day row's dates as (first, last + 1): UTC midnights, end exclusive."""
    first = _utc_date(begin)
    return (first, first + timedelta(days=1)) if end <= begin else (first, _utc_date(end - 1) + timedelta(days=1))


def select_rows(rows: list, begin: int, finish: int, n: int, offset=offset_ms) -> list:
    """Rows of a widened query that belong to the local window [begin, finish),
    sorted by local start (an all-day event at local midnight of its first
    date, before timed events starting then); at most ``n``. All-day rows are
    kept when their dates overlap the window's local dates: Android compares
    their UTC midnights with local instants, a day off either side of UTC."""
    first_day, last_day = local_date(begin, offset), local_date(finish - 1, offset)
    keep = []
    for r in rows:
        b, e = int(r.get("begin") or 0), int(r.get("end") or 0)
        if r.get("allDay"):
            d0, d_end = all_day_dates(b, e)
            if not (d0 <= last_day and d_end > first_day):
                continue
            key = parse_time(d0.isoformat(), 0, offset)
        else:
            # Overlaps the window; a zero-length event counts when it starts inside.
            if not (b < finish and (e > begin or b >= begin)):
                continue
            key = b
        keep.append((key, 0 if r.get("allDay") else 1, str(r.get("title") or ""), r))
    keep.sort(key=lambda k: k[:3])
    return [k[3] for k in keep[:n]]


def event_row(row: dict, offset=offset_ms) -> dict:
    all_day = bool(row.get("allDay"))
    begin, end = int(row.get("begin") or 0), int(row.get("end") or 0)
    if all_day:   # the end is an exclusive midnight: a one-day event ends the same date
        start_s, end_s = _utc_date(begin).isoformat(), _utc_date(max(begin, end - 1)).isoformat()
    else:
        start_s, end_s = iso_local(begin, offset), iso_local(end, offset)
    out = {"title": row.get("title") or "(no title)", "start": start_s, "end": end_s, "all_day": all_day,
           "start_ms": begin, "end_ms": end, "location": row.get("eventLocation") or "",
           "calendar": row.get("calendar_displayName") or "", "account": row.get("account_name") or "",
           "account_type": row.get("account_type") or "", "event_id": row.get("event_id")}
    if row.get("selfAttendeeStatus") == 2:
        out["declined"] = True
    return out


def list_events(ctx, start, end, limit):
    android.need("READ_CALENDAR")
    begin = parse_time(start, int(time.time() * 1000))
    finish = parse_time(end, begin + DAY_MS)
    if finish <= begin:
        raise KitError("end must be after start")
    if finish - begin > MAX_SPAN_MS:
        raise KitError("window too long (366 days at most)")
    # Widened a day each side: all-day rows sit at UTC midnight, the window is local.
    rows = android.query(f"content://com.android.calendar/instances/when/{begin - DAY_MS}/{finish + DAY_MS}",
                         COLUMNS, "visible = 1", None, "begin ASC, allDay DESC, title ASC", limit=MAX_ROWS)
    events = [event_row(r) for r in select_rows(rows, begin, finish, limit)]
    return {"start": iso_local(begin), "end": iso_local(finish),
            "timezone_offset_min": offset_ms(begin) // 60000, "events": events}


# -- changes ------------------------------------------------------------------

def _writable():
    android.assistant_may_act()
    android.need("WRITE_CALENDAR")


def _calendars() -> list[dict]:
    rows = android.query(CALENDARS, ["_id", "calendar_displayName", "account_name", "isPrimary",
                                     "calendar_access_level", "visible"])
    return [{"id": int(r["_id"]), "name": r.get("calendar_displayName") or "", "account": r.get("account_name") or "",
             "primary": r.get("isPrimary") == 1, "access": r.get("calendar_access_level") or 0,
             "visible": r.get("visible") == 1} for r in rows]


def _values(title=None, location=None, description=None, times=None, calendar_id=None):
    """ContentValues for an insert or update; ``times`` is (begin_ms, end_ms, all_day)."""
    J = android.jclass
    cv = J("android.content.ContentValues")()
    if calendar_id is not None:
        cv.put("calendar_id", J("java.lang.Long")(calendar_id))
    for key, value, cap in (("title", title, 500), ("eventLocation", location, 500),
                            ("description", description, 4000)):
        if value is not None:
            cv.put(key, str(value)[:cap])
    if times is not None:
        begin, finish, all_day = times
        tz = str(J("java.util.TimeZone").getDefault().getID())
        if all_day:   # stored at UTC midnight, covering every local date touched
            off = offset_ms(begin)
            begin = ((begin + off) // DAY_MS) * DAY_MS
            finish = max(begin + DAY_MS, ((finish + off + DAY_MS - 1) // DAY_MS) * DAY_MS)
            tz = "UTC"
        cv.put("dtstart", J("java.lang.Long")(begin))
        cv.put("dtend", J("java.lang.Long")(finish))
        cv.put("allDay", J("java.lang.Integer")(1 if all_day else 0))
        cv.put("eventTimezone", tz)
    return cv


def _times(start, end, all_day, was=None):
    """(begin, end, all_day) from the args. ``was`` is an event's (begin, end):
    what isn't given keeps its time, and a moved start keeps the length."""
    if was is None:
        begin = parse_time(start, int(time.time() * 1000))
        finish = parse_time(end, begin + HOUR_MS)
    else:
        b0, e0 = was[0], was[1] or was[0] + HOUR_MS
        begin = parse_time(start, b0)
        finish = parse_time(end, begin + (e0 - b0))
    if finish <= begin:
        raise KitError("end must be after start")
    return begin, finish, bool(all_day)


def _event_uri(event_id):
    return f"{EVENTS}/{int(event_id)}"


def _event(event_id) -> dict:
    rows = android.query(_event_uri(event_id), ["dtstart", "dtend", "title", "allDay"])
    if not rows:
        raise KitError(f"no event {event_id}")
    return rows[0]


def create(ctx, title, start, end, all_day, location, description, calendar):
    _writable()
    cals = [c for c in _calendars() if c["access"] >= CAN_WRITE]
    want = calendar.strip().lower()
    if want:
        pick = [c for c in cals if want in (c["name"].lower(), c["account"].lower())]
    else:
        pick = [c for c in cals if c["primary"] and c["visible"]] or [c for c in cals if c["visible"]]
    if not pick:
        names = ", ".join(sorted({c["name"] for c in cals})) or "none"
        raise KitError(f"no calendar to write to{' called ' + calendar if want else ''} (writable: {names})")
    begin, finish, day = _times(start, end, all_day)
    cv = _values(title, location or None, description or None, (begin, finish, day), pick[0]["id"])
    J = android.jclass
    uri = android.context().getContentResolver().insert(J("android.net.Uri").parse(EVENTS), cv)
    if uri is None:
        raise KitError("the calendar refused the event")
    return {"event_id": int(J("android.content.ContentUris").parseId(uri)), "calendar": pick[0]["name"],
            "start": iso_local(int(cv.getAsLong("dtstart"))), "end": iso_local(int(cv.getAsLong("dtend")))}


def update(ctx, event_id, title=None, start=None, end=None, all_day=None, location=None, description=None):
    _writable()
    was = _event(event_id)
    b0 = int(was.get("dtstart") or 0)
    e0 = was.get("dtend")
    times = None
    if start is not None or end is not None or all_day is not None:
        day = bool(was.get("allDay")) if all_day is None else all_day
        times = _times(start, end, day, (b0, int(e0) if e0 else None))
    cv = _values(title, location, description, times)
    if cv.size() == 0:
        raise KitError("nothing to change")
    uri = android.jclass("android.net.Uri").parse(_event_uri(event_id))
    if android.context().getContentResolver().update(uri, cv, None, None) <= 0:
        raise KitError("the calendar refused the change")
    return {"event_id": int(event_id), "title": title or was.get("title") or ""}


def delete(ctx, event_id):
    _writable()
    was = _event(event_id)
    uri = android.jclass("android.net.Uri").parse(_event_uri(event_id))
    if android.context().getContentResolver().delete(uri, None, None) <= 0:
        raise KitError("the calendar refused the delete")
    return {"event_id": int(event_id), "title": was.get("title") or ""}
