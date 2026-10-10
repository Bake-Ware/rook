"""Jobs time module (rook/hub/plugins/jobs/cron.py): durations, zone-aware
instants, cron parsing, next fire times across zones, and the DST rules from
docs/design/jobs.md 3 (a missing local time fires once at the next valid
instant; a repeated local time fires once, on its first occurrence)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from rook.hub.plugins.jobs.cron import (Cron, describe, iso, local_instant, parse_duration,
                                        parse_instant, valid_zone)

TOR = "America/Toronto"


def at(y, mo, d, h=0, mi=0, tz=TOR, fold=0):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz), fold=fold).timestamp()


def utc(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).timestamp()


def fires(expr, after, n, tz=TOR):
    c, out, t = Cron(expr), [], after
    for _ in range(n):
        t = c.next_fire(t, tz)
        out.append(t)
    return out


# -- durations and instants ----------------------------------------------------------

@pytest.mark.parametrize("text,secs", [("30s", 30), ("5m", 300), ("1h30m", 5400), ("2d", 172800),
                                        ("1w", 604800), (90, 90), ("45", 45), ("0.5s", 0.5),
                                        ("250ms", 0.25)])
def test_parse_duration(text, secs):
    assert parse_duration(text) == secs


@pytest.mark.parametrize("bad", ["", "5x", "m5", "-3", True, -1, "5m garbage"])
def test_parse_duration_rejects(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_instants_must_carry_an_offset():
    assert parse_instant("2026-10-11T09:00:00-04:00") == utc(2026, 10, 11, 13)
    assert parse_instant("2026-10-11T13:00:00Z") == utc(2026, 10, 11, 13)
    assert parse_instant(12.5) == 12.5
    with pytest.raises(ValueError, match="no UTC offset"):
        parse_instant("2026-10-11T09:00:00")
    with pytest.raises(ValueError):
        parse_instant("tomorrow")


def test_iso_is_zone_aware_and_never_the_host_zone():
    t = utc(2026, 1, 15, 17)
    assert iso(t, TOR) == "2026-01-15T12:00:00-05:00"
    assert iso(t) == "2026-01-15T17:00:00+00:00"
    assert iso(t, "Not/AZone") == "2026-01-15T17:00:00+00:00"
    assert valid_zone("Asia/Kolkata") and not valid_zone("Mars/Base") and not valid_zone("")


# -- parsing ----------------------------------------------------------------------------

def test_fields_lists_ranges_steps_and_names():
    c = Cron("0,30 9-17/2 * jan-mar mon-fri")
    assert c.minutes == {0, 30}
    assert c.hours == {9, 11, 13, 15, 17}
    assert c.months == {1, 2, 3}
    assert c.dows == {1, 2, 3, 4, 5}
    assert Cron("0 0 * * 7").dows == {0}  # 7 is Sunday too
    assert Cron("5/20 * * * *").minutes == {5, 25, 45}


@pytest.mark.parametrize("macro,expr", [("@hourly", "0 * * * *"), ("@daily", "0 0 * * *"),
                                         ("@weekly", "0 0 * * 0"), ("@monthly", "0 0 1 * *")])
def test_macros(macro, expr):
    a, b = Cron(macro), Cron(expr)
    assert (a.minutes, a.hours, a.doms, a.months, a.dows) == (b.minutes, b.hours, b.doms, b.months, b.dows)


@pytest.mark.parametrize("bad", ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *",
                                 "* * 0 * *", "* * * 13 *", "5-1 * * * *", "*/0 * * * *",
                                 "@fortnightly", "a * * * *", ",1 * * * *"])
def test_bad_expressions(bad):
    with pytest.raises(ValueError):
        Cron(bad)


def test_never_firing_expression_returns_none():
    assert Cron("0 0 31 2 *").next_fire(utc(2026, 1, 1)) is None


def test_day_of_month_or_day_of_week_when_both_are_set():
    # Vixie cron: the 13th OR any Friday.
    got = fires("0 12 13 * 5", at(2026, 2, 1), 4)
    assert got == [at(2026, 2, 6, 12), at(2026, 2, 13, 12), at(2026, 2, 20, 12), at(2026, 2, 27, 12)]
    got = fires("0 12 13 * *", at(2026, 2, 1), 1)
    assert got == [at(2026, 2, 13, 12)]


def test_describe_reads_back():
    assert describe("30 3 * * *") == "at 3:30"
    assert "every hour" in describe("@hourly")


# -- next fire times ------------------------------------------------------------------

def test_next_fire_is_strictly_after():
    c = Cron("0 3 * * *")
    t = at(2026, 6, 1, 3)
    assert c.next_fire(t, TOR) == at(2026, 6, 2, 3)
    assert c.next_fire(t - 1, TOR) == t


def test_evaluated_in_the_schedule_zone_not_the_host():
    after = utc(2026, 6, 1)
    assert Cron("0 9 * * *").next_fire(after, TOR) == utc(2026, 6, 1, 13)          # EDT, -4
    assert Cron("0 9 * * *").next_fire(after, "Asia/Kolkata") == utc(2026, 6, 1, 3, 30)  # +5:30
    assert Cron("0 9 * * *").next_fire(after, "UTC") == utc(2026, 6, 1, 9)
    assert Cron("0 9 * * *").next_fire(utc(2026, 1, 1), TOR) == utc(2026, 1, 1, 14)  # EST, -5


def test_fires_lists_a_window():
    c = Cron("*/15 * * * *")
    assert c.fires(utc(2026, 1, 1, 0, 0), utc(2026, 1, 1, 1, 0), "UTC") == [
        utc(2026, 1, 1, 0, 15), utc(2026, 1, 1, 0, 30), utc(2026, 1, 1, 0, 45), utc(2026, 1, 1, 1, 0)]
    assert len(c.fires(utc(2026, 1, 1), utc(2026, 2, 1), "UTC", limit=10)) == 10


def test_monthly_and_weekly():
    assert fires("@monthly", at(2026, 1, 15), 2) == [at(2026, 2, 1), at(2026, 3, 1)]
    assert fires("@weekly", at(2026, 10, 7), 1) == [at(2026, 10, 11)]  # a Sunday


# -- DST: spring forward (2026-03-08 02:00 -> 03:00 in Toronto) ----------------------------

def test_spring_forward_missing_time_fires_once_at_the_next_valid_instant():
    got = fires("30 2 * * *", at(2026, 3, 7, 12), 3)
    assert got[0] == at(2026, 3, 8, 3, 0) == utc(2026, 3, 8, 7, 0)  # the moment clocks jump
    assert got[1] == at(2026, 3, 9, 2, 30)
    assert got[2] == at(2026, 3, 10, 2, 30)


def test_spring_forward_several_missing_times_fire_once():
    got = fires("*/15 2 * * *", at(2026, 3, 8, 0), 3)
    assert got[0] == utc(2026, 3, 8, 7, 0)       # 02:00..02:45 all missing: one fire at 03:00 EDT
    assert got[1] == at(2026, 3, 9, 2, 0)        # then the next day
    assert got[2] == at(2026, 3, 9, 2, 15)


def test_spring_forward_hourly_has_no_duplicate():
    got = fires("0 * * * *", utc(2026, 3, 8, 6, 30), 3)  # 01:30 EST
    assert got == [utc(2026, 3, 8, 7), utc(2026, 3, 8, 8), utc(2026, 3, 8, 9)]


# -- DST: fall back (2026-11-01 02:00 -> 01:00 in Toronto) ---------------------------------

def test_fall_back_repeated_time_fires_once_on_the_first_occurrence():
    got = fires("30 1 * * *", at(2026, 10, 31, 12), 2)
    assert got[0] == at(2026, 11, 1, 1, 30, fold=0) == utc(2026, 11, 1, 5, 30)  # EDT, first
    assert got[1] == at(2026, 11, 2, 1, 30) == utc(2026, 11, 2, 6, 30)


def test_fall_back_hourly_skips_the_repeated_hour():
    got = fires("0 * * * *", utc(2026, 11, 1, 4, 30), 3)  # 00:30 EDT
    assert got == [utc(2026, 11, 1, 5), utc(2026, 11, 1, 7), utc(2026, 11, 1, 8)]


def test_fall_back_starting_inside_the_second_occurrence_does_not_refire():
    # Hub (re)starts at 01:10 EST (the second 01:10): the 01:15/01:30 fires
    # already happened in the first pass, so the next is tomorrow.
    after = at(2026, 11, 1, 1, 10, fold=1)
    assert after == utc(2026, 11, 1, 6, 10)
    assert Cron("30 1 * * *").next_fire(after, TOR) == at(2026, 11, 2, 1, 30)


def test_other_zones_dst():
    # London: 2026-03-29 01:00 GMT -> 02:00 BST.
    assert Cron("30 1 * * *").next_fire(utc(2026, 3, 28, 12), "Europe/London") == utc(2026, 3, 29, 1, 0)
    # Lord Howe moves 30 minutes: 2026-10-04 02:00 -> 02:30.
    assert Cron("15 2 * * *").next_fire(utc(2026, 10, 3), "Australia/Lord_Howe") == \
        at(2026, 10, 4, 2, 30, tz="Australia/Lord_Howe")
    # Kolkata has no DST: same UTC time every day.
    got = fires("0 9 * * *", utc(2026, 3, 1), 2, tz="Asia/Kolkata")
    assert got[1] - got[0] == 86400


def test_local_instant_rules_directly():
    z = ZoneInfo(TOR)
    assert local_instant(datetime(2026, 3, 8, 2, 30), z) == utc(2026, 3, 8, 7)
    assert local_instant(datetime(2026, 11, 1, 1, 30), z) == utc(2026, 11, 1, 5, 30)
    assert local_instant(datetime(2026, 7, 1, 12, 0), z) == utc(2026, 7, 1, 16)
