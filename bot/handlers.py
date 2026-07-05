"""Telegram command and callback handlers (all user-facing text in Hebrew)."""

import json
import logging
from datetime import date, datetime
from urllib.parse import quote

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config
from .db import Database, SlotTakenError
from .scheduling import (
    WEEKDAY_NAMES_HE,
    available_slots,
    hebrew_day_label,
    parse_time,
    parse_weekday,
)

logger = logging.getLogger(__name__)

# keys used in application.bot_data
DB = "db"
CFG = "cfg"

# Always-visible reply-keyboard buttons
BTN_BOOK = "📅 הזמנת אימון"
BTN_MY = "🗓 האימונים שלי"
BTN_SCHEDULE = "📋 המערכת השבועית"
BTN_ALL = "👥 כל האימונים"
BTN_EDIT = "⚙️ עריכת המערכת"

# Telegram requires command names in Latin letters; the labels are Hebrew.
TRAINEE_COMMANDS = [
    BotCommand("book", "הזמנת אימון"),
    BotCommand("mybookings", "האימונים שלי / ביטול"),
    BotCommand("start", "התחלה והסבר"),
]
TRAINER_COMMANDS = TRAINEE_COMMANDS + [
    BotCommand("schedule", "המערכת השבועית"),
    BotCommand("addslot", "הוספת מועד שבועי"),
    BotCommand("delslot", "מחיקת מועד שבועי"),
    BotCommand("bookings", "כל האימונים הקרובים"),
]


async def setup_commands_menu(app: Application) -> None:
    """Register the ☰ menu commands: trainee set for everyone, full set for the trainer."""
    cfg: Config = app.bot_data[CFG]
    await app.bot.set_my_commands(TRAINEE_COMMANDS, scope=BotCommandScopeDefault())
    # Telegram rejects chat-scoped commands ("Chat not found") until the trainer
    # has opened a chat with the bot, so this must not be fatal; it is retried
    # when the trainer sends /start.
    await _set_trainer_menu(app.bot, cfg.trainer_id)


async def _set_trainer_menu(bot, trainer_id: int) -> bool:
    try:
        await bot.set_my_commands(
            TRAINER_COMMANDS, scope=BotCommandScopeChat(chat_id=trainer_id)
        )
        return True
    except TelegramError as exc:
        logger.warning(
            "Could not register the trainer commands menu (%s). "
            "The trainer should open the bot and press Start; "
            "the menu will be registered then.",
            exc,
        )
        return False


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data[DB]


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data[CFG]


def _is_trainer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_user.id == _cfg(context).trainer_id


def _now(context: ContextTypes.DEFAULT_TYPE) -> datetime:
    return datetime.now(_cfg(context).timezone)


def _display_name(user) -> str:
    name = user.full_name or str(user.id)
    return f"{name} (@{user.username})" if user.username else name


async def _notify_trainer(context, text: str) -> None:
    """Best-effort notification; must never break the trainee's flow."""
    try:
        await context.bot.send_message(_cfg(context).trainer_id, text)
    except TelegramError as exc:
        logger.warning("Could not notify the trainer: %s", exc)


def _webapp_edit_url(cfg: Config, db: Database) -> str:
    """Mini-app URL carrying the current schedule, so the app opens pre-filled."""
    slots = [
        {
            "weekday": row["weekday"],
            "start_time": row["start_time"],
            "duration_min": row["duration_min"],
        }
        for row in db.list_slots()
    ]
    separator = "&" if "?" in cfg.webapp_url else "?"
    return f"{cfg.webapp_url}{separator}slots={quote(json.dumps(slots))}"


def _main_keyboard(context: ContextTypes.DEFAULT_TYPE, is_trainer: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(BTN_BOOK), KeyboardButton(BTN_MY)]]
    if is_trainer:
        rows.append([KeyboardButton(BTN_SCHEDULE), KeyboardButton(BTN_ALL)])
        cfg = _cfg(context)
        if cfg.webapp_url:
            rows.append(
                [
                    KeyboardButton(
                        BTN_EDIT,
                        web_app=WebAppInfo(url=_webapp_edit_url(cfg, _db(context))),
                    )
                ]
            )
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


# --- shared commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_trainer = _is_trainer(update, context)
    if is_trainer:
        # The chat now definitely exists, so the trainer menu can be registered.
        await _set_trainer_menu(context.bot, _cfg(context).trainer_id)
        text = (
            "שלום, המאמן! אפשר להשתמש בכפתורים למטה:\n"
            f"{BTN_EDIT} — עריכת המערכת השבועית במסך נוח\n"
            f"{BTN_SCHEDULE} — הצגת המערכת השבועית\n"
            f"{BTN_ALL} — האימונים הקרובים שהוזמנו\n\n"
            "אפשר גם בפקודות: /addslot שני 10:00 60 או /delslot <מספר>"
        )
    else:
        text = (
            "ברוכים הבאים! כאן מזמינים אימונים אצל המאמן.\n"
            f"{BTN_BOOK} — הצגת מועדים פנויים והזמנת אימון\n"
            f"{BTN_MY} — האימונים הקרובים שלכם (עם אפשרות ביטול)"
        )
    await update.message.reply_text(
        text, reply_markup=_main_keyboard(context, is_trainer)
    )


# --- trainee commands ---

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, cfg = _db(context), _cfg(context)
    now = _now(context)
    open_slots = available_slots(
        db.list_slots(), db.booked_pairs_from(now.date()), now, cfg.booking_days_ahead
    )
    if not open_slots:
        await update.message.reply_text(
            f"אין מועדים פנויים ב־{cfg.booking_days_ahead} הימים הקרובים. "
            "נסו שוב מאוחר יותר!"
        )
        return
    keyboard = [
        [
            InlineKeyboardButton(
                slot.label(),
                callback_data=f"book|{slot.slot_id}|{slot.day.isoformat()}",
            )
        ]
        for slot in open_slots
    ]
    await update.message.reply_text(
        "מועדים פנויים — לחצו על מועד כדי להזמין:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    rows = db.bookings_for_user(update.effective_user.id, _now(context).date())
    if not rows:
        await update.message.reply_text(
            f"אין לכם אימונים קרובים. להזמנה: {BTN_BOOK}"
        )
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"❌ ביטול {_fmt_booking(row)}",
                callback_data=f"cancel|{row['id']}",
            )
        ]
        for row in rows
    ]
    await update.message.reply_text(
        "האימונים הקרובים שלכם — לחצו כדי לבטל:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def _fmt_booking(row) -> str:
    day = date.fromisoformat(row["date"])
    return hebrew_day_label(day, row["start_time"], row["duration_min"])


# --- reply-keyboard button presses (arrive as plain text) ---

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if text == BTN_BOOK:
        await book(update, context)
    elif text == BTN_MY:
        await my_bookings(update, context)
    elif text == BTN_SCHEDULE:
        await schedule(update, context)
    elif text == BTN_ALL:
        await bookings(update, context)


# --- mini-app result (trainer saved the schedule in the web app) ---

async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    db = _db(context)
    try:
        data = json.loads(update.effective_message.web_app_data.data)
        desired = []
        for slot in data["slots"]:
            weekday = int(slot["weekday"])
            if not 0 <= weekday <= 6:
                raise ValueError(f"weekday out of range: {weekday}")
            start_time = parse_time(str(slot["start_time"]))
            duration = int(slot.get("duration_min", 60))
            if not 0 < duration <= 480:
                raise ValueError(f"duration out of range: {duration}")
            desired.append((weekday, start_time, duration))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Bad web app data: %s", exc)
        await update.effective_message.reply_text(
            "התקבלו נתונים לא תקינים מהמיני־אפ. נסו שוב."
        )
        return

    added, removed, updated = db.sync_slots(desired)
    summary = f"✅ המערכת עודכנה: נוספו {added}, נמחקו {removed}, שונו {updated}."
    if removed:
        summary += "\n(הזמנות עתידיות למועדים שנמחקו בוטלו.)"
    # Refresh the keyboard so the edit button carries the new schedule.
    await update.effective_message.reply_text(
        summary, reply_markup=_main_keyboard(context, is_trainer=True)
    )


# --- inline button callbacks ---

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, _, payload = query.data.partition("|")
    if action == "book":
        await _handle_book(query, context, payload)
    elif action == "cancel":
        await _handle_cancel(query, context, payload)


async def _handle_book(query, context, payload: str) -> None:
    db = _db(context)
    slot_id_str, _, day_str = payload.partition("|")
    slot_id, day = int(slot_id_str), date.fromisoformat(day_str)
    user = query.from_user

    slot = db.get_slot(slot_id)
    if slot is None:
        await query.edit_message_text(
            "המועד הזה כבר לא קיים. נסו שוב עם /book."
        )
        return
    try:
        db.book(slot_id, day, user.id, _display_name(user))
    except SlotTakenError:
        await query.edit_message_text(
            "מצטערים, המועד הרגע נתפס. בחרו מועד אחר עם /book."
        )
        return

    label = hebrew_day_label(day, slot["start_time"], slot["duration_min"])
    await query.edit_message_text(f"✅ האימון נקבע: {label}\nנתראה באימון!")
    await _notify_trainer(context, f"📅 הזמנה חדשה: {label} — {_display_name(user)}")


async def _handle_cancel(query, context, payload: str) -> None:
    db, cfg = _db(context), _cfg(context)
    booking_id = int(payload)
    row = db.get_booking(booking_id)
    is_trainer = query.from_user.id == cfg.trainer_id
    if row is None or (row["user_id"] != query.from_user.id and not is_trainer):
        await query.edit_message_text(
            "ההזמנה לא נמצאה (ייתכן שכבר בוטלה)."
        )
        return
    db.cancel_booking(booking_id)
    label = _fmt_booking(row)
    await query.edit_message_text(f"❌ האימון בוטל: {label}")
    if not is_trainer:
        await _notify_trainer(context, f"❌ בוטל אימון: {label} — {row['user_name']}")


# --- trainer commands ---

async def add_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    usage = "שימוש: /addslot <יום> <HH:MM> [דקות] — למשל: /addslot שני 10:00 60"
    try:
        if len(context.args) < 2:
            raise ValueError(usage)
        weekday = parse_weekday(context.args[0])
        start_time = parse_time(context.args[1])
        if len(context.args) > 2:
            if not context.args[2].isdigit():
                raise ValueError(usage)
            duration = int(context.args[2])
        else:
            duration = 60
        if not 0 < duration <= 480:
            raise ValueError("משך האימון חייב להיות בין 1 ל־480 דקות.")
        slot_id = _db(context).add_slot(weekday, start_time, duration)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        await update.message.reply_text(
            "לא ניתן להוסיף — כבר קיים מועד ביום ובשעה האלה."
        )
        return
    await update.message.reply_text(
        f"נוסף מועד #{slot_id}: יום {WEEKDAY_NAMES_HE[weekday]} "
        f"{start_time} ({duration} דק')",
        reply_markup=_main_keyboard(context, is_trainer=True),
    )


async def del_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text(
            "שימוש: /delslot <מספר> (המספרים מופיעים ב־/schedule)"
        )
        return
    removed = _db(context).remove_slot(int(context.args[0]))
    await update.message.reply_text(
        "המועד נמחק (הזמנות עתידיות למועד זה בוטלו)."
        if removed
        else "אין מועד עם המספר הזה. ראו /schedule.",
        reply_markup=_main_keyboard(context, is_trainer=True),
    )


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    rows = _db(context).list_slots()
    if not rows:
        await update.message.reply_text(
            f"אין עדיין מועדים שבועיים. הוסיפו דרך {BTN_EDIT} "
            "או עם: /addslot שני 10:00 60"
        )
        return
    lines = [
        f"#{row['id']} יום {WEEKDAY_NAMES_HE[row['weekday']]} {row['start_time']} "
        f"({row['duration_min']} דק')"
        for row in rows
    ]
    await update.message.reply_text("המערכת השבועית:\n" + "\n".join(lines))


async def bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    rows = _db(context).bookings_from(_now(context).date())
    if not rows:
        await update.message.reply_text("אין אימונים קרובים.")
        return
    lines = [f"{_fmt_booking(row)} — {row['user_name']}" for row in rows]
    await update.message.reply_text("האימונים הקרובים:\n" + "\n".join(lines))


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("book", book))
    app.add_handler(CommandHandler("mybookings", my_bookings))
    app.add_handler(CommandHandler("addslot", add_slot))
    app.add_handler(CommandHandler("delslot", del_slot))
    app.add_handler(CommandHandler("schedule", schedule))
    app.add_handler(CommandHandler("bookings", bookings))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
