"""Pure scheduling logic: parsing schedule definitions and computing open slots."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

# Index = Python weekday (Monday = 0)
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WEEKDAY_NAMES_HE = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

_WEEKDAY_LOOKUP = {name.lower(): i for i, name in enumerate(WEEKDAY_NAMES)}
_WEEKDAY_LOOKUP.update(
    {
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
    }
)
_WEEKDAY_LOOKUP.update({name: i for i, name in enumerate(WEEKDAY_NAMES_HE)})
# Alternative Hebrew spellings
_WEEKDAY_LOOKUP.update({"ראשון": 6, "שני": 0, "שלישי": 1, "רביעי": 2, "חמישי": 3, "שישי": 4, "שבת": 5})

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def parse_weekday(text: str) -> int:
    """Turn 'mon' / 'Monday' / 'שני' into 0..6. Raises ValueError on unknown input."""
    cleaned = text.strip().lower()
    if cleaned.startswith("יום "):  # allow "יום שני"
        cleaned = cleaned[4:]
    day = _WEEKDAY_LOOKUP.get(cleaned)
    if day is None:
        raise ValueError(
            f"יום לא מוכר: {text}. אפשר לכתוב ראשון/שני/שלישי... או Mon..Sun."
        )
    return day


def parse_time(text: str) -> str:
    """Validate 'HH:MM' and return it zero-padded, e.g. '9:00' -> '09:00'."""
    match = _TIME_RE.match(text.strip())
    if not match:
        raise ValueError(f"שעה לא תקינה: {text}. יש לכתוב HH:MM, למשל 09:30.")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def hebrew_day_label(
    day: date, start_time: str, duration_min: int, capacity: int = 1, booked_count: int = 0
) -> str:
    label = (
        f"יום {WEEKDAY_NAMES_HE[day.weekday()]} {day.strftime('%d/%m')} "
        f"{start_time} ({duration_min} דק')"
    )
    if capacity > 1:
        label += f" — {booked_count}/{capacity} נרשמו"
    return label


@dataclass(frozen=True)
class OpenSlot:
    slot_id: int
    day: date
    start_time: str  # "HH:MM"
    duration_min: int
    capacity: int = 1
    booked_count: int = 0

    @property
    def start_dt(self) -> datetime:
        hour, minute = map(int, self.start_time.split(":"))
        return datetime.combine(self.day, time(hour, minute))

    def label(self) -> str:
        return hebrew_day_label(
            self.day, self.start_time, self.duration_min, self.capacity, self.booked_count
        )


def available_slots(
    slots,
    booking_counts: dict[tuple[int, str], int],
    now: datetime,
    days_ahead: int,
) -> list[OpenSlot]:
    """Expand the weekly schedule into concrete open slots for the next N days.

    ``slots`` is an iterable of mappings with keys id/weekday/start_time/
    duration_min/capacity (sqlite3.Row works). ``booking_counts`` maps
    (slot_id, iso_date) to how many users are already enrolled. Slots already
    started or already at capacity are excluded.
    """
    by_weekday: dict[int, list] = {}
    for slot in slots:
        by_weekday.setdefault(slot["weekday"], []).append(slot)

    result: list[OpenSlot] = []
    today = now.date()
    for offset in range(days_ahead + 1):
        day = today + timedelta(days=offset)
        for slot in by_weekday.get(day.weekday(), []):
            capacity = slot["capacity"]
            booked_count = booking_counts.get((slot["id"], day.isoformat()), 0)
            if booked_count >= capacity:
                continue
            open_slot = OpenSlot(
                slot_id=slot["id"],
                day=day,
                start_time=slot["start_time"],
                duration_min=slot["duration_min"],
                capacity=capacity,
                booked_count=booked_count,
            )
            if open_slot.start_dt <= now.replace(tzinfo=None):
                continue
            result.append(open_slot)
    result.sort(key=lambda s: (s.day, s.start_time))
    return result
