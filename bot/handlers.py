"""Telegram command and callback handlers (all user-facing text in Hebrew)."""

import io
import json
import logging
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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
BTN_ADD_ADMIN = "➕ הוספת מנהל"

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
    BotCommand("webapplink", "קישור לעריכת המערכת בדפדפן"),
    BotCommand("admins", "רשימת מנהלים"),
    BotCommand("addadmin", "הוספת מנהל"),
    BotCommand("deladmin", "הסרת מנהל"),
    BotCommand("auditlog", "יומן פעולות"),
    BotCommand("pending", "בקשות הרשמה ממתינות"),
    BotCommand("trainees", "רשימת מתאמנים (קישור לדפדפן)"),
]


async def setup_commands_menu(app: Application) -> None:
    """Register the ☰ menu commands: trainee set for everyone, full set for the trainer."""
    cfg: Config = app.bot_data[CFG]
    db: Database = app.bot_data[DB]
    await app.bot.set_my_commands(TRAINEE_COMMANDS, scope=BotCommandScopeDefault())
    # Telegram rejects chat-scoped commands ("Chat not found") until each admin
    # has opened a chat with the bot, so this must not be fatal; it is retried
    # when that admin sends /start.
    await _set_trainer_menu(app.bot, cfg.trainer_id)
    for row in db.list_admins():
        await _set_trainer_menu(app.bot, row["user_id"])


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


def _is_trainer_id(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if user_id == _cfg(context).trainer_id:
        return True
    return _db(context).is_admin(user_id)


def _is_trainer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return _is_trainer_id(context, update.effective_user.id)


def _now(context: ContextTypes.DEFAULT_TYPE) -> datetime:
    return datetime.now(_cfg(context).timezone)


def _display_name(user) -> str:
    name = user.full_name or str(user.id)
    return f"{name} (@{user.username})" if user.username else name


def _log(context: ContextTypes.DEFAULT_TYPE, user, action: str, details: str = "") -> None:
    """Record a state-changing action to the audit log (see /auditlog)."""
    _db(context).log_action(user.id, _display_name(user), action, details)


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


def mini_app_payload(cfg: Config, db: Database) -> dict:
    """The current schedule, in the shape docs/index.html expects."""
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
    return {"recurring": recurring, "one_time": one_time}


def _webapp_edit_url(cfg: Config, db: Database) -> str:
    """Mini-app URL, so the app opens pre-filled with the current schedule.

    Self-hosted (webapp_secret set): the server injects the schedule itself
    (see bot/webapp_server.py), so the URL only needs the short token — no
    long encoded payload to carry around or paste.
    Github Pages (no secret, purely static hosting): the page has no server
    logic to fetch its own data from, so the schedule still has to travel in
    the URL itself.
    """
    separator = "&" if "?" in cfg.webapp_url else "?"
    if cfg.webapp_secret:
        return f"{cfg.webapp_url}{separator}token={quote(cfg.webapp_secret)}"
    payload = mini_app_payload(cfg, db)
    return f"{cfg.webapp_url}{separator}data={quote(json.dumps(payload))}"


def _main_keyboard(context: ContextTypes.DEFAULT_TYPE, is_trainer: bool) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(BTN_BOOK), KeyboardButton(BTN_MY)]]
    if is_trainer:
        rows.append([KeyboardButton(BTN_SCHEDULE), KeyboardButton(BTN_ALL)])
        rows.append([KeyboardButton(BTN_ADD_ADMIN)])
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

_TRAINEE_WELCOME = (
    "ברוכים הבאים! כאן מזמינים אימונים אצל המאמן.\n"
    f"{BTN_BOOK} — הצגת מועדים פנויים והזמנת אימון\n"
    f"{BTN_MY} — האימונים הקרובים שלכם (עם אפשרות ביטול)"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    is_trainer = _is_trainer(update, context)
    if is_trainer:
        # The chat now definitely exists, so this admin's menu can be registered.
        await _set_trainer_menu(context.bot, update.effective_user.id)
        db, cfg = _db(context), _cfg(context)
        if update.effective_user.id != cfg.trainer_id and db.is_admin(update.effective_user.id):
            # Refresh their display name now that we actually have it (they were
            # added by numeric ID alone, before ever interacting with the bot).
            db.add_admin(update.effective_user.id, _display_name(update.effective_user), added_by=cfg.trainer_id)
        text = (
            "שלום, המאמן! אפשר להשתמש בכפתורים למטה:\n"
            f"{BTN_EDIT} — עריכת המערכת השבועית במסך נוח\n"
            f"{BTN_SCHEDULE} — הצגת המערכת השבועית\n"
            f"{BTN_ALL} — האימונים הקרובים שהוזמנו\n"
            f"{BTN_ADD_ADMIN} — הוספת מנהל נוסף\n\n"
            "אפשר גם בפקודות: /addslot שני 10:00 60 או /delslot <מספר>"
        )
        await update.message.reply_text(text, reply_markup=_main_keyboard(context, is_trainer=True))
        return

    trainee = _db(context).get_trainee(update.effective_user.id)
    if trainee is None or trainee["status"] == "rejected":
        context.user_data["registration_step"] = "name"
        await update.message.reply_text(
            "ברוכים הבאים! כדי להזמין אימונים צריך להירשם קודם.\n"
            "מה השם המלא שלכם?",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if trainee["status"] == "pending":
        await update.message.reply_text(
            "ההרשמה שלכם התקבלה וממתינה לאישור המאמן. תקבלו הודעה ברגע שהיא תאושר."
        )
        return
    # approved
    await update.message.reply_text(_TRAINEE_WELCOME, reply_markup=_main_keyboard(context, is_trainer=False))


# --- trainee commands ---

def _trainee_gate_message(db: Database, user_id: int) -> str | None:
    """None if this (non-admin) user may book; otherwise the message to show instead."""
    trainee = db.get_trainee(user_id)
    if trainee is None:
        return "צריך להירשם קודם. שלחו /start כדי להתחיל."
    if trainee["status"] == "pending":
        return "ההרשמה שלכם עדיין ממתינה לאישור המאמן."
    if trainee["status"] == "rejected":
        return "בקשת ההרשמה שלכם נדחתה. אפשר לשלוח /start כדי לנסות שוב."
    return None  # approved


async def book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db, cfg = _db(context), _cfg(context)
    if not _is_trainer(update, context):
        blocked = _trainee_gate_message(db, update.effective_user.id)
        if blocked:
            await update.message.reply_text(blocked)
            return
    _log(context, update.effective_user, "view_book")
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
    _log(context, update.effective_user, "view_my_bookings")
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

BTN_SHARE_PHONE = "📱 שיתוף מספר טלפון"


def _looks_like_phone(text: str) -> bool:
    digits = "".join(ch for ch in text if ch.isdigit())
    return len(digits) >= 7


async def _notify_admins_new_registration(context: ContextTypes.DEFAULT_TYPE, user_id: int, name: str, phone: str) -> None:
    cfg, db = _cfg(context), _db(context)
    admin_ids = [cfg.trainer_id] + [row["user_id"] for row in db.list_admins()]
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ אישור", callback_data=f"approve_trainee|{user_id}"),
                InlineKeyboardButton("❌ דחייה", callback_data=f"reject_trainee|{user_id}"),
            ]
        ]
    )
    text = f"בקשת הרשמה חדשה:\n👤 {name}\n📱 {phone}\n🆔 {user_id}"
    for admin_id in admin_ids:
        try:
            await context.bot.send_message(admin_id, text, reply_markup=keyboard)
        except TelegramError as exc:
            logger.warning("Could not notify admin %s of new registration: %s", admin_id, exc)


async def _finish_registration(update: Update, context: ContextTypes.DEFAULT_TYPE, phone: str) -> None:
    db = _db(context)
    user = update.effective_user
    name = context.user_data.pop("registration_name", _display_name(user))
    context.user_data.pop("registration_step", None)
    db.register_trainee(user.id, name, phone)
    _log(context, user, "register", f"{name} / {phone}")
    await update.message.reply_text(
        "תודה! הבקשה שלכם נשלחה למאמן וממתינה לאישור. תקבלו הודעה ברגע שהיא תאושר.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await _notify_admins_new_registration(context, user.id, name, phone)


async def _handle_registration_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("צריך לשלוח שם. מה השם המלא שלכם?")
        return
    context.user_data["registration_name"] = name
    context.user_data["registration_step"] = "phone"
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_SHARE_PHONE, request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        "תודה! עכשיו שתפו את מספר הטלפון שלכם (או הקלידו אותו):",
        reply_markup=keyboard,
    )


async def _handle_registration_phone_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    phone = (update.message.text or "").strip()
    if not _looks_like_phone(phone):
        await update.message.reply_text(
            f"זה לא נראה כמו מספר טלפון תקין. אפשר לשתף עם {BTN_SHARE_PHONE} "
            "או להקליד מספר, למשל 0501234567."
        )
        return
    await _finish_registration(update, context, phone)


async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("registration_step") != "phone":
        return
    await _finish_registration(update, context, update.message.contact.phone_number)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    step = context.user_data.get("registration_step")
    if step == "name":
        await _handle_registration_name(update, context)
        return
    if step == "phone":
        await _handle_registration_phone_text(update, context)
        return
    if context.user_data.pop("awaiting_admin_id", False):
        await _handle_admin_id_reply(update, context)
        return
    text = (update.message.text or "").strip()
    if text == BTN_BOOK:
        await book(update, context)
    elif text == BTN_MY:
        await my_bookings(update, context)
    elif text == BTN_SCHEDULE:
        await schedule(update, context)
    elif text == BTN_ALL:
        await bookings(update, context)
    elif text == BTN_ADD_ADMIN:
        await add_admin_prompt(update, context)


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
    _log(
        context, update.effective_user,
        "schedule_edit",
        f"נוספו {total_added}, נמחקו {total_removed}, שונו {total_updated}",
    )
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
    elif action == "approve_trainee":
        await _handle_trainee_decision(query, context, payload, "approved")
    elif action == "reject_trainee":
        await _handle_trainee_decision(query, context, payload, "rejected")


async def _handle_trainee_decision(query, context, payload: str, status: str) -> None:
    if not _is_trainer_id(context, query.from_user.id):
        return
    user_id = int(payload)
    db = _db(context)
    trainee = db.get_trainee(user_id)
    if trainee is None or trainee["status"] != "pending":
        await query.edit_message_text("הבקשה לא נמצאה (ייתכן שכבר טופלה).")
        return
    db.set_trainee_status(user_id, status, decided_by=query.from_user.id)
    _log(context, query.from_user, f"{status}_trainee", f"{trainee['full_name']} (ID: {user_id})")
    decision_label = "אושרה ✅" if status == "approved" else "נדחתה ❌"
    await query.edit_message_text(
        f"בקשת ההרשמה של {trainee['full_name']} (ID: {user_id}) {decision_label} "
        f"על ידי {_display_name(query.from_user)}."
    )
    try:
        if status == "approved":
            await context.bot.send_message(
                user_id,
                f"ההרשמה שלכם אושרה! 🎉\n\n{_TRAINEE_WELCOME}",
                reply_markup=_main_keyboard(context, is_trainer=False),
            )
        else:
            await context.bot.send_message(
                user_id,
                "לצערנו הבקשה שלכם נדחתה. אפשר לשלוח /start כדי לנסות שוב.",
                reply_markup=ReplyKeyboardRemove(),
            )
    except TelegramError as exc:
        logger.warning("Could not notify trainee %s of decision: %s", user_id, exc)


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

    if not _is_trainer_id(context, user.id):
        blocked = _trainee_gate_message(db, user.id)
        if blocked:
            await query.edit_message_text(blocked)
            return

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
    _log(context, user, "book", label)
    await _notify_trainer(context, f"📅 הזמנה חדשה: {label} — {_display_name(user)}")


async def _handle_waitlist_join(query, context, payload: str) -> None:
    db, cfg = _db(context), _cfg(context)
    slot_id_str, _, day_str = payload.partition("|")
    slot_id, day = int(slot_id_str), date.fromisoformat(day_str)
    user = query.from_user

    if not _is_trainer_id(context, user.id):
        blocked = _trainee_gate_message(db, user.id)
        if blocked:
            await query.edit_message_text(blocked)
            return

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
        _log(context, user, "book", label)
        await _notify_trainer(context, f"📅 הזמנה חדשה: {label} — {_display_name(user)}")
        return

    try:
        db.join_waitlist(slot_id, day, user.id, _display_name(user))
    except AlreadyWaitlistedError:
        await query.edit_message_text("כבר נרשמתם לרשימת ההמתנה למועד הזה.")
        return

    label = hebrew_day_label(day, slot["start_time"], slot["duration_min"])
    _log(context, user, "waitlist_join", label)
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
    slot = db.get_slot(entry["slot_id"])
    if slot is not None:
        label = hebrew_day_label(date.fromisoformat(entry["date"]), slot["start_time"], slot["duration_min"])
        _log(context, query.from_user, "waitlist_leave", label)
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
    db = _db(context)
    booking_id = int(payload)
    row = db.get_booking(booking_id)
    is_trainer = _is_trainer_id(context, query.from_user.id)
    if row is None or (row["user_id"] != query.from_user.id and not is_trainer):
        await query.edit_message_text(
            "ההזמנה לא נמצאה (ייתכן שכבר בוטלה)."
        )
        return
    db.cancel_booking(booking_id)
    label = _fmt_booking(row)
    await query.edit_message_text(f"❌ האימון בוטל: {label}")
    _log(context, query.from_user, "cancel", label)
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
        db.log_action(promoted["user_id"], promoted["user_name"], "waitlist_promoted", label)


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
    slot_label = f"יום {WEEKDAY_NAMES_HE[weekday]} {start_time} ({duration} דק'{extra})"
    _log(context, update.effective_user, "addslot", slot_label)
    await update.message.reply_text(
        f"נוסף מועד #{slot_id}: {slot_label}",
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
    if removed:
        _log(context, update.effective_user, "delslot", f"#{context.args[0]}")
    await update.message.reply_text(
        "המועד נמחק (הזמנות עתידיות למועד זה בוטלו)."
        if removed
        else "אין מועד עם המספר הזה. ראו /schedule.",
        reply_markup=_main_keyboard(context, is_trainer=True),
    )


async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    _log(context, update.effective_user, "view_schedule")
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
    _log(context, update.effective_user, "view_bookings")
    db = _db(context)
    rows = db.bookings_from(_now(context).date())
    if not rows:
        await update.message.reply_text("אין אימונים קרובים.")
        return
    await update.message.reply_text(
        "האימונים הקרובים — לחצו על מועד לצפייה במשתתפים ולביטול:",
        reply_markup=_session_keyboard(db, rows),
    )


async def webapp_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    _log(context, update.effective_user, "view_webapplink")
    cfg, db = _cfg(context), _db(context)
    if not cfg.webapp_url:
        await update.message.reply_text("לא הוגדר WEBAPP_URL. ראו .env.example.")
        return
    url = _webapp_edit_url(cfg, db)
    if cfg.webapp_secret:
        note = "הקישור קבוע ותמיד מציג את המערכת העדכנית — אין צורך לרענן אותו."
    else:
        note = "הקישור הזה כולל תמונת מצב של המערכת; לאחר עדכון שלחו /start לקבלת קישור חדש."
    keyboard = [[InlineKeyboardButton("🌐 פתיחה בדפדפן", url=url)]]
    await update.message.reply_text(
        f"קישור לעריכת המערכת — פותח בכל דפדפן (כולל במחשב), לא רק בטלגרם.\n{note}\n\n{url}",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def trainees_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    _log(context, update.effective_user, "view_trainees")
    cfg = _cfg(context)
    if not (cfg.webapp_url and cfg.webapp_secret):
        await update.message.reply_text(
            "התכונה הזו זמינה רק כשהמיני-אפ מתארח עצמאית (WEBAPP_SECRET מוגדר). "
            "ראו את 'Option B' ב-README."
        )
        return
    separator = "&" if "?" in cfg.webapp_url else "?"
    url = f"{cfg.webapp_url}{separator}view=users&token={quote(cfg.webapp_secret)}"
    keyboard = [[InlineKeyboardButton("🌐 רשימת מתאמנים", url=url)]]
    await update.message.reply_text(
        "רשימת המתאמנים וההיסטוריה שלהם:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# --- managing additional admins ---

async def _do_add_admin(context: ContextTypes.DEFAULT_TYPE, requester, new_id: int) -> str:
    cfg, db = _cfg(context), _db(context)
    if new_id == cfg.trainer_id or db.is_admin(new_id):
        return "המשתמש הזה כבר מנהל."
    db.add_admin(new_id, user_name=str(new_id), added_by=requester.id)
    await _set_trainer_menu(context.bot, new_id)
    _log(context, requester, "add_admin", f"ID: {new_id}")
    try:
        await context.bot.send_message(
            new_id, "מוניתם כמנהל בבוט. שלחו /start כדי לראות את תפריט הניהול."
        )
    except TelegramError:
        pass  # they haven't started a chat with the bot yet — they'll see the menu once they do
    return f"✅ נוסף מנהל חדש (ID: {new_id})."


async def add_admin_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    _log(context, update.effective_user, "open_add_admin_prompt")
    context.user_data["awaiting_admin_id"] = True
    await update.message.reply_text(
        "שלחו את המספר המזהה (ID) של המנהל החדש.\n"
        "אפשר לקבל אותו מהמשתמש עצמו, למשל דרך @userinfobot."
    )


async def _handle_admin_id_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    text = (update.message.text or "").strip()
    if not text.isdigit():
        await update.message.reply_text(
            f"זה לא נראה כמו מספר מזהה תקין. אפשר לנסות שוב עם {BTN_ADD_ADMIN}."
        )
        return
    msg = await _do_add_admin(context, update.effective_user, int(text))
    await update.message.reply_text(msg)


async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("שימוש: /addadmin <ID>")
        return
    msg = await _do_add_admin(context, update.effective_user, int(context.args[0]))
    await update.message.reply_text(msg)


async def del_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.message.reply_text("שימוש: /deladmin <ID>")
        return
    target_id = int(context.args[0])
    if target_id == _cfg(context).trainer_id:
        await update.message.reply_text(
            "לא ניתן להסיר את המנהל הראשי (מוגדר ב-TRAINER_ID ב-.env)."
        )
        return
    db = _db(context)
    removed = db.remove_admin(target_id)
    if not removed:
        await update.message.reply_text("לא נמצא מנהל עם המספר הזה.")
        return
    try:
        await context.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=target_id))
    except TelegramError:
        pass
    _log(context, update.effective_user, "remove_admin", f"ID: {target_id}")
    await update.message.reply_text(f"✅ המנהל הוסר (ID: {target_id}).")
    try:
        await context.bot.send_message(target_id, "הוסרתם מרשימת המנהלים בבוט.")
    except TelegramError:
        pass


async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    _log(context, update.effective_user, "view_admins")
    cfg, db = _cfg(context), _db(context)
    lines = [f"#{cfg.trainer_id} (מנהל ראשי, מוגדר ב-.env)"]
    lines += [f"#{row['user_id']} — {row['user_name']}" for row in db.list_admins()]
    await update.message.reply_text("מנהלים:\n" + "\n".join(lines))


# --- trainee registration approval (viewed by the trainer/admins) ---

async def pending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    rows = _db(context).list_trainees(status="pending")
    if not rows:
        await update.message.reply_text("אין בקשות הרשמה ממתינות.")
        return
    for row in rows:
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ אישור", callback_data=f"approve_trainee|{row['user_id']}"),
                    InlineKeyboardButton("❌ דחייה", callback_data=f"reject_trainee|{row['user_id']}"),
                ]
            ]
        )
        await update.message.reply_text(
            f"👤 {row['full_name']}\n📱 {row['phone']}\n🆔 {row['user_id']}\n"
            f"נשלח: {row['requested_at']}",
            reply_markup=keyboard,
        )


# --- audit log ---

_ACTION_LABELS = {
    "book": "הזמנת אימון",
    "cancel": "ביטול אימון",
    "waitlist_join": "הצטרפות לרשימת המתנה",
    "waitlist_leave": "עזיבת רשימת המתנה",
    "waitlist_promoted": "עלייה אוטומטית מרשימת המתנה",
    "schedule_edit": "עריכת המערכת",
    "addslot": "הוספת מועד",
    "delslot": "מחיקת מועד",
    "add_admin": "הוספת מנהל",
    "remove_admin": "הסרת מנהל",
    "view_book": "צפייה במועדים פנויים",
    "view_my_bookings": "צפייה באימונים שלי",
    "view_schedule": "צפייה במערכת השבועית",
    "view_bookings": "צפייה ברשימת האימונים הקרובים",
    "view_webapplink": "בקשת קישור לעריכת המערכת",
    "view_admins": "צפייה ברשימת מנהלים",
    "open_add_admin_prompt": "פתיחת טופס הוספת מנהל",
    "register": "הרשמה כמתאמן",
    "approved_trainee": "אישור מתאמן",
    "rejected_trainee": "דחיית מתאמן",
    "view_trainees": "בקשת קישור למתאמנים",
}


AUDIT_LOG_FILE_LIMIT = 1000


def _local_timestamp(created_at_utc: str, tz) -> str:
    """SQLite's datetime('now') stores UTC; render it in the bot's timezone."""
    try:
        dt = datetime.fromisoformat(created_at_utc).replace(tzinfo=timezone.utc)
        return dt.astimezone(tz).strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return created_at_utc


async def audit_log_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_trainer(update, context):
        return
    rows = _db(context).list_audit_log(limit=AUDIT_LOG_FILE_LIMIT)
    if not rows:
        await update.message.reply_text("יומן הפעולות ריק.")
        return
    tz = _cfg(context).timezone
    lines = []
    for row in rows:
        action = _ACTION_LABELS.get(row["action"], row["action"])
        stamp = _local_timestamp(row["created_at"], tz)
        line = f"{stamp} — {row['user_name']} ({row['user_id']}): {action}"
        if row["details"]:
            line += f" — {row['details']}"
        lines.append(line)
    content = "יומן פעולות\n" + "=" * 30 + "\n" + "\n".join(lines) + "\n"
    filename = f"audit-log-{_now(context).strftime('%Y%m%d-%H%M')}.txt"
    await update.message.reply_document(
        document=io.BytesIO(content.encode("utf-8")),
        filename=filename,
        caption=f"יומן פעולות — {len(rows)} הרשומות האחרונות",
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
    app.add_handler(CommandHandler("webapplink", webapp_link))
    app.add_handler(CommandHandler("admins", list_admins_command))
    app.add_handler(CommandHandler("addadmin", add_admin_command))
    app.add_handler(CommandHandler("deladmin", del_admin_command))
    app.add_handler(CommandHandler("auditlog", audit_log_command))
    app.add_handler(CommandHandler("pending", pending_command))
    app.add_handler(CommandHandler("trainees", trainees_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    app.add_handler(MessageHandler(filters.CONTACT, on_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
