# Training Booking Bot

![CI](https://github.com/andreyshindler/training-booking-bot/actions/workflows/ci.yml/badge.svg)

A Telegram bot that lets a trainer's clients book training sessions from the
trainer's predefined weekly schedule.

- The **trainer** (admin) defines a weekly recurring schedule of slots (e.g.
  "Mon 10:00, 60 min", optionally with room for several participants) and can
  also add one-time (non-recurring) lessons for a specific date via the mini
  app's calendar.
- **Trainees** run `/book`, see the open slots for the next N days as buttons,
  and tap one to book it. A slot can hold as many participants as its
  capacity allows; once full, trainees can join a waiting list and are
  automatically booked (with a notification) the moment someone cancels.
  A trainee can hold only one active booking of a given recurring slot at a
  time — they can book the next week's occurrence only after the current
  one has ended.
- Trainees can set optional reminders per booking (1 day / 2 hours / 1 hour
  before, any combination), managed right after booking or later from
  `/mybookings`.
- The trainer gets a Telegram notification for every booking and cancellation.
- The bot speaks **Hebrew**: all messages, dates, and button labels are in
  Hebrew, and the ☰ commands menu is registered automatically with Hebrew
  descriptions (command names themselves stay Latin — a Telegram requirement).
  The trainer sees the full command menu; everyone else sees only the trainee
  commands. `/addslot` accepts Hebrew day names too, e.g. `/addslot שני 10:00 60`.

## Commands

Trainee:

| Command | Description |
|---|---|
| `/book` | Show open slots for the coming week and book one (or join the waiting list if full) |
| `/mybookings` | List your upcoming sessions and waitlist entries — cancel, leave, or manage reminders |

Trainer only:

| Command | Description |
|---|---|
| `/addslot <day> <HH:MM> [minutes] [participants]` | Add a weekly slot, e.g. `/addslot Mon 10:00 60 5` |
| `/delslot <id>` | Remove a slot, recurring or one-time (ids shown by `/schedule`) |
| `/schedule` | Show the weekly schedule and upcoming one-time lessons |
| `/bookings` | Tappable list of upcoming sessions — view each session's roster and waiting list, cancel a participant |
| `/webapplink` | The mini app link as plain text, so it can be opened in any browser (not just Telegram's in-app view) |

## Buttons instead of commands

`/start` pins an always-visible button keyboard in the chat, so nobody has to
type commands: trainees get `📅 הזמנת אימון` and `🗓 האימונים שלי`; the trainer
additionally gets `📋 המערכת השבועית`, `👥 כל האימונים`, and — when the mini app
is configured — `⚙️ עריכת המערכת`.

## Schedule-editing mini app (trainer)

`docs/index.html` is a Telegram Mini App: a Hebrew, RTL, touch-friendly weekly
calendar (page through weeks with ‹ ›, up to a year ahead) for managing
lessons — day/time/duration/participant-count pickers, and a toggle for
recurring (repeats every week) vs one-time (that date only). Telegram passes
the saved result straight back to the bot, so no database/API access from the
page itself is needed either way. Two ways to host it:

### Option A — GitHub Pages (quick, free, but public)

1. GitHub → repo **Settings → Pages → Source: Deploy from a branch →
   `main` / `docs`** → Save. After a minute the page is live at
   `https://<username>.github.io/training-booking-bot/`.
2. Put that URL in `.env`: `WEBAPP_URL=https://<username>.github.io/training-booking-bot/`
3. Restart the bot.

Note: GitHub Pages sites built from a public repo are always publicly
reachable — anyone with the link can view it (no login). Not a write/security
hole (see Option B's note on `on_web_app_data`), just not private.

### Option B — self-hosted on your own server, admin-only

Serves the exact same page from the bot's own process instead, gated by a
secret token so only requests carrying it get through — no GitHub Pages
involved, and nothing publicly reachable without the token.

1. Generate a secret and put it, plus the URL you'll route to it, in `.env`:
   ```
   WEBAPP_URL=https://yourdomain.com/training-booking/
   WEBAPP_SECRET=<output of: openssl rand -hex 32>
   # WEBAPP_PORT=8082   # only if 8082 is already taken on your server
   ```
2. Rebuild/restart the bot (`docker compose up -d --build`, or restart your
   process manager if running without Docker). It now also listens on
   `127.0.0.1:8082` (Docker: published from the container to the host's
   loopback only in `docker-compose.yml` — not reachable from the internet
   directly).
3. Point your existing nginx at it. Add this `location` inside the `server {
   }` block for the domain in step 1 (adjust the path to match), then
   `sudo nginx -t && sudo systemctl reload nginx`:
   ```nginx
   location /training-booking/ {
       proxy_pass http://127.0.0.1:8082/;
       proxy_set_header Host $host;
   }
   ```
   Since this reuses a path under a domain you already serve over HTTPS, no
   new DNS record or certificate is needed.

Either way, once `WEBAPP_URL` is set the trainer's keyboard shows
`⚙️ עריכת המערכת`, which opens the mini app pre-filled with the current
schedule. Saving replaces the schedule: new lessons are added, missing ones
removed (their future bookings are cancelled), duration/capacity changes keep
existing bookings. Note that *viewing* the page is what the GitHub-Pages-vs-
self-hosted choice above controls — *saving* changes is always restricted to
the real trainer regardless of hosting, since `on_web_app_data` in
`bot/handlers.py` only applies data sent by the Telegram user ID matching
`TRAINER_ID`.

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
| `WEBAPP_URL` | — | Mini app URL (see [Schedule-editing mini app](#schedule-editing-mini-app-trainer)) |
| `WEBAPP_SECRET` | — | Self-hosted mini app only: shared secret required as `?token=` |
| `WEBAPP_PORT` | `8082` | Self-hosted mini app only: port the bot listens on |

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

## Automatic deployment (pull-based, cron)

For a Linux server/VPS. One-time setup after cloning and configuring `.env`:

```bash
bash scripts/install-auto-deploy-cron.sh
```

From then on it's fully automatic:

- Merge to `main` → the server pulls and redeploys within ~1 minute. Pull-based,
  so no inbound SSH or webhooks are needed.
- You get a ✅/❌ Telegram message after each deploy (sent to `TRAINER_ID`
  using the bot's own token from `.env`).
- Watch deploys: `tail -f auto-deploy.log` in the repository directory
  (override the location with the `AUTO_DEPLOY_LOG` environment variable).

`scripts/auto-deploy.sh` is a no-op when `main` hasn't changed, uses a lock so
runs never overlap, and hard-resets to `origin/main` (don't keep local edits
on the server).

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The core logic (slot expansion in `bot/scheduling.py`, storage in `bot/db.py`)
is independent of Telegram and fully unit-tested; `bot/handlers.py` wires it to
python-telegram-bot v21.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs the test suite on Python 3.11
and 3.12, an import smoke test, and a Docker image build with a container
smoke test. It triggers on every push to `main` and every pull request, plus a
daily cron run at 06:00 UTC so dependency breakage is caught even without new
commits. It can also be started manually from the Actions tab
(`workflow_dispatch`).
