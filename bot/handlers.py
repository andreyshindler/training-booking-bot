"""Telegram command and callback handlers."""

from datetime import date, datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from .config import Config
from .db import Database, SlotTakenError
from .scheduling import (
    WEEKDAY_NAMES,
    available_slots,
    parse_time,
    parse_weekday,
)

# keys used in application.bot_data
DB = "db"
CFG = "cfg"


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
            "Welcome, coach! Trainer commands:\n"
            "/addslot <day> <HH:MM> [minutes] — add a weekly slot "
            "(e.g. /addslot Mon 10:00 60)\n"
            "/delslot <id> — remove a weekly slot\n"
            "/schedule — show the weekly schedule\n"
            "/bookings — upcoming booked sessions"
        )
    else:
        text = (
            "Welcome! I book training sessions with the coach.\n"
            "/book — see open slots and book one\n"
            "/mybookings — your upcoming sessions (with cancel buttons)"
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
            "No open slots in the next "
            f"{cfg.booking_days_ahead} days. Check back later!"
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
        "Open slots — tap one to book:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    rows = db.bookings_for_user(update.effective_user.id, _now(context).date())
    if not rows:
        await update.message.reply_text("You have no upcoming bookings. Use /book.")
        return
    keyboard = [
        [
            InlineKeyboardButton(
                f"❌ Cancel {_fmt_booking(row)}",
                callback_data=f"cancel|{row['id']}",
            )
        ]
        for row in rows
    ]
    await update.message.reply_text(
        "Your upcoming sessions — tap to cancel:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


def _fmt_booking(row) -> str:
    day = date.fromisoformat(row["date"])
    return (
        f"{WEEKDAY_NAMES[day.weekday()]} {day.strftime('%d %b')} "
        f"{row['start_time']} ({row['duration_min']} min)"
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
    db, cfg = _db(context), _cfg(context)
    slot_id_str, _, day_str = payload.partition("|")
    slot_id, day = int(slot_id_str), date.fromisoformat(day_str)
    user = query.from_user

    slot = db.get_slot(slot_id)
    if slot is None:
        await query.edit_message_text("That slot no longer exists. Use /book again.")
        return
    try:
        db.book(slot_id, day, user.id, _display_name(user))
    except SlotTakenError:
        await query.edit_message_text(
            "Sorry, that slot was just taken. Use /book to pick another."
        )
        return

    label = (
        f"{WEEKDAY_NAMES[day.weekday()]} {day.strftime('%d %b')} "
        f"{slot['start_time']} ({slot['duration_min']} min)"
    )
    await query.edit_message_text(f"✅ Booked: {label}\nSee you there!")
    await context.bot.send_message(
        cfg.trainer_id, f"📅 New booking: {label} — {_display_name(user)}"
    )


async def _handle_cancel(query, context, payload: str) -> None:
    db, cfg = _db(context), _cfg(context)
    booking_id = int(payload)
    row = db.get_booking(booking_id)
    is_trainer = query.from_user.id == cfg.trainer_id
    if row is None or (row["user_id"] != query.from_user.id and not is_trainer):
        await query.edit_message_text("Booking not found (maybe already cancelled).")
        return
    db.cancel_booking(booking_id)
    label = _fmt_booking(row)
    await query.edit_message_text(f"❌ Cancelled: {label}")
    if not is_trainer:
        await context.bot.send_message(
            cfg.trainer_id, f"❌ Cancelled: {label} — {row['user_name']}"
        )


# --- trainer commands ---

async def add_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    try:
        if len(context.args) < 2:
            raise ValueError("Usage: /addslot <day> <HH:MM> [minutes]")
        weekday = parse_weekday(context.args[0])
        start_time = parse_time(context.args[1])
        duration = int(context.args[2]) if len(context.args) > 2 else 60
        if not 0 < duration <= 480:
            raise ValueError("Duration must be between 1 and 480 minutes.")
        slot_id = _db(context).add_slot(weekday, start_time, duration)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        await update.message.reply_text(
            "Could not add slot — a slot at that day and time already exists."
        )
        return
    await update.message.reply_text(
        f"Added slot #{slot_id}: {WEEKDAY_NAMES[weekday]} {start_time} ({duration} min)"
    )


async def del_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /delslot <id> (see /schedule for ids)")
        return
    removed = _db(context).remove_slot(int(context.args[0]))
    await update.message.reply_text(
        "Slot removed (future bookings for it were cancelled)."
        if removed
        else "No slot with that id. See /schedule."
    )


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    rows = _db(context).list_slots()
    if not rows:
        await update.message.reply_text(
            "No weekly slots yet. Add one with /addslot Mon 10:00 60"
        )
        return
    lines = [
        f"#{row['id']} {WEEKDAY_NAMES[row['weekday']]} {row['start_time']} "
        f"({row['duration_min']} min)"
        for row in rows
    ]
    await update.message.reply_text("Weekly schedule:\n" + "\n".join(lines))


async def bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    rows = _db(context).bookings_from(_now(context).date())
    if not rows:
        await update.message.reply_text("No upcoming bookings.")
        return
    lines = [f"{_fmt_booking(row)} — {row['user_name']}" for row in rows]
    await update.message.reply_text("Upcoming sessions:\n" + "\n".join(lines))


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
