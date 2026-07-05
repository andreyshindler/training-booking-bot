"""Entry point: python -m bot.main"""

import logging

from telegram.ext import ApplicationBuilder

from .config import load_config
from .db import Database
from .handlers import CFG, DB, register_handlers, setup_commands_menu
from .reminders import send_due_reminders

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
    app.bot_data[DB] = Database(cfg.db_path)
    app.bot_data[CFG] = cfg
    register_handlers(app)
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
