"""Telegram command and callback handlers (all user-facing text in Hebrew)."""

from datetime import date, datetime

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
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

# keys used in application.bot_data
DB = "db"
CFG = "cfg"

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
    await app.bot.set_my_commands(
        TRAINER_COMMANDS, scope=BotCommandScopeChat(chat_id=cfg.trainer_id)
    )


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


# --- shared commands ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_trainer(update, context):
        text = (
            "שלום, המאמן! פקודות ניהול:\n"
            "/addslot <יום> <HH:MM> [דקות] — הוספת מועד שבועי "
            "(למשל: /addslot שני 10:00 60)\n"
            "/delslot <מספר> — מחיקת מועד שבועי\n"
            "/schedule — הצגת המערכת השבועית\n"
            "/bookings — האימונים הקרובים שהוזמנו"
        )
    else:
        text = (
            "ברוכים הבאים! כאן מזמינים אימונים אצל המאמן.\n"
            "/book — הצגת מועדים פנויים והזמנת אימון\n"
            "/mybookings — האימונים הקרובים שלכם (עם אפשרות ביטול)"
        )
    await update.message.reply_text(text)


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
            "אין לכם אימונים קרובים. להזמנה: /book"
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
    db, cfg = _db(context), _cfg(context)
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
    await context.bot.send_message(
        cfg.trainer_id, f"📅 הזמנה חדשה: {label} — {_display_name(user)}"
    )


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
        await context.bot.send_message(
            cfg.trainer_id, f"❌ בוטל אימון: {label} — {row['user_name']}"
        )


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
        f"{start_time} ({duration} דק')"
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
        else "אין מועד עם המספר הזה. ראו /schedule."
    )


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    rows = _db(context).list_slots()
    if not rows:
        await update.message.reply_text(
            "אין עדיין מועדים שבועיים. הוסיפו עם: /addslot שני 10:00 60"
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
