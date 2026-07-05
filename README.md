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

## Running with Docker (recommended for always-on hosting)

Requires Docker with the compose plugin. From the repository root:

```bash
cp .env.example .env   # then fill in BOT_TOKEN and TRAINER_ID
docker compose up -d --build
```

That's it — the bot runs in the background and restarts automatically after
crashes or server reboots (`restart: unless-stopped`). The SQLite database
lives on a named volume (`bot-data`), so bookings survive rebuilds and
upgrades.

Useful commands:

```bash
docker compose logs -f          # watch the bot's logs
docker compose restart          # restart the bot
docker compose down             # stop it (bookings are kept)
git pull && docker compose up -d --build   # upgrade to the latest code
```

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The core logic (slot expansion in `bot/scheduling.py`, storage in `bot/db.py`)
is independent of Telegram and fully unit-tested; `bot/handlers.py` wires it to
python-telegram-bot v21.
