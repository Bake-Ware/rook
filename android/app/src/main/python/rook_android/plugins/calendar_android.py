"""calendar.* — read the phone's calendars (Chaquopy java bridge, READ_CALENDAR).

Reads Android's CalendarContract.Instances, the store every calendar app syncs
into: Google Calendar, and Outlook when its "Sync calendars" setting is on,
plus Samsung/Exchange/CalDAV accounts. Recurring events come back as individual
occurrences in the requested window.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context, jclass, has_permission

_READ_CALENDAR = "android.permission.READ_CALENDAR"
NO_PERMISSION = ("calendar access not granted: open Rook on the phone, Settings > "
                 "Permissions > \"Grant SMS / contacts / calendar / location\", and allow Calendar")
DAY_MS = 24 * 3600 * 1000
MAX_LIMIT = 200
MAX_SPAN_MS = 366 * DAY_MS
# Rows read from one (widened) instances query before filtering; a runaway guard.
MAX_ROWS = 5000

# CalendarContract.Instances columns.
_COLUMNS = ["title", "begin", "end", "allDay", "eventLocation", "calendar_displayName",
            "account_name", "account_type", "event_id", "eventTimezone", "selfAttendeeStatus"]


def _offset_ms(epoch_ms: int) -> int:
    """The device's UTC offset at ``epoch_ms`` (Python's own zone on Android is UTC)."""
    try:
        return int(jclass("java.util.TimeZone").getDefault().getOffset(int(epoch_ms)))
    except Exception:
        return int(-time.timezone * 1000) if not time.daylight else int(-time.altzone * 1000)


def parse_time(value, default_ms: int, offset=_offset_ms) -> int:
    """Epoch ms from None (``default_ms``), "now", epoch seconds/ms, or an ISO date/time.

    Naive ISO times (no offset) are device-local time. Raises ValueError.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return int(default_ms)
    if isinstance(value, bool):
        raise ValueError(f"not a time: {value!r}")
    if isinstance(value, (int, float)):
        if value != value:
            raise ValueError("not a time: NaN")
        v = float(value)
        return int(v * 1000) if abs(v) < 1e11 else int(v)  # seconds vs milliseconds
    s = str(value).strip()
    if s.lower() == "now":
        return int(time.time() * 1000)
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return parse_time(float(s), default_ms, offset)
    iso = s[:-1] + "+00:00" if s[-1:] in ("Z", "z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(f"not a time: {value!r} (use ISO 8601 like 2026-10-05T14:00 or epoch ms)") from None
    if dt.tzinfo is not None:
        return int(dt.timestamp() * 1000)
    naive_ms = int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    guess = naive_ms - offset(naive_ms)
    return naive_ms - offset(guess)  # second pass settles DST edges


def iso_local(epoch_ms: int, offset=_offset_ms) -> str:
    off = offset(epoch_ms)
    tz = timezone(timedelta(milliseconds=off))
    return datetime.fromtimestamp(epoch_ms / 1000, tz).isoformat(timespec="minutes")


def iso_date_utc(epoch_ms: int) -> str:
    """All-day events are stored at UTC midnight: report the calendar date."""
    return datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).date().isoformat()


def local_date(epoch_ms: int, offset=_offset_ms) -> date:
    """The device-local calendar date at ``epoch_ms``."""
    return datetime.fromtimestamp((epoch_ms + offset(epoch_ms)) / 1000, timezone.utc).date()


def all_day_dates(begin: int, end: int) -> tuple[date, date]:
    """An all-day row's calendar dates as (first, last+1): stored as UTC midnights, end exclusive."""
    first = datetime.fromtimestamp(begin / 1000, timezone.utc).date()
    if end <= begin:
        return first, first + timedelta(days=1)
    return first, datetime.fromtimestamp((end - 1) / 1000, timezone.utc).date() + timedelta(days=1)


def select_rows(rows: list[dict], begin: int, finish: int, n: int, offset=_offset_ms) -> list[dict]:
    """Rows from a widened instances query that belong to the local window [begin, finish).

    All-day rows are kept when their dates overlap the window's local dates (Android's
    window test compares their UTC-midnight times with local instants, which is a day off
    either side of UTC). Sorted by local start: an all-day event at local midnight of its
    first date, before timed events starting then. At most ``n``.
    """
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
            # Overlaps the window; a zero-length event counts when it starts inside it.
            if not (b < finish and (e > begin or b >= begin)):
                continue
            key = b
        keep.append((key, 0 if r.get("allDay") else 1, str(r.get("title") or ""), r))
    keep.sort(key=lambda k: k[:3])
    return [k[3] for k in keep[:n]]


def event_row(row: dict, offset=_offset_ms) -> dict:
    """One Instances row (raw column values) to the cap's event shape."""
    all_day = bool(row.get("allDay"))
    begin, end = int(row.get("begin") or 0), int(row.get("end") or 0)
    if all_day:
        start_s = iso_date_utc(begin)
        # The end is exclusive midnight: a one-day event ends the same calendar date.
        end_s = iso_date_utc(max(begin, end - 1))
    else:
        start_s, end_s = iso_local(begin, offset), iso_local(end, offset)
    out = {
        "title": row.get("title") or "(no title)",
        "start": start_s, "end": end_s, "all_day": all_day,
        "start_ms": begin, "end_ms": end,
        "location": row.get("eventLocation") or None,
        "calendar": row.get("calendar_displayName") or None,
        "account": row.get("account_name") or None,
        "account_type": row.get("account_type") or None,
        "event_id": row.get("event_id"),
    }
    if row.get("selfAttendeeStatus") == 2:
        out["declined"] = True
    return out


class AndroidCalendarPlugin(Plugin):
    NAMESPACE = "calendar"

    def available(self) -> bool:
        # Registered on every phone so a missing grant is a clear error, not a missing cap.
        return app_context() is not None

    @capability("list", risk="read", tags=("personal",))
    def _list(self, start: str | int | None = None, end: str | int | None = None,
              limit: int = 20) -> dict:
        """Calendar events on the phone (Google Calendar, Outlook if it syncs calendars, others).

        Args:
          start: window start: ISO 8601 ("2026-10-05", "2026-10-05T09:00", with or
            without an offset; no offset = phone's local time), epoch ms or seconds,
            or "now". Default: now.
          end: window end, same forms. Default: start + 24 hours.
          limit: most events to return, soonest first (1-200, default 20).

        Returns {ok, count, start, end, timezone_offset_min, events:[{title, start, end,
        all_day, location, calendar, account, account_type, start_ms, end_ms,
        event_id, declined?}]}. Times are phone-local ISO; all-day events use dates.
        Ongoing events (started before ``start``, ending after it) are included.
        """
        ctx = app_context()
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        if not has_permission(_READ_CALENDAR):
            return {"ok": False, "error": NO_PERMISSION, "needs_permission": "READ_CALENDAR"}
        now = int(time.time() * 1000)
        try:
            begin = parse_time(start, now)
            finish = parse_time(end, begin + DAY_MS)
            n = max(1, min(int(limit), MAX_LIMIT))
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        if finish <= begin:
            return {"ok": False, "error": "end must be after start"}
        if finish - begin > MAX_SPAN_MS:
            return {"ok": False, "error": "window too long (366 days at most)"}
        try:
            # Widened by a day each side: all-day rows sit at UTC midnight, the window is local.
            rows = self._query(ctx, begin - DAY_MS, finish + DAY_MS, MAX_ROWS)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        events = [event_row(r) for r in select_rows(rows, begin, finish, n)]
        return {"ok": True, "count": len(events), "start": iso_local(begin), "end": iso_local(finish),
                "timezone_offset_min": _offset_ms(begin) // 60000, "events": events}

    def _query(self, ctx, begin: int, finish: int, n: int) -> list[dict]:
        Uri = jclass("android.net.Uri")
        uri = Uri.parse(f"content://com.android.calendar/instances/when/{int(begin)}/{int(finish)}")
        cursor = ctx.getContentResolver().query(uri, _COLUMNS, "visible = 1", None,
                                                "begin ASC, allDay DESC, title ASC")
        if cursor is None:
            raise RuntimeError("calendar query returned no cursor")
        out: list[dict] = []
        try:
            idx = {c: cursor.getColumnIndex(c) for c in _COLUMNS}
            longs = {"begin", "end", "event_id"}
            ints = {"allDay", "selfAttendeeStatus"}
            while cursor.moveToNext() and len(out) < n:
                row = {}
                for c, i in idx.items():
                    if i < 0 or cursor.isNull(i):
                        row[c] = None
                    elif c in longs:
                        row[c] = int(cursor.getLong(i))
                    elif c in ints:
                        row[c] = int(cursor.getInt(i))
                    else:
                        row[c] = cursor.getString(i)
                out.append(row)
        finally:
            cursor.close()
        return out


PLUGIN = AndroidCalendarPlugin
