"""SQLite storage for schedule slots and bookings."""

import sqlite3
from datetime import date

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 60,
    capacity INTEGER NOT NULL DEFAULT 1,
    UNIQUE (weekday, start_time)
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (slot_id, date, user_id)
);
"""


class SlotTakenError(Exception):
    """Raised when this user already has a booking for this slot on this date."""


class SlotFullError(Exception):
    """Raised when the slot's capacity is already reached for that date."""


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """Upgrade databases created before the capacity/group-booking feature."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(slots)")}
        if "capacity" not in cols:
            self.conn.execute(
                "ALTER TABLE slots ADD COLUMN capacity INTEGER NOT NULL DEFAULT 1"
            )

        unique_cols: set[str] = set()
        for idx in self.conn.execute("PRAGMA index_list(bookings)").fetchall():
            if idx["unique"]:
                info = self.conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
                unique_cols = {r["name"] for r in info}
                break
        if unique_cols == {"slot_id", "date"}:
            # Old schema allowed only one booking per slot/date; recreate the
            # table so several users can enroll in the same open session.
            self.conn.executescript(
                """
                PRAGMA foreign_keys = OFF;
                ALTER TABLE bookings RENAME TO bookings_old;
                CREATE TABLE bookings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
                    date TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    user_name TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    UNIQUE (slot_id, date, user_id)
                );
                INSERT INTO bookings SELECT * FROM bookings_old;
                DROP TABLE bookings_old;
                PRAGMA foreign_keys = ON;
                """
            )

    # --- schedule slots (managed by the trainer) ---

    def add_slot(
        self, weekday: int, start_time: str, duration_min: int = 60, capacity: int = 1
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO slots (weekday, start_time, duration_min, capacity) "
                "VALUES (?, ?, ?, ?)",
                (weekday, start_time, duration_min, capacity),
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

    def sync_slots(self, desired) -> tuple[int, int, int]:
        """Make the stored schedule match ``desired``.

        Each item is (weekday, "HH:MM", duration_min[, capacity]); capacity
        defaults to 1. Slots are matched by (weekday, start_time): missing
        ones are removed (cascading their bookings), new ones added, and
        duration/capacity changes applied in place so existing bookings
        survive. Returns (added, removed, updated).
        """
        existing = {(r["weekday"], r["start_time"]): r for r in self.list_slots()}
        want: dict[tuple[int, str], tuple[int, int]] = {}
        for weekday, start_time, duration_min, *rest in desired:
            capacity = rest[0] if rest else 1
            want[(weekday, start_time)] = (duration_min, capacity)

        added = removed = updated = 0
        for key, row in existing.items():
            if key not in want:
                self.remove_slot(row["id"])
                removed += 1
            elif (row["duration_min"], row["capacity"]) != want[key]:
                duration_min, capacity = want[key]
                with self.conn:
                    self.conn.execute(
                        "UPDATE slots SET duration_min = ?, capacity = ? WHERE id = ?",
                        (duration_min, capacity, row["id"]),
                    )
                updated += 1
        for key, (duration_min, capacity) in want.items():
            if key not in existing:
                self.add_slot(key[0], key[1], duration_min, capacity)
                added += 1
        return added, removed, updated

    # --- bookings (made by trainees) ---

    def book(self, slot_id: int, day: date, user_id: int, user_name: str) -> int:
        with self.conn:
            slot = self.conn.execute(
                "SELECT capacity FROM slots WHERE id = ?", (slot_id,)
            ).fetchone()
            count = self.conn.execute(
                "SELECT COUNT(*) AS c FROM bookings WHERE slot_id = ? AND date = ?",
                (slot_id, day.isoformat()),
            ).fetchone()["c"]
            if slot is not None and count >= slot["capacity"]:
                raise SlotFullError(f"Slot {slot_id} on {day} is full")
            try:
                cur = self.conn.execute(
                    "INSERT INTO bookings (slot_id, date, user_id, user_name) "
                    "VALUES (?, ?, ?, ?)",
                    (slot_id, day.isoformat(), user_id, user_name),
                )
            except sqlite3.IntegrityError as exc:
                raise SlotTakenError(
                    f"User {user_id} already booked slot {slot_id} on {day}"
                ) from exc
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

    def booking_counts_from(self, from_day: date) -> dict[tuple[int, str], int]:
        """Number of enrolled users per (slot_id, iso_date), for availability filtering."""
        rows = self.conn.execute(
            "SELECT slot_id, date, COUNT(*) AS c FROM bookings "
            "WHERE date >= ? GROUP BY slot_id, date",
            (from_day.isoformat(),),
        ).fetchall()
        return {(row["slot_id"], row["date"]): row["c"] for row in rows}
