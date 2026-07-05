"""Entry point: python -m bot.main"""

import logging

from telegram.ext import ApplicationBuilder

from .config import load_config
from .db import Database
from .handlers import CFG, DB, mini_app_payload, register_handlers, setup_commands_menu
from .reminders import send_due_reminders
from .webapp_server import start_webapp_server

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)


def main() -> None:
    cfg = load_config()
    app = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .post_init(setup_commands_menu)
        .build()
    )
    db = Database(cfg.db_path)
    app.bot_data[DB] = db
    app.bot_data[CFG] = cfg
    register_handlers(app)
    if cfg.webapp_url and cfg.webapp_secret:
        def get_payload():
            # sqlite3 connections may only be used from the thread that
            # created them; the webapp server runs its own background
            # thread(s), so open (and close) a fresh connection per request
            # rather than sharing the main thread's `db`.
            webapp_db = Database(cfg.db_path)
            try:
                return mini_app_payload(cfg, webapp_db)
            finally:
                webapp_db.close()

        start_webapp_server(cfg.webapp_secret, cfg.webapp_port, get_payload)
    if app.job_queue is not None:
        app.job_queue.run_repeating(send_due_reminders, interval=60, first=10)
    else:
        logging.getLogger(__name__).warning(
            "JobQueue unavailable (install python-telegram-bot[job-queue]); "
            "reminders will not be sent."
        )
    logging.getLogger(__name__).info("Bot starting (polling)...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
