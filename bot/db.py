"""SQLite storage for schedule slots and bookings."""

import sqlite3
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 60,
    UNIQUE (weekday, start_time)
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (slot_id, date)
);
"""


class SlotTakenError(Exception):
    """Raised when the slot is already booked for that date."""


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- schedule slots (managed by the trainer) ---

    def add_slot(self, weekday: int, start_time: str, duration_min: int = 60) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO slots (weekday, start_time, duration_min) VALUES (?, ?, ?)",
                (weekday, start_time, duration_min),
            )
        return cur.lastrowid

    def remove_slot(self, slot_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        return cur.rowcount > 0

    def list_slots(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM slots ORDER BY weekday, start_time"
        ).fetchall()

    def get_slot(self, slot_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM slots WHERE id = ?", (slot_id,)
        ).fetchone()

    def sync_slots(self, desired: list[tuple[int, str, int]]) -> tuple[int, int, int]:
        """Make the stored schedule match ``desired`` [(weekday, "HH:MM", minutes)].

        Slots are matched by (weekday, start_time): missing ones are removed
        (cascading their bookings), new ones added, and duration changes applied
        in place so existing bookings survive. Returns (added, removed, updated).
        """
        existing = {(r["weekday"], r["start_time"]): r for r in self.list_slots()}
        want: dict[tuple[int, str], int] = {}
        for weekday, start_time, duration_min in desired:
            want[(weekday, start_time)] = duration_min

        added = removed = updated = 0
        for key, row in existing.items():
            if key not in want:
                self.remove_slot(row["id"])
                removed += 1
            elif row["duration_min"] != want[key]:
                with self.conn:
                    self.conn.execute(
                        "UPDATE slots SET duration_min = ? WHERE id = ?",
                        (want[key], row["id"]),
                    )
                updated += 1
        for key, duration_min in want.items():
            if key not in existing:
                self.add_slot(key[0], key[1], duration_min)
                added += 1
        return added, removed, updated

    # --- bookings (made by trainees) ---

    def book(self, slot_id: int, day: date, user_id: int, user_name: str) -> int:
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO bookings (slot_id, date, user_id, user_name) "
                    "VALUES (?, ?, ?, ?)",
                    (slot_id, day.isoformat(), user_id, user_name),
                )
        except sqlite3.IntegrityError as exc:
            raise SlotTakenError(f"Slot {slot_id} on {day} is already booked") from exc
        return cur.lastrowid

    def get_booking(self, booking_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT b.*, s.start_time, s.duration_min FROM bookings b "
            "JOIN slots s ON s.id = b.slot_id WHERE b.id = ?",
            (booking_id,),
        ).fetchone()

    def cancel_booking(self, booking_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM bookings WHERE id = ?", (booking_id,)
            )
        return cur.rowcount > 0

    def bookings_for_user(self, user_id: int, from_day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT b.*, s.start_time, s.duration_min FROM bookings b "
            "JOIN slots s ON s.id = b.slot_id "
            "WHERE b.user_id = ? AND b.date >= ? "
            "ORDER BY b.date, s.start_time",
            (user_id, from_day.isoformat()),
        ).fetchall()

    def bookings_from(self, from_day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT b.*, s.start_time, s.duration_min FROM bookings b "
            "JOIN slots s ON s.id = b.slot_id "
            "WHERE b.date >= ? "
            "ORDER BY b.date, s.start_time",
            (from_day.isoformat(),),
        ).fetchall()

    def booked_pairs_from(self, from_day: date) -> set[tuple[int, str]]:
        """(slot_id, iso_date) pairs already booked, for availability filtering."""
        rows = self.conn.execute(
            "SELECT slot_id, date FROM bookings WHERE date >= ?",
            (from_day.isoformat(),),
        ).fetchall()
        return {(row["slot_id"], row["date"]) for row in rows}
