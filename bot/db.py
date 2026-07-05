"""SQLite storage for schedule slots and bookings."""

import sqlite3
from datetime import date, datetime

from .scheduling import is_reminder_due

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_time TEXT NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 60,
    capacity INTEGER NOT NULL DEFAULT 1,
    date TEXT
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
CREATE TABLE IF NOT EXISTS waitlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER NOT NULL REFERENCES slots(id) ON DELETE CASCADE,
    date TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    user_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (slot_id, date, user_id)
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    offset_minutes INTEGER NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (booking_id, offset_minutes)
);
"""


class SlotTakenError(Exception):
    """Raised when this user already has a booking for this slot on this date."""


class SlotFullError(Exception):
    """Raised when the slot's capacity is already reached for that date."""


class AlreadyWaitlistedError(Exception):
    """Raised when this user is already on the waiting list for this slot/date."""


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
        """Upgrade databases created before the capacity/one-time-lesson features."""
        cols = {row["name"] for row in self.conn.execute("PRAGMA table_info(slots)")}
        if "capacity" not in cols:
            self.conn.execute(
                "ALTER TABLE slots ADD COLUMN capacity INTEGER NOT NULL DEFAULT 1"
            )
            cols.add("capacity")

        if "date" not in cols:
            # SQLite can't drop the old table-level UNIQUE(weekday, start_time)
            # via ALTER TABLE, and one-time lessons need a nullable date column
            # with its own partial-unique rule, so recreate the table. Renaming
            # "slots" itself (rather than the replacement) would make SQLite
            # silently rewrite bookings' FK to point at the old name, so instead
            # build the replacement under a temp name, drop "slots", then rename
            # the temp table into place.
            self.conn.executescript(
                """
                PRAGMA foreign_keys = OFF;
                CREATE TABLE slots_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    weekday INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
                    start_time TEXT NOT NULL,
                    duration_min INTEGER NOT NULL DEFAULT 60,
                    capacity INTEGER NOT NULL DEFAULT 1,
                    date TEXT
                );
                INSERT INTO slots_new (id, weekday, start_time, duration_min, capacity, date)
                    SELECT id, weekday, start_time, duration_min, capacity, NULL FROM slots;
                DROP TABLE slots;
                ALTER TABLE slots_new RENAME TO slots;
                PRAGMA foreign_keys = ON;
                """
            )

        # The "date" column is now guaranteed to exist (either just added above,
        # or present since this connection's first SCHEMA run); these partial
        # unique indexes replace the old table-level UNIQUE(weekday, start_time).
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_slots_recurring_unique "
            "ON slots (weekday, start_time) WHERE date IS NULL"
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_slots_one_time_unique "
            "ON slots (date, start_time) WHERE date IS NOT NULL"
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

    # --- recurring weekly slots (managed by the trainer) ---

    def add_slot(
        self, weekday: int, start_time: str, duration_min: int = 60, capacity: int = 1
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO slots (weekday, start_time, duration_min, capacity, date) "
                "VALUES (?, ?, ?, ?, NULL)",
                (weekday, start_time, duration_min, capacity),
            )
        return cur.lastrowid

    def remove_slot(self, slot_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
        return cur.rowcount > 0

    def list_slots(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM slots WHERE date IS NULL ORDER BY weekday, start_time"
        ).fetchall()

    def get_slot(self, slot_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM slots WHERE id = ?", (slot_id,)
        ).fetchone()

    def sync_slots(self, desired) -> tuple[int, int, int]:
        """Make the recurring weekly schedule match ``desired``.

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

    # --- one-time (non-recurring) slots ---

    def add_one_time_slot(
        self, day: date, start_time: str, duration_min: int = 60, capacity: int = 1
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO slots (weekday, start_time, duration_min, capacity, date) "
                "VALUES (?, ?, ?, ?, ?)",
                (day.weekday(), start_time, duration_min, capacity, day.isoformat()),
            )
        return cur.lastrowid

    def list_one_time_slots(self, from_day: date, to_day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM slots WHERE date IS NOT NULL AND date BETWEEN ? AND ? "
            "ORDER BY date, start_time",
            (from_day.isoformat(), to_day.isoformat()),
        ).fetchall()

    def sync_one_time_slots(self, desired, from_day: date, to_day: date) -> tuple[int, int, int]:
        """Make one-time slots within [from_day, to_day] match ``desired``.

        Each item is ("YYYY-MM-DD", "HH:MM", duration_min[, capacity]); capacity
        defaults to 1. Only slots whose date falls in the given window are
        considered, so one-time lessons outside the range the caller sent
        (e.g. the mini app's visible window) are left untouched.
        """
        existing = {
            (r["date"], r["start_time"]): r for r in self.list_one_time_slots(from_day, to_day)
        }
        want: dict[tuple[str, str], tuple[int, int]] = {}
        for day_iso, start_time, duration_min, *rest in desired:
            capacity = rest[0] if rest else 1
            want[(day_iso, start_time)] = (duration_min, capacity)

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
                day_iso, start_time = key
                self.add_one_time_slot(date.fromisoformat(day_iso), start_time, duration_min, capacity)
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
            "SELECT b.*, s.start_time, s.duration_min, s.capacity FROM bookings b "
            "JOIN slots s ON s.id = b.slot_id "
            "WHERE b.date >= ? "
            "ORDER BY b.date, s.start_time",
            (from_day.isoformat(),),
        ).fetchall()

    def bookings_for_slot(self, slot_id: int, day: date) -> list[sqlite3.Row]:
        """Everyone enrolled in one specific session, for the trainer's roster view."""
        return self.conn.execute(
            "SELECT * FROM bookings WHERE slot_id = ? AND date = ? ORDER BY created_at",
            (slot_id, day.isoformat()),
        ).fetchall()

    def booking_counts_from(self, from_day: date) -> dict[tuple[int, str], int]:
        """Number of enrolled users per (slot_id, iso_date), for availability filtering."""
        rows = self.conn.execute(
            "SELECT slot_id, date, COUNT(*) AS c FROM bookings "
            "WHERE date >= ? GROUP BY slot_id, date",
            (from_day.isoformat(),),
        ).fetchall()
        return {(row["slot_id"], row["date"]): row["c"] for row in rows}

    def bookings_for_user_and_slot(self, user_id: int, slot_id: int) -> list[sqlite3.Row]:
        """All of one user's bookings (any date) for one recurring slot, used to
        enforce that they hold only one active booking of it at a time."""
        return self.conn.execute(
            "SELECT b.*, s.start_time, s.duration_min FROM bookings b "
            "JOIN slots s ON s.id = b.slot_id "
            "WHERE b.user_id = ? AND b.slot_id = ?",
            (user_id, slot_id),
        ).fetchall()

    # --- waiting list (for full sessions) ---

    def join_waitlist(self, slot_id: int, day: date, user_id: int, user_name: str) -> int:
        try:
            with self.conn:
                cur = self.conn.execute(
                    "INSERT INTO waitlist (slot_id, date, user_id, user_name) "
                    "VALUES (?, ?, ?, ?)",
                    (slot_id, day.isoformat(), user_id, user_name),
                )
        except sqlite3.IntegrityError as exc:
            raise AlreadyWaitlistedError(
                f"User {user_id} already waitlisted for slot {slot_id} on {day}"
            ) from exc
        return cur.lastrowid

    def get_waitlist_entry(self, waitlist_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM waitlist WHERE id = ?", (waitlist_id,)
        ).fetchone()

    def leave_waitlist(self, waitlist_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM waitlist WHERE id = ?", (waitlist_id,))
        return cur.rowcount > 0

    def waitlist_for_slot(self, slot_id: int, day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM waitlist WHERE slot_id = ? AND date = ? ORDER BY created_at",
            (slot_id, day.isoformat()),
        ).fetchall()

    def waitlist_for_user(self, user_id: int, from_day: date) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT w.*, s.start_time, s.duration_min FROM waitlist w "
            "JOIN slots s ON s.id = w.slot_id "
            "WHERE w.user_id = ? AND w.date >= ? "
            "ORDER BY w.date, s.start_time",
            (user_id, from_day.isoformat()),
        ).fetchall()

    def promote_next_waitlisted(self, slot_id: int, day: date) -> tuple[sqlite3.Row, int] | None:
        """Pop the earliest waitlisted user for this session and book them.

        Returns (waitlist_row, new_booking_id), or None if nobody was waiting.
        If booking the next-in-line somehow fails (edge case), tries the one
        after them instead.
        """
        while True:
            entry = self.conn.execute(
                "SELECT * FROM waitlist WHERE slot_id = ? AND date = ? "
                "ORDER BY created_at LIMIT 1",
                (slot_id, day.isoformat()),
            ).fetchone()
            if entry is None:
                return None
            with self.conn:
                self.conn.execute("DELETE FROM waitlist WHERE id = ?", (entry["id"],))
            try:
                booking_id = self.book(slot_id, day, entry["user_id"], entry["user_name"])
            except (SlotFullError, SlotTakenError):
                continue
            return entry, booking_id

    def prune_stale_waitlist(self, today: date) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM waitlist WHERE date < ?", (today.isoformat(),))

    # --- per-booking reminders ---

    def reminders_for_booking(self, booking_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM reminders WHERE booking_id = ?", (booking_id,)
        ).fetchall()

    def toggle_reminder(self, booking_id: int, offset_minutes: int) -> bool:
        """Flip one reminder on/off for a booking. Returns the new state (True = on)."""
        existing = self.conn.execute(
            "SELECT id FROM reminders WHERE booking_id = ? AND offset_minutes = ?",
            (booking_id, offset_minutes),
        ).fetchone()
        with self.conn:
            if existing:
                self.conn.execute("DELETE FROM reminders WHERE id = ?", (existing["id"],))
            else:
                self.conn.execute(
                    "INSERT INTO reminders (booking_id, offset_minutes) VALUES (?, ?)",
                    (booking_id, offset_minutes),
                )
        return existing is None

    def due_reminders(self, now: datetime) -> list[sqlite3.Row]:
        """Not-yet-sent reminders whose trigger time has been reached."""
        candidates = self.conn.execute(
            "SELECT r.id AS reminder_id, r.offset_minutes, b.user_id, b.date, "
            "s.start_time, s.duration_min "
            "FROM reminders r "
            "JOIN bookings b ON b.id = r.booking_id "
            "JOIN slots s ON s.id = b.slot_id "
            "WHERE r.sent = 0 AND b.date >= ?",
            (now.date().isoformat(),),
        ).fetchall()
        return [row for row in candidates if is_reminder_due(row, now)]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,))
