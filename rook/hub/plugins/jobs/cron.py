"""Time for jobs: durations, zone-aware instants and 5-field cron.

Everything stored is epoch seconds (UTC). Schedules are evaluated in an IANA
zone (the job trigger's ``tz``, else the hub setting ``job.timezone``), never
the host clock's zone. DST rules (docs/design/jobs.md 3):

* a local time that does not exist (spring forward) fires once, at the next
  valid instant (the moment the clocks jump);
* a local time that happens twice (fall back) fires once, on the first
  occurrence.

Cron is the usual five fields (minute hour day-of-month month day-of-week)
with ``*``, lists, ranges, steps and month/day names, plus ``@hourly``,
``@daily``, ``@weekly``, ``@monthly`` (and ``@yearly``/``@annually``,
``@midnight``). As in Vixie cron, when both day fields are restricted a day
matches either of them.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TZ = "America/Toronto"

MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DAYS = {d: i for i, d in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
# (name, low, high, names)
_FIELDS = (("minute", 0, 59, {}), ("hour", 0, 23, {}), ("day of month", 1, 31, {}),
           ("month", 1, 12, _MONTHS), ("day of week", 0, 7, _DAYS))
#: How far ahead next_fire looks before deciding an expression never fires
#: (covers Feb 29 on a given weekday).
SEARCH_DAYS = 366 * 8 + 2

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)")
_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# -- durations ---------------------------------------------------------------

def parse_duration(value) -> float:
    """``"30s"``, ``"5m"``, ``"1h30m"``, ``"2d"`` or a number of seconds."""
    if isinstance(value, bool):
        raise ValueError(f"not a duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError("a duration cannot be negative")
        return float(value)
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        raise ValueError("empty duration")
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    pos, total = 0, 0.0
    for m in _DURATION.finditer(text):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _UNITS[m.group(2)]
        pos = m.end()
    if pos != len(text):
        raise ValueError(f"not a duration: {value!r} (use e.g. 30s, 5m, 1h30m, 2d)")
    return total


# -- zones and instants -----------------------------------------------------

@lru_cache(maxsize=64)
def zone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("time zone must be an IANA name such as America/Toronto")
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown time zone {name!r}") from None


def valid_zone(name: str) -> bool:
    try:
        zone(name)
        return True
    except ValueError:
        return False


def parse_instant(value) -> float:
    """A zone-aware ISO 8601 string (``2026-10-11T09:00:00-04:00``, ``…Z``)
    or epoch seconds. Naive strings are refused: their zone is a guess."""
    if isinstance(value, bool):
        raise ValueError(f"not a time: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"not an ISO 8601 time: {value!r}") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{value!r} has no UTC offset; add one (e.g. -04:00 or Z)")
    return dt.timestamp()


def iso(ts: float | None, tz: str = "UTC") -> str | None:
    """Epoch seconds as an ISO 8601 string in ``tz``, with its offset."""
    if ts is None:
        return None
    try:
        zi = zone(tz) if tz and tz != "UTC" else timezone.utc
    except ValueError:
        zi = timezone.utc
    return datetime.fromtimestamp(float(ts), zi).isoformat(timespec="seconds")


# -- cron ---------------------------------------------------------------------

def _parse_field(text: str, name: str, low: int, high: int, names: dict) -> tuple[frozenset, bool]:
    """(allowed values, restricted?) for one field."""
    def num(tok: str) -> int:
        tok = tok.lower()
        if tok in names:
            return names[tok]
        if not tok.isdigit():
            raise ValueError(f"cron {name}: {tok!r} is not a number")
        v = int(tok)
        if not low <= v <= high:
            raise ValueError(f"cron {name}: {v} is outside {low}-{high}")
        return v

    out: set[int] = set()
    restricted = text != "*"
    for part in text.split(","):
        if not part:
            raise ValueError(f"cron {name}: empty list item")
        base, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise ValueError(f"cron {name}: bad step {step_s!r}")
            step = int(step_s)
        if base == "*":
            lo, hi = low, high
        elif "-" in base:
            a, _, b = base.partition("-")
            lo, hi = num(a), num(b)
            if lo > hi:
                raise ValueError(f"cron {name}: range {base!r} runs backwards")
        else:
            lo = num(base)
            hi = high if step_s else lo
        out.update(range(lo, hi + 1, step))
    return frozenset(out), restricted


class Cron:
    """A parsed cron expression. ``next_fire(after, tz)`` is the first fire
    instant strictly after ``after`` (epoch seconds), or ``None`` if the
    expression never fires (``0 0 31 2 *``)."""

    def __init__(self, expr: str) -> None:
        if not isinstance(expr, str) or not expr.strip():
            raise ValueError("cron expression is empty")
        self.expr = expr.strip()
        text = MACROS.get(self.expr.lower(), self.expr)
        if text.startswith("@"):
            raise ValueError(f"unknown cron macro {self.expr!r} (use {', '.join(MACROS)})")
        parts = text.split()
        if len(parts) != 5:
            raise ValueError(f"cron needs 5 fields (minute hour day month weekday), got {len(parts)}")
        fields = [_parse_field(p, *f) for p, f in zip(parts, _FIELDS)]
        (self.minutes, _), (self.hours, _), (self.doms, self.dom_r), \
            (self.months, _), (dows, self.dow_r) = fields
        self.dows = frozenset(d % 7 for d in dows)  # 7 is Sunday too
        self._mins = sorted(self.minutes)
        self._hrs = sorted(self.hours)

    def day_matches(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.doms
        dow = (d.isoweekday() % 7) in self.dows
        if self.dom_r and self.dow_r:
            return dom or dow
        return dom and dow

    def next_fire(self, after: float, tz: str = DEFAULT_TZ) -> float | None:
        zi = zone(tz)
        start = datetime.fromtimestamp(after, zi).replace(tzinfo=None, second=0, microsecond=0)
        day = start.date()
        for _ in range(SEARCH_DAYS):
            if self.day_matches(day):
                for h in self._hrs:
                    for m in self._mins:
                        local = datetime.combine(day, dtime(h, m))
                        if local < start:
                            continue
                        ts = local_instant(local, zi)
                        if ts > after:
                            return ts
            day += timedelta(days=1)
        return None

    def fires(self, after: float, until: float, tz: str = DEFAULT_TZ, limit: int = 1000) -> list[float]:
        """Fire instants in (after, until], at most ``limit``."""
        out: list[float] = []
        t = after
        while len(out) < limit:
            t = self.next_fire(t, tz)
            if t is None or t > until:
                break
            out.append(t)
        return out


def local_instant(local: datetime, zi: ZoneInfo) -> float:
    """The UTC instant of a naive local wall time in ``zi``. A time that
    happens twice is its first occurrence; a time that does not exist is the
    next valid instant (the first wall minute after the gap)."""
    aware = local.replace(tzinfo=zi, fold=0)
    utc = aware.astimezone(timezone.utc)
    if utc.astimezone(zi).replace(tzinfo=None) == local:
        return utc.timestamp()
    probe = local
    for _ in range(24 * 60):  # gaps are at most a day (Samoa 2011)
        probe += timedelta(minutes=1)
        aware = probe.replace(tzinfo=zi, fold=0)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zi).replace(tzinfo=None) == probe:
            return utc.timestamp()
    return aware.astimezone(timezone.utc).timestamp()


def describe(expr: str) -> str:
    """A short human reading of a cron expression (the editor's helper)."""
    c = Cron(expr)
    text = MACROS.get(c.expr.lower(), c.expr)
    mi, hr, dom, mon, dow = text.split()
    when = ("every minute" if mi == "*" and hr == "*" else
            f"at minute {mi} of every hour" if hr == "*" else
            f"at {hr}:{int(mi):02d}" if mi.isdigit() and hr.isdigit() else
            f"at minute {mi}, hour {hr}")
    days = []
    if dom != "*":
        days.append(f"on day {dom} of the month")
    if dow != "*":
        days.append(f"on weekday {dow} (0=Sun)")
    if mon != "*":
        days.append(f"in month {mon}")
    return " ".join([when] + days) or text
