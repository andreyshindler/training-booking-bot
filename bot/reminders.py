"""Background job: periodically send due session reminders and tidy up stale waitlist entries."""

import logging
from datetime import datetime

from telegram.error import TelegramError
from telegram.ext import ContextTypes

from .config import Config
from .db import Database
from .handlers import CFG, DB
from .scheduling import hebrew_day_label

logger = logging.getLogger(__name__)

_OFFSET_NOUNS = {1440: "יום", 120: "שעתיים", 60: "שעה"}


async def send_due_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data[DB]
    cfg: Config = context.bot_data[CFG]
    now = datetime.now(cfg.timezone).replace(tzinfo=None)

    for reminder in db.due_reminders(now):
        label = hebrew_day_label(
            datetime.strptime(reminder["date"], "%Y-%m-%d").date(),
            reminder["start_time"],
            reminder["duration_min"],
        )
        noun = _OFFSET_NOUNS.get(reminder["offset_minutes"], f"{reminder['offset_minutes']} דקות")
        try:
            await context.bot.send_message(
                reminder["user_id"], f"🔔 תזכורת: אימון בעוד {noun} — {label}"
            )
        except TelegramError as exc:
            logger.warning("Could not send reminder to %s: %s", reminder["user_id"], exc)
        db.mark_reminder_sent(reminder["reminder_id"])

    db.prune_stale_waitlist(now.date())
