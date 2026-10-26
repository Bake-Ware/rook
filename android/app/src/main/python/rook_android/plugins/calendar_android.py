"""calendar.* — the phone's calendars (Chaquopy java bridge, READ_CALENDAR / WRITE_CALENDAR).

Reads Android's CalendarContract.Instances, the store every calendar app syncs
into: Google Calendar, and Outlook when its "Sync calendars" setting is on,
plus Samsung/Exchange/CalDAV accounts. Recurring events come back as individual
occurrences in the requested window.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta, timezone

from rook.worker.plugin import ANDROID_APP, Plugin, capability, place
from rook_android.androidctx import app_context, jclass, has_permission

_READ_CALENDAR = "android.permission.READ_CALENDAR"
_WRITE_CALENDAR = "android.permission.WRITE_CALENDAR"
NO_WRITE = ("calendar changes not granted: open Rook on the phone, Settings > Permissions > "
            "\"Grant SMS / contacts / calendar / location\", and allow Calendar")
_EVENTS = "content://com.android.calendar/events"
_CALENDARS = "content://com.android.calendar/calendars"
# CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR: the account may add events.
_CAN_WRITE = 500
#: The phone's own opt-in (Settings > App), off by default: without it nothing is changed.
NOT_ALLOWED = ("this phone doesn't let the assistant change things: on the phone, Settings > App > "
               "\"Let the assistant act for me on this phone\"")


def assistant_allowed(ctx) -> bool:
    try:
        return bool(ctx.getSharedPreferences("rook", 0).getBoolean("assistant_actions", False))
    except Exception:
        return False
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
    PLACEMENT = place(ANDROID_APP)

    def available(self) -> bool:
        # Registered on every phone so a missing grant is a clear error, not a missing cap.
        return app_context() is not None

    @capability("list", risk="read", tags=("personal", "sensitive"), scope="self")
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

    # -- changes (the house agent, for the owner: bakephone only) ---------------------
    def _calendars(self, ctx) -> list[dict]:
        Uri = jclass("android.net.Uri")
        cols = ["_id", "calendar_displayName", "account_name", "isPrimary",
                "calendar_access_level", "visible", "ownerAccount"]
        cur = ctx.getContentResolver().query(Uri.parse(_CALENDARS), cols, None, None, None)
        out = []
        if cur is None:
            return out
        try:
            while cur.moveToNext():
                out.append({"id": int(cur.getLong(0)), "name": cur.getString(1) or "",
                            "account": cur.getString(2) or "",
                            "primary": (not cur.isNull(3)) and cur.getInt(3) == 1,
                            "access": 0 if cur.isNull(4) else int(cur.getInt(4)),
                            "visible": (not cur.isNull(5)) and cur.getInt(5) == 1})
        finally:
            cur.close()
        return out

    def _values(self, title, start, end, all_day, location, description, calendar_id=None,
                defaults_ms=None):
        cv = jclass("android.content.ContentValues")()
        tz = str(jclass("java.util.TimeZone").getDefault().getID())
        if calendar_id is not None:
            cv.put("calendar_id", jclass("java.lang.Long")(int(calendar_id)))
        if title is not None:
            cv.put("title", str(title)[:500])
        if location is not None:
            cv.put("eventLocation", str(location)[:500])
        if description is not None:
            cv.put("description", str(description)[:4000])
        if start is not None or end is not None or all_day is not None:
            now = int(time.time() * 1000)
            b0, e0 = defaults_ms or (now, now + 3600 * 1000)
            begin = parse_time(start, b0)
            finish = parse_time(end, begin + (e0 - b0 if defaults_ms else 3600 * 1000))
            if finish <= begin:
                raise ValueError("end must be after start")
            day = bool(all_day)
            if day:
                # All-day events are stored at UTC midnight.
                off = _offset_ms(begin)
                begin = ((begin + off) // DAY_MS) * DAY_MS
                finish = max(begin + DAY_MS, ((finish + off + DAY_MS - 1) // DAY_MS) * DAY_MS)
                tz = "UTC"
            cv.put("dtstart", jclass("java.lang.Long")(int(begin)))
            cv.put("dtend", jclass("java.lang.Long")(int(finish)))
            cv.put("allDay", jclass("java.lang.Integer")(1 if day else 0))
            cv.put("eventTimezone", tz)
        return cv

    def _writable(self, ctx):
        if ctx is None:
            return {"ok": False, "error": "not an Android host"}
        if not assistant_allowed(ctx):
            return {"ok": False, "error": NOT_ALLOWED, "needs_setting": "assistant_actions"}
        if not has_permission(_WRITE_CALENDAR):
            return {"ok": False, "error": NO_WRITE, "needs_permission": "WRITE_CALENDAR"}
        return None

    @capability("create", risk="exec", tags=("personal",), scope="self")
    def _create(self, title: str, start: str | int, end: str | int | None = None,
                all_day: bool = False, location: str = "", description: str = "",
                calendar: str = "") -> dict:
        """Add an event to a calendar on the phone (the owner's own calendar).

        Args:
          title: what it is. start / end: as for calendar.list (ISO local time, epoch, "now");
            end defaults to an hour after start. all_day: a whole-day event.
          location, description: optional. calendar: a calendar's name or account
            (default: the primary calendar the phone may write to).

        Returns {ok, event_id, calendar, start, end}."""
        ctx = app_context()
        bad = self._writable(ctx)
        if bad:
            return bad
        if not str(title or "").strip():
            return {"ok": False, "error": "title: what the event is"}
        try:
            cals = [c for c in self._calendars(ctx) if c["access"] >= _CAN_WRITE]
            want = str(calendar or "").strip().lower()
            pick = ([c for c in cals if want and want in (c["name"].lower(), c["account"].lower())]
                    or ([] if want else [c for c in cals if c["primary"] and c["visible"]])
                    or ([] if want else [c for c in cals if c["visible"]]))
            if not pick:
                names = ", ".join(sorted({c["name"] for c in cals})) or "none"
                return {"ok": False, "error": f"no calendar to write to{' called ' + calendar if want else ''} "
                                              f"(writable: {names})"}
            cv = self._values(title, start, end, all_day, location or None, description or None,
                              calendar_id=pick[0]["id"])
            Uri = jclass("android.net.Uri")
            uri = ctx.getContentResolver().insert(Uri.parse(_EVENTS), cv)
            if uri is None:
                return {"ok": False, "error": "the calendar refused the event"}
            eid = int(jclass("android.content.ContentUris").parseId(uri))
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": True, "event_id": eid, "calendar": pick[0]["name"],
                "start": iso_local(int(cv.getAsLong("dtstart"))),
                "end": iso_local(int(cv.getAsLong("dtend")))}

    def _event_uri(self, event_id):
        return jclass("android.content.ContentUris").withAppendedId(
            jclass("android.net.Uri").parse(_EVENTS), int(event_id))

    def _times_of(self, ctx, event_id):
        cur = ctx.getContentResolver().query(self._event_uri(event_id), ["dtstart", "dtend", "title"],
                                             None, None, None)
        if cur is None:
            return None
        try:
            if not cur.moveToNext():
                return None
            return int(cur.getLong(0)), (int(cur.getLong(1)) if not cur.isNull(1) else None), cur.getString(2)
        finally:
            cur.close()

    @capability("update", risk="exec", tags=("personal",), scope="self")
    def _update(self, event_id: int, title: str | None = None, start: str | int | None = None,
                end: str | int | None = None, all_day: bool | None = None,
                location: str | None = None, description: str | None = None) -> dict:
        """Change an event on the phone by ``event_id`` (from calendar.list): only the fields given.
        Moving ``start`` alone keeps the event's length. Returns {ok, event_id, title}."""
        ctx = app_context()
        bad = self._writable(ctx)
        if bad:
            return bad
        try:
            was = self._times_of(ctx, event_id)
            if was is None:
                return {"ok": False, "error": f"no event {event_id}"}
            b0, e0, old_title = was
            e0 = e0 if e0 is not None else b0 + 3600 * 1000
            if start is not None and end is None:
                end = parse_time(start, b0) + (e0 - b0)
            times = (start, end, all_day) if any(v is not None for v in (start, end, all_day)) \
                else (None, None, None)
            cv = self._values(title, times[0], times[1], times[2], location, description,
                              defaults_ms=(b0, e0))
            if cv.size() == 0:
                return {"ok": False, "error": "nothing to change"}
            n = ctx.getContentResolver().update(self._event_uri(event_id), cv, None, None)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": n > 0, "event_id": int(event_id), "title": title or old_title,
                **({} if n > 0 else {"error": "the calendar refused the change"})}

    @capability("delete", risk="exec", tags=("personal", "destructive"), scope="self")
    def _delete(self, event_id: int) -> dict:
        """Delete an event on the phone by ``event_id`` (from calendar.list). Not undone."""
        ctx = app_context()
        bad = self._writable(ctx)
        if bad:
            return bad
        try:
            was = self._times_of(ctx, event_id)
            if was is None:
                return {"ok": False, "error": f"no event {event_id}"}
            n = ctx.getContentResolver().delete(self._event_uri(event_id), None, None)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        return {"ok": n > 0, "event_id": int(event_id), "title": was[2]}

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
