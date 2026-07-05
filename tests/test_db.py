from datetime import date

import pytest

from bot.db import Database, SlotTakenError


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


def test_booking_and_double_booking(db):
    slot_id = db.add_slot(0, "10:00")
    day = date(2026, 7, 6)
    db.book(slot_id, day, 111, "Alice")
    with pytest.raises(SlotTakenError):
        db.book(slot_id, day, 222, "Bob")
    # same slot on a different date is fine
    db.book(slot_id, date(2026, 7, 13), 222, "Bob")
    assert db.booked_pairs_from(day) == {
        (slot_id, "2026-07-06"),
        (slot_id, "2026-07-13"),
    }


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
