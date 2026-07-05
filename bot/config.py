"""Configuration loaded from environment variables (optionally via a .env file)."""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader so the bot runs without extra dependencies."""
    env_file = Path(path)
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Config:
    bot_token: str
    trainer_id: int
    db_path: str
    timezone: ZoneInfo
    booking_days_ahead: int


def load_config() -> Config:
    _load_dotenv()
    token = os.environ.get("BOT_TOKEN", "")
    trainer_id = os.environ.get("TRAINER_ID", "")
    if not token or not trainer_id:
        raise SystemExit(
            "BOT_TOKEN and TRAINER_ID must be set (via environment or .env file). "
            "See .env.example."
        )
    return Config(
        bot_token=token,
        trainer_id=int(trainer_id),
        db_path=os.environ.get("DB_PATH", "bookings.db"),
        timezone=ZoneInfo(os.environ.get("TIMEZONE", "UTC")),
        booking_days_ahead=int(os.environ.get("BOOKING_DAYS_AHEAD", "7")),
    )
