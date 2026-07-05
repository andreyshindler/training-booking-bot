# Training Booking Bot

A Telegram bot that lets a trainer's clients book training sessions from the
trainer's predefined weekly schedule.

- The **trainer** (admin) defines a weekly schedule of slots, e.g. "Mon 10:00,
  60 min".
- **Trainees** run `/book`, see the open slots for the next N days as buttons,
  and tap one to book it. Each slot can be booked by exactly one person per
  date; double bookings are rejected atomically at the database level.
- The trainer gets a Telegram notification for every booking and cancellation.

## Commands

Trainee:

| Command | Description |
|---|---|
| `/book` | Show open slots for the coming week and book one |
| `/mybookings` | List your upcoming sessions, with cancel buttons |

Trainer only:

| Command | Description |
|---|---|
| `/addslot <day> <HH:MM> [minutes]` | Add a weekly slot, e.g. `/addslot Mon 10:00 60` |
| `/delslot <id>` | Remove a weekly slot (ids shown by `/schedule`) |
| `/schedule` | Show the weekly schedule |
| `/bookings` | List all upcoming booked sessions |

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Find the trainer's numeric Telegram id (e.g. via [@userinfobot](https://t.me/userinfobot)).
3. Configure and run:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in BOT_TOKEN and TRAINER_ID
python -m bot.main
```

Configuration (environment variables or `.env`):

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | — | Telegram bot token (required) |
| `TRAINER_ID` | — | Telegram user id of the trainer (required) |
| `DB_PATH` | `bookings.db` | SQLite database file |
| `TIMEZONE` | `UTC` | IANA timezone for the schedule, e.g. `America/Chicago` |
| `BOOKING_DAYS_AHEAD` | `7` | How many days ahead trainees can book |

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The core logic (slot expansion in `bot/scheduling.py`, storage in `bot/db.py`)
is independent of Telegram and fully unit-tested; `bot/handlers.py` wires it to
python-telegram-bot v21.
