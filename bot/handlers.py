"""Telegram command and callback handlers (all user-facing text in Hebrew)."""

import json
import logging
from datetime import date, datetime, timedelta
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
from .db import AlreadyWaitlistedError, Database, SlotFullError, SlotTakenError
from .scheduling import (
    REMINDER_OFFSETS,
    WEEKDAY_NAMES_HE,
    available_slots,
    has_unexpired_recurring_booking,
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


# How far the mini-app's one-time-lesson calendar reaches: a small buffer
# into the past (so "this week" still shows anything just added) and a full
# year ahead, matching the year-long week-by-week view the trainer pages through.
ONE_TIME_WINDOW_PAST_DAYS = 7
ONE_TIME_WINDOW_FUTURE_DAYS = 365


def _one_time_window(cfg: Config) -> tuple[date, date]:
    today = datetime.now(cfg.timezone).date()
    return (
        today - timedelta(days=ONE_TIME_WINDOW_PAST_DAYS),
        today + timedelta(days=ONE_TIME_WINDOW_FUTURE_DAYS),
    )


def _webapp_edit_url(cfg: Config, db: Database) -> str:
    """Mini-app URL carrying the current schedule, so the app opens pre-filled."""
    recurring = [
        {
            "weekday": row["weekday"],
            "start_time": row["start_time"],
            "duration_min": row["duration_min"],
            "capacity": row["capacity"],
        }
        for row in db.list_slots()
    ]
    from_day, to_day = _one_time_window(cfg)
    one_time = [
        {
            "date": row["date"],
            "start_time": row["start_time"],
            "duration_min": row["duration_min"],
            "capacity": row["capacity"],
        }
        for row in db.list_one_time_slots(from_day, to_day)
    ]
    payload = {"recurring": recurring, "one_time": one_time}
    separator = "&" if "?" in cfg.webapp_url else "?"
    url = f"{cfg.webapp_url}{separator}data={quote(json.dumps(payload))}"
    if cfg.webapp_secret:
        url += f"&token={quote(cfg.webapp_secret)}"
    return url


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
    one_time = db.list_one_time_slots(now.date(), now.date() + timedelta(days=cfg.booking_days_ahead))
    open_slots = available_slots(
        db.list_slots(), one_time, db.booking_counts_from(now.date()), now, cfg.booking_days_ahead
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


def _reminder_keyboard(db: Database, booking_id: int, extra_rows=None) -> InlineKeyboardMarkup:
    active = {r["offset_minutes"] for r in db.reminders_for_booking(booking_id)}
    keyboard = [
        [
            InlineKeyboardButton(
                ("☑️ " if minutes in active else "⬜ ") + f"{noun} לפני",
                callback_data=f"remindtoggle|{booking_id}|{minutes}",
            )
        ]
        for minutes, noun in REMINDER_OFFSETS
    ]
    if extra_rows:
        keyboard.extend(extra_rows)
    return InlineKeyboardMarkup(keyboard)


def _my_bookings_keyboard(rows, waiting=()) -> InlineKeyboardMarkup:
    keyboard = []
    for row in rows:
        keyboard.append(
            [InlineKeyboardButton(f"⏰ תזכורות — {_fmt_booking(row)}", callback_data=f"remindopen|{row['id']}")]
        )
        keyboard.append(
            [InlineKeyboardButton("❌ ביטול", callback_data=f"cancel|{row['id']}")]
        )
    for row in waiting:
        day = date.fromisoformat(row["date"])
        label = hebrew_day_label(day, row["start_time"], row["duration_min"])
        keyboard.append(
            [InlineKeyboardButton(f"⏳ בהמתנה: {label}", callback_data="noop")]
        )
        keyboard.append(
            [InlineKeyboardButton("❌ עזיבת רשימת המתנה", callback_data=f"waitlistleave|{row['id']}")]
        )
    return InlineKeyboardMarkup(keyboard)


async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = _db(context)
    user_id = update.effective_user.id
    today = _now(context).date()
    rows = db.bookings_for_user(user_id, today)
    waiting = db.waitlist_for_user(user_id, today)
    if not rows and not waiting:
        await update.message.reply_text(
            f"אין לכם אימונים קרובים. להזמנה: {BTN_BOOK}"
        )
        return
    await update.message.reply_text(
        "האימונים הקרובים שלכם — לחצו לביטול או לניהול תזכורות:",
        reply_markup=_my_bookings_keyboard(rows, waiting),
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

def _parse_slot_fields(slot: dict) -> tuple[str, int, int]:
    start_time = parse_time(str(slot["start_time"]))
    duration = int(slot.get("duration_min", 60))
    if not 0 < duration <= 480:
        raise ValueError(f"duration out of range: {duration}")
    capacity = int(slot.get("capacity", 1))
    if not 0 < capacity <= 100:
        raise ValueError(f"capacity out of range: {capacity}")
    return start_time, duration, capacity


async def on_web_app_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    db, cfg = _db(context), _cfg(context)
    try:
        data = json.loads(update.effective_message.web_app_data.data)
        desired_recurring = []
        for slot in data["recurring"]:
            weekday = int(slot["weekday"])
            if not 0 <= weekday <= 6:
                raise ValueError(f"weekday out of range: {weekday}")
            start_time, duration, capacity = _parse_slot_fields(slot)
            desired_recurring.append((weekday, start_time, duration, capacity))

        desired_one_time = []
        for slot in data["one_time"]:
            day_iso = date.fromisoformat(str(slot["date"])).isoformat()
            start_time, duration, capacity = _parse_slot_fields(slot)
            desired_one_time.append((day_iso, start_time, duration, capacity))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Bad web app data: %s", exc)
        await update.effective_message.reply_text(
            "התקבלו נתונים לא תקינים מהמיני־אפ. נסו שוב."
        )
        return

    added, removed, updated = db.sync_slots(desired_recurring)
    from_day, to_day = _one_time_window(cfg)
    ot_added, ot_removed, ot_updated = db.sync_one_time_slots(desired_one_time, from_day, to_day)
    total_added, total_removed, total_updated = (
        added + ot_added,
        removed + ot_removed,
        updated + ot_updated,
    )
    summary = (
        f"✅ המערכת עודכנה: נוספו {total_added}, נמחקו {total_removed}, "
        f"שונו {total_updated}."
    )
    if total_removed:
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
    elif action == "roster":
        await _handle_roster(query, context, payload)
    elif action == "rosterback":
        await _handle_roster_back(query, context)
    elif action == "waitlistjoin":
        await _handle_waitlist_join(query, context, payload)
    elif action == "waitlistskip":
        await query.edit_message_text("בסדר, אין בעיה. אפשר לנסות מועד אחר עם /book.")
    elif action == "waitlistleave":
        await _handle_waitlist_leave(query, context, payload)
    elif action == "remindopen":
        await _handle_remind_open(query, context, payload)
    elif action == "remindback":
        await _handle_remind_back(query, context)
    elif action == "remindtoggle":
        await _handle_remind_toggle(query, context, payload)


def _recurring_conflict(db: Database, cfg: Config, slot, day: date, user_id: int) -> bool:
    """True if booking ``day`` for this recurring slot would give the user a
    second active (not-yet-ended) booking of it. One-time slots never conflict."""
    if slot["date"] is not None:
        return False
    now = datetime.now(cfg.timezone).replace(tzinfo=None)
    existing = db.bookings_for_user_and_slot(user_id, slot["id"])
    return has_unexpired_recurring_booking(existing, day, now)


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

    if _recurring_conflict(db, cfg, slot, day, user.id):
        await query.edit_message_text(
            "כבר יש לכם הרשמה למועד החוזר הזה. אפשר להירשם למועד הבא "
            "רק אחרי שהאימון הנוכחי מסתיים."
        )
        return

    try:
        booking_id = db.book(slot_id, day, user.id, _display_name(user))
    except SlotFullError:
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ הצטרפות לרשימת המתנה",
                    callback_data=f"waitlistjoin|{slot_id}|{day.isoformat()}",
                )
            ],
            [InlineKeyboardButton("❌ לא, תודה", callback_data="waitlistskip")],
        ]
        await query.edit_message_text(
            "מצטערים, כל המקומות תפוסים במועד הזה. להצטרף לרשימת המתנה? "
            "אם יתפנה מקום תקבלו הודעה אוטומטית ותירשמו במקומו.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return
    except SlotTakenError:
        await query.edit_message_text("כבר נרשמתם למועד הזה.")
        return

    label = hebrew_day_label(day, slot["start_time"], slot["duration_min"])
    await query.edit_message_text(
        f"✅ האימון נקבע: {label}\nנתראה באימון!\n\n"
        "אפשר להוסיף תזכורות (ניתן לבחור כמה, ולשנות בכל עת דרך "
        f"{BTN_MY}):",
        reply_markup=_reminder_keyboard(db, booking_id),
    )
    await _notify_trainer(context, f"📅 הזמנה חדשה: {label} — {_display_name(user)}")


async def _handle_waitlist_join(query, context, payload: str) -> None:
    db, cfg = _db(context), _cfg(context)
    slot_id_str, _, day_str = payload.partition("|")
    slot_id, day = int(slot_id_str), date.fromisoformat(day_str)
    user = query.from_user

    slot = db.get_slot(slot_id)
    if slot is None:
        await query.edit_message_text("המועד הזה כבר לא קיים. נסו שוב עם /book.")
        return
    if _recurring_conflict(db, cfg, slot, day, user.id):
        await query.edit_message_text(
            "כבר יש לכם הרשמה למועד החוזר הזה. אפשר להירשם למועד הבא "
            "רק אחרי שהאימון הנוכחי מסתיים."
        )
        return

    try:
        # A spot may have opened up between the "it's full" message and this tap.
        booking_id = db.book(slot_id, day, user.id, _display_name(user))
    except SlotFullError:
        pass
    except SlotTakenError:
        await query.edit_message_text("כבר נרשמתם למועד הזה.")
        return
    else:
        label = hebrew_day_label(day, slot["start_time"], slot["duration_min"])
        await query.edit_message_text(
            f"התפנה מקום בינתיים! ✅ האימון נקבע: {label}\nנתראה באימון!\n\n"
            "אפשר להוסיף תזכורות (ניתן לבחור כמה):",
            reply_markup=_reminder_keyboard(db, booking_id),
        )
        await _notify_trainer(context, f"📅 הזמנה חדשה: {label} — {_display_name(user)}")
        return

    try:
        db.join_waitlist(slot_id, day, user.id, _display_name(user))
    except AlreadyWaitlistedError:
        await query.edit_message_text("כבר נרשמתם לרשימת ההמתנה למועד הזה.")
        return

    label = hebrew_day_label(day, slot["start_time"], slot["duration_min"])
    await query.edit_message_text(
        f"✅ נרשמתם לרשימת ההמתנה: {label}\nאם יתפנה מקום, תקבלו הודעה אוטומטית."
    )


async def _handle_waitlist_leave(query, context, payload: str) -> None:
    db = _db(context)
    waitlist_id = int(payload)
    entry = db.get_waitlist_entry(waitlist_id)
    if entry is None or entry["user_id"] != query.from_user.id:
        await query.edit_message_text("הרשומה לא נמצאה (ייתכן שכבר הוסרה).")
        return
    db.leave_waitlist(waitlist_id)
    await query.edit_message_text("✅ הוסרתם מרשימת ההמתנה.")


_BACK_TO_MY_BOOKINGS = [InlineKeyboardButton("🔙 חזרה לאימונים שלי", callback_data="remindback")]


async def _handle_remind_open(query, context, payload: str) -> None:
    db = _db(context)
    booking_id = int(payload)
    row = db.get_booking(booking_id)
    if row is None or row["user_id"] != query.from_user.id:
        await query.edit_message_text("ההזמנה לא נמצאה (ייתכן שכבר בוטלה).")
        return
    await query.edit_message_text(
        f"תזכורות עבור {_fmt_booking(row)}:\nבחרו מתי לקבל תזכורת (אפשר כמה):",
        reply_markup=_reminder_keyboard(db, booking_id, [_BACK_TO_MY_BOOKINGS]),
    )


async def _handle_remind_back(query, context) -> None:
    db = _db(context)
    user_id = query.from_user.id
    today = _now(context).date()
    rows = db.bookings_for_user(user_id, today)
    waiting = db.waitlist_for_user(user_id, today)
    if not rows and not waiting:
        await query.edit_message_text(f"אין לכם אימונים קרובים. להזמנה: {BTN_BOOK}")
        return
    await query.edit_message_text(
        "האימונים הקרובים שלכם — לחצו לביטול או לניהול תזכורות:",
        reply_markup=_my_bookings_keyboard(rows, waiting),
    )


async def _handle_remind_toggle(query, context, payload: str) -> None:
    db = _db(context)
    booking_id_str, _, minutes_str = payload.partition("|")
    booking_id, minutes = int(booking_id_str), int(minutes_str)
    row = db.get_booking(booking_id)
    if row is None or row["user_id"] != query.from_user.id:
        await query.answer("ההזמנה לא נמצאה.", show_alert=True)
        return
    db.toggle_reminder(booking_id, minutes)
    has_back = any(
        btn.callback_data == "remindback"
        for keyboard_row in query.message.reply_markup.inline_keyboard
        for btn in keyboard_row
    )
    extra = [_BACK_TO_MY_BOOKINGS] if has_back else None
    await query.edit_message_reply_markup(reply_markup=_reminder_keyboard(db, booking_id, extra))


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

    promotion = db.promote_next_waitlisted(row["slot_id"], date.fromisoformat(row["date"]))
    if promotion is not None:
        promoted, promoted_booking_id = promotion
        try:
            await context.bot.send_message(
                promoted["user_id"],
                f"🎉 התפנה מקום ונרשמתם אוטומטית לאימון: {label}\nנתראה באימון!\n\n"
                "אפשר להוסיף תזכורות (ניתן לבחור כמה):",
                reply_markup=_reminder_keyboard(db, promoted_booking_id),
            )
        except TelegramError as exc:
            logger.warning(
                "Could not notify waitlisted user %s: %s", promoted["user_id"], exc
            )


def _session_keyboard(db: Database, rows) -> InlineKeyboardMarkup:
    """One button per (slot, date) session, grouping the flat booking rows."""
    keyboard = []
    seen = set()
    for row in rows:
        key = (row["slot_id"], row["date"])
        if key in seen:
            continue
        seen.add(key)
        count = sum(1 for r in rows if (r["slot_id"], r["date"]) == key)
        day = date.fromisoformat(row["date"])
        label = hebrew_day_label(day, row["start_time"], row["duration_min"], row["capacity"], count)
        waiting_count = len(db.waitlist_for_slot(row["slot_id"], day))
        if waiting_count:
            label += f" · {waiting_count} בהמתנה"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"roster|{row['slot_id']}|{row['date']}")]
        )
    return InlineKeyboardMarkup(keyboard)


async def _handle_roster(query, context, payload: str) -> None:
    db = _db(context)
    slot_id_str, _, day_str = payload.partition("|")
    slot_id, day = int(slot_id_str), date.fromisoformat(day_str)
    slot = db.get_slot(slot_id)
    participants = db.bookings_for_slot(slot_id, day)
    if slot is None or not participants:
        await query.edit_message_text("ההרשמות למועד הזה כבר לא זמינות.")
        return
    waiting = db.waitlist_for_slot(slot_id, day)
    label = hebrew_day_label(day, slot["start_time"], slot["duration_min"], slot["capacity"], len(participants))
    if waiting:
        label += f" · {len(waiting)} בהמתנה"
    keyboard = [
        [InlineKeyboardButton(f"❌ {row['user_name']}", callback_data=f"cancel|{row['id']}")]
        for row in participants
    ]
    keyboard.append([InlineKeyboardButton("🔙 חזרה לרשימה", callback_data="rosterback")])
    text = f"משתתפים ב{label}:\n(לחצו על שם כדי לבטל את ההרשמה שלו)"
    if waiting:
        names = ", ".join(row["user_name"] for row in waiting)
        text += f"\n\nרשימת המתנה: {names}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def _handle_roster_back(query, context) -> None:
    db = _db(context)
    rows = db.bookings_from(_now(context).date())
    if not rows:
        await query.edit_message_text("אין אימונים קרובים.")
        return
    await query.edit_message_text(
        "האימונים הקרובים — לחצו על מועד לצפייה במשתתפים ולביטול:",
        reply_markup=_session_keyboard(db, rows),
    )


# --- trainer commands ---

async def add_slot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    usage = (
        "שימוש: /addslot <יום> <HH:MM> [דקות] [משתתפים] — "
        "למשל: /addslot שני 10:00 60 5"
    )
    try:
        if len(context.args) < 2:
            raise ValueError(usage)
        weekday = parse_weekday(context.args[0])
        start_time = parse_time(context.args[1])
        duration = 60
        capacity = 1
        if len(context.args) > 2:
            if not context.args[2].isdigit():
                raise ValueError(usage)
            duration = int(context.args[2])
        if len(context.args) > 3:
            if not context.args[3].isdigit():
                raise ValueError(usage)
            capacity = int(context.args[3])
        if not 0 < duration <= 480:
            raise ValueError("משך האימון חייב להיות בין 1 ל־480 דקות.")
        if not 0 < capacity <= 100:
            raise ValueError("מספר המשתתפים חייב להיות בין 1 ל־100.")
        slot_id = _db(context).add_slot(weekday, start_time, duration, capacity)
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    except Exception:
        await update.message.reply_text(
            "לא ניתן להוסיף — כבר קיים מועד ביום ובשעה האלה."
        )
        return
    extra = f", {capacity} משתתפים" if capacity > 1 else ""
    await update.message.reply_text(
        f"נוסף מועד #{slot_id}: יום {WEEKDAY_NAMES_HE[weekday]} "
        f"{start_time} ({duration} דק'{extra})",
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
    db, cfg = _db(context), _cfg(context)
    rows = db.list_slots()
    sections = []
    if rows:
        lines = [
            f"#{row['id']} יום {WEEKDAY_NAMES_HE[row['weekday']]} {row['start_time']} "
            f"({row['duration_min']} דק'"
            + (f", {row['capacity']} משתתפים)" if row["capacity"] > 1 else ")")
            for row in rows
        ]
        sections.append("המערכת השבועית (חוזרת):\n" + "\n".join(lines))

    from_day, to_day = _one_time_window(cfg)
    one_time_rows = db.list_one_time_slots(from_day, to_day)
    if one_time_rows:
        lines = [
            f"#{row['id']} {date.fromisoformat(row['date']).strftime('%d/%m/%Y')} "
            f"{row['start_time']} ({row['duration_min']} דק'"
            + (f", {row['capacity']} משתתפים)" if row["capacity"] > 1 else ")")
            for row in one_time_rows
        ]
        sections.append("שיעורים חד-פעמיים:\n" + "\n".join(lines))

    if not sections:
        await update.message.reply_text(
            f"אין עדיין מועדים. הוסיפו דרך {BTN_EDIT} "
            "או עם: /addslot שני 10:00 60"
        )
        return
    await update.message.reply_text("\n\n".join(sections))


async def bookings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    db = _db(context)
    rows = db.bookings_from(_now(context).date())
    if not rows:
        await update.message.reply_text("אין אימונים קרובים.")
        return
    await update.message.reply_text(
        "האימונים הקרובים — לחצו על מועד לצפייה במשתתפים ולביטול:",
        reply_markup=_session_keyboard(db, rows),
    )


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
