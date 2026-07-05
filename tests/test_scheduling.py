from datetime import date, datetime

import pytest

from bot.scheduling import available_slots, parse_time, parse_weekday


def test_parse_weekday_accepts_short_and_long_names():
    assert parse_weekday("mon") == 0
    assert parse_weekday("Monday") == 0
    assert parse_weekday("SUN") == 6
    assert parse_weekday(" fri ") == 4


def test_parse_weekday_rejects_garbage():
    with pytest.raises(ValueError):
        parse_weekday("someday")


def test_parse_time_pads_and_validates():
    assert parse_time("9:05") == "09:05"
    assert parse_time("23:59") == "23:59"
    for bad in ["24:00", "9:5", "nine", "12:60", ""]:
        with pytest.raises(ValueError):
            parse_time(bad)


def _slot(slot_id, weekday, start_time, duration=60):
    return {
        "id": slot_id,
        "weekday": weekday,
        "start_time": start_time,
        "duration_min": duration,
    }


# 2026-07-06 is a Monday
NOW = datetime(2026, 7, 6, 8, 0)


def test_available_slots_expands_weekly_schedule():
    slots = [_slot(1, 0, "10:00"), _slot(2, 2, "18:30", 90)]
    result = available_slots(slots, set(), NOW, days_ahead=7)
    labels = [(s.slot_id, s.day, s.start_time) for s in result]
    assert labels == [
        (1, date(2026, 7, 6), "10:00"),   # this Monday
        (2, date(2026, 7, 8), "18:30"),   # this Wednesday
        (1, date(2026, 7, 13), "10:00"),  # next Monday (day 7)
    ]


def test_available_slots_excludes_past_times_today():
    slots = [_slot(1, 0, "07:00"), _slot(2, 0, "09:00")]
    result = available_slots(slots, set(), NOW, days_ahead=0)
    assert [s.slot_id for s in result] == [2]


def test_available_slots_excludes_booked():
    slots = [_slot(1, 0, "10:00")]
    booked = {(1, "2026-07-06")}
    result = available_slots(slots, booked, NOW, days_ahead=7)
    assert [(s.slot_id, s.day) for s in result] == [(1, date(2026, 7, 13))]


def test_open_slot_label():
    result = available_slots([_slot(1, 0, "10:00", 45)], set(), NOW, days_ahead=0)
    assert result[0].label() == "Mon 06 Jul 10:00 (45 min)"
