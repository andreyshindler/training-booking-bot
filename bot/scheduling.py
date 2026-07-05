"""Pure scheduling logic: parsing schedule definitions and computing open slots."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WEEKDAY_LOOKUP = {name.lower(): i for i, name in enumerate(WEEKDAY_NAMES)}
_WEEKDAY_LOOKUP.update(
    {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
)

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_weekday(text: str) -> int:
    """Turn 'mon' / 'Monday' into 0..6. Raises ValueError on unknown input."""
    day = _WEEKDAY_LOOKUP.get(text.strip().lower())
    if day is None:
        raise ValueError(f"Unknown weekday: {text!r}. Use Mon..Sun.")
    return day


def parse_time(text: str) -> str:
    """Validate 'HH:MM' and return it zero-padded, e.g. '9:00' -> '09:00'."""
    match = _TIME_RE.match(text.strip())
    if not match:
        raise ValueError(f"Invalid time: {text!r}. Use HH:MM, e.g. 09:30.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


@dataclass(frozen=True)
class OpenSlot:
    slot_id: int
    day: date
    start_time: str  # "HH:MM"
    duration_min: int

    @property
    def start_dt(self) -> datetime:
        hour, minute = map(int, self.start_time.split(":"))
        return datetime.combine(self.day, time(hour, minute))

    def label(self) -> str:
        return (
            f"{WEEKDAY_NAMES[self.day.weekday()]} {self.day.strftime('%d %b')} "
            f"{self.start_time} ({self.duration_min} min)"
        )


def available_slots(
    slots,
    booked_pairs: set[tuple[int, str]],
    now: datetime,
    days_ahead: int,
) -> list[OpenSlot]:
    """Expand the weekly schedule into concrete open slots for the next N days.

    ``slots`` is an iterable of mappings with keys id/weekday/start_time/duration_min
    (sqlite3.Row works). Slots already booked or already started are excluded.
    """
    by_weekday: dict[int, list] = {}
    for slot in slots:
        by_weekday.setdefault(slot["weekday"], []).append(slot)

    result: list[OpenSlot] = []
    today = now.date()
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        for slot in by_weekday.get(day.weekday(), []):
            if (slot["id"], day.isoformat()) in booked_pairs:
                continue
            open_slot = OpenSlot(
                slot_id=slot["id"],
                day=day,
                start_time=slot["start_time"],
                duration_min=slot["duration_min"],
            )
            if open_slot.start_dt <= now.replace(tzinfo=None):
                continue
            result.append(open_slot)
    result.sort(key=lambda s: (s.day, s.start_time))
    return result
