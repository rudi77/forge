"""Tests für den minimalen Cron-Matcher (forge_cli.schedule)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from forge_cli.schedule import CronError, cron_due, cron_matches, parse_cron


def _dt(y=2026, mo=6, d=4, h=12, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_parse_cron_wrong_field_count() -> None:
    with pytest.raises(CronError):
        parse_cron("* * * *")
    with pytest.raises(CronError):
        parse_cron("* * * * * *")


def test_parse_cron_star_is_full_range() -> None:
    minute, hour, dom, month, dow = parse_cron("* * * * *")
    assert minute == set(range(60))
    assert hour == set(range(24))
    assert dom == set(range(1, 32))
    assert month == set(range(1, 13))
    assert dow == set(range(7))


def test_parse_cron_step_range_list() -> None:
    (minute, *_rest) = parse_cron("*/15 * * * *")
    assert minute == {0, 15, 30, 45}
    (m2, *_r2) = parse_cron("0-10/2 * * * *")
    assert m2 == {0, 2, 4, 6, 8, 10}
    (m3, *_r3) = parse_cron("1,2,5 * * * *")
    assert m3 == {1, 2, 5}


def test_parse_cron_out_of_bounds() -> None:
    with pytest.raises(CronError):
        parse_cron("99 * * * *")
    with pytest.raises(CronError):
        parse_cron("* 25 * * *")


def test_cron_matches_minute_and_hour() -> None:
    # 09:30 every day
    assert cron_matches("30 9 * * *", _dt(h=9, mi=30))
    assert not cron_matches("30 9 * * *", _dt(h=9, mi=31))
    assert not cron_matches("30 9 * * *", _dt(h=10, mi=30))


def test_cron_matches_day_of_week_sunday_zero_and_seven() -> None:
    # 2026-06-07 is a Sunday.
    sunday = _dt(d=7, h=0, mi=0)
    assert cron_matches("0 0 * * 0", sunday)
    assert cron_matches("0 0 * * 7", sunday)  # 7 also = Sunday
    # 2026-06-04 is a Thursday → weekday()==3 → cron dow 4.
    assert cron_matches("0 12 * * 4", _dt(d=4, h=12))
    assert not cron_matches("0 12 * * 0", _dt(d=4, h=12))


def test_cron_matches_dom_or_dow_when_both_restricted() -> None:
    # Vixie semantics: dom=1 OR dow=Mon → matches on the 1st OR any Monday.
    expr = "0 0 1 * 1"
    assert cron_matches(expr, _dt(mo=6, d=1, h=0, mi=0))  # the 1st (Monday too)
    # 2026-06-08 is a Monday but not the 1st → still matches via dow.
    assert cron_matches(expr, _dt(mo=6, d=8, h=0, mi=0))
    # 2026-06-09 is a Tuesday and not the 1st → no match.
    assert not cron_matches(expr, _dt(mo=6, d=9, h=0, mi=0))


def test_cron_due_first_run_uses_now() -> None:
    # last=None → due iff now matches.
    assert cron_due("0 12 * * *", last=None, now=_dt(h=12, mi=0))
    assert not cron_due("0 12 * * *", last=None, now=_dt(h=12, mi=1))


def test_cron_due_window_catches_missed_minute() -> None:
    # Heartbeat slept across the scheduled minute (interval > 60s).
    last = _dt(h=11, mi=58)
    now = _dt(h=12, mi=2)
    assert cron_due("0 12 * * *", last=last, now=now)  # 12:00 fell in window


def test_cron_due_not_due_between_ticks() -> None:
    last = _dt(h=12, mi=0)
    now = _dt(h=12, mi=5)
    assert not cron_due("0 13 * * *", last=last, now=now)


def test_cron_due_no_double_fire_same_minute() -> None:
    # Already fired at 12:00; same now → not due again.
    t = _dt(h=12, mi=0)
    assert not cron_due("0 12 * * *", last=t, now=t)
