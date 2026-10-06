"""calendar.list and notify.list (Android APK plugins) against fake Java bridges, no device needed."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PY = Path(__file__).parents[1] / "android/app/src/main/python"
HOUR = 3600 * 1000
CDT = -5 * HOUR  # a fixed device offset for the tests
DAY = 24 * HOUR
TZ = {"offset": CDT}  # the fake device zone (fake_jclass)


def load(monkeypatch, name):
    monkeypatch.setitem(sys.modules, "rook_android.androidctx", SimpleNamespace(
        app_context=lambda: None, jclass=None, has_permission=lambda p: False))
    spec = importlib.util.spec_from_file_location(f"{name}_test", PY / f"rook_android/plugins/{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def cal(monkeypatch):
    return load(monkeypatch, "calendar_android")


class FakeCursor:
    def __init__(self, columns, rows):
        self.columns, self.rows, self.i, self.closed = columns, rows, -1, False

    def getColumnIndex(self, c): return self.columns.index(c) if c in self.columns else -1
    def moveToNext(self):
        self.i += 1
        return self.i < len(self.rows)
    def _v(self, i): return self.rows[self.i].get(self.columns[i])
    def isNull(self, i): return self._v(i) is None
    def getLong(self, i): return int(self._v(i))
    def getInt(self, i): return int(self._v(i))
    def getString(self, i): return str(self._v(i))
    def close(self): self.closed = True


class FakeResolver:
    def __init__(self, rows):
        self.rows, self.queries, self.cursor = rows, [], None

    def query(self, uri, projection, selection, args, order):
        self.queries.append(dict(uri=uri, projection=list(projection), selection=selection, order=order))
        self.cursor = FakeCursor(list(projection), self.rows)
        return self.cursor


def fake_jclass(name):
    if name == "android.net.Uri":
        return SimpleNamespace(parse=lambda s: s)
    if name == "java.util.TimeZone":
        return SimpleNamespace(getDefault=lambda: SimpleNamespace(getOffset=lambda ms: TZ["offset"]))
    raise AssertionError(name)


def plugin(cal, monkeypatch, rows, granted=True, tz=CDT):
    monkeypatch.setitem(TZ, "offset", tz)
    resolver = FakeResolver(rows)
    monkeypatch.setattr(cal, "app_context", lambda: SimpleNamespace(getContentResolver=lambda: resolver))
    monkeypatch.setattr(cal, "has_permission", lambda p: granted and p == "android.permission.READ_CALENDAR")
    monkeypatch.setattr(cal, "jclass", fake_jclass)
    return cal.AndroidCalendarPlugin(), resolver


def offset(ms):
    return CDT


def test_parse_time_forms(cal):
    base = 1_790_000_000_000
    assert cal.parse_time(None, base, offset) == base
    assert cal.parse_time("", base, offset) == base
    assert cal.parse_time(1_790_000_000, 0, offset) == base           # epoch seconds
    assert cal.parse_time(base, 0, offset) == base                     # epoch ms
    assert cal.parse_time(str(base), 0, offset) == base
    assert cal.parse_time("2026-10-05T14:00:00Z", 0, offset) == 1_791_208_800_000
    assert cal.parse_time("2026-10-05T09:00-05:00", 0, offset) == 1_791_208_800_000
    # Naive = device-local time (UTC-5 here).
    assert cal.parse_time("2026-10-05T09:00", 0, offset) == 1_791_208_800_000
    assert cal.parse_time("2026-10-05", 0, offset) == 1_791_176_400_000
    for bad in ("tomorrowish", True, float("nan")):
        with pytest.raises(ValueError):
            cal.parse_time(bad, 0, offset)


def test_event_row_shapes_timed_and_all_day(cal):
    timed = cal.event_row({"title": "Standup", "begin": 1_791_208_800_000, "end": 1_791_210_600_000, "allDay": 0,
                           "eventLocation": "Room 4", "calendar_displayName": "Work", "account_name": "me@outlook.example",
                           "account_type": "com.microsoft.office.outlook", "event_id": 7, "selfAttendeeStatus": 1}, offset)
    assert timed["start"] == "2026-10-05T09:00-05:00" and timed["end"] == "2026-10-05T09:30-05:00"
    assert timed["all_day"] is False and timed["calendar"] == "Work" and "declined" not in timed
    day = cal.event_row({"title": None, "begin": 1_791_158_400_000, "end": 1_791_244_800_000, "allDay": 1,
                         "selfAttendeeStatus": 2}, offset)
    assert day["start"] == day["end"] == "2026-10-05"
    assert day["title"] == "(no title)" and day["declined"] is True and day["location"] is None


def test_list_queries_instances_window_and_limits(cal, monkeypatch):
    rows = [{"title": f"E{i}", "begin": 1_791_208_800_000 + i * HOUR, "end": 1_791_208_800_000 + (i + 1) * HOUR,
             "allDay": 0, "calendar_displayName": "Personal", "account_name": "me@gmail.example",
             "account_type": "com.google", "event_id": i} for i in range(5)]
    p, resolver = plugin(cal, monkeypatch, rows)
    r = p._list(start="2026-10-05T00:00", end="2026-10-06T00:00", limit=3)
    assert r["ok"] and r["count"] == 3 and [e["title"] for e in r["events"]] == ["E0", "E1", "E2"]
    assert r["events"][0]["account"] == "me@gmail.example" and r["timezone_offset_min"] == -300
    q = resolver.queries[0]
    begin = 1_791_176_400_000
    # Widened a day each side (all-day rows are filtered by local date afterwards).
    assert q["uri"] == f"content://com.android.calendar/instances/when/{begin - DAY}/{begin + 2 * DAY}"
    assert q["selection"] == "visible = 1" and q["order"].startswith("begin ASC")
    assert resolver.cursor.closed


def test_list_defaults_to_next_24_hours(cal, monkeypatch):
    p, resolver = plugin(cal, monkeypatch, [])
    monkeypatch.setattr(cal.time, "time", lambda: 1_790_000_000.0)
    r = p._list()
    assert r == {"ok": True, "count": 0, "start": r["start"], "end": r["end"], "timezone_offset_min": -300, "events": []}
    assert resolver.queries[0]["uri"].endswith(f"/{1_790_000_000_000 - DAY}/{1_790_000_000_000 + 2 * DAY}")


def utc_ms(y, mo, d, h=0, mi=0):
    from datetime import datetime, timezone
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp() * 1000)


def all_day(title, y, mo, d, days=1):
    b = utc_ms(y, mo, d)
    return {"title": title, "begin": b, "end": b + days * DAY, "allDay": 1, "event_id": len(title)}


def timed(title, start_ms, minutes=30):
    return {"title": title, "begin": start_ms, "end": start_ms + minutes * 60_000, "allDay": 0, "event_id": start_ms}


# All-day rows (UTC midnight) around 2026-10-05; the fake cursor returns them all,
# as Android's widened instances query would.
DAYS = [all_day("Oct4", 2026, 10, 4), all_day("Oct5", 2026, 10, 5), all_day("Oct6", 2026, 10, 6),
        all_day("Trip", 2026, 10, 3, days=3)]  # Oct 3-5


def titles(r):
    assert r["ok"], r
    return [e["title"] for e in r["events"]]


def test_all_day_utc_minus_5(cal, monkeypatch):
    p, _ = plugin(cal, monkeypatch, DAYS + [timed("Late", utc_ms(2026, 10, 6, 0, 30))], tz=-5 * HOUR)  # 19:30 local Oct 5
    # Local "today": Oct 5 only (UTC-midnight rows used to leak tomorrow in).
    assert titles(p._list(start="2026-10-05", end="2026-10-06")) == ["Trip", "Oct5", "Late"]
    # After 19:00 local (past UTC midnight): today's all-day events still show, tomorrow's don't.
    assert titles(p._list(start="2026-10-05T20:00", end="2026-10-05T23:00")) == ["Trip", "Oct5"]
    # A window into tomorrow includes tomorrow's all-day event, sorted at local midnight.
    assert titles(p._list(start="2026-10-05T20:00", end="2026-10-06T12:00")) == ["Trip", "Oct5", "Oct6"]


def test_all_day_utc_plus_9(cal, monkeypatch):
    morning = timed("Breakfast", utc_ms(2026, 10, 4, 23))  # 08:00 local Oct 5
    p, _ = plugin(cal, monkeypatch, DAYS + [morning], tz=9 * HOUR)
    r = p._list(start="2026-10-05", end="2026-10-06")
    # Yesterday's all-day event no longer leaks in; the all-day event sorts before 08:00.
    assert titles(r) == ["Trip", "Oct5", "Breakfast"]
    assert r["events"][1]["start"] == r["events"][1]["end"] == "2026-10-05"
    assert r["events"][2]["start"] == "2026-10-05T08:00+09:00"
    assert titles(p._list(start="2026-10-05T18:00", end="2026-10-05T23:00")) == ["Trip", "Oct5"]


def test_all_day_utc(cal, monkeypatch):
    p, _ = plugin(cal, monkeypatch, DAYS + [timed("Noon", utc_ms(2026, 10, 5, 12))], tz=0)
    assert titles(p._list(start="2026-10-05", end="2026-10-06")) == ["Trip", "Oct5", "Noon"]
    assert titles(p._list(start="2026-10-06", end="2026-10-07")) == ["Oct6"]
    assert titles(p._list(start="2026-10-05", end="2026-10-07", limit=3)) == ["Trip", "Oct5", "Noon"]


def test_timed_rows_outside_widened_window_are_dropped(cal, monkeypatch):
    rows = [timed("Before", utc_ms(2026, 10, 5, 3)), timed("Ongoing", utc_ms(2026, 10, 5, 4, 45), 60),
            timed("In", utc_ms(2026, 10, 5, 15)), timed("After", utc_ms(2026, 10, 6, 5))]
    p, _ = plugin(cal, monkeypatch, rows, tz=-5 * HOUR)
    assert titles(p._list(start="2026-10-05", end="2026-10-06")) == ["Ongoing", "In"]


def test_permission_and_argument_errors(cal, monkeypatch):
    assert cal.AndroidCalendarPlugin().available() is False             # off device
    p, resolver = plugin(cal, monkeypatch, [], granted=False)
    assert p.available() is True                                         # registered even without the grant
    r = p._list()
    assert r["ok"] is False and "grant" in r["error"].lower() and r["needs_permission"] == "READ_CALENDAR"
    assert resolver.queries == []
    p, _ = plugin(cal, monkeypatch, [])
    assert "after start" in p._list(start="2026-10-05", end="2026-10-04")["error"]
    assert "too long" in p._list(start="2026-01-01", end="2028-01-01")["error"]
    assert "not a time" in p._list(start="soonish")["error"]


def test_list_is_described_with_typed_args(cal):
    from rook.core.registry import CapabilityRegistry
    reg = CapabilityRegistry()
    for path, fn in cal.AndroidCalendarPlugin().caps().items():
        reg.register(path, fn)
    d = reg.describe("calendar.")["calendar.list"]
    assert [x["name"] for x in d["params"]] == ["start", "end", "limit"]
    assert d["params"][2]["default"] == 20 and d["risk"] == "read"
    assert "calendar" in d["doc"].lower()


# ---- notify.list (mail via notifications) ----------------------------------

class FakeListener:
    items = [
        {"key": "1", "package": "com.google.android.gm", "title": "Ana", "text": "Lunch?", "ts": 1790000000.5},
        {"key": "2", "package": "com.whatsapp", "title": "Bo", "text": "hi", "ts": 1790000001.0, "posted_ms": 1790000001000},
        {"key": "3", "package": "com.microsoft.office.outlook", "title": "IT", "text": "Patch", "ts": 1790000002.0},
    ]

    @staticmethod
    def isEnabled(ctx): return True
    @staticmethod
    def isConnected(): return True
    @staticmethod
    def snapshotJson(limit): return json.dumps(FakeListener.items[:limit])


def test_notify_list_filters_mail_packages_and_has_posted_time(monkeypatch):
    m = load(monkeypatch, "notify_android")
    monkeypatch.setattr(m, "_Listener", FakeListener)
    p = m.AndroidNotifyPlugin()
    r = p._list(packages="com.google.android.gm, com.microsoft.office.outlook")
    assert r["ok"] and [i["key"] for i in r["notifications"]] == ["1", "3"]
    assert all({"package", "title", "text", "posted_ms"} <= set(i) for i in r["notifications"])
    assert r["notifications"][0]["posted_ms"] == 1790000000500
    assert [i["key"] for i in p._list(limit=1, packages=["com.microsoft.office.outlook"])["notifications"]] == ["3"]
    assert p._list()["count"] == 3
