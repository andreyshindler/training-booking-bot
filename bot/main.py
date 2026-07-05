"""Entry point: python -m bot.main"""

import logging

from telegram.ext import ApplicationBuilder

from .config import load_config
from .db import Database
from .handlers import CFG, DB, register_handlers, setup_commands_menu

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
    logging.getLogger(__name__).info("Bot starting (polling)...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
