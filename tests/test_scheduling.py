from datetime import date, datetime

import pytest

from bot.scheduling import (
    available_slots,
    has_unexpired_recurring_booking,
    is_reminder_due,
    parse_time,
    parse_weekday,
)


def test_parse_weekday_accepts_short_and_long_names():
    assert parse_weekday("mon") == 0
    assert parse_weekday("Monday") == 0
    assert parse_weekday("SUN") == 6
    assert parse_weekday(" fri ") == 4


def test_parse_weekday_accepts_hebrew_names():
    assert parse_weekday("שני") == 0
    assert parse_weekday("ראשון") == 6
    assert parse_weekday("יום שלישי") == 1
    assert parse_weekday("שבת") == 5


def test_parse_weekday_rejects_garbage():
    with pytest.raises(ValueError):
        parse_weekday("someday")


def test_parse_time_pads_and_validates():
    assert parse_time("9:05") == "09:05"
    assert parse_time("23:59") == "23:59"
    for bad in ["24:00", "9:5", "nine", "12:60", ""]:
        with pytest.raises(ValueError):
            parse_time(bad)


def _slot(slot_id, weekday, start_time, duration=60, capacity=1):
    return {
        "id": slot_id,
        "weekday": weekday,
        "start_time": start_time,
        "duration_min": duration,
        "capacity": capacity,
    }


def _one_time(slot_id, day_iso, start_time, duration=60, capacity=1):
    return {
        "id": slot_id,
        "date": day_iso,
        "start_time": start_time,
        "duration_min": duration,
        "capacity": capacity,
    }


# 2026-07-06 is a Monday
NOW = datetime(2026, 7, 6, 8, 0)


def test_available_slots_expands_weekly_schedule():
    slots = [_slot(1, 0, "10:00"), _slot(2, 2, "18:30", 90)]
    result = available_slots(slots, [], {}, NOW, days_ahead=7)
    labels = [(s.slot_id, s.day, s.start_time) for s in result]
    assert labels == [
        (1, date(2026, 7, 6), "10:00"),   # this Monday
        (2, date(2026, 7, 8), "18:30"),   # this Wednesday
        (1, date(2026, 7, 13), "10:00"),  # next Monday (day 7)
    ]


def test_available_slots_excludes_past_times_today():
    slots = [_slot(1, 0, "07:00"), _slot(2, 0, "09:00")]
    result = available_slots(slots, [], {}, NOW, days_ahead=0)
    assert [s.slot_id for s in result] == [2]


def test_available_slots_excludes_booked():
    slots = [_slot(1, 0, "10:00")]
    booked = {(1, "2026-07-06"): 1}
    result = available_slots(slots, [], booked, NOW, days_ahead=7)
    assert [(s.slot_id, s.day) for s in result] == [(1, date(2026, 7, 13))]


def test_available_slots_group_slot_stays_open_below_capacity():
    slots = [_slot(1, 0, "10:00", capacity=3)]
    booked = {(1, "2026-07-06"): 2}
    result = available_slots(slots, [], booked, NOW, days_ahead=0)
    assert len(result) == 1
    assert result[0].booked_count == 2 and result[0].capacity == 3


def test_open_slot_label_is_hebrew():
    result = available_slots([_slot(1, 0, "10:00", 45)], [], {}, NOW, days_ahead=0)
    assert result[0].label() == "יום שני 06/07 10:00 (45 דק')"


def test_open_slot_label_shows_occupancy_for_group_slots():
    result = available_slots(
        [_slot(1, 0, "10:00", 45, capacity=5)], [], {(1, "2026-07-06"): 2}, NOW, days_ahead=0
    )
    assert result[0].label() == "יום שני 06/07 10:00 (45 דק') — 2/5 נרשמו"


def test_available_slots_includes_one_time_lesson_on_its_date():
    one_time = [_one_time(9, "2026-07-08", "16:00", 45)]
    result = available_slots([], one_time, {}, NOW, days_ahead=7)
    assert [(s.slot_id, s.day, s.start_time) for s in result] == [
        (9, date(2026, 7, 8), "16:00")
    ]


def test_available_slots_one_time_lesson_does_not_recur():
    one_time = [_one_time(9, "2026-07-08", "16:00", 45)]
    result = available_slots([], one_time, {}, NOW, days_ahead=14)
    assert len(result) == 1  # only appears once, unlike a recurring slot


def test_available_slots_merges_recurring_and_one_time_on_same_day():
    recurring = [_slot(1, 2, "10:00")]  # Wednesday
    one_time = [_one_time(9, "2026-07-08", "16:00", 45)]  # same Wednesday
    result = available_slots(recurring, one_time, {}, NOW, days_ahead=7)
    assert [(s.slot_id, s.start_time) for s in result] == [
        (1, "10:00"),
        (9, "16:00"),
    ]


def _booking(day_iso, start_time, duration=60):
    return {"date": day_iso, "start_time": start_time, "duration_min": duration}


def test_has_unexpired_recurring_booking_blocks_next_week_before_current_ends():
    # this week's occurrence hasn't started yet (NOW is 2026-07-06 08:00, session at 10:00)
    existing = [_booking("2026-07-06", "10:00", 60)]
    assert has_unexpired_recurring_booking(existing, date(2026, 7, 13), NOW) is True


def test_has_unexpired_recurring_booking_allows_after_current_ends():
    # this week's occurrence already ended (10:00-11:00, now is 12:00)
    existing = [_booking("2026-07-06", "10:00", 60)]
    later_now = datetime(2026, 7, 6, 12, 0)
    assert has_unexpired_recurring_booking(existing, date(2026, 7, 13), later_now) is False


def test_has_unexpired_recurring_booking_ignores_the_same_date():
    existing = [_booking("2026-07-06", "10:00", 60)]
    assert has_unexpired_recurring_booking(existing, date(2026, 7, 6), NOW) is False


def test_has_unexpired_recurring_booking_true_with_no_bookings():
    assert has_unexpired_recurring_booking([], date(2026, 7, 13), NOW) is False


def _reminder(day_iso, start_time, offset_minutes):
    return {"date": day_iso, "start_time": start_time, "offset_minutes": offset_minutes}


def test_is_reminder_due_true_at_trigger_point():
    reminder = _reminder("2026-07-06", "10:00", 60)  # 1 hour before
    assert is_reminder_due(reminder, datetime(2026, 7, 6, 9, 0)) is True


def test_is_reminder_due_false_before_trigger_point():
    reminder = _reminder("2026-07-06", "10:00", 60)
    assert is_reminder_due(reminder, datetime(2026, 7, 6, 8, 59)) is False


def test_is_reminder_due_false_after_session_started():
    # bot was down and only came back after the session already began; skip it
    reminder = _reminder("2026-07-06", "10:00", 60)
    assert is_reminder_due(reminder, datetime(2026, 7, 6, 10, 0)) is False
