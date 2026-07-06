from datetime import date, datetime

import pytest

from bot.db import AlreadyWaitlistedError, Database, SlotFullError, SlotTakenError


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


def test_bookings_for_user_and_slot(db):
    slot_id = db.add_slot(0, "10:00", 60, capacity=5)
    other_slot_id = db.add_slot(2, "10:00", 60, capacity=5)
    db.book(slot_id, date(2026, 7, 6), 111, "Alice")
    db.book(slot_id, date(2026, 7, 13), 111, "Alice")
    db.book(other_slot_id, date(2026, 7, 8), 111, "Alice")  # different slot, excluded
    db.book(slot_id, date(2026, 7, 6), 222, "Bob")  # different user, excluded
    rows = db.bookings_for_user_and_slot(111, slot_id)
    assert sorted(r["date"] for r in rows) == ["2026-07-06", "2026-07-13"]


# --- waiting list ---


def test_join_waitlist_and_duplicate_rejected(db):
    slot_id = db.add_slot(0, "10:00")
    day = date(2026, 7, 6)
    db.join_waitlist(slot_id, day, 111, "Alice")
    with pytest.raises(AlreadyWaitlistedError):
        db.join_waitlist(slot_id, day, 111, "Alice")
    assert len(db.waitlist_for_slot(slot_id, day)) == 1


def test_leave_waitlist(db):
    slot_id = db.add_slot(0, "10:00")
    day = date(2026, 7, 6)
    entry_id = db.join_waitlist(slot_id, day, 111, "Alice")
    assert db.leave_waitlist(entry_id) is True
    assert db.leave_waitlist(entry_id) is False
    assert db.waitlist_for_slot(slot_id, day) == []


def test_promote_next_waitlisted_books_earliest_in_line(db):
    slot_id = db.add_slot(0, "10:00")  # capacity 1
    day = date(2026, 7, 6)
    booking_id = db.book(slot_id, day, 111, "Alice")
    db.join_waitlist(slot_id, day, 222, "Bob")
    db.join_waitlist(slot_id, day, 333, "Carol")

    db.cancel_booking(booking_id)
    promotion = db.promote_next_waitlisted(slot_id, day)
    assert promotion is not None
    entry, new_booking_id = promotion
    assert entry["user_name"] == "Bob"
    roster = db.bookings_for_slot(slot_id, day)
    assert [r["user_name"] for r in roster] == ["Bob"]
    # Carol is still waiting; Bob is gone from the waitlist
    assert [r["user_name"] for r in db.waitlist_for_slot(slot_id, day)] == ["Carol"]


def test_promote_next_waitlisted_returns_none_when_empty(db):
    slot_id = db.add_slot(0, "10:00")
    assert db.promote_next_waitlisted(slot_id, date(2026, 7, 6)) is None


def test_waitlist_for_user_and_prune_stale(db):
    slot_id = db.add_slot(0, "10:00")
    db.join_waitlist(slot_id, date(2026, 7, 6), 111, "Alice")
    db.join_waitlist(slot_id, date(2020, 1, 1), 111, "Alice")  # stale, different slot instance
    assert len(db.waitlist_for_user(111, date(2026, 1, 1))) == 1
    db.prune_stale_waitlist(date(2026, 1, 1))
    assert len(db.waitlist_for_user(111, date(2020, 1, 1))) == 1  # only the future one survives


# --- per-booking reminders ---


def test_toggle_reminder_on_and_off(db):
    slot_id = db.add_slot(0, "10:00")
    booking_id = db.book(slot_id, date(2026, 7, 6), 111, "Alice")
    assert db.toggle_reminder(booking_id, 60) is True
    assert {r["offset_minutes"] for r in db.reminders_for_booking(booking_id)} == {60}
    assert db.toggle_reminder(booking_id, 60) is False
    assert db.reminders_for_booking(booking_id) == []


def test_due_reminders_and_mark_sent(db):
    slot_id = db.add_slot(0, "10:00")  # session at 10:00
    booking_id = db.book(slot_id, date(2026, 7, 6), 111, "Alice")
    db.toggle_reminder(booking_id, 60)  # 1 hour before -> due at 09:00

    not_yet = db.due_reminders(datetime(2026, 7, 6, 8, 59))
    assert not_yet == []

    due = db.due_reminders(datetime(2026, 7, 6, 9, 0))
    assert len(due) == 1
    assert due[0]["user_id"] == 111
    db.mark_reminder_sent(due[0]["reminder_id"])

    assert db.due_reminders(datetime(2026, 7, 6, 9, 30)) == []


def test_reminders_cascade_deleted_with_booking(db):
    slot_id = db.add_slot(0, "10:00")
    booking_id = db.book(slot_id, date(2026, 7, 6), 111, "Alice")
    db.toggle_reminder(booking_id, 60)
    db.cancel_booking(booking_id)
    assert db.reminders_for_booking(booking_id) == []


# --- additional admins ---


def test_add_list_remove_admin(db):
    assert db.is_admin(555) is False
    db.add_admin(555, "Bob", added_by=111)
    assert db.is_admin(555) is True
    assert [r["user_id"] for r in db.list_admins()] == [555]
    assert db.remove_admin(555) is True
    assert db.remove_admin(555) is False
    assert db.is_admin(555) is False


def test_add_admin_upserts_name(db):
    db.add_admin(555, "555", added_by=111)
    db.add_admin(555, "Bob Real Name", added_by=111)
    rows = db.list_admins()
    assert len(rows) == 1
    assert rows[0]["user_name"] == "Bob Real Name"


# --- audit log ---


def test_log_action_and_list(db):
    db.log_action(111, "Alice", "book", "Monday 10:00")
    db.log_action(222, "Bob", "cancel", "Tuesday 11:00")
    rows = db.list_audit_log()
    # most recent first
    assert [r["action"] for r in rows] == ["cancel", "book"]
    assert rows[0]["user_name"] == "Bob"
    assert rows[0]["details"] == "Tuesday 11:00"


def test_log_action_details_default_empty(db):
    db.log_action(111, "Alice", "add_admin")
    assert db.list_audit_log()[0]["details"] == ""


def test_list_audit_log_respects_limit(db):
    for i in range(5):
        db.log_action(111, "Alice", "book", str(i))
    rows = db.list_audit_log(limit=3)
    assert len(rows) == 3
    assert rows[0]["details"] == "4"  # most recent first


def test_list_audit_log_for_user_filters_by_user(db):
    db.log_action(111, "Alice", "book", "Monday")
    db.log_action(222, "Bob", "book", "Tuesday")
    db.log_action(111, "Alice", "cancel", "Monday")
    rows = db.list_audit_log_for_user(111)
    assert [r["action"] for r in rows] == ["cancel", "book"]
    assert all(r["user_id"] == 111 for r in rows)


# --- trainee registration / approval ---


def test_register_trainee_defaults_to_pending(db):
    assert db.get_trainee(111) is None
    db.register_trainee(111, "Alice Smith", "0501234567")
    row = db.get_trainee(111)
    assert row["full_name"] == "Alice Smith"
    assert row["phone"] == "0501234567"
    assert row["status"] == "pending"
    assert row["decided_at"] is None


def test_set_trainee_status_approve(db):
    db.register_trainee(111, "Alice", "0501234567")
    db.set_trainee_status(111, "approved", decided_by=999)
    row = db.get_trainee(111)
    assert row["status"] == "approved"
    assert row["decided_by"] == 999
    assert row["decided_at"] is not None


def test_reregister_after_rejection_resets_to_pending(db):
    db.register_trainee(111, "Alice", "0501234567")
    db.set_trainee_status(111, "rejected", decided_by=999)
    db.register_trainee(111, "Alice S.", "0509999999")  # retried with a correction
    row = db.get_trainee(111)
    assert row["status"] == "pending"
    assert row["full_name"] == "Alice S."
    assert row["phone"] == "0509999999"
    assert row["decided_at"] is None


def test_list_trainees_filters_by_status(db):
    db.register_trainee(111, "Alice", "1")
    db.register_trainee(222, "Bob", "2")
    db.set_trainee_status(222, "approved", decided_by=999)
    pending = db.list_trainees(status="pending")
    approved = db.list_trainees(status="approved")
    assert [r["user_id"] for r in pending] == [111]
    assert [r["user_id"] for r in approved] == [222]
    assert len(db.list_trainees()) == 2


def test_list_audit_users_groups_and_uses_latest_name(db):
    db.log_action(111, "Alice", "book")
    db.log_action(222, "Bob", "book")
    db.log_action(111, "Alice Cohen", "cancel")  # renamed since first action
    rows = db.list_audit_users()
    assert [(r["user_id"], r["user_name"], r["actions"]) for r in rows] == [
        (111, "Alice Cohen", 2),  # most recently active first
        (222, "Bob", 1),
    ]
