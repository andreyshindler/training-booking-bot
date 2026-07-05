from datetime import date

import pytest

from bot.db import Database, SlotFullError, SlotTakenError


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


def test_add_list_remove_slots(db):
    slot_id = db.add_slot(0, "10:00", 60)
    db.add_slot(2, "18:00", 90)
    rows = db.list_slots()
    assert [(r["weekday"], r["start_time"]) for r in rows] == [
        (0, "10:00"),
        (2, "18:00"),
    ]
    assert db.remove_slot(slot_id) is True
    assert db.remove_slot(slot_id) is False
    assert len(db.list_slots()) == 1


def test_duplicate_slot_rejected(db):
    db.add_slot(0, "10:00")
    with pytest.raises(Exception):
        db.add_slot(0, "10:00")


def test_booking_full_capacity_rejected(db):
    slot_id = db.add_slot(0, "10:00")  # default capacity 1
    day = date(2026, 7, 6)
    db.book(slot_id, day, 111, "Alice")
    with pytest.raises(SlotFullError):
        db.book(slot_id, day, 222, "Bob")
    # same slot on a different date is fine
    db.book(slot_id, date(2026, 7, 13), 222, "Bob")
    assert db.booking_counts_from(day) == {
        (slot_id, "2026-07-06"): 1,
        (slot_id, "2026-07-13"): 1,
    }


def test_booking_same_user_twice_rejected(db):
    slot_id = db.add_slot(0, "10:00", 60, capacity=5)
    day = date(2026, 7, 6)
    db.book(slot_id, day, 111, "Alice")
    with pytest.raises(SlotTakenError):
        db.book(slot_id, day, 111, "Alice")


def test_group_slot_allows_multiple_users_up_to_capacity(db):
    slot_id = db.add_slot(0, "10:00", 60, capacity=2)
    day = date(2026, 7, 6)
    db.book(slot_id, day, 111, "Alice")
    db.book(slot_id, day, 222, "Bob")
    with pytest.raises(SlotFullError):
        db.book(slot_id, day, 333, "Carol")
    assert db.booking_counts_from(day) == {(slot_id, "2026-07-06"): 2}


def test_bookings_for_slot_returns_only_that_sessions_roster(db):
    slot_id = db.add_slot(0, "10:00", 60, capacity=5)
    other_slot_id = db.add_slot(0, "12:00", 60, capacity=5)
    day = date(2026, 7, 6)
    db.book(slot_id, day, 111, "Alice")
    db.book(slot_id, day, 222, "Bob")
    db.book(slot_id, date(2026, 7, 13), 111, "Alice")  # different date, excluded
    db.book(other_slot_id, day, 333, "Carol")  # different slot, excluded
    roster = db.bookings_for_slot(slot_id, day)
    assert [r["user_name"] for r in roster] == ["Alice", "Bob"]


def test_cancel_booking_frees_slot(db):
    slot_id = db.add_slot(0, "10:00")
    day = date(2026, 7, 6)
    booking_id = db.book(slot_id, day, 111, "Alice")
    assert db.cancel_booking(booking_id) is True
    assert db.cancel_booking(booking_id) is False
    db.book(slot_id, day, 222, "Bob")  # slot is free again


def test_bookings_for_user_only_upcoming(db):
    slot_id = db.add_slot(0, "10:00")
    db.book(slot_id, date(2026, 6, 29), 111, "Alice")  # past
    db.book(slot_id, date(2026, 7, 13), 111, "Alice")  # future
    db.book(slot_id, date(2026, 7, 6), 222, "Bob")     # other user
    rows = db.bookings_for_user(111, date(2026, 7, 1))
    assert [r["date"] for r in rows] == ["2026-07-13"]
    assert rows[0]["start_time"] == "10:00"


def test_sync_slots_adds_removes_updates(db):
    db.add_slot(0, "10:00", 60)   # will stay unchanged
    db.add_slot(2, "18:00", 60)   # will get a new duration
    db.add_slot(4, "07:00", 60)   # will be removed
    added, removed, updated = db.sync_slots(
        [(0, "10:00", 60), (2, "18:00", 90), (6, "09:00", 45)]
    )
    assert (added, removed, updated) == (1, 1, 1)
    rows = {(r["weekday"], r["start_time"]): r["duration_min"] for r in db.list_slots()}
    assert rows == {(0, "10:00"): 60, (2, "18:00"): 90, (6, "09:00"): 45}


def test_sync_slots_sets_capacity(db):
    db.add_slot(0, "10:00", 60)  # default capacity 1
    added, removed, updated = db.sync_slots([(0, "10:00", 60, 8)])
    assert (added, removed, updated) == (0, 0, 1)
    assert db.list_slots()[0]["capacity"] == 8


def test_sync_slots_duration_change_keeps_bookings(db):
    slot_id = db.add_slot(0, "10:00", 60)
    db.book(slot_id, date(2026, 7, 6), 111, "Alice")
    db.sync_slots([(0, "10:00", 90)])
    rows = db.bookings_from(date(2026, 1, 1))
    assert len(rows) == 1 and rows[0]["duration_min"] == 90


def test_sync_slots_removal_cancels_bookings(db):
    slot_id = db.add_slot(0, "10:00", 60)
    db.book(slot_id, date(2026, 7, 6), 111, "Alice")
    db.sync_slots([])
    assert db.list_slots() == []
    assert db.bookings_from(date(2026, 1, 1)) == []


def test_removing_slot_cascades_bookings(db):
    slot_id = db.add_slot(0, "10:00")
    db.book(slot_id, date(2026, 7, 6), 111, "Alice")
    db.remove_slot(slot_id)
    assert db.bookings_from(date(2026, 1, 1)) == []


# --- one-time (non-recurring) slots ---


def test_add_one_time_slot_is_excluded_from_recurring_list(db):
    db.add_slot(0, "10:00")  # recurring
    db.add_one_time_slot(date(2026, 7, 8), "16:00", 45)
    assert [(r["weekday"], r["start_time"]) for r in db.list_slots()] == [(0, "10:00")]
    one_time = db.list_one_time_slots(date(2026, 1, 1), date(2027, 1, 1))
    assert [(r["date"], r["start_time"], r["duration_min"]) for r in one_time] == [
        ("2026-07-08", "16:00", 45)
    ]


def test_one_time_slot_duplicate_rejected(db):
    db.add_one_time_slot(date(2026, 7, 8), "16:00")
    with pytest.raises(Exception):
        db.add_one_time_slot(date(2026, 7, 8), "16:00")


def test_recurring_and_one_time_can_share_weekday_and_time(db):
    # Same weekday+time is fine as long as one is recurring and the other is dated.
    db.add_slot(2, "16:00")  # every Wednesday
    db.add_one_time_slot(date(2026, 7, 8), "16:00")  # a specific Wednesday
    assert len(db.list_slots()) == 1
    assert len(db.list_one_time_slots(date(2026, 1, 1), date(2027, 1, 1))) == 1


def test_list_one_time_slots_respects_window(db):
    db.add_one_time_slot(date(2026, 7, 8), "16:00")
    db.add_one_time_slot(date(2026, 12, 1), "16:00")
    rows = db.list_one_time_slots(date(2026, 7, 1), date(2026, 7, 31))
    assert [r["date"] for r in rows] == ["2026-07-08"]


def test_sync_one_time_slots_adds_removes_updates(db):
    db.add_one_time_slot(date(2026, 7, 8), "16:00", 45)   # will get a new duration
    db.add_one_time_slot(date(2026, 7, 9), "09:00", 60)   # will be removed
    added, removed, updated = db.sync_one_time_slots(
        [("2026-07-08", "16:00", 90), ("2026-07-10", "11:00", 30)],
        date(2026, 7, 1),
        date(2026, 7, 31),
    )
    assert (added, removed, updated) == (1, 1, 1)
    rows = {(r["date"], r["start_time"]): r["duration_min"] for r in db.list_one_time_slots(
        date(2026, 7, 1), date(2026, 7, 31)
    )}
    assert rows == {("2026-07-08", "16:00"): 90, ("2026-07-10", "11:00"): 30}


def test_sync_one_time_slots_ignores_entries_outside_window(db):
    db.add_one_time_slot(date(2026, 12, 25), "10:00")  # outside the synced window
    added, removed, updated = db.sync_one_time_slots([], date(2026, 7, 1), date(2026, 7, 31))
    assert (added, removed, updated) == (0, 0, 0)
    assert len(db.list_one_time_slots(date(2026, 1, 1), date(2027, 1, 1))) == 1


def test_one_time_slot_removal_cancels_bookings(db):
    slot_id = db.add_one_time_slot(date(2026, 7, 8), "16:00")
    db.book(slot_id, date(2026, 7, 8), 111, "Alice")
    db.remove_slot(slot_id)
    assert db.bookings_from(date(2026, 1, 1)) == []
